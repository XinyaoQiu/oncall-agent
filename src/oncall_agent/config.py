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

    llm_max_attempts: int = 4
    max_search_rounds: int = 3
    query_window_minutes: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            grafana_url=os.getenv("GRAFANA_URL"),
            grafana_token=os.getenv("GRAFANA_TOKEN"),
            use_sample_metrics=os.getenv("USE_SAMPLE_METRICS", "").lower() in ("1", "true", "yes"),
            knowledge_repo=Path(
                os.getenv("KNOWLEDGE_REPO", str(Path.home() / "Project" / "rec-knowledge"))
            ),
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


def resolve_deployment(
    host: str | None = None, path: str | None = None, app_label: str | None = None
) -> Deployment | None:
    """Look up which deployment serves a host/path."""
    if app_label:
        return next((d for d in DEPLOYMENTS if d.app_label == app_label), None)

    candidates = DEPLOYMENTS
    if host:
        candidates = [d for d in candidates if host in d.hosts] or candidates

    if path:
        # Longest prefix wins, so /Website/channel/ beats the "/" catch-all.
        best, best_len = None, -1
        for d in candidates:
            for prefix in d.path_prefixes:
                if path.startswith(prefix) and len(prefix) > best_len:
                    best, best_len = d, len(prefix)
        if best:
            return best

    return candidates[0] if candidates and host else None


def resolve_blast_radius(code_path: str) -> list[Deployment]:
    """Inverse direction: which deployments run this code."""
    return [d for d in DEPLOYMENTS if any(code_path.startswith(p) for p in d.code_route_paths)]
