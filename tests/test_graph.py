"""The graph's invariants, proved with a fake model and no network.

Everything here is a §9 constraint whose mechanism is code rather than prompt text:
baseline's position in the wiring (8), the budget (11), partial findings after a model
failure (12). A prompt-only version of any of these would pass a reading and fail in
production without a trace, so each is asserted against the thing that actually enforces it
— the compiled edge list, the guard function, the node's return value.
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.config import Settings
from app.evidence.envelope import ExecutedStep, Observation
from app.graph import guards
from app.graph.build import BASELINE, PLANNER, build_graph
from app.graph.guards import Budget, clamp_replan, seen_call_signature, should_stop
from app.graph.nodes.executor import executor_node
from app.graph.nodes.planner import Plan
from app.graph.nodes.replanner import Act, replanner_node
from app.graph.nodes.respond import respond_node


def settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "mcp_enabled": "",
        "repo_root": "/nonexistent-repo-root",
        "llm_provider": "openai",
        "openai_api_key": "test",
        "max_steps": 3,
        "replan_ban_after": 2,
        "wall_clock_seconds": 120.0,
    }
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------- fakes


class FakeStructured:
    def __init__(self, results):
        self._results = list(results)

    async def ainvoke(self, messages, **kwargs):
        if not self._results:
            raise AssertionError("fake model ran out of structured results")
        result = self._results[0] if len(self._results) == 1 else self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeBound:
    def __init__(self, replies):
        self._replies = list(replies)

    async def ainvoke(self, messages, **kwargs):
        reply = self._replies.pop(0) if self._replies else AIMessage(content="done")
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeLLM:
    """Enough of a chat model for the nodes: structured output and tool binding."""

    def __init__(self, *, structured=(), replies=()):
        self.structured = list(structured)
        self.replies = list(replies)

    def with_structured_output(self, schema, **kwargs):
        wanted = [s for s in self.structured if isinstance(s, (schema, Exception))]
        return FakeStructured(wanted or self.structured)

    def bind_tools(self, tools, **kwargs):
        return FakeBound(self.replies)

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content="summarised")


class RecordingTool(BaseTool):
    name: str = "list_dir"
    description: str = "list a directory"
    calls: list = []

    class Args(BaseModel):
        repo: str

    args_schema: type = Args

    def _run(self, **kwargs):
        self.calls.append(kwargs)
        return "one-file.go"

    async def _arun(self, **kwargs):
        self.calls.append(kwargs)
        return "one-file.go"


def loader_for(tools):
    async def load():
        return list(tools), []

    return load


def patch_llm(monkeypatch, llm):
    monkeypatch.setattr("app.core.llm_factory.get_llm", lambda *a, **k: llm)


# --------------------------------------------------------------------------- tests


class TestWiring:
    """Constraint 8: there is no route to the planner that skips the evidence floor."""

    def test_graph_compiles(self):
        graph = build_graph(settings())
        assert graph is not None

    def test_baseline_cannot_be_routed_around(self):
        edges = build_graph(settings()).get_graph().edges
        into_planner = {e.source for e in edges if e.target == PLANNER}
        assert into_planner == {BASELINE}, f"planner reachable from {into_planner}"

        entrypoints = {e.target for e in edges if e.source == "__start__"}
        assert entrypoints == {BASELINE}


class TestBudget:
    """Constraint 11: the loop terminates, and not because a prompt asked it to."""

    def test_step_count_stops_the_loop(self):
        budget = Budget.from_settings(settings(max_steps=3))
        state = {"past_steps": [ExecutedStep(step=f"s{i}", result="x") for i in range(3)]}
        assert "step budget" in (should_stop(state, budget) or "")

    def test_under_the_step_budget_keeps_going(self):
        budget = Budget.from_settings(settings(max_steps=3))
        state = {"past_steps": [ExecutedStep(step="s", result="x")]}
        assert should_stop(state, budget) is None

    def test_wall_clock_stops_the_loop(self):
        budget = Budget.from_settings(settings(), started_at=0.0)
        assert "wall-clock" in (should_stop({}, budget, now=10_000.0) or "")

    def test_start_is_wall_clock_not_monotonic(self):
        """A monotonic stamp is meaningless once a checkpoint crosses a process."""
        import time

        budget = Budget.from_settings(settings())
        assert abs(budget.started_at - time.time()) < 5
        assert budget.deadline == budget.started_at + budget.wall_clock_seconds

    @pytest.mark.asyncio
    async def test_replanner_stops_before_calling_the_model(self):
        llm = FakeLLM(structured=[AssertionError("the model must not be consulted")])
        state = {
            "plan": ["another step", "and another"],
            "past_steps": [ExecutedStep(step=f"s{i}", result="x") for i in range(3)],
        }
        node = replanner_node(settings(max_steps=3), loader_for([]))
        with pytest.MonkeyPatch.context() as mp:
            patch_llm(mp, llm)
            update = await node(state, {"configurable": {"started_at": 0.0}})

        assert update["plan"] == []
        assert "step budget" in update["stopped_because"]


class TestReplanCannotGrow:
    """Constraint 11, second half: 'add three more probes' is unrepresentable."""

    def test_clamp_truncates_to_the_remaining_length(self):
        assert clamp_replan(["a", "b", "c", "d"], ["x", "y"]) == ["a", "b"]

    def test_clamp_allows_a_shorter_plan(self):
        assert clamp_replan(["a"], ["x", "y", "z"]) == ["a"]

    def test_clamp_drops_blanks(self):
        assert clamp_replan(["  ", "a", ""], ["x", "y"]) == ["a"]

    @pytest.mark.asyncio
    async def test_replanner_cannot_extend_the_plan(self):
        llm = FakeLLM(structured=[Act(action="replan", new_steps=["1", "2", "3", "4", "5"])])
        node = replanner_node(settings(replan_ban_after=99), loader_for([]))
        with pytest.MonkeyPatch.context() as mp:
            patch_llm(mp, llm)
            update = await node({"plan": ["only", "two"]}, {"configurable": {"started_at": None}})

        assert update["plan"] == ["1", "2"]

    @pytest.mark.asyncio
    async def test_unknown_action_continues(self):
        llm = FakeLLM(structured=[Act(action="ponder", new_steps=["x"])])
        node = replanner_node(settings(), loader_for([]))
        with pytest.MonkeyPatch.context() as mp:
            patch_llm(mp, llm)
            update = await node({"plan": ["keep me"]}, None)

        assert update == {}, "an unrecognised action must not change the plan"


class TestDuplicateCalls:
    """A repeated call cannot produce a new observation, so it does not get a round."""

    def test_signature_matching_reads_recorded_steps(self):
        state = {
            "past_steps": [
                ExecutedStep(
                    step="look",
                    result="ok",
                    tool=guards.call_signature("list_dir", {"repo": "server"}),
                )
            ]
        }
        assert seen_call_signature(state, "list_dir", {"repo": "server"})
        assert not seen_call_signature(state, "list_dir", {"repo": "other"})

    @pytest.mark.asyncio
    async def test_executor_rejects_a_repeat_of_an_earlier_step(self):
        tool = RecordingTool()
        tool.calls = []
        call = {"name": "list_dir", "args": {"repo": "server"}, "id": "c1"}
        llm = FakeLLM(
            replies=[AIMessage(content="", tool_calls=[call]), AIMessage(content="reported")]
        )

        state = {
            "plan": ["list the repo again"],
            "past_steps": [
                ExecutedStep(
                    step="list the repo",
                    result="one-file.go",
                    tool=guards.call_signature("list_dir", {"repo": "server"}),
                )
            ],
        }
        node = executor_node(settings(), loader_for([tool]))
        with pytest.MonkeyPatch.context() as mp:
            patch_llm(mp, llm)
            update = await node(state)

        assert tool.calls == [], "an identical call must not reach the tool a second time"
        assert update["past_steps"][0].tool is None, "no call was made, so none is recorded"

    @pytest.mark.asyncio
    async def test_executor_rejects_a_call_missing_required_args(self):
        tool = RecordingTool()
        tool.calls = []
        call = {"name": "list_dir", "args": {}, "id": "c1"}
        llm = FakeLLM(
            replies=[AIMessage(content="", tool_calls=[call]), AIMessage(content="reported")]
        )

        node = executor_node(settings(), loader_for([tool]))
        with pytest.MonkeyPatch.context() as mp:
            patch_llm(mp, llm)
            update = await node({"plan": ["list it"]})

        assert tool.calls == [], "a tool must not run without its required argument"
        assert update["past_steps"][0].ok

    def test_rejection_names_the_missing_field(self):
        missing = guards.missing_required_args(RecordingTool(), {})
        assert missing == ["repo"]
        assert "repo" in guards.rejection_message("list_dir", missing, {})


class TestExecutorComposition:
    """The reference implementation's three silent defects, asserted against."""

    @pytest.mark.asyncio
    async def test_earlier_results_reach_the_next_step(self):
        from app.graph.nodes.executor import step_context

        state = {
            "plan": ["step two"],
            "past_steps": [ExecutedStep(step="step one", result="the pod restarted at 10:02")],
        }
        context = step_context(state, "step two")
        assert "step one" in context and "restarted at 10:02" in context

    def test_content_blocks_are_flattened(self):
        from app.graph.nodes.executor import _text

        message = AIMessage(
            content=[{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
        )
        assert _text(message) == "first\nsecond"

    @pytest.mark.asyncio
    async def test_tool_calls_in_the_second_reply_are_not_dropped(self):
        tool = RecordingTool()
        tool.calls = []
        first = AIMessage(
            content="", tool_calls=[{"name": "list_dir", "args": {"repo": "a"}, "id": "1"}]
        )
        second = AIMessage(
            content="", tool_calls=[{"name": "list_dir", "args": {"repo": "b"}, "id": "2"}]
        )
        llm = FakeLLM(replies=[first, second, AIMessage(content="both listed")])

        node = executor_node(settings(), loader_for([tool]))
        with pytest.MonkeyPatch.context() as mp:
            patch_llm(mp, llm)
            update = await node({"plan": ["list both repos"]})

        assert [c["repo"] for c in tool.calls] == ["a", "b"]
        assert update["past_steps"][0].result == "both listed"


class TestPartialFindingsSurvive:
    """Constraint 12: a model failure loses the judgment, never the measurements."""

    @pytest.mark.asyncio
    async def test_no_cause_is_invented_when_the_model_fails(self):
        class Failing(FakeLLM):
            def with_structured_output(self, schema, **kwargs):
                return FakeStructured([RuntimeError("overloaded")])

        state = {
            "input": "why is web 5xx-ing",
            "baseline": [
                Observation(
                    query="up{app='web'}",
                    purpose="the alert's own expression",
                    source="query_metric",
                )
            ],
            "past_steps": [
                ExecutedStep(step="check recent deploys", result="no deploy in the window")
            ],
            "stopped_because": "step budget (3 steps) reached",
        }
        node = respond_node(settings())
        with pytest.MonkeyPatch.context() as mp:
            patch_llm(mp, Failing())
            update = await node(state)

        assert update["diagnosis"] is None
        response = update["response"]
        assert "No cause determined" in response
        assert "check recent deploys" in response, "what was checked must survive"
        assert "no deploy in the window" in response
        assert "stopped early" in response
        assert any(p.probe == "diagnosis" for p in update["skipped"])

    @pytest.mark.asyncio
    async def test_a_failed_planner_still_leaves_the_floor_intact(self):
        from app.graph.nodes.planner import planner_node

        class Failing(FakeLLM):
            def with_structured_output(self, schema, **kwargs):
                return FakeStructured([RuntimeError("overloaded")])

        node = planner_node(settings(), loader_for([]))
        with pytest.MonkeyPatch.context() as mp:
            patch_llm(mp, Failing())
            update = await node({"input": "what happened", "baseline": []})

        assert update["plan"] == []
        assert "unavailable" in update["stopped_because"]


class TestEndToEnd:
    """The loop runs, terminates on the step budget, and answers from what it has."""

    @pytest.mark.asyncio
    async def test_budget_stops_a_model_that_never_converges(self):
        class Endless(FakeLLM):
            def with_structured_output(self, schema, **kwargs):
                if schema is Plan:
                    return FakeStructured([Plan(steps=["a", "b", "c"])])
                if schema is Act:
                    return FakeStructured([Act(action="continue")])
                return FakeStructured([RuntimeError("no diagnosis in this test")])

            def bind_tools(self, tools, **kwargs):
                return FakeBound([AIMessage(content="looked")] * 40)

        cfg = settings(max_steps=3)
        with pytest.MonkeyPatch.context() as mp:
            patch_llm(mp, Endless())
            graph = build_graph(cfg)
            final = await graph.ainvoke(
                {"input": "5xx on www.example.com", "alert_text": "5xx spike host=www.example.com"},
                config={
                    "configurable": {"thread_id": "t1", "started_at": None},
                    "recursion_limit": 40,
                },
            )

        assert len(final["past_steps"]) == cfg.max_steps, "the loop must run, then stop"
        assert "step budget" in final["stopped_because"]
        assert "No cause determined" in final["response"]
        assert "stopped early" in final["response"]
        assert final["skipped"], "an alert with no backend must say what it did not measure"

    @pytest.mark.asyncio
    async def test_run_turn_streams_one_event_shape(self):
        """Slack and SSE read this stream. Two streams drift; one does not."""
        from app.graph.build import run_turn
        from app.graph.nodes.respond import DiagnosisDraft

        class Model(FakeLLM):
            def with_structured_output(self, schema, **kwargs):
                if schema is Plan:
                    return FakeStructured([Plan(steps=["check recent deploys"])])
                if schema is Act:
                    return FakeStructured([Act(action="respond")])
                return FakeStructured(
                    [
                        DiagnosisDraft(
                            summary="no deploy in the window",
                            likely_cause="not established",
                            confidence="low",
                            victim_or_cause="victim",
                            evidence_cited=["the alert's own query returned no data"],
                        )
                    ]
                )

            def bind_tools(self, tools, **kwargs):
                return FakeBound([AIMessage(content="no deploys in the window")])

        with pytest.MonkeyPatch.context() as mp:
            patch_llm(mp, Model())
            events = [
                event
                async for event in run_turn(
                    {"input": "why 5xx", "alert_text": "5xx spike host=www.example.com"},
                    settings=settings(),
                    thread_id="slack:T1:C1:1700000000.1",
                )
            ]

        assert all({"type", "stage", "message"} <= set(e) for e in events)
        stages = [e["stage"] for e in events]
        assert stages[0] == "start" and stages[-1] == "complete"
        assert stages.index("baseline") < stages.index("planner")
        assert events[-1]["response"], "the last event carries the answer"


def test_module_imports():
    import app.core.llm_factory  # noqa: F401
    import app.graph.build  # noqa: F401
    import app.graph.guards  # noqa: F401
    import app.graph.nodes.baseline  # noqa: F401
    import app.graph.nodes.executor  # noqa: F401
    import app.graph.nodes.planner  # noqa: F401
    import app.graph.nodes.replanner  # noqa: F401
    import app.graph.nodes.respond  # noqa: F401

    assert asyncio is not None
