"""The renderer is the last thing between a caveat and a reader who cannot re-derive it.

These tests exist because every failure they cover renders identically to a clean run:
fixture numbers look like measurements, a guessed workload's figures look confirmed, an
empty query looks healthy, and a probe that never ran looks like a probe that found nothing.
"""

from app.evidence.envelope import Observation, Series, SkippedProbe
from app.graph.state import AlertIdentity, Diagnosis, ExecutedStep, OncallState, Resolution
from app.render.answer import accounting, banners, render_answer

MARKUPS = ["markdown", "slack"]


def _state(**overrides) -> OncallState:
    base: OncallState = {
        "identity": AlertIdentity(alert_name="large-scale-5xx"),
        "baseline": [],
        "past_steps": [],
        "skipped": [],
        "used_synthetic": False,
    }
    base.update(overrides)
    return base


def _texts(state: OncallState) -> set[str]:
    return {b.text for b in banners(state)}


class TestBannersFireFromStateAlone:
    def test_synthetic_data_is_disclosed(self):
        texts = " ".join(_texts(_state(used_synthetic=True)))
        assert "fixture data" in texts
        assert "not reflect" in texts or "Nothing below reflects the live system" in texts

    def test_live_data_carries_no_synthetic_banner(self):
        assert not any(b.level == "synthetic" for b in banners(_state(used_synthetic=False)))

    def test_low_confidence_attribution_names_the_assumed_workload(self):
        state = _state(
            resolution=Resolution(
                app_label="server-feed",
                confidence="low",
                matched_by="host",
                note="host matched but path did not",
            )
        )
        text = next(iter(_texts(state)))
        assert "Service attribution unconfirmed" in text
        assert "host matched but path did not" in text
        assert "server-feed" in text

    def test_exact_attribution_is_not_flagged(self):
        state = _state(
            resolution=Resolution(
                app_label="server-feed", confidence="exact", matched_by="host+path"
            )
        )
        assert _texts(state) == set()

    def test_unresolved_deployment_says_nothing_is_scoped(self):
        state = _state(
            resolution=Resolution(
                confidence="unresolved", note="no deployment matches foo.example.com"
            )
        )
        text = next(iter(_texts(state)))
        assert "No deployment resolved" in text
        assert "Nothing below is scoped to a workload" in text

    def test_unresolved_without_a_note_stays_quiet(self):
        assert _texts(_state(resolution=Resolution(confidence="unresolved"))) == set()

    def test_degraded_model_is_named(self):
        text = next(iter(_texts(_state(degraded_model="qwen-turbo"))))
        assert "qwen-turbo" in text
        assert "overloaded" in text

    def test_normal_model_is_not_flagged(self):
        assert _texts(_state(degraded_model=None)) == set()

    def test_unidentified_alert_says_so(self):
        text = next(iter(_texts(_state(identity=AlertIdentity(alert_name="unknown")))))
        assert "Could not identify this alert" in text

    def test_identified_alert_has_no_unknown_banner(self):
        assert _texts(_state()) == set()

    def test_banners_stack(self):
        state = _state(
            identity=AlertIdentity(alert_name="unknown"),
            used_synthetic=True,
            degraded_model="qwen-turbo",
            resolution=Resolution(confidence="unresolved", note="nothing to go on"),
        )
        assert len(banners(state)) == 4


class TestAccountingIsFourNumbers:
    """Empty and failed are different stories: one about the system, one about the tooling."""

    def _mixed(self) -> OncallState:
        return _state(
            baseline=[
                Observation(query="a", series=[Series(labels={}, points=[(0.0, 1.0)])]),
                Observation(query="b", series=[Series(labels={}, points=[(0.0, 2.0)])]),
                Observation(query="c", series=[]),
                Observation(query="d", error="timeout"),
            ]
        )

    def test_all_four_numbers_are_reported(self):
        line = accounting(self._mixed())
        assert "4 queries issued" in line
        assert "2 series returned" in line
        assert "1 empty" in line
        assert "1 failed" in line

    def test_empty_is_not_counted_as_failed(self):
        state = _state(baseline=[Observation(query="a", series=[])])
        line = accounting(state)
        assert "1 empty" in line
        assert "0 failed" in line

    def test_failed_is_not_counted_as_empty(self):
        state = _state(baseline=[Observation(query="a", error="502 from grafana")])
        line = accounting(state)
        assert "1 failed" in line
        assert "0 empty" in line

    def test_a_failed_query_contributes_no_series(self):
        state = _state(baseline=[Observation(query="a", error="boom")])
        assert "0 series returned" in accounting(state)

    def test_text_only_observation_is_not_empty(self):
        state = _state(baseline=[Observation(query="logs", text="two matching lines")])
        assert "0 empty" in accounting(state)

    def test_skipped_probes_are_never_silent(self):
        state = _state(
            skipped=[
                SkippedProbe(probe="impact_quantification", reason="no host label on the alert")
            ]
        )
        out = accounting(state)
        assert "not measured: impact_quantification — no host label on the alert" in out

    def test_skipped_probes_reach_the_reply(self):
        state = _state(skipped=[SkippedProbe(probe="pod_restarts", reason="no app label")])
        for markup in MARKUPS:
            out = render_answer(state, markup=markup)
            assert "not measured: pod_restarts — no app label" in out


