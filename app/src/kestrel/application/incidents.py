"""Incident use cases: what the control plane is allowed to ask for.

Three operations, and the rules they carry live here, not in the HTTP layer:

  * submitting is idempotent — one incident, one run, a duplicate is refused;
  * reading a run derives what the caller cares about (`awaiting_approval`)
    from the run itself;
  * a decision is only accepted while the run is actually waiting for one.

They are the input ports of the application. The FastAPI routes and the operator
CLI both call them, so a third surface (a Slack bot, a ticketing webhook) reuses
the rules instead of re-implementing them against the engine.
"""

from __future__ import annotations

from kestrel.application.errors import NoPendingApproval
from kestrel.application.ports import IncidentRunView, RunGateway, RunHandle
from kestrel.domain.models import Approval, Incident
from kestrel.domain.policy import RunLimits


async def submit_incident(runs: RunGateway, incident: Incident, limits: RunLimits) -> RunHandle:
    """Accept an incident for triage.

    The identity of the run is derived from the incident (`workflow_id`), so
    submitting the same incident twice is refused by the gateway: a retrying
    webhook cannot start a second remediation.
    """
    return await runs.start(incident, limits)


async def inspect_incident(runs: RunGateway, incident_id: str) -> IncidentRunView:
    """Read one run: status, identity and whether it waits on a human."""
    return await runs.view(incident_id)


async def decide_incident(runs: RunGateway, incident_id: str, decision: Approval) -> None:
    """Deliver a human decision for the plan currently on the table.

    Refusing when nothing is pending is the application's rule, not the
    engine's: it is what stops a stale retry from recording an approval for a
    plan the run is not asking about.
    """
    if await runs.pending_approval(incident_id) is None:
        raise NoPendingApproval(incident_id)
    await runs.submit_decision(incident_id, decision)
