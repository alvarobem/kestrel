"""Use cases: the incident lifecycle, one function per step.

Every node runs as a Temporal activity, so it must be async and it must not
mutate the state it receives: take the state, return a partial update.

What this module may not do is the point of the layering: no `get_settings()`,
no `httpx`, no `interrupt()`. Whatever a node needs from the outside arrives as
a port or a frozen policy value in `CoreDeps` (`application/deps.py`). That is
what lets a test drive the whole lifecycle with a fake approval channel and a
fake executor.

The wiring is real and runnable end to end; the LLM-driven steps are explicit
stubs. Keep the guard rails: read-only investigation, approval before any
non-dry-run action, and one audit record per executed step.
"""

from __future__ import annotations

from kestrel.application.deps import CoreDeps
from kestrel.application.errors import TelemetryUnavailable
from kestrel.application.state import IncidentState
from kestrel.domain.models import (
    ActionRecord,
    Approval,
    Evidence,
    EvidenceKind,
    Hypothesis,
    IncidentReport,
    Phase,
    RemediationPlan,
    RemediationStep,
    Verification,
)
from kestrel.domain.policy import (
    DEFAULT_MAX_INVESTIGATION_ATTEMPTS,
    DEFAULT_MAX_REMEDIATION_ATTEMPTS,
)

# --- pure helpers ----------------------------------------------------------
# These run inside the workflow when a router calls them, so they may only
# inspect state: no settings, no clock, no I/O.


def _latest_approval(state: IncidentState) -> Approval | None:
    """The decision that applies to the plan currently on the table.

    Approvals accumulate across plan attempts, so the last one is the only one
    that describes the current plan.
    """
    approvals = state.get("approvals") or []
    return approvals[-1] if approvals else None


def _approved_for_current_plan(state: IncidentState) -> bool:
    approval = _latest_approval(state)
    return approval is not None and approval.approved


def _applied_anything(state: IncidentState) -> bool:
    return any(action.executed for action in (state.get("actions") or []))


async def receive(state: IncidentState, deps: CoreDeps) -> dict:
    """Normalize the incoming incident and open the audit trail.

    It also materializes the additive channels: with the `add` reducer, a
    channel that nobody writes stays absent, and the final state would expose
    `null` where the contract says `list`.
    """
    incident = state["incident"]
    return {
        "phase": Phase.RECEIVED,
        "evidence": [],
        "approvals": [],
        "actions": [],
        "notes": [f"incident {incident.id} received from {incident.source}"],
    }


async def triage(state: IncidentState, deps: CoreDeps) -> dict:
    """Classify severity, scope and ownership.

    TODO(node): call the LLM with the incident payload plus the ownership map
    (management zones / tags) and produce severity + affected services.
    """
    incident = state["incident"]
    confidence = 0.6 if incident.severity.value != "unknown" else 0.3
    return {
        "phase": Phase.TRIAGING,
        "evidence": [
            Evidence(
                # The system's own classification, not a telemetry fact.
                provider="kestrel",
                kind=EvidenceKind.NOTE,
                summary=(
                    f"triage placeholder: severity={incident.severity.value} "
                    f"entities={len(incident.affected_entities)}"
                ),
                confidence=confidence,
            )
        ],
        "notes": ["triage is a stub; severity is taken from the source payload"],
    }


