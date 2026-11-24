"""Graph state.

Rules for this TypedDict:
  * every field must be JSON-serializable (it is checkpointed after each node)
  * fields that accumulate across nodes use an additive reducer so parallel
    branches and retries never lose history
  * new fields are added here, never smuggled through `notes`
  * nothing that is not data goes in here: no client, no connection, no
    closure. Capabilities travel through `CoreDeps`, never through state.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict

from kestrel.domain.models import (
    ActionRecord,
    Approval,
    Evidence,
    Hypothesis,
    Incident,
    IncidentReport,
    Phase,
    RemediationPlan,
    Verification,
)
from kestrel.domain.policy import RunLimits


class IncidentState(TypedDict, total=False):
    incident: Incident
    phase: Phase

    # Snapshotted when the run starts. Routers execute inside the Temporal
    # workflow, so they read the limits from state and never from settings:
    # an env read there would make replay diverge from history.
    max_investigation_attempts: int
    max_remediation_attempts: int

    evidence: Annotated[list[Evidence], add]
    hypothesis: Hypothesis | None
    plan: RemediationPlan | None

    approvals: Annotated[list[Approval], add]
    actions: Annotated[list[ActionRecord], add]
    verification: Verification | None
    report: IncidentReport | None

    investigation_attempts: int
    remediation_attempts: int
    notes: Annotated[list[str], add]


def initial_state(incident: Incident, limits: RunLimits | None = None) -> IncidentState:
    run_limits = limits if limits is not None else RunLimits()
    return IncidentState(
        incident=incident,
        phase=Phase.RECEIVED,
        max_investigation_attempts=run_limits.max_investigation_attempts,
        max_remediation_attempts=run_limits.max_remediation_attempts,
        evidence=[],
        approvals=[],
        actions=[],
        hypothesis=None,
        plan=None,
        verification=None,
        report=None,
        investigation_attempts=0,
        remediation_attempts=0,
        notes=[],
    )