class TestMarkupChangesGlyphsNotDisclosure:
    def _loaded(self) -> OncallState:
        return _state(
            identity=AlertIdentity(alert_name="unknown"),
            used_synthetic=True,
            degraded_model="qwen-turbo",
            resolution=Resolution(app_label="server-feed", confidence="low", note="host only"),
            baseline=[Observation(query="a", series=[]), Observation(query="b", error="timeout")],
            skipped=[SkippedProbe(probe="impact", reason="no host label")],
        )

    def test_every_banner_text_survives_both_markups(self):
        state = self._loaded()
        rendered = {m: render_answer(state, markup=m) for m in MARKUPS}
        for banner in banners(state):
            for markup, text in rendered.items():
                assert banner.text in text, f"{markup} dropped: {banner.text}"

    def test_the_two_markups_agree_on_the_banner_set(self):
        state = self._loaded()
        found = {
            markup: {
                b.text for b in banners(state) if b.text in render_answer(state, markup=markup)
            }
            for markup in MARKUPS
        }
        assert found["markdown"] == found["slack"] == {b.text for b in banners(state)}

    def test_the_two_markups_really_do_differ(self):
        state = self._loaded()
        assert render_answer(state, markup="markdown") != render_answer(state, markup="slack")
        assert "•" in render_answer(_with_diagnosis(state), markup="slack")

    def test_an_unknown_markup_still_discloses(self):
        state = self._loaded()
        out = render_answer(state, markup="hieroglyphs")
        for banner in banners(state):
            assert banner.text in out


def _with_diagnosis(state: OncallState) -> OncallState:
    return state | {
        "diagnosis": Diagnosis(
            summary="5xx on the web ingress",
            likely_cause="a bad commit plus HPA thrash",
            confidence="medium",
            victim_or_cause="cause",
            evidence_cited=["ingress request rate", "pod start times"],
            suggested_next_steps=["roll back the commit"],
            related_incidents=["2026-06-10 a4api-web"],
        )
    }


class TestAnswerComposition:
    def test_diagnosis_fields_all_render(self):
        out = render_answer(_with_diagnosis(_state()))
        for fragment in [
            "5xx on the web ingress",
            "a bad commit plus HPA thrash",
            "medium confidence",
            "Victim or cause:",
            "ingress request rate",
            "roll back the commit",
            "2026-06-10 a4api-web",
        ]:
            assert fragment in out

    def test_a_missing_diagnosis_invents_no_cause(self):
        state = _state(baseline=[Observation(query="a", series=[])])
        out = render_answer(state)
        assert "No cause determined" in out
        assert "1 queries issued" in out

    def test_investigation_trail_shows_failures(self):
        state = _state(
            past_steps=[
                ExecutedStep(step="check pod restarts", result="none in 6h"),
                ExecutedStep(step="query loki", result="shard unavailable", ok=False),
            ]
        )
        out = render_answer(state)
        assert "check pod restarts — none in 6h" in out
        assert "query loki — failed: shard unavailable" in out

    def test_stop_reason_is_visible(self):
        out = render_answer(_state(stopped_because="wall-clock budget exhausted"))
        assert "wall-clock budget exhausted" in out

    def test_accounting_closes_the_answer(self):
        out = render_answer(_with_diagnosis(_state()))
        assert "queries issued" in out.splitlines()[-1]
