"""Guard-rail tests.

These cover the invariants that must never regress silently: no approval means
no execution, remediation is never retried automatically, retries are bounded,
routers stay pure, and every node declares how it runs.

They run against the injected seam (`CoreDeps`), so no test needs the process
environment: a guard rail that cannot be tested in isolation is a guard rail
nobody verifies.
"""

from __future__ import annotations

import inspect

from kestrel.application import nodes
from kestrel.application.deps import CoreDeps
from kestrel.application.graph import ACTIVITY_OPTIONS, NODE_FUNCTIONS, build_graph
from kestrel.application.policy import workflow_id
from kestrel.application.state import initial_state
from kestrel.domain.models import (
    ActionRecord,
    Approval,
    Incident,
    Phase,
    RemediationPlan,
    Verification,
)
from kestrel.domain.policy import RunLimits


async def _approve(state: dict, *, actor: str = "oncall") -> None:
    state["approvals"] = [Approval(approved=True, actor=actor)]


async def test_route_after_approval_rejects_without_approval(state: dict) -> None:
    assert await nodes.route_after_approval(state) == "report"


async def test_route_after_approval_executes_when_approved(state: dict) -> None:
    await _approve(state)
    assert await nodes.route_after_approval(state) == "execute"


async def test_rejected_replan_is_not_authorized_by_an_earlier_approval(
    state: dict, plan: RemediationPlan
) -> None:
    """Approving plan A must never authorize plan B, even when B is rejected."""
    state["plan"] = plan
    state["approvals"] = [
        Approval(approved=True, actor="oncall", comment="first plan"),
        Approval(approved=False, actor="oncall", comment="second plan rejected"),
    ]
    assert await nodes.route_after_approval(state) == "report"


async def test_execute_refuses_after_the_latest_decision_rejected_the_plan(
    state: dict, plan: RemediationPlan, deps: CoreDeps
) -> None:
    state["plan"] = plan
    state["approvals"] = [
        Approval(approved=True, actor="oncall"),
        Approval(approved=False, actor="oncall"),
    ]
    result = await nodes.execute(state, deps)
    assert result["phase"] == Phase.FAILED
    assert "actions" not in result


async def test_execute_refuses_without_approval(
    state: dict, plan: RemediationPlan, deps: CoreDeps
) -> None:
    state["plan"] = plan
    result = await nodes.execute(state, deps)
    assert result["phase"] == Phase.FAILED
    assert "no approval" in result["notes"][0]
    assert "actions" not in result


async def test_execute_records_dry_run_actions(
    state: dict, plan: RemediationPlan, deps: CoreDeps
) -> None:
    state["plan"] = plan
    await _approve(state)
    result = await nodes.execute(state, deps)
    assert result["phase"] == Phase.EXECUTING
    assert len(result["actions"]) == 1
    action = result["actions"][0]
    assert action.executed is False  # DRY_RUN defaults to true
    # A step that did not run carries no outcome and no output.
    assert action.exit_code is None
    assert action.stdout == ""


async def test_execute_fails_closed_when_no_executor_is_wired(
    state: dict, plan: RemediationPlan
) -> None:
    """DRY_RUN=false with no executor must not report a successful step."""
    state["plan"] = plan
    await _approve(state)
    deps = CoreDeps(approval=_NeverCalled(), dry_run=False)
    result = await nodes.execute(state, deps)
    assert result["phase"] == Phase.FAILED
    assert "actions" not in result


async def test_route_after_verify_stops_when_resolved(state: dict) -> None:
    state["verification"] = Verification(ok=True, detail="problem closed")
    assert await nodes.route_after_verify(state) == "report"


async def test_route_after_verify_retries_once_before_giving_up(state: dict) -> None:
    state["verification"] = Verification(ok=False, detail="still failing")
    state["actions"] = [ActionRecord(step_order=1, command="kubectl get pods", executed=True)]
    assert await nodes.route_after_verify(state) == "investigate"

    state["remediation_attempts"] = 99
    assert await nodes.route_after_verify(state) == "report"


