"""Command-line driver, for running the pipeline without Slack."""

import json
import sys
from pathlib import Path

import typer

from .config import Settings
from .llm import LLMUnavailable
from .models import ThreadMessage
from .pipeline import format_reply, triage
from .sources.knowledge import KnowledgeRepo

app = typer.Typer(help="On-call triage agent", no_args_is_help=True)


@app.command()
def analyze(
    alert: str = typer.Argument(..., help="Alert text, or a path to a file containing it"),
    thread: Path = typer.Option(None, help="JSON file with thread messages"),
    sample: bool = typer.Option(False, "--sample", help="Use sample metrics instead of Grafana"),
    raw: bool = typer.Option(False, "--raw", help="Print the full result as JSON"),
    ask: str = typer.Option(None, "--ask", help="What to focus the analysis on"),
):
    """Analyze an alert and print the reply."""
    settings = Settings.from_env()
    if sample:
        settings.use_sample_metrics = True

    text = Path(alert).read_text() if Path(alert).is_file() else alert

    messages = []
    if thread:
        messages = [ThreadMessage(**m) for m in json.loads(thread.read_text())]

    try:
        result = triage(text, messages, question=ask, settings=settings)
    except LLMUnavailable as exc:
        typer.secho(f"Cannot analyze: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    if raw:
        typer.echo(result.model_dump_json(indent=2))
    else:
        typer.echo(format_reply(result))


@app.command()
def search(term: str, limit: int = 10):
    """Search rec-knowledge directly."""
    settings = Settings.from_env()
    repo = KnowledgeRepo(settings.knowledge_repo)
    hits = repo.search(term, max_hits=limit)

    if not hits:
        typer.echo(f"No hits for {term!r}")
        return
    for hit in hits:
        typer.echo(f"{hit.path}:{hit.line_number}: {hit.line}")


@app.command()
def evidence(
    alert: str,
    sample: bool = typer.Option(False, "--sample", help="Use sample metrics instead of Grafana"),
):
    """Show the deterministic evidence only, without calling the model."""
    from .analysis import evidence as ev
    from .analysis.identify import identify
    from .sources.grafana import GrafanaClient

    settings = Settings.from_env()
    if sample:
        settings.use_sample_metrics = True

    text = Path(alert).read_text() if Path(alert).is_file() else alert
    identity = identify(text, llm=None)

    grafana = GrafanaClient(settings)
    rule = ev.fetch_rule(grafana, identity)
    resolution = ev.resolve_service(identity, rule)
    metrics = ev.gather(grafana, identity, rule, resolution)

    if resolution.deployment:
        suffix = "" if resolution.is_confident else f"  (unconfirmed: {resolution.note})"
        shown = f"{resolution.deployment.app_label}{suffix}"
    else:
        shown = f"- unresolved -  ({resolution.note})" if resolution.note else "- unresolved -"

    typer.echo(f"alert:      {identity.alert_name} (via {identity.identified_by})")
    typer.echo(f"labels:     {identity.labels or '-'}")
    typer.echo(f"rule:       {rule.expression if rule else '- not found -'}")
    typer.echo(f"deployment: {shown}")

    for m in metrics:
        typer.echo(f"\nquery: {m.query}\n{m.summarize()}")

    for check in ev.benign_checks(identity):
        typer.echo(f"\nbenign check: {check}")


@app.command()
def record(
    alert: str = typer.Argument(..., help="Alert text, or a path to a file containing it"),
    thread: Path = typer.Option(None, help="JSON file with thread messages"),
    dry_run: bool = typer.Option(True, help="Print the draft instead of opening a PR"),
):
    """Draft a rec-knowledge entry from a resolved incident thread."""
    from .analysis import writeback
    from .analysis.identify import identify
    from .llm import LLMClient

    settings = Settings.from_env()
    text = Path(alert).read_text() if Path(alert).is_file() else alert

    messages = []
    if thread:
        messages = [ThreadMessage(**m) for m in json.loads(thread.read_text())]

    try:
        llm = LLMClient(settings)
    except LLMUnavailable as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    repo = KnowledgeRepo(settings.knowledge_repo)
    repo.pull()

    identity = identify(text, llm)
    existing = repo.search_many([identity.alert_name], max_per_term=5)

    try:
        entry = writeback.extract(llm, identity.alert_name, messages, None, existing)
    except LLMUnavailable as exc:
        typer.secho(f"Extraction failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    if entry is None:
        typer.echo("Declined: no durable lesson worth recording in this thread.")
        return

    typer.echo(f"--- {entry.filename} ---")
    typer.echo(entry.body)

    if entry.supersedes:
        typer.secho(f"supersedes: {entry.supersedes}", fg=typer.colors.YELLOW)

    if dry_run:
        typer.echo("\n(dry run — pass --no-dry-run to open a PR)")
        return

    url = repo.open_pr(entry, alert_name=identity.alert_name)
    typer.secho(f"PR opened: {url}", fg=typer.colors.GREEN)


@app.command()
def runs(
    limit: int = typer.Option(20, help="How many to list"),
    alert: str = typer.Option(None, help="Filter by alert name"),
):
    """List recent invocations."""
    from .storage.records import open_store

    store = open_store(Settings.from_env().database_url)
    if not store:
        typer.secho("No DATABASE_URL configured.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)

    rows = store.recent(limit=limit, alert_name=alert)
    if not rows:
        typer.echo("No runs recorded.")
        return

    for r in rows:
        stamp = r["created_at"].strftime("%m-%d %H:%M")
        flags = []
        if r["degraded_tier"]:
            flags.append("degraded")
        if r["error"]:
            flags.append("error")
        if r["verdict"]:
            flags.append(r["verdict"])
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        typer.echo(
            f"#{r['id']:<5} {stamp}  {r['alert_name']:<28} "
            f"{r['confidence'] or '-':<7} {r['investigation_rounds']}r "
            f"{(r['duration_ms'] or 0) / 1000:.0f}s{suffix}"
        )


@app.command()
def replay(invocation_id: int):
    """Show every round of one investigation."""
    from .storage.records import open_store

    store = open_store(Settings.from_env().database_url)
    if not store:
        typer.secho("No DATABASE_URL configured.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)

    run = store.replay(invocation_id)
    if not run:
        typer.secho(f"No run #{invocation_id}", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.secho(f"#{run['id']}  {run['alert_name']}  ({run['created_at']:%Y-%m-%d %H:%M})",
                bold=True)
    typer.echo(f"source: {run['source']}  model: {run['model']}  "
               f"confidence: {run['confidence']}  {(run['duration_ms'] or 0) / 1000:.1f}s")
    if run["question"]:
        typer.echo(f"asked: {run['question']}")

    for step in run["steps"]:
        typer.secho(f"\n[{step['round']}] {step['tool']}({step['args']})",
                    fg=typer.colors.CYAN)
        if step["reasoning"]:
            typer.echo(f"    why: {step['reasoning']}")
        first = (step["observation"] or "").splitlines()[:6]
        for line in first:
            typer.echo(f"    {line[:150]}")

    if run["diagnosis"]:
        typer.secho("\ndiagnosis:", bold=True)
        typer.echo(f"  {run['diagnosis'].get('likely_cause', '')}")


@app.command()
def verdict(
    invocation_id: int,
    result: str = typer.Argument(..., help="correct | wrong | partial"),
    note: str = typer.Option(None, help="What actually happened"),
):
    """Judge a past run, so accuracy can be measured rather than assumed."""
    from .storage.records import open_store

    if result not in ("correct", "wrong", "partial"):
        typer.secho("verdict must be correct, wrong, or partial", fg=typer.colors.RED)
        raise typer.Exit(1)

    store = open_store(Settings.from_env().database_url)
    if not store:
        typer.secho("No DATABASE_URL configured.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)

    if store.set_verdict(invocation_id, result, note):
        typer.secho(f"#{invocation_id} marked {result}", fg=typer.colors.GREEN)


@app.command()
def stats(days: int = typer.Option(30, help="Window in days")):
    """Aggregate metrics over recorded runs."""
    from .storage.records import open_store

    store = open_store(Settings.from_env().database_url)
    if not store:
        typer.secho("No DATABASE_URL configured.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)

    data = store.stats(days=days)
    if not data or not data.get("overall"):
        typer.echo("No data.")
        return

    o = data["overall"]
    typer.secho(f"Last {days} days", bold=True)
    typer.echo(f"  runs:        {o['runs']}  ({o['failed']} failed)")
    typer.echo(f"  reviewed:    {o['reviewed']}")
    typer.echo(f"  avg rounds:  {o['avg_rounds']}")
    typer.echo(f"  avg latency: {(o['avg_ms'] or 0) / 1000:.1f}s")
    if o["degraded"]:
        typer.echo(f"  degraded:    {o['degraded']}")
    if o["on_sample_data"]:
        typer.echo(f"  sample data: {o['on_sample_data']}")

    if data.get("by_confidence"):
        typer.secho("\nBy confidence", bold=True)
        for row in data["by_confidence"]:
            # Unreviewed runs are shown, not folded into a success rate.
            rate = (
                f"{row['wrong'] / row['reviewed']:.0%} wrong"
                if row["reviewed"] else "unreviewed"
            )
            typer.echo(f"  {row['confidence']:<7} {row['n']:>4} runs   {rate}")

    if data.get("tools"):
        typer.secho("\nTool usage", bold=True)
        for row in data["tools"]:
            typer.echo(f"  {row['tool']:<14} {row['n']}")


@app.command()
def serve():
    """Start the Slack bot."""
    from .slack_app import run

    run()


def main():
    sys.exit(app())


if __name__ == "__main__":
    main()
