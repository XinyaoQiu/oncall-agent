"""Tools the investigation loop can call.

Every tool returns text the model reads next round, so truncation *is* the accuracy
work: a 500-hit grep list cannot enter context, and which hits survive determines
whether the answer is found at all.
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..models import MetricResult
from ..repos import Repo, RepoRegistry
from ..sources.grafana import GrafanaClient

SEARCH_TIMEOUT = 20
MAX_FILES_LISTED = 12
MAX_HITS_PER_FILE = 3


@dataclass
class ToolResult:
    text: str
    hit_count: int = 0
    truncated: bool = False


def _rg_available() -> bool:
    return shutil.which("rg") is not None


def _build_search_cmd(repo: Repo, pattern: str, path_filter: str | None) -> list[str]:
    target = path_filter or "."
    if _rg_available():
        cmd = ["rg", "--no-heading", "--line-number", "--ignore-case", "--max-count",
               str(MAX_HITS_PER_FILE)]
        for d in repo.exclude_dirs:
            cmd += ["--glob", f"!{d}/**"]
        for g in repo.exclude_globs:
            cmd += ["--glob", f"!{g}"]
        return cmd + [pattern, target]

    cmd = ["grep", "-rniE", "--line-number", f"--max-count={MAX_HITS_PER_FILE}"]
    for d in repo.exclude_dirs:
        cmd.append(f"--exclude-dir={d}")
    for g in repo.exclude_globs:
        cmd.append(f"--exclude={g}")
    return cmd + [pattern, target]


def search_code(
    registry: RepoRegistry, pattern: str, repo: str | None = None,
    path_filter: str | None = None,
) -> ToolResult:
    """Lexical search across one repo or all of them.

    Results are aggregated by file rather than listed line by line. An identifier
    appearing forty times in one file is one fact, not forty.
    """
    targets = [registry.get(repo)] if repo else registry.available()
    targets = [t for t in targets if t and t.available]
    if not targets:
        known = ", ".join(r.name for r in registry.available()) or "none"
        return ToolResult(text=f"No such repo: {repo!r}. Available: {known}")

    by_file: dict[str, list[str]] = {}
    for target in targets:
        try:
            proc = subprocess.run(
                _build_search_cmd(target, pattern, path_filter),
                cwd=target.path, capture_output=True, text=True, timeout=SEARCH_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            by_file[f"{target.name}: <search failed>"] = [str(exc)]
            continue

        for line in proc.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            path, lineno, content = parts
            key = f"{target.name}/{path.lstrip('./')}"
            by_file.setdefault(key, []).append(f"  {lineno}: {content.strip()[:160]}")

    if not by_file:
        scope = repo or "any repo"
        return ToolResult(text=f"No matches for {pattern!r} in {scope}.")

    total = sum(len(v) for v in by_file.values())
    shown = sorted(by_file.items(), key=lambda kv: -len(kv[1]))[:MAX_FILES_LISTED]

    lines = [f"{total} matches in {len(by_file)} files:"]
    for path, hits in shown:
        lines.append(f"{path} ({len(hits)})")
        lines.extend(hits[:MAX_HITS_PER_FILE])

    truncated = len(by_file) > MAX_FILES_LISTED
    if truncated:
        lines.append(f"... {len(by_file) - MAX_FILES_LISTED} more files not shown")

    return ToolResult(text="\n".join(lines), hit_count=total, truncated=truncated)


def read_file(
    registry: RepoRegistry, repo: str, path: str, start: int = 1, lines: int = 60
) -> ToolResult:
    """Read a bounded window of a file."""
    target = registry.get(repo)
    if not target or not target.available:
        return ToolResult(text=f"No such repo: {repo!r}")

    resolved = (target.path / path).resolve()
    # Keep reads inside the repo even when the model proposes a traversal path.
    if not str(resolved).startswith(str(target.path.resolve())):
        return ToolResult(text=f"Path escapes the repo: {path}")
    if not resolved.is_file():
        return ToolResult(text=f"No such file: {repo}/{path}")

    content = resolved.read_text(errors="replace").splitlines()
    window = content[max(start - 1, 0): max(start - 1, 0) + min(lines, 200)]
    numbered = "\n".join(f"{start + i}: {line}" for i, line in enumerate(window))
    return ToolResult(
        text=f"{repo}/{path} lines {start}-{start + len(window) - 1} of {len(content)}:\n{numbered}"
    )


def git_log(
    registry: RepoRegistry, repo: str, path: str | None = None, since: str = "7 days ago"
) -> ToolResult:
    """Recent commits, for correlating a change with an alert window."""
    target = registry.get(repo)
    if not target or not target.available:
        return ToolResult(text=f"No such repo: {repo!r}")

    cmd = ["git", "log", f"--since={since}", "--pretty=format:%h %ad %an: %s",
           "--date=format:%Y-%m-%d %H:%M", "-20"]
    if path:
        cmd += ["--", path]

    try:
        proc = subprocess.run(
            cmd, cwd=target.path, capture_output=True, text=True, timeout=SEARCH_TIMEOUT
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return ToolResult(text=f"git log failed: {exc}")

    scope = f" touching {path}" if path else ""
    if not proc.stdout.strip():
        return ToolResult(text=f"No commits in {repo}{scope} since {since}.")
    return ToolResult(text=f"Commits in {repo}{scope} since {since}:\n{proc.stdout}")


def list_dir(registry: RepoRegistry, repo: str, path: str = ".") -> ToolResult:
    target = registry.get(repo)
    if not target or not target.available:
        return ToolResult(text=f"No such repo: {repo!r}")

    resolved = (target.path / path).resolve()
    if not str(resolved).startswith(str(target.path.resolve())) or not resolved.is_dir():
        return ToolResult(text=f"No such directory: {repo}/{path}")

    entries = sorted(
        f"{e.name}/" if e.is_dir() else e.name
        for e in resolved.iterdir()
        if e.name not in target.exclude_dirs
    )[:60]
    return ToolResult(text=f"{repo}/{path}:\n" + "\n".join(entries))


def query_metric(grafana: GrafanaClient, expr: str, minutes: int = 60) -> ToolResult:
    """Run a PromQL query the model composed."""
    result: MetricResult = grafana.query_range(expr, minutes=minutes)
    return ToolResult(text=f"query: {expr}\n{result.summarize()}", hit_count=len(result.series))


def build_toolset(settings: Settings, registry: RepoRegistry, grafana: GrafanaClient) -> dict:
    """Name → callable, matching the schema the model chooses from."""
    return {
        "search_code": lambda pattern, repo=None, path_filter=None: search_code(
            registry, pattern, repo, path_filter
        ),
        "read_file": lambda repo, path, start=1, lines=60: read_file(
            registry, repo, path, start, lines
        ),
        "git_log": lambda repo, path=None, since="7 days ago": git_log(
            registry, repo, path, since
        ),
        "list_dir": lambda repo, path=".": list_dir(registry, repo, path),
        "query_metric": lambda expr, minutes=60: query_metric(grafana, expr, minutes),
    }