async def test_route_after_verify_does_not_retry_when_nothing_was_applied(state: dict) -> None:
    """Re-investigating a dry run (or a fail-closed execute) cannot change anything."""
    state["verification"] = Verification(ok=False, detail="dry run verified nothing")
    state["actions"] = [ActionRecord(step_order=1, command="kubectl get pods", executed=False)]
    assert await nodes.route_after_verify(state) == "report"


async def test_route_after_investigation_is_bounded_by_the_snapshot(state: dict) -> None:
    state["investigation_attempts"] = state["max_investigation_attempts"]
    assert await nodes.route_after_investigation(state) == "plan"

    state["investigation_attempts"] = 0
    assert await nodes.route_after_investigation(state) == "investigate"


async def test_routers_are_pure_and_take_no_dependencies(state: dict) -> None:
    """Routers run inside the workflow: state in, a name out, nothing else.

    The signature check is the structural half: a router that cannot receive a
    port cannot grow I/O, and the architecture test keeps the framework out of
    the module they live in.
    """
    routers = (
        nodes.route_after_investigation,
        nodes.route_after_approval,
        nodes.route_after_verify,
    )
    for router in routers:
        assert list(inspect.signature(router).parameters) == ["state"]
        first = await router(state)
        second = await router(state)
        assert first == second


async def test_verify_never_claims_success_from_the_dry_run(state: dict, deps: CoreDeps) -> None:
    """A dry run applies nothing, so it must not report a verified recovery."""
    result = await nodes.verify(state, deps)

    assert result["verification"].ok is False
    assert "dry run" in result["verification"].detail


async def test_report_does_not_claim_resolution_without_a_verification(
    state: dict, deps: CoreDeps
) -> None:
    result = await nodes.report(state, deps)
    assert result["report"].resolved is False
    assert result["phase"] == Phase.FAILED


async def test_report_claims_resolution_only_when_verification_passed(
    state: dict, deps: CoreDeps
) -> None:
    state["verification"] = Verification(ok=True, detail="problem closed")
    result = await nodes.report(state, deps)
    assert result["report"].resolved is True
    assert result["phase"] == Phase.RESOLVED


def test_initial_state_snapshots_the_attempt_limits() -> None:
    """The limits travel with the run; routers must not read them from settings."""
    state = initial_state(
        Incident(id="test-2", title="limits"),
        RunLimits(max_investigation_attempts=7, max_remediation_attempts=4),
    )
    assert state["max_investigation_attempts"] == 7
    assert state["max_remediation_attempts"] == 4


def test_every_node_declares_activity_options() -> None:
    assert set(NODE_FUNCTIONS) == set(ACTIVITY_OPTIONS)
    for name, options in ACTIVITY_OPTIONS.items():
        assert options["execute_in"] == "activity", f"{name} must run as an activity"


def test_remediation_is_never_retried_automatically() -> None:
    # A blind retry of a destructive command is an incident, not a recovery.
    assert ACTIVITY_OPTIONS["execute"]["retry_policy"].maximum_attempts == 1


def test_graph_compiles(deps: CoreDeps) -> None:
    graph = build_graph(deps)
    for name in NODE_FUNCTIONS:
        assert name in graph.nodes


def test_plugin_accepts_the_graph(deps: CoreDeps) -> None:
    from temporalio.langgraph import LangGraphPlugin

    LangGraphPlugin(graphs={"incident": build_graph(deps)})


def test_workflow_id_is_deterministic() -> None:
    assert workflow_id("PD-123") == "incident-PD-123"


def test_serde_covers_the_state_models() -> None:
    from kestrel.adapters.outbound.checkpoint import STATE_TYPES, build_serde

    assert build_serde() is not None
    declared = {name for _, name in STATE_TYPES}
    assert {"Incident", "RemediationPlan", "Approval"} <= declared


class _NeverCalled:
    """An approval gateway that fails loudly if the code path is not guarded."""

    def await_decision(self, *, incident_id: str, plan: object) -> None:
        raise AssertionError("the approval gateway must not be reached on this path")
