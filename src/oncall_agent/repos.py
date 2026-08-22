"""The repositories the agent may search.

Production is not one repo. An alert on an endpoint can lead into the service that
serves it, a shared library it calls, the config repo that deployed it, or the infra
repo that owns its ingress — and which one matters is rarely known up front.
"""

from pathlib import Path

from pydantic import BaseModel, Field


class Repo(BaseModel):
    """One searchable repository."""

    name: str
    path: Path
    language: str = "go"
    description: str = ""

    # Ranked ahead of others when the alert points at this repo's services.
    owns_services: list[str] = Field(default_factory=list)

    # Directories that dominate grep results without explaining outages.
    exclude_dirs: list[str] = Field(
        default_factory=lambda: [
            "vendor", "node_modules", ".git", "testdata", "mocks", "generated",
        ]
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: ["*.pb.go", "*_test.go", "*.min.js", "*.lock"]
    )

    @property
    def available(self) -> bool:
        return self.path.is_dir()


class RepoRegistry(BaseModel):
    repos: list[Repo] = Field(default_factory=list)

    def get(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)

    def available(self) -> list[Repo]:
        return [r for r in self.repos if r.available]

    def rank_for(self, service: str | None) -> list[Repo]:
        """Repos most likely to explain an alert on this service, best first.

        Ordering matters more than filtering: a search budget spent on the wrong repo
        is a round the agent does not get back, but excluding a repo outright would
        hide the cross-repo causes that make multi-repo triage hard in the first place.
        """
        repos = self.available()
        if not service:
            return repos
        return sorted(repos, key=lambda r: 0 if service in r.owns_services else 1)

    def describe(self) -> str:
        """Repo list for the model, so it can choose where to look."""
        lines = []
        for r in self.available():
            owns = f" (serves {', '.join(r.owns_services)})" if r.owns_services else ""
            lines.append(f"- {r.name}: {r.description or r.language}{owns}")
        return "\n".join(lines) or "(no repositories configured)"


def default_registry(root: Path | None = None) -> RepoRegistry:
    """Registry built from a checkout root, keeping only repos that exist locally."""
    root = root or Path.home() / "Project"

    candidates = [
        Repo(
            name="server",
            path=root / "server",
            description="main API binary, deployed as several path-scoped deployments",
            owns_services=[
                "server-default", "server-feed", "server-a4api-web", "server-a4api-default",
            ],
        ),
        Repo(
            name="local-server",
            path=root / "local-server",
            description="local/POI services (gas stations, weather)",
            owns_services=["local-server"],
        ),
        Repo(
            name="rec-knowledge",
            path=root / "rec-knowledge",
            language="markdown",
            description="incident write-ups and runbooks",
        ),
        Repo(
            name="sre-configs",
            path=root / "sre-configs",
            language="yaml",
            description="ingress, HPA and deployment configuration",
        ),
        Repo(
            name="api-schema",
            path=root / "api-schema",
            language="protobuf",
            description="shared request/response schemas",
        ),
    ]
    return RepoRegistry(repos=[r for r in candidates if r.available])
