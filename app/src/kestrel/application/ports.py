"""Ports: the capabilities the application needs, declared by the application.

A port is a boundary the core owns. The core says "I need to ask a human to
decide on this plan", "I need these steps executed somewhere isolated" and "I
need to submit an incident and read its run"; the adapter decides whether that
is a LangGraph interrupt, a sandbox container, a Temporal cluster or a stub in a
test.

A port's contract also *states the rule*, which is what keeps an adapter from
owning one: `start` raising `IncidentAlreadyRunning` is the idempotency rule,
not a Temporal quirk. The adapter enforces it with whatever primitive its engine
has; the application decides that it holds.

Two rules keep this honest:

  * a port is declared here, never in an adapter, so the dependency points
    inward;
  * a port is added when a use case needs it, not in anticipation. An interface
    with no implementation and no caller is speculation, not architecture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from kestrel.domain.models import (
    ActionRecord,
    Approval,
    Evidence,
    Incident,
    RemediationPlan,
    RemediationStep,
)
from kestrel.domain.policy import InvestigationScope, RunLimits

# --- what a run looks like from the outside --------------------------------


@dataclass(frozen=True, slots=True)
class RunHandle:
    """The identity of a run that was just accepted."""

    incident_id: str
    workflow_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class IncidentRunView:
    """A read of one run, with no transport vocabulary in it."""

    incident_id: str
    workflow_id: str
    status: str
    run_id: str
    pending_approval: dict | None = None

    @property
    def awaiting_approval(self) -> bool:
        """The only place that answers "is this run blocked on a human?"."""
        return self.pending_approval is not None


# --- ports -----------------------------------------------------------------


@runtime_checkable
class ApprovalGateway(Protocol):
    """Ask a human to decide on a plan, and wait for the answer.

    Blocking and idempotent from the caller's point of view: the core calls it
    once per plan attempt and receives one `Approval`. How the wait survives a
    restart (or a redeploy) is the adapter's problem.

    The call is synchronous on purpose: the production adapter is a graph
    interruption, which is control flow rather than I/O.
    """

    def await_decision(self, *, incident_id: str, plan: RemediationPlan) -> Approval: ...


@runtime_checkable
class RemediationExecutor(Protocol):
    """Run one approved step in the isolated environment and report what happened.

    Returns the `ActionRecord` that describes the attempt, so the audit trail
    keeps exactly one record per step. An implementation must never report
    success for work it did not do.
    """

    async def execute(self, step: RemediationStep) -> ActionRecord: ...


@runtime_checkable
class TelemetryProvider(Protocol):
    """Read-only view of one observability tool, as the investigation needs it.

    Exactly one implementation is active per run, chosen in the composition
    root. The core asks a question in its own vocabulary ("what do you know about
    this incident, inside this scope?") and the adapter maps its vendor's nouns
    onto `Evidence`.

    Contract:
      * returns `Evidence` for what it can see, and an empty list when the
        incident is unknown to the tool — never a fabricated item;
      * raises `TelemetryUnavailable` when it cannot answer, so the use case can
        degrade the run with a recorded reason instead of guessing;
      * is read-only. A provider never mutates the tool it reads from.
    """

    #: Stable, lowercase id. It is the value that selects this adapter and the
    #: `provider` recorded on every `Evidence` it returns.
    provider_id: str

    async def investigate(
        self, incident: Incident, scope: InvestigationScope
    ) -> list[Evidence]: ...


@runtime_checkable
class RunGateway(Protocol):
    """Where runs live: submit one, read one, decide one.

    This is the port the control plane drives, so the HTTP layer and the CLI
    share it instead of each assembling a workflow start by hand.

    Contract:
      * `start` raises `IncidentAlreadyRunning` when that incident already has a
        run — duplicate ingestion is refused, never merged;
      * every method raises `IncidentNotFound` for an unknown incident and
        `RunUnavailable` when the engine cannot answer;
      * `pending_approval` returns `None` when the run is not blocked on a
        human, and `submit_decision` only delivers: whether a decision is
        allowed is the application's call, not the engine's.
    """

    async def start(self, incident: Incident, limits: RunLimits) -> RunHandle: ...

    async def view(self, incident_id: str) -> IncidentRunView: ...

    async def pending_approval(self, incident_id: str) -> dict | None: ...

    async def submit_decision(self, incident_id: str, decision: Approval) -> None: ...

    async def outcome(self, incident_id: str, *, timeout_seconds: float) -> dict[str, Any]: ...
