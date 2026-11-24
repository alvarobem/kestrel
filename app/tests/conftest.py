"""Shared fixtures and test doubles.

The ports exist so a test can substitute them: `StubApprovalGateway` replaces
the human-in-the-loop adapter, `StubExecutor` replaces the sandbox and
`StubTelemetryProvider` replaces the observability tool. None of them is
something a unit test should reach over the network.
"""

from __future__ import annotations

import os

import pytest

from kestrel.application.deps import CoreDeps
from kestrel.application.state import initial_state
from kestrel.domain.models import (
    ActionRecord,
    Approval,
    Evidence,
    Hypothesis,
    Incident,
    RemediationPlan,
    RemediationStep,
)
from kestrel.domain.policy import InvestigationScope


class StubTelemetryProvider:
    """Answers with canned evidence, or with a recorded failure."""

    provider_id = "stub"

    def __init__(
        self, evidence: list[Evidence] | None = None, error: Exception | None = None
    ) -> None:
        self.evidence = list(evidence or [])
        self.error = error
        self.calls: list[tuple[Incident, InvestigationScope]] = []

    async def investigate(
        self, incident: Incident, scope: InvestigationScope
    ) -> list[Evidence]:
        self.calls.append((incident, scope))
        if self.error is not None:
            raise self.error
        return list(self.evidence)


class StubApprovalGateway:
    """Answers with a canned decision and records what it was asked."""

    def __init__(self, approval: Approval | None = None) -> None:
        self.approval = (
            approval if approval is not None else Approval(approved=True, actor="oncall")
        )
        self.calls: list[tuple[str, RemediationPlan]] = []
        #: What the human was shown alongside the question.
        self.evidence: list[str] = []

    def await_decision(
        self, *, incident_id: str, plan: RemediationPlan, evidence: list[str]
    ) -> Approval:
        self.calls.append((incident_id, plan))
        self.evidence = list(evidence)
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


class StubLlmClient:
    """Answers with canned text and records the prompts it was given.

    It answers in order and fails loudly when asked for more than it holds, so a
    test that asserts "exactly one repair attempt" cannot pass by accident.
    """

    model_id = "stub-llm"

    def __init__(self, answers: list[str] | None = None, error: Exception | None = None) -> None:
        self.answers = list(answers or [])
        self.error = error
        #: Every prompt, as `(system, user)`.
        self.prompts: list[tuple[str, str]] = []

    async def complete(self, *, system: str, user: str) -> str:
        self.prompts.append((system, user))
        if self.error is not None:
            raise self.error
        if not self.answers:
            raise AssertionError("the stub was asked for more answers than it holds")
        return self.answers.pop(0)


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite hermetic.

    `Settings()` reads the process environment, and `make test` runs inside the
    worker container, which `docker compose` fills with the operator's `.env`.
    Without this, a configured machine turns every unit test into a live tenant
    call: `build_deps(Settings())` would build a real client and the assertions
    about the unconfigured path would fail against a real 403.
    """
    for name in list(os.environ):
        if name.startswith(("DYNATRACE_", "LANGFUSE_", "LLM_")):
            monkeypatch.delenv(name, raising=False)


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
def telemetry() -> StubTelemetryProvider:
    return StubTelemetryProvider()


@pytest.fixture
def llm() -> StubLlmClient:
    return StubLlmClient()


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
