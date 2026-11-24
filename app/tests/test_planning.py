"""The planning step: what the model is asked, and what survives its answer.

Two kinds of test, and the split is the point. The rules — prompt building, JSON
parsing, guard rails — are pure and need no model at all; the node is driven
through the `LlmClient` port with a stub that records what it was asked. Nothing
here reaches a provider, and nothing depends on the process environment.

The guard-rail tests are the ones that matter on a bad day: they are the reason a
model's answer can never put an unreviewable command in front of a human. They
also pin the property that keeps this honest beyond on-call — a capability is not
tied to kubectl, and a surface that names `gh` is checked by the same code.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kestrel.application import nodes
from kestrel.application.deps import CoreDeps
from kestrel.application.errors import LlmUnavailable, PlanUnusable
from kestrel.application.planner import build_prompt, parse_plan, require_usable
from kestrel.config import Settings
from kestrel.domain.models import Evidence, EvidenceKind, Incident, Phase
from kestrel.domain.policy import Capability, RemediationSurface

#: Two capabilities, deliberately from different worlds: a production restart and
#: an integration pipeline. One mechanism, two environments.
SURFACE = RemediationSurface(
    capabilities=(
        Capability(
            id="restart_workload",
            tool="kubectl",
            verbs=("rollout", "scale", "delete"),
            must_contain=("-n checkout",),
            description="restart, scale or delete one workload in checkout",
        ),
        Capability(
            id="rerun_pipeline",
            tool="gh",
            verbs=("workflow",),
            must_contain=("deploy.yml",),
            description="re-run the integration pipeline for one service",
        ),
        Capability(
            id="inspect_workload",
            tool="kubectl",
            verbs=("get", "describe", "logs"),
            must_contain=("-n checkout",),
            effect="read",
            description="read the state or logs of one workload",
        ),
    )
)


def _step(**overrides: Any) -> dict:
    step = {
        "order": 1,
        "description": "restart the deployment",
        "command": "kubectl rollout restart deploy/checkout -n checkout",
        "target": "checkout",
        "capability": "restart_workload",
        "reversible": True,
        "risk": "low",
        "rollback_command": "kubectl rollout undo deploy/checkout -n checkout",
    }
    step.update(overrides)
    return step


def _payload(**overrides: Any) -> dict:
    payload = {
        "summary": "restart the wedged checkout deployment",
        "hypothesis": {
            "statement": "the deployment is wedged after a failed rollout",
            "confidence": 0.7,
            "supporting_evidence": ["error log volume is steady at 1.0x baseline"],
        },
        "steps": [_step()],
        "blast_radius": "checkout only",
    }
    payload.update(overrides)
    return payload


GOOD_ANSWER = json.dumps(_payload())


def _usable(payload: dict, surface: RemediationSurface = SURFACE) -> None:
    require_usable(parse_plan(json.dumps(payload)), surface)


# --- the answer becomes a plan, or it does not ------------------------------


def test_parse_ignores_the_prose_and_fences_around_the_json() -> None:
    answer = f"Sure. Here is the plan:\n\n```json\n{GOOD_ANSWER}\n```\n\nLet me know."

    plan = parse_plan(answer)

    assert plan.steps[0].command == "kubectl rollout restart deploy/checkout -n checkout"
    assert plan.hypothesis.confidence == 0.7


def test_parse_forces_the_approval_the_model_tried_to_skip() -> None:
    # The model does not decide whether a human is asked. That is policy.
    plan = parse_plan(json.dumps(_payload(requires_approval=False)))

    assert plan.requires_approval is True


def test_parse_renumbers_the_steps_in_order() -> None:
    answer = json.dumps(
        _payload(
            steps=[
                _step(order=9, command="kubectl get pods -n checkout"),
                _step(order=2),
            ]
        )
    )

    plan = parse_plan(answer)

    assert [step.order for step in plan.steps] == [1, 2]
    # The model's numbering is a hint; the normalized order is what the executor
    # follows, so step 1 is the one the model marked as happening first.
    assert plan.steps[0].command == "kubectl rollout restart deploy/checkout -n checkout"


def test_parse_refuses_text_that_is_not_json() -> None:
    with pytest.raises(PlanUnusable) as caught:
        parse_plan("I cannot help with that.")

    assert caught.value.repairable is True


def test_parse_treats_an_empty_plan_as_a_repairable_omission() -> None:
    answer = json.dumps(_payload(steps=[], summary="restart the checkout deployment"))

    with pytest.raises(PlanUnusable) as caught:
        parse_plan(answer)

    # The summary proposed an action the steps do not contain. One retry names that
    # contradiction; a model can also legitimately repeat the empty answer.
    assert caught.value.repairable is True
    assert "restart the checkout deployment" in caught.value.detail


# --- the guard rails --------------------------------------------------------


def test_guard_rail_refuses_a_wildcard() -> None:
    with pytest.raises(PlanUnusable, match="wildcard"):
        _usable(_payload(steps=[_step(command="kubectl delete pods -l app=* -n checkout")]))


def test_guard_rail_refuses_a_command_that_addresses_everything() -> None:
    with pytest.raises(PlanUnusable, match="every object"):
        _usable(_payload(steps=[_step(command="kubectl rollout restart deploy --all -n checkout")]))


def test_guard_rail_refuses_an_irreversible_step_without_a_rollback() -> None:
    with pytest.raises(PlanUnusable, match="no rollback"):
        _usable(_payload(steps=[_step(reversible=False, rollback_command=None)]))


def test_guard_rail_accepts_an_irreversible_step_that_can_be_undone() -> None:
    # Deleting a pod is not "reversible", but the deployment brings it back: the
    # rollback is the part that matters, not the label.
    _usable(
        _payload(
            steps=[
                _step(
                    reversible=False,
                    command="kubectl delete pod checkout-abc123 -n checkout",
                    rollback_command="kubectl rollout undo deploy/checkout -n checkout",
                )
            ]
        )
    )


def test_guard_rail_refuses_a_step_without_a_target() -> None:
    with pytest.raises(PlanUnusable, match="names no target"):
        _usable(_payload(steps=[_step(target="  ")]))


def test_guard_rail_refuses_a_step_that_names_no_capability() -> None:
    with pytest.raises(PlanUnusable, match="names no capability"):
        _usable(_payload(steps=[_step(capability=None)]))


def test_guard_rail_refuses_a_capability_that_was_not_declared() -> None:
    with pytest.raises(PlanUnusable, match="unknown capability"):
        _usable(_payload(steps=[_step(capability="drop_database")]))


def test_guard_rail_refuses_a_tool_the_capability_does_not_use() -> None:
    # The step claims "restart a workload" and then runs something else.
    with pytest.raises(PlanUnusable, match="but capability"):
        _usable(_payload(steps=[_step(command="helm upgrade checkout ./chart -n checkout")]))


def test_guard_rail_refuses_a_verb_the_capability_does_not_allow() -> None:
    with pytest.raises(PlanUnusable, match="does not allow"):
        _usable(_payload(steps=[_step(command="kubectl exec checkout-abc123 -- rm -rf /tmp -n checkout")]))


def test_guard_rail_refuses_a_command_that_does_not_say_where_it_lands() -> None:
    # Same capability, same tool, but the command omits the namespace the surface
    # requires: it would target whatever context the sandbox happened to have.
    with pytest.raises(PlanUnusable, match="must contain"):
        _usable(_payload(steps=[_step(command="kubectl rollout restart deploy/checkout")]))


def test_a_capability_that_is_not_kubectl_is_checked_the_same_way() -> None:
    # This is the point of declaring capabilities instead of verbs: a service that
    # will not boot in integration is remediated by a pipeline, not a restart, and
    # the guard rail does not care which.
    _usable(
        _payload(
            steps=[
                _step(
                    capability="rerun_pipeline",
                    command="gh workflow run deploy.yml -f service=checkout",
                    target="checkout",
                    reversible=False,
                    rollback_command="gh run cancel 12345",
                )
            ]
        )
    )


def test_guard_rail_refuses_a_plan_that_only_reads() -> None:
    # The investigation already read the logs and the events. Proposing to read
    # them again spends a human approval on a no-op, so the plan is refused.
    with pytest.raises(PlanUnusable, match="only reads"):
        _usable(
            _payload(
                steps=[
                    _step(
                        capability="inspect_workload",
                        command="kubectl logs deployment/checkout -n checkout",
                    )
                ]
            )
        )


def test_guard_rail_accepts_a_read_step_next_to_a_change() -> None:
    # Inspecting the result of an action is legitimate; only a plan made of
    # nothing else is not.
    _usable(
        _payload(
            steps=[
                _step(),
                _step(
                    order=2,
                    capability="inspect_workload",
                    command="kubectl get pods -n checkout",
                    rollback_command=None,
                ),
            ]
        )
    )


def test_a_human_step_needs_no_command_and_no_capability() -> None:
    # Some remediation is not the agent's to perform: a vendor console, a partner
    # call, a code change in a repository it cannot reach.
    _usable(
        _payload(
            steps=[
                _step(
                    actor="human",
                    command=None,
                    capability=None,
                    description=(
                        "Rotate the Avios API credential in the partner console and confirm "
                        "the next request succeeds"
                    ),
                )
            ]
        )
    )


def test_a_human_step_may_not_carry_a_command() -> None:
    # A step with a command and a human owner is ambiguous about who runs it.
    with pytest.raises(PlanUnusable, match="must not carry a command"):
        _usable(
            _payload(
                steps=[
                    _step(
                        actor="human",
                        capability=None,
                        description="Rotate the credential in the vendor console",
                    )
                ]
            )
        )


def test_a_human_step_needs_a_concrete_instruction() -> None:
    with pytest.raises(PlanUnusable, match="concrete instruction"):
        _usable(
            _payload(
                steps=[_step(actor="human", command=None, capability=None, description="investigate")]
            )
        )


def test_an_agent_step_without_a_command_is_refused() -> None:
    with pytest.raises(PlanUnusable, match="no command"):
        _usable(_payload(steps=[_step(command=None)]))


def test_a_mixed_plan_is_accepted() -> None:
    _usable(
        _payload(
            steps=[
                _step(),
                _step(
                    order=2,
                    actor="human",
                    command=None,
                    capability=None,
                    description="Confirm with the partner that their outage is acknowledged",
                ),
            ]
        )
    )


def test_an_undeclared_surface_refuses_every_plan() -> None:
    with pytest.raises(PlanUnusable) as caught:
        require_usable(parse_plan(GOOD_ANSWER), RemediationSurface())

    # Never repairable: no answer from a model can declare the vocabulary.
    assert caught.value.repairable is False


# --- the question -----------------------------------------------------------


def test_prompt_carries_evidence_summaries_and_never_the_payload() -> None:
    incident = Incident(id="PD-1", title="checkout is failing", description="5xx spike")
    evidence = [
        Evidence(
            provider="dynatrace",
            kind=EvidenceKind.LOG,
            summary="NullPointerException in OrderHandler",
            payload={"Authorization": "Bearer super-secret-token"},
            occurrences=12,
        )
    ]

    prompt = build_prompt(incident, evidence, SURFACE)

    assert "NullPointerException in OrderHandler" in prompt.user
    assert "seen x12" in prompt.user
    # The prompt leaves this process for a vendor: raw payloads stay home.
    assert "super-secret-token" not in prompt.user
    assert "super-secret-token" not in prompt.system


def test_prompt_declares_every_capability_and_its_constraints() -> None:
    prompt = build_prompt(Incident(id="PD-2", title="order-handler will not boot"), [], SURFACE)

    assert "restart_workload" in prompt.user
    assert "-n checkout" in prompt.user
    # The integration capability travels too: the model is not told this is an
    # on-call tool.
    assert "rerun_pipeline" in prompt.user
    assert "deploy.yml" in prompt.user
    # And a read-only capability is labelled as such.
    assert "inspect_workload" in prompt.user
    assert "(read-only" in prompt.user


def test_prompt_says_the_evidence_is_final() -> None:
    # The model cannot gather more information: it has no tools and no turns. A
    # prompt that leaves that implied invites "read the logs" as a plan step.
    prompt = build_prompt(Incident(id="PD-3", title="checkout is down"), [], SURFACE)

    assert "evidence is final" in prompt.system
    assert "changes the system" in prompt.system


def test_the_declaration_parses_into_capabilities() -> None:
    settings = Settings(
        remediation_capabilities=(
            "restart_workload|kubectl|rollout,scale|-n checkout|change|restart one workload;"
            "inspect_workload|kubectl|get,logs|-n checkout|read|look at one workload;"
            "rerun_pipeline|gh|workflow|deploy.yml||re-run the pipeline"
        )
    )

    surface = settings.remediation_surface

    assert [capability.id for capability in surface.capabilities] == [
        "restart_workload",
        "inspect_workload",
        "rerun_pipeline",
    ]
    assert surface.find("inspect_workload").effect == "read"
    # An empty effect means "change": the common case is an action.
    assert surface.find("rerun_pipeline").effect == "change"
    assert surface.find("rerun_pipeline").must_contain == ("deploy.yml",)
    assert surface.find("restart_workload").verbs == ("rollout", "scale")


def test_a_malformed_declaration_fails_loudly() -> None:
    # A typo must stop the worker, not produce runs that plan nothing.
    settings = Settings(remediation_capabilities="restart_workload")

    with pytest.raises(ValueError, match="REMEDIATION_CAPABILITIES"):
        _ = settings.remediation_surface


def test_an_unknown_effect_fails_loudly() -> None:
    settings = Settings(remediation_capabilities="restart|kubectl|||write|not a real effect")

    with pytest.raises(ValueError, match="effect"):
        _ = settings.remediation_surface


# --- the node ---------------------------------------------------------------


def _deps(approval: Any, llm: Any, surface: RemediationSurface = SURFACE) -> CoreDeps:
    return CoreDeps(approval=approval, llm=llm, surface=surface)


async def test_plan_records_what_the_model_proposed(
    state: dict, approval_gateway: Any, llm: Any
) -> None:
    llm.answers = [GOOD_ANSWER]

    result = await nodes.plan(state, _deps(approval_gateway, llm))

    assert result["phase"] == Phase.PLANNING
    assert result["plan"].steps[0].target == "checkout"
    assert result["plan"].steps[0].capability == "restart_workload"
    assert result["hypothesis"].confidence == 0.7
    assert "stub-llm" in result["notes"][0]


async def test_plan_degrades_without_a_model(state: dict, approval_gateway: Any) -> None:
    result = await nodes.plan(state, _deps(approval_gateway, None))

    assert result["plan"] is None
    assert "make llm-login" in result["notes"][0]
    # The reason travels as evidence, so the report a human reads explains it.
    assert result["evidence"][0].kind == EvidenceKind.NOTE


async def test_plan_degrades_when_no_capability_is_declared(
    state: dict, approval_gateway: Any, llm: Any
) -> None:
    llm.answers = [GOOD_ANSWER]

    result = await nodes.plan(state, _deps(approval_gateway, llm, RemediationSurface()))

    assert result["plan"] is None
    assert "REMEDIATION_CAPABILITIES" in result["notes"][0]
    # It never spent a call: there was nothing usable to ask for.
    assert llm.prompts == []


async def test_plan_degrades_when_the_model_cannot_answer(
    state: dict, approval_gateway: Any, llm: Any
) -> None:
    llm.error = LlmUnavailable("stub-llm", "the seat token expired")

    result = await nodes.plan(state, _deps(approval_gateway, llm))

    assert result["plan"] is None
    assert "seat token expired" in result["notes"][0]


async def test_plan_spends_exactly_one_repair_attempt(
    state: dict, approval_gateway: Any, llm: Any
) -> None:
    llm.answers = ["not json at all", "still not json"]

    result = await nodes.plan(state, _deps(approval_gateway, llm))

    assert result["plan"] is None
    assert len(llm.prompts) == 2
    # The repair prompt says what was wrong, or it is just a blind retry.
    assert "rejected" in llm.prompts[1][1]


async def test_plan_recovers_on_the_repair_attempt(
    state: dict, approval_gateway: Any, llm: Any
) -> None:
    llm.answers = ["I cannot produce JSON", GOOD_ANSWER]

    result = await nodes.plan(state, _deps(approval_gateway, llm))

    assert result["plan"] is not None
    assert len(llm.prompts) == 2


async def test_plan_retries_an_empty_plan_once_then_degrades(
    state: dict, approval_gateway: Any, llm: Any
) -> None:
    llm.answers = [json.dumps(_payload(steps=[], summary="nothing safe to do"))] * 2

    result = await nodes.plan(state, _deps(approval_gateway, llm))

    assert result["plan"] is None
    assert len(llm.prompts) == 2
    assert "no steps" in result["notes"][0]


async def test_plan_recovers_when_the_retry_supplies_the_step(
    state: dict, approval_gateway: Any, llm: Any
) -> None:
    llm.answers = [json.dumps(_payload(steps=[], summary="restart the deployment")), GOOD_ANSWER]

    result = await nodes.plan(state, _deps(approval_gateway, llm))

    assert result["plan"] is not None
    assert len(llm.prompts) == 2