async def investigate(state: IncidentState, deps: CoreDeps) -> dict:
    """Gather evidence from the active telemetry provider.

    TODO(node): the depth is Roadmap Phase 1 — related events, logs (DQL),
    metrics and evidence ranking. What this node already owns is the contract:
    it asks the port for facts inside the run's scope, records nothing it did not
    receive, and turns a provider outage into recorded evidence instead of a
    crashed incident.

    The provider is injected, never constructed: this module cannot name a vendor
    and cannot import one, which is what makes a second tool an adapter change.
    """
    attempts = state.get("investigation_attempts", 0) + 1
    provider = deps.telemetry

    if provider is None:
        return {
            "phase": Phase.INVESTIGATING,
            "investigation_attempts": attempts,
            "evidence": [
                Evidence(
                    provider="kestrel",
                    kind=EvidenceKind.NOTE,
                    summary=(
                        "no telemetry provider is configured; investigation degraded to the "
                        "incident payload"
                    ),
                    confidence=0.2,
                )
            ],
            "notes": ["investigate degraded: no telemetry provider is wired for this run"],
        }

    reason: str | None = None
    try:
        found = await provider.investigate(state["incident"], deps.scope)
    except TelemetryUnavailable as exc:
        found = []
        reason = f"{provider.provider_id} could not answer: {exc.detail}"

    if not found:
        reason = reason or (
            f"{provider.provider_id} returned no evidence for this incident inside the run scope"
        )
        return {
            "phase": Phase.INVESTIGATING,
            "investigation_attempts": attempts,
            "evidence": [
                Evidence(
                    provider=provider.provider_id,
                    kind=EvidenceKind.NOTE,
                    summary=reason,
                    confidence=0.0,
                )
            ],
            "notes": [f"investigate degraded: {reason}"],
        }

    return {
        "phase": Phase.INVESTIGATING,
        "investigation_attempts": attempts,
        "evidence": found,
        "notes": [f"{provider.provider_id} returned {len(found)} evidence item(s)"],
    }


async def plan(state: IncidentState, deps: CoreDeps) -> dict:
    """Turn evidence into a ranked hypothesis and a remediation plan.

    TODO(node): LLM + runbook retrieval. Steps must be reversible where
    possible, carry a rollback command, and never include credentials.
    """
    incident = state["incident"]
    placeholder_step = RemediationStep(
        order=1,
        description="placeholder: no remediation logic implemented yet",
        command=f"echo 'dry-run remediation for {incident.id}'",
        target=incident.affected_entities[0] if incident.affected_entities else "unknown",
        reversible=True,
        risk="low",
    )
    hypothesis = Hypothesis(statement="unconfirmed: plan is a stub", confidence=0.1)
    return {
        "phase": Phase.PLANNING,
        "hypothesis": hypothesis,
        "plan": RemediationPlan(
            summary="placeholder plan",
            hypothesis=hypothesis,
            steps=[placeholder_step],
            blast_radius="none (dry run)",
            requires_approval=True,
        ),
        "notes": ["plan is a stub"],
    }


async def approval_gate(state: IncidentState, deps: CoreDeps) -> dict:
    """Human-in-the-loop gate.

    The wait is a port: the adapter suspends the graph here (or asks a chat
    channel, later) and hands back one recorded decision. The core never learns
    how the pause works, and a test substitutes it with a canned answer.
    """
    plan = state.get("plan")
    if plan is None:
        return {"phase": Phase.AWAITING_APPROVAL, "notes": ["approval gate reached with no plan"]}

    decision = deps.approval.await_decision(incident_id=state["incident"].id, plan=plan)
    return {"phase": Phase.AWAITING_APPROVAL, "approvals": [decision]}


async def execute(state: IncidentState, deps: CoreDeps) -> dict:
    """Run the approved plan.

    TODO(node): the sandbox executor is the remaining design decision; until it
    exists, `deps.executor` is `None` and this node fails closed.

    Two rules that must not regress:
      * only the latest human decision counts — approving plan A never
        authorizes plan B;
      * an `ActionRecord` describes what happened, so a step that did not run
        carries no exit code and no output, and a missing executor fails closed
        instead of fabricating a success.
    """
    plan = state.get("plan")
    if plan is None:
        return {"phase": Phase.FAILED, "notes": ["execute reached with no plan"]}

    if not _approved_for_current_plan(state):
        return {
            "phase": Phase.FAILED,
            "notes": ["execute blocked: no approval for the plan on the table"],
        }

    attempts = state.get("remediation_attempts", 0) + 1

    if deps.dry_run:
        records = [
            ActionRecord(step_order=step.order, command=step.command, executed=False)
            for step in plan.steps
        ]
        return {
            "phase": Phase.EXECUTING,
            "actions": records,
            "remediation_attempts": attempts,
            "notes": ["dry run: commands recorded, nothing executed"],
        }

    if deps.executor is None:
        # Recording exit_code=0 here would write a fake success into the audit
        # trail. Fail closed instead.
        return {
            "phase": Phase.FAILED,
            "remediation_attempts": attempts,
            "notes": ["execute blocked: no sandbox executor is wired and DRY_RUN is false"],
        }

    records = [await deps.executor.execute(step) for step in plan.steps]
    return {
        "phase": Phase.EXECUTING,
        "actions": records,
        "remediation_attempts": attempts,
        "notes": [f"{len(records)} step(s) executed through the sandbox executor"],
    }


