"""The adapters, proved with nothing running.

Both surfaces are asserted against the same property: the service must come up, and the
deterministic floor must produce evidence, with no Postgres, no Milvus, no MCP server and no
API key anywhere. That is not a convenience for testing — it is the design. The floor is the
product (spec §9 constraints 3–8), and a triage path that only works once four dependencies
are configured is a path that is unavailable during the outage that takes one of them down.

The other half is what must *not* happen: with no model configured, `/triage` emits an error
and stops. It does not stream a pack whose analysis section is quietly missing, because such
a pack renders identically to a complete one (constraint 14).
"""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import Settings, get_settings
from app.main import create_app
from app.storage.records import deployment_label, tool_names

ALERT = "[FIRING] 5xx spike host=www.example.com path=/api/v1/feed"

# Port 1 is never listening, so every probe fails fast and deterministically instead of
# depending on whether this machine happens to run Prometheus.
CLOSED_PORT = "http://127.0.0.1:1"


def settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "database_url": "",
        "grafana_url": "",
        "dashscope_api_key": "",
        "openai_api_key": "",
        "mcp_enabled": "grafana",
        "prometheus_base_url": CLOSED_PORT,
        "repo_root": "/nonexistent-repo-root",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(settings()))


@pytest.fixture
def cli(monkeypatch) -> CliRunner:
    for name in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "DATABASE_URL", "GRAFANA_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROMETHEUS_BASE_URL", CLOSED_PORT)
    get_settings.cache_clear()
    yield CliRunner()
    get_settings.cache_clear()


def sse_events(body: str) -> list[dict]:
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]


class TestServiceComesUp:
    """The app assembles and answers with no dependency running."""

    def test_app_builds_without_touching_anything(self):
        assert create_app(settings()).routes

    def test_startup_and_health_need_no_dependencies(self):
        with TestClient(create_app(settings())) as client:
            response = client.get("/health")

        assert response.status_code == 200, "health must answer even when everything is down"
        body = response.json()
        assert body["status"] in ("ok", "degraded")
        assert body["service"] and body["version"]

    def test_health_reports_each_dependency_separately(self, client):
        body = client.get("/health").json()
        names = {d["name"] for d in body["dependencies"]}

        assert "postgres" in names and "milvus" in names and "grafana" in names
        assert "mcp:grafana" in names, "an enabled MCP server gets its own line"
        assert any(n.startswith("llm:") for n in names)

    def test_unconfigured_is_not_reported_as_healthy(self, client):
        by_name = {d["name"]: d for d in client.get("/health").json()["dependencies"]}

        assert by_name["postgres"]["configured"] is False
        assert by_name["postgres"]["reachable"] is None, "nothing was dialled, so nothing is known"
        assert by_name["postgres"]["detail"], "an unset dependency says what it costs"

    def test_the_model_is_never_dialled_by_a_health_check(self, client):
        dependencies = client.get("/health").json()["dependencies"]
        llm = next(d for d in dependencies if d["name"].startswith("llm:"))
        assert llm["reachable"] is None

    def test_root_names_the_triage_route(self, client):
        assert client.get("/").json()["triage"] == "POST /triage"


class TestTriageWithoutAModel:
    """Constraint 14: an unavailable model is an error, never a thinner answer."""

    def test_error_event_instead_of_a_partial_pack(self, client):
        response = client.post("/triage", json={"input": ALERT, "alert_text": ALERT})
        events = sse_events(response.text)

        assert events, "the stream must say something"
        assert events[0]["type"] == "error"
        assert "API_KEY" in events[0]["message"] or "api" in events[0]["message"].lower()
        assert not any(e["type"] == "complete" for e in events)
        assert not any(e.get("response") for e in events)


class FakeStructured:
    def __init__(self, result):
        self._result = result

    async def ainvoke(self, messages, **kwargs):
        return self._result


class FakeLLM:
    """Enough of a chat model to drive one turn: structured output and tool binding."""

    def with_structured_output(self, schema, **kwargs):
        from app.graph.nodes.planner import Plan
        from app.graph.nodes.replanner import Act
        from app.graph.nodes.respond import DiagnosisDraft

        if schema is Plan:
            return FakeStructured(Plan(steps=["check recent deploys"]))
        if schema is Act:
            return FakeStructured(Act(action="respond"))
        return FakeStructured(
            DiagnosisDraft(
                summary="no deploy in the window",
                likely_cause="not established",
                confidence="low",
                victim_or_cause="victim",
                evidence_cited=["the alert's own query returned no data"],
            )
        )

    def bind_tools(self, tools, **kwargs):
        from langchain_core.messages import AIMessage

        return FakeStructured(AIMessage(content="no deploys in the window"))


