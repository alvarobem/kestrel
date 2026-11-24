"""The ports, the injected dependencies and the composition root.

These are the tests that could not exist before: the core runs with a fake
approval channel and a fake executor, and the whole application is assembled
from an explicitly constructed `Settings`, without reading the environment.
"""

from __future__ import annotations

from typing import Any

import pytest

from kestrel.adapters.outbound.approval import (
    AutoApprovalGateway,
    GraphApprovalGateway,
    to_approval,
)
from kestrel.adapters.outbound.providers import UnknownTelemetryProvider
from kestrel.application import nodes
from kestrel.application.deps import CoreDeps
from kestrel.application.graph import NODE_FUNCTIONS
from kestrel.config import Settings
from kestrel.domain.models import (
    Approval,
    Evidence,
    EvidenceKind,
    Hypothesis,
    Phase,
    RemediationPlan,
    RemediationStep,
    StepActor,
)
from kestrel.domain.policy import (
    DEFAULT_MAX_INVESTIGATION_ATTEMPTS,
    DEFAULT_MAX_REMEDIATION_ATTEMPTS,
    Capability,
    RemediationSurface,
    RunLimits,
)
from kestrel.worker import build_app, build_deps, describe_surface


class ExplodingGateway:
    """Fails loudly if the code path reaches the approval channel."""

    def await_decision(self, *, incident_id: str, plan: Any, evidence: list[str]) -> Approval:
        raise AssertionError("nobody should be asked for a decision on this path")


# --- approval through the port ---------------------------------------------


async def test_approval_gate_asks_the_injected_gateway(
    state: dict, plan: RemediationPlan, approval_gateway: Any
) -> None:
    state["plan"] = plan
    deps = CoreDeps(approval=approval_gateway)

    result = await nodes.approval_gate(state, deps)

    assert result["phase"] == Phase.AWAITING_APPROVAL
    assert [(a.approved, a.actor) for a in result["approvals"]] == [(True, "oncall")]
    assert approval_gateway.calls == [("test-1", plan)]


async def test_approval_gate_records_a_rejection(
    state: dict, plan: RemediationPlan, make_gateway: Any
) -> None:
    state["plan"] = plan
    rejection = Approval(approved=False, actor="oncall", comment="too risky")

    result = await nodes.approval_gate(state, CoreDeps(approval=make_gateway(rejection)))

    assert result["approvals"] == [rejection]


async def test_approval_gate_without_a_plan_never_reaches_the_channel(state: dict) -> None:
    deps = CoreDeps(approval=ExplodingGateway())

    result = await nodes.approval_gate(state, deps)

    assert result["phase"] == Phase.AWAITING_APPROVAL
    assert "no plan" in result["notes"][0]


def test_to_approval_reads_the_decision_payload() -> None:
    parsed = to_approval({"approved": True, "actor": "oncall", "comment": "ok"})
    assert (parsed.approved, parsed.actor, parsed.comment) == (True, "oncall", "ok")
    # A malformed resume value is a rejection, never an implicit approval.
    assert to_approval(None).approved is False
    assert to_approval({}).approved is False
    assert to_approval({"approved": True}).actor == "unknown"


def test_auto_approval_names_itself() -> None:
    approval = AutoApprovalGateway().await_decision(  # type: ignore[arg-type]
        incident_id="i", plan=None, evidence=[]
    )

    assert approval.approved is True
    assert approval.actor == "auto"
    assert "APPROVAL_MODE=approved" in approval.comment


# --- execution through the port --------------------------------------------


async def test_execute_uses_the_injected_executor(
    state: dict, plan: RemediationPlan, executor: Any
) -> None:
    state["plan"] = plan
    state["approvals"] = [Approval(approved=True, actor="oncall")]
    deps = CoreDeps(approval=ExplodingGateway(), dry_run=False, executor=executor)

    result = await nodes.execute(state, deps)

    assert result["phase"] == Phase.EXECUTING
    assert executor.steps == list(plan.steps)
    assert [action.executed for action in result["actions"]] == [True]


def _mixed_plan() -> RemediationPlan:
    return RemediationPlan(
        summary="restart the deployment and confirm with the partner",
        hypothesis=Hypothesis(statement="wedged after a failed rollout", confidence=0.6),
        steps=[
            RemediationStep(
                order=1,
                description="restart the deployment",
                command="kubectl rollout restart deploy/checkout",
                target="checkout",
                capability="restart_workload",
                rollback_command="kubectl rollout undo deploy/checkout",
            ),
            RemediationStep(
                order=2,
                description="Confirm with the partner that their outage is acknowledged",
                target="avios",
                actor=StepActor.HUMAN,
            ),
        ],
    )


async def test_execute_hands_a_human_step_to_a_person(state: dict, executor: Any) -> None:
    state["plan"] = _mixed_plan()
    state["approvals"] = [Approval(approved=True, actor="oncall")]
    deps = CoreDeps(approval=ExplodingGateway(), dry_run=False, executor=executor)

    result = await nodes.execute(state, deps)

    # The sandbox only receives what the sandbox can run.
    assert executor.steps == [state["plan"].steps[0]]
    records = {record.step_order: record for record in result["actions"]}
    assert records[1].executed is True
    assert records[2].actor is StepActor.HUMAN
    assert records[2].executed is False
    assert "assigned to a person" in records[2].note
    assert "handed to a person" in result["notes"][0]