async def verify(state: IncidentState, deps: CoreDeps) -> dict:
    """Check whether the incident is actually gone.

    TODO(node): re-query the active telemetry provider (problem closed, error
    rate back to baseline) instead of trusting the executor's exit code.

    `ok` is never derived from `DRY_RUN`: a dry run applies nothing, so there is
    nothing to verify and claiming success would put a false resolution in the
    audit trail.
    """
    if deps.dry_run:
        return {
            "phase": Phase.VERIFYING,
            "verification": Verification(
                ok=False,
                detail="dry run: no change was applied, so nothing was verified",
            ),
            "notes": ["verify skipped: the dry run applied nothing"],
        }

    return {
        "phase": Phase.VERIFYING,
        "verification": Verification(
            ok=False,
            detail=(
                "verification is not implemented: query the active telemetry provider before "
                "treating an incident as resolved"
            ),
        ),
        "notes": ["verify reached without a telemetry check (not implemented yet)"],
    }


async def report(state: IncidentState, deps: CoreDeps) -> dict:
    """Write the post-incident report and close the workflow."""
    incident = state["incident"]
    verification = state.get("verification")
    resolved = bool(verification and verification.ok)
    actions = [
        f"{a.step_order}. {a.command} (executed={a.executed})" for a in state.get("actions", [])
    ]
    hypothesis = state.get("hypothesis")
    return {
        "phase": Phase.RESOLVED if resolved else Phase.FAILED,
        "report": IncidentReport(
            incident_id=incident.id,
            summary=f"{incident.title}: {len(actions)} action(s), resolved={resolved}",
            root_cause=hypothesis.statement if hypothesis else None,
            actions_taken=actions,
            resolved=resolved,
            follow_ups=["wire real investigation, planning and verification"],
        ),
        "notes": [f"report generated (resolved={resolved})"],
    }


# --- routing ---------------------------------------------------------------
# Conditional edge functions run inside the Temporal workflow, so they must be
# async and deterministic: no I/O, no clock, no randomness. They take the state
# and nothing else, so they cannot grow a dependency even by accident.


async def route_after_investigation(state: IncidentState) -> str:
    attempts = state.get("investigation_attempts", 0)
    limit = state.get("max_investigation_attempts", DEFAULT_MAX_INVESTIGATION_ATTEMPTS)
    best = max((e.confidence for e in state.get("evidence") or []), default=0.0)
    if best < 0.6 and attempts < limit:
        return "investigate"
    return "plan"


async def route_after_approval(state: IncidentState) -> str:
    return "execute" if _approved_for_current_plan(state) else "report"


async def route_after_verify(state: IncidentState) -> str:
    verification = state.get("verification")
    if verification and verification.ok:
        return "report"
    # Re-planning only makes sense when something was actually applied. A dry
    # run or a step that failed closed cannot be fixed by investigating again,
    # and retrying would ask a human to approve the same no-op a second time.
    if not _applied_anything(state):
        return "report"
    limit = state.get("max_remediation_attempts", DEFAULT_MAX_REMEDIATION_ATTEMPTS)
    if state.get("remediation_attempts", 0) < limit:
        return "investigate"
    return "report"
