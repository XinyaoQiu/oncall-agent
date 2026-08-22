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
        result = triage(text, messages, settings=settings)
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
    deployment = ev.resolve_service(identity, rule)
    metrics = ev.gather(grafana, identity, rule, deployment)

    typer.echo(f"alert:      {identity.alert_name} (via {identity.identified_by})")
    typer.echo(f"labels:     {identity.labels or '-'}")
    typer.echo(f"rule:       {rule.expression if rule else '- not found -'}")
    typer.echo(f"deployment: {deployment.app_label if deployment else '- unresolved -'}")

    for m in metrics:
        typer.echo(f"\nquery: {m.query}\n{m.summarize()}")

    for check in ev.benign_checks(identity):
        typer.echo(f"\nbenign check: {check}")


@app.command()
def serve():
    """Start the Slack bot."""
    from .slack_app import run

    run()


def main():
    sys.exit(app())


if __name__ == "__main__":
    main()
