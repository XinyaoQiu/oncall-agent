"""`oncall`: the agent without Slack.

Two commands matter more than the rest.

`oncall triage` runs the whole graph and prints exactly what Slack would post — the same
`render_answer()` output, because there is one renderer and the adapters differ only in
markup (spec §9 item 19). A second formatter here would drift, and the one that drifts is
the one that silently stops disclosing.

`oncall evidence` is the floor with **no model at all**: identify, walk the rule ladder,
resolve the workload, measure, and print what was measured and what was not. It needs no API
key, no MCP server and no database — it is the path that still works when everything else is
missing, and it is what the deterministic layer is for. It talks to Prometheus directly
rather than through MCP for the same reason: a probe that cannot run must still report
*that*, and a failed query is an observation, never silence.
"""

import asyncio
import json
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import typer
from loguru import logger

from app.config import Settings, get_settings
from app.core.llm_factory import LLMUnavailable, get_llm
from app.domain.alerts import benign_checks
from app.domain.deployments import resolve
from app.evidence.baseline import collect_baseline
from app.evidence.envelope import Series
from app.evidence.identify import identify
from app.evidence.rules import fetch_rule
from app.graph.build import graph_for
from app.render.answer import accounting, render_answer
from app.storage.records import VERDICTS, RecordStore, open_store

app = typer.Typer(help="On-call triage agent", no_args_is_help=True)

# --no-deep bounds the investigation loop; the unconditional floor is never what it skips.
SHALLOW_STEPS = 1

DASHSCOPE_COMPAT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBED_BATCH = 10
EMBED_DIMENSIONS = 1024


def _alert_text(alert: str) -> str:
    path = Path(alert)
    return path.read_text() if path.is_file() else alert


