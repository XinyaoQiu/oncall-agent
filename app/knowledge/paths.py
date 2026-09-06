"""两个知识库的位置。

confluence 是公司 wiki 的镜像——只读，进向量库，我们不写它。
rec-knowledge 是这个 agent 自己的知识——datasources / cases / lessons，走 grep 和常驻注入。

两者都在仓库外面，因为它们的生命周期和代码无关：wiki 由别的团队维护，
rec-knowledge 由 agent 自己写、人 merge。路径可配，默认按同级目录找。
"""

from functools import lru_cache
from pathlib import Path

from app.config import Settings, get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


@lru_cache(maxsize=8)
def _cached(raw: str) -> Path:
    return _resolve(raw)


def confluence_root(settings: Settings | None = None) -> Path:
    return _cached((settings or get_settings()).confluence_dir)


def rec_knowledge_root(settings: Settings | None = None) -> Path:
    return _cached((settings or get_settings()).rec_knowledge_dir)


def datasources_dir(settings: Settings | None = None) -> Path:
    return rec_knowledge_root(settings) / "datasources"


def lessons_dir(settings: Settings | None = None) -> Path:
    return rec_knowledge_root(settings) / "lessons"


def cases_dir(settings: Settings | None = None) -> Path:
    return rec_knowledge_root(settings) / "cases"


def alerts_registry_path(settings: Settings | None = None) -> Path:
    return rec_knowledge_root(settings) / "alerts.yaml"
