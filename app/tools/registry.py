"""Tool loading, and the provenance envelope applied at load time (spec §7.2).

This is the highest-risk correctness issue in the system, and MCP makes it harder rather
than easier: the protocol has no provenance field, so a loaded tool is a name, a description
and an argument schema. A planner asked "how many users were affected" cannot tell a
log-search tool whose `total` is a `limit=100`-capped row count from an ingress counter.

So provenance is attached **where tools are loaded**, not where they are written. Every tool
— local or MCP — is wrapped so its result comes back as `Observation.render()` output, which
prints the sampling rate, the retention window and the "not usable for" list in the same
string as the number. There is no code path that returns the bare value.

Three rules, in code:

1. **Default deny.** `contract_for()` gives an unregistered tool `usable_for:
   [qualitative_breakdown]` and `not_usable_for: [impact_quantification]`. Adding an MCP
   server cannot silently add an impact source; it can only add a qualitative one.
2. **Impact is not a tool the model routes.** `quantify_impact()` is local and has no source
   parameter, so counting from a sampled log is not prohibited — it is inexpressible.
3. **Synthetic data is disclosed structurally.** A tool whose contract says `synthetic: true`
   renders "SYNTHETIC" into every result, and `ContractualTool.contract.synthetic` is what
   the executor reads to set `used_synthetic` in state.
"""

import importlib
import json
from typing import Any

from langchain_core.tools import BaseTool
from loguru import logger
from pydantic import ConfigDict

from app.config import Settings
from app.domain.repos import load_registry
from app.domain.sources import SOURCES, SourceContract, contract_for
from app.evidence.envelope import Observation
from app.tools.code_search import code_search_tools
from app.tools.mcp_client import McpClientHolder

SYNTHETIC_MARKER = "SYNTHETIC"

# Local tool factories that land later in the migration. A module that does not exist yet is
# skipped; one that exists without its factory is a wiring mistake and warns.
OPTIONAL_LOCAL_FACTORIES: list[tuple[str, str]] = [
    ("app.tools.knowledge", "knowledge_tools"),
    ("app.tools.impact", "impact_tools"),
    ("app.tools.deployment", "deployment_tools"),
]


def _compact(args: Any) -> str:
    if isinstance(args, dict):
        try:
            return json.dumps(args, ensure_ascii=False, default=str, sort_keys=True)
        except TypeError:
            return str(args)
    return str(args)


def _with_source(observation: Observation, tool_name: str) -> Observation:
    if observation.source != "unknown":
        return observation
    return observation.model_copy(update={"source": tool_name})


def to_observations(tool_name: str, args: Any, raw: Any) -> list[Observation]:
    """Whatever a tool returned, as observations carrying `source=<tool name>`.

    The source is the tool name because that is the key `contract_for()` resolves, so a tool
    that returns a plain string still arrives with its sampling caveat attached.
    """
    call = f"{tool_name}({_compact(args)})"

    if isinstance(raw, Observation):
        return [_with_source(raw, tool_name)]

    if isinstance(raw, (list, tuple)) and raw and all(isinstance(r, Observation) for r in raw):
        return [_with_source(r, tool_name) for r in raw]

    if isinstance(raw, dict) and ("series" in raw or "query" in raw):
        try:
            observation = Observation.model_validate({"query": call, **raw})
        except Exception:
            observation = None
        if observation is not None:
            if observation.source == "unknown":
                observation = observation.model_copy(update={"source": tool_name})
            return [observation]

    if raw is None:
        text = None
    elif isinstance(raw, str):
        text = raw
    else:
        text = _compact(raw) if isinstance(raw, dict) else str(raw)

    return [Observation(query=call, text=text, source=tool_name)]


