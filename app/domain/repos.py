"""The repositories the agent may search.

Production is not one repo. An alert on an endpoint can lead into the service that serves
it, a shared library it calls, the config repo that deployed it, or the infra repo that
owns its ingress — and which one matters is rarely known up front.
"""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.config import get_settings

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "repos.yaml"

DEFAULT_EXCLUDE_DIRS = ["vendor", "node_modules", ".git", "testdata", "mocks", "generated"]
DEFAULT_EXCLUDE_GLOBS = ["*.pb.go", "*_test.go", "*.min.js", "*.lock"]


class Repo(BaseModel):
    """One searchable repository."""

    name: str
    path: Path
    language: str = "go"
    description: str = ""

    # Ranked ahead of others when the alert points at this repo's services.
    owns_services: list[str] = Field(default_factory=list)

    # Directories that dominate grep results without explaining outages.
    exclude_dirs: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS))
    exclude_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))

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

        Ordering matters more than filtering: a search budget spent on the wrong repo is a
        round the agent does not get back, but excluding a repo outright would hide the
        cross-repo causes that make multi-repo triage hard in the first place. Every
        available repo is always returned.
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


def _expand(raw_path: str, root: Path) -> Path:
    return Path(raw_path.replace("${REPO_ROOT}", str(root))).expanduser()


def load_registry(root: Path | None = None) -> RepoRegistry:
    """Registry from config/repos.yaml, with ${REPO_ROOT} bound to `root`.

    Repos absent from this checkout are kept, not dropped: `available()` decides what can
    be searched, and `get()` should still be able to explain a repo the config names.
    """
    root = Path(root) if root else get_settings().repo_root
    if not CONFIG_PATH.is_file():
        return RepoRegistry()

    raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    exclude_dirs = raw.get("exclude_dirs") or DEFAULT_EXCLUDE_DIRS
    exclude_globs = raw.get("exclude_globs") or DEFAULT_EXCLUDE_GLOBS

    repos = []
    for body in raw.get("repos") or []:
        body = dict(body)
        body["path"] = _expand(str(body.get("path", "")), root)
        body.setdefault("exclude_dirs", list(exclude_dirs))
        body.setdefault("exclude_globs", list(exclude_globs))
        repos.append(Repo(**body))
    return RepoRegistry(repos=repos)


@lru_cache
def default_registry() -> RepoRegistry:
    return load_registry()
