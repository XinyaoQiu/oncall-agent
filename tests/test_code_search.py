"""Spec §9 constraint 15 (path containment) and the by-file flood guard (§7.3).

Both are correctness, not tidiness: a read that escapes the repo root exfiltrates whatever
the process can open, and a hit list that floods the context window decides which evidence
the model never sees.
"""

from pathlib import Path

import pytest

from app.domain.repos import Repo, RepoRegistry
from app.tools.code_search import MAX_HITS_PER_FILE, code_search_tools


def _hits_under(output: str, file_line_prefix: str) -> int:
    lines = output.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(file_line_prefix))
    hits = 0
    for line in lines[start + 1:]:
        if not line.startswith("  "):
            break
        hits += 1
    return hits


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    (root / "internal").mkdir(parents=True)
    (root / "internal" / "hot.go").write_text(
        "\n".join(f"line {i}: x_status_code = {i}" for i in range(40))
    )
    (root / "cold.go").write_text("one mention of x_status_code here\n")
    (tmp_path / "secret.txt").write_text("credentials\n")
    return root


@pytest.fixture
def tools(repo_root: Path) -> dict:
    registry = RepoRegistry(repos=[Repo(name="demo", path=repo_root, language="go")])
    return {t.name: t for t in code_search_tools(registry)}


async def test_path_escape_rejected_on_read_file(tools):
    out = await tools["read_file"].ainvoke({"repo": "demo", "path": "../secret.txt"})
    assert "escapes the repo" in out
    assert "credentials" not in out


async def test_absolute_path_rejected_on_read_file(tools):
    out = await tools["read_file"].ainvoke({"repo": "demo", "path": "/etc/passwd"})
    assert "escapes the repo" in out


async def test_path_escape_rejected_on_list_dir(tools, repo_root):
    out = await tools["list_dir"].ainvoke({"repo": "demo", "path": ".."})
    assert "No such directory" in out
    assert "secret.txt" not in out


async def test_contained_paths_still_work(tools):
    out = await tools["read_file"].ainvoke({"repo": "demo", "path": "internal/../cold.go"})
    assert "one mention of x_status_code" in out

    listing = await tools["list_dir"].ainvoke({"repo": "demo", "path": "."})
    assert "internal/" in listing
    assert "cold.go" in listing


async def test_search_aggregates_by_file(tools):
    out = await tools["search_code"].ainvoke({"pattern": "x_status_code", "repo": "demo"})

    assert out.splitlines()[0].endswith("in 2 files:")
    assert out.count("demo/internal/hot.go") == 1
    assert out.count("demo/cold.go") == 1

    assert 0 < _hits_under(out, "demo/internal/hot.go") <= MAX_HITS_PER_FILE


async def test_no_match_is_not_silence(tools):
    out = await tools["search_code"].ainvoke({"pattern": "definitely_absent_token", "repo": "demo"})
    assert "No matches" in out


async def test_unknown_repo_lists_what_exists(tools):
    out = await tools["search_code"].ainvoke({"pattern": "x", "repo": "nope"})
    assert "No such repo" in out
    assert "demo" in out