async def test_a_plan_of_human_steps_needs_no_executor(state: dict) -> None:
    plan = RemediationPlan(
        summary="rotate the credential",
        hypothesis=Hypothesis(statement="the credential expired", confidence=0.7),
        steps=[
            RemediationStep(
                order=1,
                description="Rotate the Avios credential in the partner console",
                target="avios",
                actor=StepActor.HUMAN,
            )
        ],
    )
    state["plan"] = plan
    state["approvals"] = [Approval(approved=True, actor="oncall")]
    # No executor wired, and not a dry run: human steps alone do not need one.
    deps = CoreDeps(approval=ExplodingGateway(), dry_run=False, executor=None)

    result = await nodes.execute(state, deps)

    assert result["phase"] == Phase.EXECUTING
    assert [record.executed for record in result["actions"]] == [False]


def test_the_startup_log_names_the_declared_capabilities() -> None:
    """The log line runs once, in `main()`, where no test reaches otherwise.

    It broke exactly this way: the surface was redesigned from tool+verbs to a
    capability list, the log kept asking for `.tool`, and the worker died at boot
    with every unit test green.
    """
    assert describe_surface(RemediationSurface()) == "undeclared"

    declared = RemediationSurface(
        capabilities=(
            Capability(id="restart_workload", tool="kubectl"),
            Capability(id="raise_partner_incident", tool="gh"),
        )
    )
    assert describe_surface(declared) == "restart_workload,raise_partner_incident"


def test_build_deps_carries_the_declared_surface() -> None:
    settings = Settings(
        remediation_capabilities="restart_workload|kubectl|rollout|-n checkout|change|restart one workload"
    )

    deps = build_deps(settings)

    assert describe_surface(deps.surface) == "restart_workload"
    assert deps.surface.find("restart_workload").must_contain == ("-n checkout",)


# --- investigation without the environment ---------------------------------

async def test_investigate_degrades_without_a_telemetry_source(state: dict) -> None:
    deps = CoreDeps(approval=ExplodingGateway())

    result = await nodes.investigate(state, deps)

    assert result["investigation_attempts"] == 1
    assert "no telemetry provider" in result["evidence"][0].summary


# --- run policy ------------------------------------------------------------


def test_run_limits_round_trip_through_the_workflow_input() -> None:
    limits = RunLimits(max_investigation_attempts=5, max_remediation_attempts=1)
    payload = limits.as_workflow_input()

    assert payload == {"max_investigation_attempts": 5, "max_remediation_attempts": 1}
    assert RunLimits.from_workflow_input(payload) == limits


def test_run_limits_fall_back_to_the_defaults() -> None:
    limits = RunLimits.from_workflow_input(None)

    assert limits.max_investigation_attempts == DEFAULT_MAX_INVESTIGATION_ATTEMPTS
    assert limits.max_remediation_attempts == DEFAULT_MAX_REMEDIATION_ATTEMPTS


# --- composition root ------------------------------------------------------


def test_build_deps_picks_the_approval_channel_from_settings() -> None:
    human = build_deps(Settings(approval_mode="human"))
    auto = build_deps(Settings(approval_mode="approved"))

    assert isinstance(human.approval, GraphApprovalGateway)
    assert isinstance(auto.approval, AutoApprovalGateway)


def test_build_deps_carries_the_policy_and_wires_no_executor() -> None:
    settings = Settings(
        dry_run=True,
        dynatrace_env_url="https://example.live.dynatrace.com",
        dynatrace_read_token="read-token",
    )

    deps = build_deps(settings)

    assert deps.dry_run is True
    assert deps.telemetry is not None
    assert deps.telemetry.provider_id == "dynatrace"
    # No scope tags configured: the run is unrestricted, and that is explicit.
    assert deps.scope.unrestricted is True
    # Remediation stays unavailable until a sandbox executor exists.
    assert deps.executor is None


def test_build_deps_degrades_when_the_provider_has_no_credentials() -> None:
    deps = build_deps(Settings())

    assert deps.telemetry is None


def test_build_deps_freezes_the_scope_from_settings() -> None:
    deps = build_deps(Settings(dynatrace_scope_tags="checkout, payments ,"))

    assert deps.scope.tags == ("checkout", "payments")
    assert deps.scope.allows(["payments"]) is True
    assert deps.scope.allows(["other"]) is False


def test_build_deps_refuses_an_unknown_telemetry_provider() -> None:
    """A typo must fail at startup, not silently produce an evidence-free run."""
    with pytest.raises(UnknownTelemetryProvider):
        build_deps(Settings(telemetry_provider="nope"))


def test_build_app_does_not_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> None:
        raise AssertionError("build_app receives its settings; it must not read them")

    monkeypatch.setattr("kestrel.worker.get_settings", forbidden)

    app = build_app(Settings())

    for name in NODE_FUNCTIONS:
        assert name in app.nodes


def test_build_app_wraps_the_nodes_when_tracing_is_configured() -> None:
    app = build_app(Settings(langfuse_public_key="pk-test", langfuse_secret_key="sk-test"))

    for name in NODE_FUNCTIONS:
        assert name in app.nodes


async def test_the_approval_shows_the_ranked_evidence(
    state: dict, plan: RemediationPlan, approval_gateway: Any
) -> None:
    """A human asked to authorize an action must see what justifies it."""
    state["plan"] = plan
    state["evidence"] = [
        Evidence(provider="kestrel", kind=EvidenceKind.NOTE, summary="bookkeeping"),
        Evidence(
            provider="dynatrace",
            kind=EvidenceKind.ALERT,
            summary="davis: checkout failing",
            external_id="P-9",
        ),
        Evidence(
            provider="dynatrace",
            kind=EvidenceKind.LOG,
            summary="order-handler: boom",
            occurrences=7,
        ),
    ]

    await nodes.approval_gate(state, CoreDeps(approval=approval_gateway))

    shown = approval_gateway.evidence
    # Ranked: the alert explains more than a log, and a note least of all.
    assert shown[0].startswith("[alert] davis: checkout failing")
    assert any("(x7)" in line for line in shown)
    assert "bookkeeping" in shown[-1]
