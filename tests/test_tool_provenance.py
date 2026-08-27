"""Spec §9 constraints 2 and 10: provenance survives tool loading.

These tests use a fake `BaseTool`. A live MCP server would prove less, not more: the point
is that a tool nobody vetted — which is what an MCP server hands you — cannot become an
impact source, and that its caveat is impossible to strip from the string the model reads.
"""

from typing import Any

import pytest
from langchain_core.tools import BaseTool

from app.config import Settings
from app.domain.sources import contract_for
from app.tools.registry import (
    ContractualTool,
    describe_tools,
    load_tools,
    result_is_synthetic,
    synthetic_tool_names,
    tool_names,
    wrap_with_contract,
)


class FakeTool(BaseTool):
    """A tool with no provenance of its own, exactly like one loaded over MCP."""

    name: str = "fake_tool"
    description: str = "returns a number that looks authoritative"
    payload: Any = "total: 412"

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        return self.payload

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        return self.payload


async def _render(tool: ContractualTool, **kwargs: Any) -> str:
    return await tool.ainvoke(kwargs)


async def test_unregistered_tool_defaults_to_qualitative():
    wrapped = wrap_with_contract(FakeTool(name="mystery_mcp_tool"))

    assert wrapped.contract.usable_for == ["qualitative_breakdown"]
    assert wrapped.contract.not_usable_for == ["impact_quantification"]
    assert wrapped.contract.is_impact_source is False

    rendered = await _render(wrapped)
    assert "not usable for impact_quantification" in rendered
    assert "not declared in config/sources.yaml" in rendered


async def test_load_warns_and_wraps_an_undeclared_tool(monkeypatch):
    monkeypatch.setattr(
        "app.tools.registry.code_search_tools", lambda registry: [FakeTool(name="mystery_mcp_tool")]
    )
    tools, warnings = await load_tools(Settings(), include_mcp=False)

    assert tool_names(tools) == ["mystery_mcp_tool"]
    assert isinstance(tools[0], ContractualTool)
    assert tools[0].contract.is_impact_source is False
    assert any("not declared in config/sources.yaml" in w for w in warnings)


async def test_loaded_local_tools_are_all_wrapped():
    tools, _ = await load_tools(Settings(), include_mcp=False)

    assert tools and all(isinstance(t, ContractualTool) for t in tools)
    assert "search_code" in tool_names(tools)


async def test_synthetic_tool_render_says_synthetic():
    wrapped = wrap_with_contract(FakeTool(name="query_cpu_metrics", payload="cpu: 88%"))

    assert wrapped.contract.synthetic is True
    rendered = await _render(wrapped, host="web-1")

    assert "SYNTHETIC" in rendered
    assert result_is_synthetic(rendered)
    assert "cpu: 88%" in rendered
    assert synthetic_tool_names([wrapped]) == {"query_cpu_metrics"}


async def test_server_logs_tool_render_carries_sampling_caveat():
    wrapped = wrap_with_contract(FakeTool(name="search_server_logs", payload="hits: 1234"))

    rendered = await _render(wrapped, query="status:500")

    assert "sampled ~1/8" in rendered
    assert "retention ~7d" in rendered
    assert "not usable for impact_quantification" in rendered
    assert "hits: 1234" in rendered


async def test_caveat_is_in_the_same_string_as_the_value():
    wrapped = wrap_with_contract(FakeTool(name="search_server_logs", payload="hits: 1234"))
    rendered = await _render(wrapped, query="status:500")

    assert "sampled ~1/8" in rendered and "hits: 1234" in rendered
    assert rendered.index("sampled ~1/8") < rendered.index("hits: 1234")


async def test_explicit_contract_caveat_is_never_dropped():
    override = contract_for("search_server_logs").model_copy(update={"tool": "odd_tool"})
    wrapped = wrap_with_contract(FakeTool(name="odd_tool"), override)

    rendered = await _render(wrapped)
    assert "sampled ~1/8" in rendered


async def test_empty_result_is_not_reported_as_healthy():
    wrapped = wrap_with_contract(FakeTool(name="query_metric", payload=None))
    rendered = await _render(wrapped, expr="up")

    assert "no data returned" in rendered
    assert "not the same as 'healthy'" in rendered


def test_describe_tools_is_one_line_per_tool():
    tools = [
        wrap_with_contract(FakeTool(name="a", description="first\ntool")),
        wrap_with_contract(FakeTool(name="b", description="second tool")),
    ]
    lines = describe_tools(tools).splitlines()

    assert lines == ["- a: first tool", "- b: second tool"]


@pytest.mark.parametrize("name", ["query_cpu_metrics", "search_log", "query_memory_metrics"])
def test_every_ported_fixture_tool_is_marked_synthetic(name):
    assert contract_for(name).synthetic is True
    assert contract_for(name).is_impact_source is False
