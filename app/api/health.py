"""`/health`, reporting each dependency separately.

The sibling project answers 503 when Milvus is down, on the reasoning that the database is
the service. Here it is not: the product is the deterministic evidence floor, which needs no
Milvus, no Postgres and no LLM to run — `oncall evidence` is that path — so a single number
folding every dependency into "unhealthy" would take the API out of rotation for capabilities
it can serve without, and a single "healthy" would hide which one is missing.

So each dependency reports its own line, and the top-level status distinguishes *degraded*
(something configured is unreachable, and the reply says which) from *ok*. Nothing here is
inferred: a dependency that is not configured says so rather than counting as healthy, and
the model is never dialled — a reachability probe that costs tokens is one nobody runs.
"""

import asyncio
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.storage.records import RecordStore

router = APIRouter()

PROBE_TIMEOUT = 2.0


class Dependency(BaseModel):
    """One dependency's standing. `reachable` is None when nothing was dialled."""

    name: str
    configured: bool
    reachable: bool | None = None
    detail: str = ""


class Health(BaseModel):
    service: str
    version: str
    status: str
    dependencies: list[Dependency] = Field(default_factory=list)


async def _tcp_error(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> str | None:
    """`None` when the port accepted a connection, otherwise why it did not."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except TimeoutError:
        return f"no answer from {host}:{port} within {timeout:.0f}s"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return None


def _host_port(url: str, default_port: int) -> tuple[str, int]:
    parsed = urlparse(url if "//" in url else f"//{url}")
    return parsed.hostname or "localhost", parsed.port or default_port


async def _database(settings: Settings) -> Dependency:
    if not settings.database_url:
        return Dependency(
            name="postgres",
            configured=False,
            detail="DATABASE_URL is unset; runs are not recorded and Slack dedupe is off",
        )
    error = await RecordStore(settings.database_url).ping()
    return Dependency(
        name="postgres", configured=True, reachable=error is None, detail=error or "answered"
    )


async def _milvus(settings: Settings) -> Dependency:
    host, port = settings.milvus_host, settings.milvus_port
    error = await _tcp_error(host, port)
    return Dependency(
        name="milvus",
        configured=True,
        reachable=error is None,
        detail=error or f"{host}:{port} accepted a connection",
    )


async def _mcp(name: str, url: str) -> Dependency:
    host, port = _host_port(url, 80)
    error = await _tcp_error(host, port)
    return Dependency(
        name=f"mcp:{name}", configured=True, reachable=error is None, detail=error or url
    )


async def _grafana(settings: Settings) -> Dependency:
    if not settings.grafana_url:
        return Dependency(
            name="grafana",
            configured=False,
            detail="GRAFANA_URL is unset; the alert's own rule cannot be fetched authoritatively",
        )
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            response = await client.get(f"{settings.grafana_url.rstrip('/')}/api/health")
        return Dependency(
            name="grafana",
            configured=True,
            reachable=response.status_code < 500,
            detail=f"HTTP {response.status_code}",
        )
    except Exception as exc:
        return Dependency(
            name="grafana", configured=True, reachable=False, detail=f"{type(exc).__name__}: {exc}"
        )


def _model(settings: Settings) -> Dependency:
    provider = (settings.llm_provider or "").strip().lower()
    key = settings.dashscope_api_key if provider == "dashscope" else settings.openai_api_key
    return Dependency(
        name=f"llm:{provider or 'unset'}",
        configured=bool(key),
        detail=(
            f"{settings.model_deep} / {settings.model_fast}; not dialled, a reachability "
            "probe would cost a request"
            if key
            else "no API key; triage returns an error rather than a partial pack"
        ),
    )


async def check_dependencies(settings: Settings) -> list[Dependency]:
    probes: list[Any] = [_database(settings), _milvus(settings), _grafana(settings)]
    probes += [_mcp(name, conn.get("url", "")) for name, conn in settings.mcp_servers.items()]
    results = await asyncio.gather(*probes, return_exceptions=True)

    dependencies: list[Dependency] = []
    for result in results:
        if isinstance(result, Dependency):
            dependencies.append(result)
        else:
            logger.warning(f"health probe raised: {result}")
    dependencies.append(_model(settings))
    return dependencies


@router.get("/health", response_model=Health)
async def health(request: Request) -> Health:
    """Per-dependency reachability. Always 200: the service is answering, and what it can
    and cannot do right now is in the body rather than in the status code."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    dependencies = await check_dependencies(settings)
    degraded = [d.name for d in dependencies if d.configured and d.reachable is False]

    return Health(
        service=settings.app_name,
        version=settings.app_version,
        status="degraded" if degraded else "ok",
        dependencies=dependencies,
    )
