"""Settings and the deployment shard table."""

import os
from pathlib import Path

from pydantic import BaseModel


class Settings(BaseModel):
    gemini_api_key: str | None = None
    gemini_model_fast: str = "gemini-flash-latest"
    gemini_model_deep: str = "gemini-pro-latest"

    grafana_url: str | None = None
    grafana_token: str | None = None
    use_sample_metrics: bool = False

    knowledge_repo: Path = Path.home() / "Project" / "rec-knowledge"
    knowledge_remote_branch_prefix: str = "agent/incident-"

    slack_bot_token: str | None = None
    slack_app_token: str | None = None

    database_url: str | None = None

    repo_root: Path = Path.home() / "Project"
    investigation_rounds: int = 6
    investigation_seconds: float = 90.0

    llm_max_attempts: int = 4
    max_search_rounds: int = 3
    query_window_minutes: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            gemini_model_fast=os.getenv("GEMINI_MODEL_FAST", "gemini-flash-latest"),
            gemini_model_deep=os.getenv("GEMINI_MODEL_DEEP", "gemini-pro-latest"),
            grafana_url=os.getenv("GRAFANA_URL"),
            grafana_token=os.getenv("GRAFANA_TOKEN"),
            use_sample_metrics=os.getenv("USE_SAMPLE_METRICS", "").lower() in ("1", "true", "yes"),
            knowledge_repo=Path(
                os.getenv("KNOWLEDGE_REPO", str(Path.home() / "Project" / "rec-knowledge"))
            ),
            database_url=os.getenv("DATABASE_URL"),
            repo_root=Path(os.getenv("REPO_ROOT", str(Path.home() / "Project"))),
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN"),
            slack_app_token=os.getenv("SLACK_APP_TOKEN"),
        )


class Deployment(BaseModel):
    """One deployment shard of the server binary.

    The four naming dimensions get conflated constantly, so all of them are returned
    explicitly rather than a single ambiguous "service name".
    """

    app_label: str
    pod_pattern: str
    hosts: list[str]
    path_prefixes: list[str]
    replicas: int
    traffic_share: str
    code_route_paths: list[str]


# The server binary is deployed as several path-scoped deployments. Mapping an alert's
# host/path to one of them is a lookup, not a search.
DEPLOYMENTS: list[Deployment] = [
    Deployment(
        app_label="server-default",
        pod_pattern="server-default-*",
        hosts=["api.newsbreak.com"],
        path_prefixes=["/"],
        replicas=40,
        traffic_share="catch-all",
        code_route_paths=["server/router/default.go"],
    ),
    Deployment(
        app_label="server-a4api-web",
        pod_pattern="server-a4api-web-*",
        hosts=["www.newsbreak.com"],
        path_prefixes=["/Website/web/"],
        replicas=12,
        traffic_share="web entry",
        code_route_paths=["server/router/web.go"],
    ),
    Deployment(
        app_label="server-feed",
        pod_pattern="server-feed-*",
        hosts=["api.newsbreak.com"],
        path_prefixes=["/Website/channel/", "/Website/news/"],
        replicas=60,
        traffic_share="~90% of feed traffic",
        code_route_paths=["server/router/feed.go", "server/controller/feed/"],
    ),
    Deployment(
        app_label="server-a4api-default",
        pod_pattern="server-a4api-default-*",
        hosts=["api.newsbreak.com"],
        path_prefixes=["/Website/profile/", "/Website/user/"],
        replicas=20,
        traffic_share="~10% of feed traffic",
        code_route_paths=["server/router/a4api.go"],
    ),
]


CATCH_ALL_PREFIX = "/"


class Resolution(BaseModel):
    """Which deployment serves an alert, and how sure the lookup is.

    Separate from Deployment because the caller needs both the answer and its standing.
    A guess that says it is a guess is usable; the same guess unmarked is a trap — the
    pod and replica queries it produces return true data about the wrong workload, and
    the failure renders exactly like a success.
    """

    deployment: Deployment | None = None
    confidence: str = "unresolved"  # exact | low | unresolved
    matched_by: str = "nothing"
    note: str = ""

    @property
    def is_confident(self) -> bool:
        return self.confidence == "exact"

    @property
    def app_label(self) -> str | None:
        return self.deployment.app_label if self.deployment else None


def resolve_deployment(
    host: str | None = None, path: str | None = None, app_label: str | None = None
) -> Deployment | None:
    """Look up which deployment serves a host/path. None when nothing matches."""
    return resolve(host=host, path=path, app_label=app_label).deployment


def resolve(
    host: str | None = None, path: str | None = None, app_label: str | None = None
) -> Resolution:
    """Resolve, and say how the answer was reached.

    Only an explicit app label or a real path prefix counts as exact. Landing on the
    catch-all, or matching a host with nothing else to narrow it, is a guess and is
    returned as one.
    """
    if app_label:
        found = next((d for d in DEPLOYMENTS if d.app_label == app_label), None)
        if found:
            return Resolution(
                deployment=found, confidence="exact", matched_by="app_label"
            )
        # A label naming a workload the table does not know is not a reason to hand back
        # some other workload.
        return Resolution(
            matched_by="app_label",
            note=f"no deployment named {app_label!r}; it may not be in the shard table",
        )

    candidates = DEPLOYMENTS
    if host:
        by_host = [d for d in candidates if host in d.hosts]
        if not by_host:
            return Resolution(
                matched_by="host",
                note=f"no deployment serves host {host!r}",
            )
        candidates = by_host

    if path:
        # Longest prefix wins, so /Website/channel/ beats the "/" catch-all.
        best, best_prefix = None, ""
        for d in candidates:
            for prefix in d.path_prefixes:
                if path.startswith(prefix) and len(prefix) > len(best_prefix):
                    best, best_prefix = d, prefix

        if best and best_prefix != CATCH_ALL_PREFIX:
            return Resolution(
                deployment=best, confidence="exact", matched_by="host+path" if host else "path"
            )
        if best:
            return Resolution(
                deployment=best,
                confidence="low",
                matched_by="catch-all",
                note=(
                    f"path {path!r} matched no deployment prefix and fell through to the "
                    f"catch-all; {best.app_label} may not serve it"
                ),
            )
        return Resolution(
            matched_by="path", note=f"path {path!r} matched no deployment prefix"
        )

    if host:
        return Resolution(
            deployment=candidates[0],
            confidence="low",
            matched_by="host-only",
            note=(
                f"resolved from host {host!r} with no path to narrow it; "
                f"{host} is served by {len(candidates)} deployment(s)"
                if len(candidates) > 1
                else f"resolved from host {host!r} with no path to narrow it"
            ),
        )

    return Resolution(note="no host, path, or app label to resolve from")


def resolve_blast_radius(code_path: str) -> list[Deployment]:
    """Inverse direction: which deployments run this code."""
    return [d for d in DEPLOYMENTS if any(code_path.startswith(p) for p in d.code_route_paths)]