class TestTriageStream:
    """The SSE envelope carries `run_turn`'s events, unchanged and in order."""

    def test_stream_ends_with_the_rendered_pack(self, monkeypatch):
        monkeypatch.setattr("app.api.triage.get_llm", lambda *a, **k: FakeLLM())
        monkeypatch.setattr("app.core.llm_factory.get_llm", lambda *a, **k: FakeLLM())

        client = TestClient(create_app(settings(openai_api_key="test", mcp_enabled="")))
        events = sse_events(client.post("/triage", json={"input": ALERT}).text)

        stages = [e["stage"] for e in events]
        assert stages[0] == "start" and stages[-1] == "complete"
        assert stages.index("baseline") < stages.index("planner")
        assert all({"type", "stage", "message"} <= set(e) for e in events)
        assert events[-1]["response"], "the last event carries the reply"
        assert events[-1]["thread_id"].startswith("api:")

    def test_the_reply_is_the_one_renderer_s_output(self, monkeypatch):
        monkeypatch.setattr("app.api.triage.get_llm", lambda *a, **k: FakeLLM())
        monkeypatch.setattr("app.core.llm_factory.get_llm", lambda *a, **k: FakeLLM())

        client = TestClient(create_app(settings(openai_api_key="test", mcp_enabled="")))
        events = sse_events(client.post("/triage", json={"input": ALERT}).text)

        response = events[-1]["response"]
        assert "queries issued" in response, "the accounting block is part of every reply"
        assert "Victim or cause:" in response


class TestAdminWithoutAStore:
    """Evaluation degrades loudly. Triage does not degrade at all."""

    def test_runs_says_there_is_no_store(self, client):
        response = client.get("/runs")
        assert response.status_code == 503
        assert "DATABASE_URL" in response.json()["detail"]

    def test_stats_says_there_is_no_store(self, client):
        assert client.get("/stats").status_code == 503


class TestEvidenceCommand:
    """The path that works with nothing configured."""

    def test_produces_observations_with_no_api_key(self, cli):
        result = cli.invoke(cli_app, ["evidence", ALERT])

        assert result.exit_code == 0, result.output
        assert "query:" in result.output, "the floor must show what it asked"

        issued = int(result.output.split(" queries issued")[0].split("\n")[-1])
        assert issued >= 1, "an alert with a host label is measurable without a model"

    def test_a_failed_query_is_reported_rather_than_swallowed(self, cli):
        result = cli.invoke(cli_app, ["evidence", ALERT])

        assert "query failed" in result.output, "a dead backend is an observation, not silence"
        assert " failed" in result.output.split("queries issued")[1]

    def test_names_the_alert_and_the_rule_provenance(self, cli):
        result = cli.invoke(cli_app, ["evidence", ALERT])

        assert "alert:" in result.output and "deployment:" in result.output
        assert "[synthesized]" in result.output or "[registry]" in result.output


class TestTriageCommand:
    def test_refuses_when_there_is_no_model_and_points_at_the_evidence_path(self, cli):
        result = cli.invoke(cli_app, ["triage", ALERT])

        assert result.exit_code == 1
        assert "oncall evidence" in result.output

    def test_prints_the_renderer_s_pack(self, cli, monkeypatch):
        monkeypatch.setattr("app.cli.get_llm", lambda *a, **k: FakeLLM())
        monkeypatch.setattr("app.core.llm_factory.get_llm", lambda *a, **k: FakeLLM())
        monkeypatch.setenv("MCP_ENABLED", "")
        get_settings.cache_clear()

        result = cli.invoke(cli_app, ["triage", ALERT, "--no-deep"])

        assert result.exit_code == 0, result.output
        assert "queries issued" in result.output
        assert "Victim or cause:" in result.output


class TestRecordShapes:
    """Two details of the store that a live Postgres would otherwise be needed to see."""

    def test_a_guess_is_labelled_as_one(self):
        from app.graph.state import Resolution

        guessed = Resolution(app_label="server-default", confidence="low", matched_by="host")
        known = Resolution(app_label="server-default", confidence="exact", matched_by="app")

        assert deployment_label({"resolution": guessed}) == "server-default?"
        assert deployment_label({"resolution": known}) == "server-default"
        assert deployment_label({}) is None

    def test_tool_names_drop_the_arguments(self):
        signature = 'list_dir({"repo": "server"}) | search_code({"pattern": "x"})'
        assert tool_names(signature) == ["list_dir", "search_code"]
        assert tool_names(None) == []


def test_module_imports():
    import app.api.admin  # noqa: F401
    import app.api.health  # noqa: F401
    import app.api.triage  # noqa: F401
    import app.cli  # noqa: F401
    import app.main  # noqa: F401
    import app.storage.records  # noqa: F401
