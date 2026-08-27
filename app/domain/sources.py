"""What each data source can and cannot be used for.

The highest-risk correctness issue in this system (tech-design §5.1): a number whose
sampling rate the reader does not know is worse than no number, because it carries into an
incident review looking authoritative and nobody re-derives it.

MCP makes this harder rather than easier. The protocol has no provenance field, so a loaded
tool is a name, a description and an argument schema — and a log-search tool whose `total`
is a limit-capped row count looks, to a planner asked "how many users were affected",
exactly like an impact source. Provenance is therefore attached where tools are loaded
(app/tools/registry.py), from this table, rather than trusted to each tool's author.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"


class SourceContract(BaseModel):
    """What a tool's results mean, and what they may not be used for."""

    tool: str
    system: str = "unknown"
    sampling_rate: str | None = None
    retention: str | None = None
    usable_for: list[str] = Field(default_factory=list)
    not_usable_for: list[str] = Field(default_factory=list)
    synthetic: bool = False
    note: str = ""

    @property
    def is_impact_source(self) -> bool:
        return "impact_quantification" in self.usable_for

    def caveat(self) -> str:
        """The one line that must travel with every number this tool returns."""
        parts = []
        if self.synthetic:
            parts.append("SYNTHETIC — generated fixture data, not a measurement")
        if self.sampling_rate:
            parts.append(f"sampled {self.sampling_rate}")
        if self.retention:
            parts.append(f"retention {self.retention}")
        if self.not_usable_for:
            parts.append("not usable for " + ", ".join(self.not_usable_for))
        if self.note:
            parts.append(self.note)
        return "; ".join(parts)


# A tool nobody has declared is qualitative and is never an impact source. Adding an MCP
# server must not be able to silently add one.
UNKNOWN_CONTRACT = SourceContract(
    tool="<unregistered>",
    usable_for=["qualitative_breakdown"],
    not_usable_for=["impact_quantification"],
    note="source not declared in config/sources.yaml; treated as qualitative only",
)


def _load() -> dict[str, SourceContract]:
    if not CONFIG_PATH.is_file():
        return {}
    raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return {
        name: SourceContract(tool=name, **(body or {}))
        for name, body in (raw.get("sources") or {}).items()
    }


SOURCES: dict[str, SourceContract] = _load()


def contract_for(tool_name: str) -> SourceContract:
    """Default deny: an unregistered tool cannot become an impact source by accident."""
    found = SOURCES.get(tool_name)
    if found:
        return found
    return UNKNOWN_CONTRACT.model_copy(update={"tool": tool_name})
