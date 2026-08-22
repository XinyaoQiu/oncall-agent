"""rec-knowledge access: lexical search, and write-back as a pull request.

Retrieval is ripgrep over a local clone rather than an embedding index. The corpus is a
few hundred markdown files and the queries are exact identifiers — service names, alert
names, error codes — which is lexical search's strong case. Paraphrase is handled by the
model re-querying with different wording, not by a vector store.
"""

import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

from ..models import KnowledgeEntry, KnowledgeHit

RIPGREP_TIMEOUT = 15
GIT_TIMEOUT = 60


class KnowledgeRepoError(RuntimeError):
    pass


class KnowledgeRepo:
    def __init__(self, path: Path):
        self.path = path
        if not (path / ".git").is_dir():
            raise KnowledgeRepoError(
                f"{path} is not a git repository. Set KNOWLEDGE_REPO to a rec-knowledge clone."
            )

    def pull(self) -> None:
        """The local clone goes stale; refresh before searching."""
        self._git("fetch", "origin", check=False)
        self._git("checkout", "main", check=False)
        self._git("pull", "--ff-only", "origin", "main", check=False)

    def search(self, term: str, *, max_hits: int = 10) -> list[KnowledgeHit]:
        """Case-insensitive fixed-string search across the repo."""
        if shutil.which("rg"):
            cmd = [
                "rg", "--no-heading", "--line-number", "--ignore-case",
                "--fixed-strings", "--max-count", "3", "--type", "md", term, ".",
            ]
        else:
            cmd = [
                "grep", "-rniF", "--include=*.md", "--exclude-dir=.git",
                "--line-number", "--max-count=3", term, ".",
            ]

        try:
            proc = subprocess.run(
                cmd, cwd=self.path, capture_output=True, text=True, timeout=RIPGREP_TIMEOUT
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise KnowledgeRepoError(f"search failed for {term!r}: {exc}") from exc

        hits = []
        for line in proc.stdout.splitlines()[:max_hits]:
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            path, lineno, content = parts
            hits.append(
                KnowledgeHit(
                    path=path.lstrip("./"),
                    line_number=int(lineno),
                    line=content.strip(),
                    matched_term=term,
                )
            )
        return hits

    def search_many(self, terms: list[str], *, max_per_term: int = 4) -> list[KnowledgeHit]:
        """Search several terms, keeping one hit per file so one document can't flood."""
        seen_files: set[str] = set()
        results: list[KnowledgeHit] = []
        for term in terms:
            for hit in self.search(term, max_hits=max_per_term):
                if hit.path not in seen_files:
                    seen_files.add(hit.path)
                    results.append(hit)
        return results

    def read_file(self, relative_path: str, max_chars: int = 4000) -> str:
        target = (self.path / relative_path).resolve()
        # Keep reads inside the repo even if the model proposes a traversal path.
        if not str(target).startswith(str(self.path.resolve())):
            raise KnowledgeRepoError(f"path escapes the repo: {relative_path}")
        if not target.is_file():
            raise KnowledgeRepoError(f"no such file: {relative_path}")
        return target.read_text()[:max_chars]

    def list_entries(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.path))
            for p in self.path.rglob("*.md")
            if ".git" not in p.parts
        )

    def open_pr(self, entry: KnowledgeEntry, *, alert_name: str) -> str:
        """Write the entry on a branch and open a PR.

        Every write goes through review: the author is a model, so PR diff/revert/
        attribution is what makes the write path acceptable rather than a convention.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", entry.title.lower()).strip("-")[:40]
        branch = f"agent/incident-{date.today().isoformat()}-{slug}"

        self._git("checkout", "main")
        self._git("pull", "--ff-only", "origin", "main", check=False)
        self._git("checkout", "-b", branch)

        target = self.path / entry.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry.body)

        self._git("add", entry.filename)
        self._git("commit", "-m", f"feat: add incident notes for {entry.title}")
        self._git("push", "-u", "origin", branch)

        body = (
            f"Drafted by oncall-agent from the `{alert_name}` thread.\n\n"
            f"Services: {', '.join(entry.services) or 'n/a'}\n\n"
            "Review before merging — this was extracted by a model and may be wrong."
        )
        if entry.supersedes:
            body += f"\n\nMay supersede: `{entry.supersedes}`"

        proc = subprocess.run(
            ["gh", "pr", "create", "--title", f"feat: {entry.title}", "--body", body,
             "--base", "main", "--head", branch],
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        self._git("checkout", "main", check=False)

        if proc.returncode != 0:
            raise KnowledgeRepoError(f"gh pr create failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, text=True, timeout=GIT_TIMEOUT
        )
        if check and proc.returncode != 0:
            raise KnowledgeRepoError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()
