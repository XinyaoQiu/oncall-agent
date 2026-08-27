"""The graph's state contract.

Two channels are append-only (`operator.add`): `baseline` and `past_steps`. No later node
can shrink the evidence floor — the reducer has no operation that removes.

`priors` and `input` carry what a human said. They are read by the planner and the
responder. They are deliberately *not* arguments to anything in `app/evidence/`: tech-design
§3.3 says prior information reorders work and never removes it, and §3.4 says the engineer's
question shapes emphasis and never collection. Both are enforced by
`collect_baseline`'s signature rather than by a prompt (see app/evidence/baseline.py).
"""

import operator
from typing import Annotated, TypedDict

from app.domain.models import (
    AlertIdentity,
    AlertRule,
    Diagnosis,
    Resolution,
    Turn,
)
from app.evidence.envelope import ExecutedStep, Observation, SkippedProbe

__all__ = [
    "AlertIdentity",
    "AlertRule",
    "Diagnosis",
    "OncallState",
    "Resolution",
    "Turn",
]


class OncallState(TypedDict, total=False):
    # --- turn input -------------------------------------------------------
    input: str
    turn: Turn
    conversation_id: str
    alert_text: str

    # --- the deterministic floor. written only by the baseline node. ------
    identity: AlertIdentity | None
    rule: AlertRule | None
    resolution: Resolution | None
    baseline: Annotated[list[Observation], operator.add]
    benign: list[str]

    # --- thread context. read by planner/responder, never by evidence. ----
    priors: list[str]
    thread_digest: str

    # --- plan / execute / replan ------------------------------------------
    plan: list[str]
    past_steps: Annotated[list[ExecutedStep], operator.add]

    # --- disclosure --------------------------------------------------------
    used_synthetic: bool
    degraded_model: str | None
    skipped: Annotated[list[SkippedProbe], operator.add]
    stopped_because: str

    # --- output ------------------------------------------------------------
    diagnosis: Diagnosis | None
    response: str