class ContractualTool(BaseTool):
    """A tool whose result is an `Observation.render()` string, caveats included.

    The wrapper is what makes the caveat non-optional: there is no accessor on it that hands
    back the raw value, so a model or a renderer cannot read the number without the line that
    says what it is worth.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inner: BaseTool
    contract: SourceContract

    @property
    def synthetic(self) -> bool:
        return self.contract.synthetic

    def _observations(self, args: Any, raw: Any) -> list[Observation]:
        observations = to_observations(self.name, args, raw)
        declared = contract_for(self.name)
        if self.contract == declared:
            return observations
        extra = self.contract.caveat()
        if not extra:
            return observations
        return [o.model_copy(update={"caveats": [*o.caveats, extra]}) for o in observations]

    def _render(self, args: Any, raw: Any) -> str:
        return "\n\n".join(o.render() for o in self._observations(args, raw))

    def _run(self, *args: Any, **kwargs: Any) -> str:
        kwargs.pop("run_manager", None)
        return self._render(kwargs, self.inner.invoke(kwargs))

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        kwargs.pop("run_manager", None)
        return self._render(kwargs, await self.inner.ainvoke(kwargs))


def wrap_with_contract(inner: BaseTool, contract: SourceContract | None = None) -> ContractualTool:
    """Attach provenance to a tool. The contract defaults to the declared one, which for an
    unregistered tool is the default-deny contract, never nothing."""
    return ContractualTool(
        name=inner.name,
        description=inner.description,
        args_schema=inner.args_schema,
        inner=inner,
        contract=contract or contract_for(inner.name),
    )


def local_tools(settings: Settings, warnings: list[str] | None = None) -> list[BaseTool]:
    """Tools that must stay in this process: path containment and the impact source (§7.1)."""
    warnings = warnings if warnings is not None else []
    tools: list[BaseTool] = list(code_search_tools(load_registry(settings.repo_root)))

    for module_name, factory_name in OPTIONAL_LOCAL_FACTORIES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            logger.debug(f"local tool module not present yet: {module_name}")
            continue
        factory = getattr(module, factory_name, None)
        if not callable(factory):
            warnings.append(f"{module_name} has no {factory_name}(); its tools were not loaded")
            continue
        try:
            tools.extend(factory())
        except Exception as exc:
            warnings.append(f"{module_name}.{factory_name}() failed: {exc}")
    return tools


async def load_tools(
    settings: Settings, *, include_mcp: bool = True
) -> tuple[list[BaseTool], list[str]]:
    """Every tool the planner may use, each wrapped with its source contract.

    Returns the tools and the warnings a reply must disclose. An unreachable MCP server is a
    warning and a shorter toolset, never an exception: a failed metric backend must not stop
    the run that would have said so.
    """
    warnings: list[str] = []
    raw_tools = local_tools(settings, warnings)

    if include_mcp:
        holder = McpClientHolder(settings)
        warnings.extend(holder.transport_warnings())
        mcp_tools, error = await holder.load_tools_safe()
        if error:
            warnings.append(f"MCP tools unavailable ({', '.join(sorted(holder.servers))}): {error}")
        raw_tools.extend(mcp_tools)

    wrapped: list[ContractualTool] = []
    seen: set[str] = set()
    for tool in raw_tools:
        if tool.name in seen:
            warnings.append(f"duplicate tool name {tool.name!r}; the later one was dropped")
            continue
        seen.add(tool.name)
        if tool.name not in SOURCES:
            warnings.append(
                f"tool {tool.name!r} is not declared in config/sources.yaml; "
                "treated as qualitative only and never as an impact source"
            )
        wrapped.append(wrap_with_contract(tool))

    logger.info(f"loaded {len(wrapped)} tools ({len(warnings)} warnings)")
    return list(wrapped), warnings


def tool_names(tools: list[BaseTool]) -> list[str]:
    return [t.name for t in tools]


def describe_tools(tools: list[BaseTool]) -> str:
    """Name and one-line description per tool, for a planner prompt."""
    lines = []
    for t in tools:
        summary = " ".join((t.description or "").split())
        if len(summary) > 200:
            summary = summary[:197] + "..."
        lines.append(f"- {t.name}: {summary}")
    return "\n".join(lines) or "(no tools available)"


def synthetic_tool_names(tools: list[BaseTool]) -> set[str]:
    """Tools whose results are fixtures. The executor uses this to set `used_synthetic`."""
    return {t.name for t in tools if contract_for(t.name).synthetic}


def result_is_synthetic(text: str) -> bool:
    return SYNTHETIC_MARKER in text
