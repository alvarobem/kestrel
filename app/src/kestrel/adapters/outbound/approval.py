"""Implementation of `ApprovalGateway`.

Two adapters, one port, and the choice between them is made by the composition
root (`worker.build_deps`), not by the use case:

  * `GraphApprovalGateway` is the real one: LangGraph suspends the graph at
    `interrupt()`, Temporal persists the run, and a signal resumes it with the
    human's decision. Minutes or days later, after a restart, unchanged.
  * `AutoApprovalGateway` answers "approved" without asking. It exists for the
    local smoke test, and it is the honest place for that shortcut: a wiring
    decision, visible at startup, instead of a branch hidden inside the guard
    rail that is supposed to prevent exactly this.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from kestrel.domain.models import Approval, RemediationPlan


def to_approval(decision: object) -> Approval:
    """Turn whatever resumed the graph into a recorded decision.

    Anything that is not a dict with `approved: true` is a rejection: a
    malformed resume value must never authorize a remediation.
    """
    if not isinstance(decision, dict):
        return Approval(approved=False, actor="unknown")
    return Approval(
        approved=bool(decision.get("approved")),
        actor=str(decision.get("actor", "unknown")),
        comment=str(decision.get("comment", "")),
    )


class GraphApprovalGateway:
    """Suspends the graph until a human decision resumes it."""

    def await_decision(
        self, *, incident_id: str, plan: RemediationPlan, evidence: list[str]
    ) -> Approval:
        payload: Any = interrupt(
            {
                "incident_id": incident_id,
                "plan": plan.model_dump(mode="json"),
                # What the plan is based on. An approver who cannot see this is
                # authorizing a command, not a decision.
                "evidence": list(evidence),
                "question": "Approve this remediation plan?",
            }
        )
        return to_approval(payload)


class AutoApprovalGateway:
    """Records an approval nobody gave. Local smoke tests only."""

    def await_decision(
        self, *, incident_id: str, plan: RemediationPlan, evidence: list[str]
    ) -> Approval:
        return Approval(
            approved=True,
            actor="auto",
            comment="APPROVAL_MODE=approved (local smoke tests only)",
        )