def _need_store(settings: Settings) -> RecordStore:
    store = asyncio.run(open_store(settings.database_url))
    if store is None:
        typer.secho(
            "No DATABASE_URL configured (or the database did not answer), so there are "
            "no recorded runs to read.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)
    return store


def _port_open(host: str, port: int, timeout: float = 2.0) -> str | None:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- triage


async def _run_triage(
    text: str, question: str | None, settings: Settings
) -> tuple[dict[str, Any], int]:
    conversation_id = f"cli:{uuid.uuid4()}"
    started = time.time()

    final = await graph_for(settings).ainvoke(
        {
            "input": question or text,
            "turn": "triage",
            "conversation_id": conversation_id,
            "alert_text": text,
        },
        config={
            "configurable": {"thread_id": conversation_id, "started_at": started},
            "recursion_limit": 2 * settings.max_steps + 8,
        },
    )
    duration_ms = int((time.time() - started) * 1000)

    store = await open_store(settings.database_url)
    if store is not None:
        await store.record(
            final,
            source="cli",
            conversation_id=conversation_id,
            question=question,
            duration_ms=duration_ms,
        )
    return final, duration_ms


@app.command()
def triage(
    alert: str = typer.Argument(..., help="Alert text, or a path to a file containing it"),
    ask: str = typer.Option(None, "--ask", help="What to focus the analysis on"),
    deep: bool = typer.Option(
        True, "--deep/--no-deep", help="--no-deep caps the investigation at one step"
    ),
    raw: bool = typer.Option(False, "--raw", help="Print the final state as JSON"),
):
    """Run the graph over an alert and print the reply."""
    settings = get_settings()
    if not deep:
        settings = settings.model_copy(update={"max_steps": SHALLOW_STEPS})

    try:
        get_llm(settings)
    except LLMUnavailable as exc:
        typer.secho(f"Cannot triage: {exc}", fg=typer.colors.RED, err=True)
        typer.secho(
            "`oncall evidence` runs the deterministic floor with no model at all.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1) from None

    state, duration_ms = asyncio.run(_run_triage(_alert_text(alert), ask, settings))

    if raw:
        typer.echo(json.dumps(state, ensure_ascii=False, default=str, indent=2))
        return
    typer.echo(render_answer(state))
    typer.secho(f"\n({duration_ms / 1000:.1f}s)", fg=typer.colors.BRIGHT_BLACK)


# --------------------------------------------------------------------------- evidence


def prometheus_query(settings: Settings):
    """`query_metric(expr, minutes=…)` straight against Prometheus, for the no-model path.

    Async so the evidence layer's `asyncio.gather` fan-out means something; failures are left
    to raise, because `run_metric_query` turns them into a failed observation and an
    exception swallowed into an empty series list reads as "no data", which reads as healthy.
    """
    import httpx

    base = settings.prometheus_base_url.rstrip("/")

    async def query_metric(expr: str, minutes: int = 60) -> list[Series]:
        end = time.time()
        step = max(15, int(minutes * 60 / 120))
        async with httpx.AsyncClient(timeout=settings.prometheus_request_timeout) as client:
            response = await client.get(
                f"{base}/api/v1/query_range",
                params={"query": expr, "start": end - minutes * 60, "end": end, "step": step},
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(payload.get("error") or "prometheus rejected the query")

        return [
            Series(
                labels={str(k): str(v) for k, v in (result.get("metric") or {}).items()},
                points=[(float(t), float(v)) for t, v in result.get("values") or []],
            )
            for result in (payload.get("data") or {}).get("result") or []
        ]

    return query_metric


async def _collect_floor(text: str, settings: Settings, minutes: int) -> dict[str, Any]:
    identity = identify(text)
    rule = await fetch_rule(identity)
    labels = identity.labels
    resolution = resolve(
        host=labels.get("host"),
        path=labels.get("path"),
        app_label=labels.get("app") or labels.get("service") or labels.get("deployment"),
    )
    observations, skipped = await collect_baseline(
        identity, rule, resolution, minutes=minutes, query_metric=prometheus_query(settings)
    )
    return {
        "identity": identity,
        "rule": rule,
        "resolution": resolution,
        "baseline": observations,
        "skipped": skipped,
    }


def _deployment_line(resolution) -> str:
    if not resolution.app_label:
        return f"- unresolved -  ({resolution.note})" if resolution.note else "- unresolved -"
    if resolution.is_confident:
        return f"{resolution.app_label}  (matched by {resolution.matched_by})"
    return f"{resolution.app_label}  (unconfirmed: {resolution.note or resolution.matched_by})"


@app.command()
def evidence(
    alert: str = typer.Argument(..., help="Alert text, or a path to a file containing it"),
    minutes: int = typer.Option(None, "--minutes", help="Query window; defaults to settings"),
):
    """The deterministic floor only: no model, no API key, no MCP server."""
    settings = get_settings()
    state = asyncio.run(
        _collect_floor(_alert_text(alert), settings, minutes or settings.query_window_minutes)
    )

    identity, rule, resolution = state["identity"], state["rule"], state["resolution"]
    typer.echo(f"alert:      {identity.alert_name} (via {identity.identified_by})")
    typer.echo(f"labels:     {identity.labels or '-'}")
    typer.echo(
        f"rule:       {rule.expression} [{rule.provenance}]" if rule else "rule:       - none -"
    )
    typer.echo(f"deployment: {_deployment_line(resolution)}")
    typer.echo(f"metrics:    {settings.prometheus_base_url}")

    for observation in state["baseline"]:
        typer.echo(f"\n{observation.render()}")

    typer.echo("")
    for line in accounting(state).splitlines():  # type: ignore[arg-type]
        typer.secho(line, fg=typer.colors.BRIGHT_BLACK)

    for check in benign_checks(identity.alert_name):
        typer.echo(f"\nbenign check: {check}")


# --------------------------------------------------------------------------- record store


@app.command()
def runs(
    limit: int = typer.Option(20, help="How many to list"),
    alert: str = typer.Option(None, help="Filter by alert name"),
):
    """List recent invocations."""
    settings = get_settings()
    rows = asyncio.run(_need_store(settings).recent(limit=limit, alert_name=alert))
    if not rows:
        typer.echo("No runs recorded.")
        return

    for row in rows:
        flags = []
        if row["degraded_model"]:
            flags.append("degraded")
        if row["used_synthetic"]:
            flags.append("synthetic")
        if row["error"]:
            flags.append("error")
        flags.append(row["verdict"] or "unreviewed")
        typer.echo(
            f"#{row['id']:<5} {row['created_at']:%m-%d %H:%M}  {row['alert_name']:<28} "
            f"{row['confidence'] or '-':<7} {row['steps_taken']} steps "
            f"{(row['duration_ms'] or 0) / 1000:.0f}s  [{', '.join(flags)}]"
        )


@app.command()
def replay(invocation_id: int):
    """Show one run and every step it executed."""
    settings = get_settings()
    run = asyncio.run(_need_store(settings).replay(invocation_id))
    if not run:
        typer.secho(f"No run #{invocation_id}", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.secho(
        f"#{run['id']}  {run['alert_name']}  ({run['created_at']:%Y-%m-%d %H:%M})", bold=True
    )
    typer.echo(
        f"source: {run['source']}  deployment: {run['deployment'] or '-'}  "
        f"confidence: {run['confidence'] or '-'}  {(run['duration_ms'] or 0) / 1000:.1f}s  "
        f"verdict: {run['verdict'] or 'unreviewed'}"
    )
    if run["question"]:
        typer.echo(f"asked: {run['question']}")
    if run["stopped_because"]:
        typer.secho(f"stopped: {run['stopped_because']}", fg=typer.colors.YELLOW)

    for entry in run["observations"]:
        state = "failed" if entry["error"] else ("empty" if entry["empty"] else "data")
        typer.echo(f"  [{state}] {entry['query']}")
    for skip in run["skipped"]:
        typer.echo(f"  [skipped] {skip['probe']} — {skip['reason']}")

    for step in run["steps"]:
        typer.secho(f"\n[{step['round']}] {step['step']}", fg=typer.colors.CYAN)
        if step["signature"]:
            typer.echo(f"    called: {step['signature']}")
        for line in (step["result"] or "").splitlines()[:6]:
            typer.echo(f"    {line[:150]}")

    if run["diagnosis"]:
        typer.secho("\ndiagnosis:", bold=True)
        typer.echo(f"  {run['diagnosis'].get('likely_cause', '')}")


@app.command()
def verdict(
    invocation_id: int,
    result: str = typer.Argument(..., help=f"One of: {', '.join(VERDICTS)}"),
    note: str = typer.Option(None, help="What actually happened"),
):
    """Judge a past run, so accuracy can be measured rather than assumed."""
    if result not in VERDICTS:
        typer.secho(f"verdict must be one of {', '.join(VERDICTS)}", fg=typer.colors.RED)
        raise typer.Exit(1)

    settings = get_settings()
    if asyncio.run(_need_store(settings).set_verdict(invocation_id, result, note)):
        typer.secho(f"#{invocation_id} marked {result}", fg=typer.colors.GREEN)
        return
    typer.secho(f"could not mark #{invocation_id}", fg=typer.colors.RED)
    raise typer.Exit(1)


@app.command()
def stats(days: int = typer.Option(30, help="Window in days")):
    """Aggregates over recorded runs. Unreviewed runs are never counted as correct."""
    settings = get_settings()
    data = asyncio.run(_need_store(settings).stats(days=days))
    if not data or not data.get("overall"):
        typer.echo("No data.")
        return

    overall = data["overall"]
    typer.secho(f"Last {days} days", bold=True)
    typer.echo(f"  runs:        {overall['runs']}  ({overall['failed']} failed)")
    typer.echo(f"  reviewed:    {overall['reviewed']}  ({overall['unreviewed']} unreviewed)")
    typer.echo(f"  avg steps:   {overall['avg_steps']}")
    typer.echo(f"  avg latency: {(overall['avg_ms'] or 0) / 1000:.1f}s")
    if overall["degraded"]:
        typer.echo(f"  degraded:    {overall['degraded']}")
    if overall["on_synthetic"]:
        typer.secho(f"  synthetic:   {overall['on_synthetic']}", fg=typer.colors.YELLOW)

    if data.get("by_confidence"):
        typer.secho("\nBy confidence", bold=True)
        for row in data["by_confidence"]:
            # Unreviewed runs are shown as unreviewed, never folded into a success rate.
            rate = (
                f"{row['wrong'] / row['reviewed']:.0%} wrong of {row['reviewed']} reviewed"
                if row["reviewed"]
                else "none reviewed"
            )
            typer.echo(f"  {row['confidence']:<7} {row['n']:>4} runs   {rate}")

    if data.get("tools"):
        typer.secho("\nTool usage", bold=True)
        for row in data["tools"]:
            typer.echo(f"  {row['tool']:<18} {row['n']}")


# --------------------------------------------------------------------------- index


def _markdown_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if ".git" not in p.parts)


def _chunks(files: list[Path], root: Path, settings: Settings) -> list[Any]:
    from langchain_text_splitters import (
        MarkdownHeaderTextSplitter,
        RecursiveCharacterTextSplitter,
    )

    by_header = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2")], strip_headers=False
    )
    by_size = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_max_size * 2, chunk_overlap=settings.chunk_overlap
    )

    documents = []
    for path in files:
        try:
            sections = by_header.split_text(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"skipping {path}: {exc}")
            continue
        for chunk in by_size.split_documents(sections):
            chunk.metadata = {**chunk.metadata, "path": str(path.relative_to(root))}
            documents.append(chunk)
    return documents


def _embeddings(settings: Settings):
    from langchain_core.embeddings import Embeddings
    from openai import OpenAI

    class DashScopeEmbeddings(Embeddings):
        """DashScope text embeddings over its OpenAI-compatible endpoint."""

        def __init__(self) -> None:
            self.client = OpenAI(
                api_key=settings.dashscope_api_key,
                base_url=settings.dashscope_api_base or DASHSCOPE_COMPAT_BASE,
            )

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), EMBED_BATCH):
                response = self.client.embeddings.create(
                    model=settings.dashscope_embedding_model,
                    input=texts[start : start + EMBED_BATCH],
                    dimensions=EMBED_DIMENSIONS,
                )
                vectors.extend(item.embedding for item in response.data)
            return vectors

        def embed_query(self, text: str) -> list[float]:
            return self.embed_documents([text])[0]

    return DashScopeEmbeddings()


