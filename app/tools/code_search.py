"""Lexical search over the repo registry, as LangChain tools.

Ported from `src/oncall_agent/investigate/tools.py`. Two behaviours are load-bearing and are
preserved exactly:

- **Aggregation by file.** An identifier appearing forty times in one file is one fact, not
  forty. Every tool return is text the model reads next round, so truncation *is* the
  accuracy work: a 500-hit hit list cannot enter context, and which hits survive decides
  whether the answer is found at all.
- **Path containment** (spec §9 constraint 15). `read_file` and `list_dir` resolve the path
  and then check it is still under the repo root, because a model that has been told about
  `../` will eventually try it.

Vector search does not replace this (spec §7.3): `ERR_4021` and `x_status_code` are exact
tokens, and embedding them loses the property that makes them findable.

`subprocess` is synchronous, so every call goes through `asyncio.to_thread` — these tools run
inside async graph nodes on the Slack event loop (spec §5.1).
"""

import asyncio
import shutil
import subprocess
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from app.domain.repos import Repo, RepoRegistry, default_registry

SEARCH_TIMEOUT = 20
MAX_FILES_LISTED = 12
MAX_HITS_PER_FILE = 3
MAX_READ_LINES = 200
MAX_DIR_ENTRIES = 60


def _rg_available() -> bool:
    return shutil.which("rg") is not None


def _build_search_cmd(repo: Repo, pattern: str, path_filter: str | None) -> list[str]:
    target = path_filter or "."
    if _rg_available():
        cmd = [
            "rg", "--no-heading", "--line-number", "--ignore-case",
            "--max-count", str(MAX_HITS_PER_FILE),
        ]
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


def _contained(root: Path, path: str) -> Path | None:
    """The resolved path, or None if it left the repo. Resolve first: `..` and symlinks both
    only show themselves after resolution."""
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        return None
    return resolved


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=SEARCH_TIMEOUT)


def code_search_tools(registry: RepoRegistry | None = None) -> list[BaseTool]:
    """The four lexical tools, bound to a registry. The model chooses the repo by name."""
    reg = registry if registry is not None else default_registry()

    @tool
    async def search_code(
        pattern: str, repo: str | None = None, path_filter: str | None = None
    ) -> str:
        """Search code lexically (regex) across one repo or all of them.

        Args:
            pattern: regex or literal identifier, e.g. x_status_code or ERR_[0-9]+
            repo: repo name; omit to search every available repo
            path_filter: restrict to a subdirectory or file glob within the repo
        """
        targets = [reg.get(repo)] if repo else reg.available()
        targets = [t for t in targets if t and t.available]
        if not targets:
            known = ", ".join(r.name for r in reg.available()) or "none"
            return f"No such repo: {repo!r}. Available: {known}"

        by_file: dict[str, list[str]] = {}
        for target in targets:
            try:
                proc = await asyncio.to_thread(
                    _run, _build_search_cmd(target, pattern, path_filter), target.path
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
            return f"No matches for {pattern!r} in {scope}."

        total = sum(len(v) for v in by_file.values())
        shown = sorted(by_file.items(), key=lambda kv: -len(kv[1]))[:MAX_FILES_LISTED]

        lines = [f"{total} matches in {len(by_file)} files:"]
        for path, hits in shown:
            lines.append(f"{path} ({len(hits)})")
            lines.extend(hits[:MAX_HITS_PER_FILE])
        if len(by_file) > MAX_FILES_LISTED:
            lines.append(f"... {len(by_file) - MAX_FILES_LISTED} more files not shown")
        return "\n".join(lines)

    @tool
    async def read_file(repo: str, path: str, start: int = 1, lines: int = 60) -> str:
        """Read a bounded, line-numbered window of one file.

        Args:
            repo: repo name
            path: path relative to the repo root
            start: first line to show (1-based)
            lines: how many lines to show (capped at 200)
        """
        target = reg.get(repo)
        if not target or not target.available:
            return f"No such repo: {repo!r}"

        resolved = _contained(target.path, path)
        if resolved is None:
            return f"Path escapes the repo: {path}"
        if not resolved.is_file():
            return f"No such file: {repo}/{path}"

        content = await asyncio.to_thread(resolved.read_text, errors="replace")
        all_lines = content.splitlines()
        offset = max(start - 1, 0)
        window = all_lines[offset: offset + min(lines, MAX_READ_LINES)]
        numbered = "\n".join(f"{start + i}: {line}" for i, line in enumerate(window))
        return (
            f"{repo}/{path} lines {start}-{start + len(window) - 1} "
            f"of {len(all_lines)}:\n{numbered}"
        )

    @tool
    async def git_log(repo: str, path: str | None = None, since: str = "7 days ago") -> str:
        """Recent commits, for correlating a change with an alert window.

        Args:
            repo: repo name
            path: restrict to commits touching this path
            since: git date expression, e.g. '2 days ago'
        """
        target = reg.get(repo)
        if not target or not target.available:
            return f"No such repo: {repo!r}"

        cmd = [
            "git", "log", f"--since={since}", "--pretty=format:%h %ad %an: %s",
            "--date=format:%Y-%m-%d %H:%M", "-20",
        ]
        if path:
            cmd += ["--", path]

        try:
            proc = await asyncio.to_thread(_run, cmd, target.path)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return f"git log failed: {exc}"

        scope = f" touching {path}" if path else ""
        if not proc.stdout.strip():
            return f"No commits in {repo}{scope} since {since}."
        return f"Commits in {repo}{scope} since {since}:\n{proc.stdout}"

    @tool
    async def list_dir(repo: str, path: str = ".") -> str:
        """List a directory inside a repo, to find where to look next.

        Args:
            repo: repo name
            path: directory relative to the repo root
        """
        target = reg.get(repo)
        if not target or not target.available:
            return f"No such repo: {repo!r}"

        resolved = _contained(target.path, path)
        if resolved is None or not resolved.is_dir():
            return f"No such directory: {repo}/{path}"

        entries = sorted(
            f"{e.name}/" if e.is_dir() else e.name
            for e in resolved.iterdir()
            if e.name not in target.exclude_dirs
        )[:MAX_DIR_ENTRIES]
        return f"{repo}/{path}:\n" + "\n".join(entries)

    return [search_code, read_file, git_log, list_dir]
