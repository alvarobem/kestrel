"""Shared fixtures and test doubles.

The ports exist so a test can substitute them: `StubApprovalGateway` replaces
the human-in-the-loop adapter and `StubExecutor` replaces the sandbox, neither
of which a unit test should reach.
"""

from __future__ import annotations

import pytest

from kestrel.application.deps import CoreDeps
from kestrel.application.state import initial_state
from kestrel.domain.models import (
    ActionRecord,
    Approval,
    Hypothesis,
    Incident,
    RemediationPlan,
    RemediationStep,
)


class StubApprovalGateway:
    """Answers with a canned decision and records what it was asked."""

    def __init__(self, approval: Approval | None = None) -> None:
        self.approval = (
            approval if approval is not None else Approval(approved=True, actor="oncall")
        )
        self.calls: list[tuple[str, RemediationPlan]] = []

    def await_decision(self, *, incident_id: str, plan: RemediationPlan) -> Approval:
        self.calls.append((incident_id, plan))
        return self.approval


class StubExecutor:
    """Stands in for the sandbox: reports one successful step per command."""

    def __init__(self) -> None:
        self.steps: list[RemediationStep] = []

    async def execute(self, step: RemediationStep) -> ActionRecord:
        self.steps.append(step)
        return ActionRecord(
            step_order=step.order,
            command=step.command,
            executed=True,
            exit_code=0,
            stdout="stub",
        )


@pytest.fixture
def approval_gateway() -> StubApprovalGateway:
    return StubApprovalGateway()


@pytest.fixture
def make_gateway():
    """Build a gateway that answers with a specific decision."""

    def _make(approval: Approval) -> StubApprovalGateway:
        return StubApprovalGateway(approval)

    return _make


@pytest.fixture
def executor() -> StubExecutor:
    return StubExecutor()


@pytest.fixture
def deps(approval_gateway: StubApprovalGateway) -> CoreDeps:
    return CoreDeps(approval=approval_gateway)


@pytest.fixture
def plan() -> RemediationPlan:
    return RemediationPlan(
        summary="restart the deployment",
        hypothesis=Hypothesis(statement="pod is wedged", confidence=0.7),
        steps=[
            RemediationStep(
                order=1,
                description="restart",
                command="kubectl rollout restart deploy/checkout",
                target="checkout",
                reversible=True,
                risk="low",
                rollback_command="kubectl rollout undo deploy/checkout",
            )
        ],
    )


@pytest.fixture
def state() -> dict:
    return dict(initial_state(Incident(id="test-1", title="test incident")))