@app.command()
def index(
    path: Path = typer.Argument(None, help="Directory of markdown; defaults to knowledge_repo"),
    collection: str = typer.Option(None, "--collection", help="Milvus collection to write to"),
):
    """Ingest markdown into Milvus for `retrieve_knowledge`.

    Every precondition is checked before a single file is read, and a missing one aborts with
    what is missing. An ingestion that "succeeded" against no vector store is worse than one
    that failed: retrieval then returns nothing, and nothing is indistinguishable from a
    corpus with no relevant entry.
    """
    settings = get_settings()
    root = (path or settings.knowledge_repo).expanduser()
    target = collection or settings.milvus_collection

    problems = []
    if not root.is_dir():
        problems.append(f"{root} is not a directory")
    if not settings.dashscope_api_key:
        problems.append("DASHSCOPE_API_KEY is unset, so no embeddings can be produced")
    milvus_error = _port_open(settings.milvus_host, settings.milvus_port)
    if milvus_error:
        problems.append(
            f"Milvus at {settings.milvus_host}:{settings.milvus_port} is unreachable "
            f"({milvus_error}); start it with `make up`"
        )
    try:
        from langchain_milvus import Milvus
    except ImportError as exc:
        problems.append(f"langchain-milvus is not installed: {exc}")

    if problems:
        typer.secho("Cannot index:", fg=typer.colors.RED, err=True)
        for problem in problems:
            typer.secho(f"  - {problem}", fg=typer.colors.RED, err=True)
        typer.secho("Nothing was indexed.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    files = _markdown_files(root)
    if not files:
        typer.secho(f"No markdown under {root}. Nothing was indexed.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)

    documents = _chunks(files, root, settings)
    typer.echo(f"{len(files)} files → {len(documents)} chunks → collection {target!r}")

    try:
        store = Milvus(
            embedding_function=_embeddings(settings),
            collection_name=target,
            connection_args={"host": settings.milvus_host, "port": settings.milvus_port},
            auto_id=True,
            drop_old=False,
        )
        store.add_documents(documents)
    except Exception as exc:
        typer.secho(f"Indexing failed: {type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        typer.secho(
            "The collection may now hold a partial corpus; re-run after fixing the cause.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1) from None

    typer.secho(f"Indexed {len(documents)} chunks into {target!r}.", fg=typer.colors.GREEN)


def main() -> None:
    sys.exit(app())


if __name__ == "__main__":
    main()
