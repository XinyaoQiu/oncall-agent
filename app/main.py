"""`oncall-api`: the HTTP surface. One of two processes, on purpose.

Slack owns its own process (`oncall-slackd`). Sharing one event loop between a WebSocket
client and a web app is something bolt-python's maintainer advises against directly, and the
lifespan workaround breaks under `uvicorn --workers > 1`: each worker opens its own Socket
Mode connection and every Slack event is handled N times (spec §2.2). Two processes also buy
restart independence — deploying the API does not drop the Socket Mode connection.

Nothing connects at import time. `create_app()` builds the app object; the record store is
opened in the lifespan and its absence is a supported configuration, not an error, so the
service still answers with no Postgres anywhere.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api import admin, health, triage
from app.config import Settings, get_settings
from app.storage.records import open_store

DESCRIPTION = (
    "On-call triage: a LangGraph plan-execute-replan graph over MCP tools, with a "
    "deterministic evidence floor that runs before anything reasons."
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the service. Safe to call in a test: it opens no sockets."""
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = config
        app.state.store = await open_store(config.database_url)
        logger.info(
            f"{config.app_name} v{config.app_version} on {config.host}:{config.port} "
            f"(records: {'on' if app.state.store else 'off'})"
        )
        yield
        logger.info(f"{config.app_name} shutting down")

    app = FastAPI(
        title=config.app_name,
        version=config.app_version,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Also set outside the lifespan: a caller can hit a route without ever running startup.
    app.state.settings = config
    app.state.store = None

    app.include_router(health.router, tags=["health"])
    app.include_router(triage.router, tags=["triage"])
    app.include_router(admin.router, tags=["admin"])

    @app.get("/", tags=["health"])
    async def root() -> dict[str, str]:
        return {
            "service": config.app_name,
            "version": config.app_version,
            "triage": "POST /triage",
            "docs": "/docs",
        }

    return app


app = create_app()


def run() -> None:
    """The `oncall-api` console script."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )
