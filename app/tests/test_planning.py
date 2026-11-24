"""The planning step: what the model is asked, and what survives its answer.

Two kinds of test, and the split is the point. The rules — prompt building, JSON
parsing, guard rails — are pure and need no model at all; the node is driven
through the `LlmClient` port with a stub that records what it was asked. Nothing
here reaches a provider, and nothing depends on the process environment.

The guard-rail tests are the ones that matter on a bad day: they are the reason a
model's answer can never put an unreviewable command in front of a human.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kestrel.application import nodes
from kestrel.application.deps import CoreDeps
from kestrel.application.errors import LlmUnavailable, PlanUnusable
from kestrel.application.planner import build_prompt, parse_plan, require_usable
from kestrel.domain.models import Evidence, EvidenceKind, Incident, Phase


def _step(**overrides: Any) -> dict:
    step = {
        "order": 1,
        "description": "restart the deployment",
        "command": "kubectl rollout restart deploy/checkout",
        "target": "checkout",
        "reversible": True,
        "risk": "low",
        "rollback_command": "kubectl rollout undo deploy/checkout",
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


# --- the answer becomes a plan, or it does not ------------------------------


def test_parse_ignores_the_prose_and_fences_around_the_json() -> None:
    answer = f"Sure. Here is the plan:\n\n```json\n{GOOD_ANSWER}\n```\n\nLet me know."

    plan = parse_plan(answer)

    assert plan.steps[0].command == "kubectl rollout restart deploy/checkout"
    assert plan.hypothesis.confidence == 0.7


def test_parse_forces_the_approval_the_model_tried_to_skip() -> None:
    # The model does not decide whether a human is asked. That is policy.
    plan = parse_plan(json.dumps(_payload(requires_approval=False)))

    assert plan.requires_approval is True


def test_parse_renumbers_the_steps_in_order() -> None:
    answer = json.dumps(
        _payload(
            steps=[
                _step(order=9, command="kubectl rollout status deploy/checkout"),
                _step(order=2, command="kubectl rollout restart deploy/checkout"),
            ]
        )
    )

    plan = parse_plan(answer)

    assert [step.order for step in plan.steps] == [1, 2]
    # The model's numbering is a hint; the normalized order is what the executor
    # follows, so step 1 is the one the model marked as happening first.
    assert plan.steps[0].command == "kubectl rollout restart deploy/checkout"
    assert plan.steps[1].command == "kubectl rollout status deploy/checkout"


def test_parse_refuses_text_that_is_not_json() -> None:
    with pytest.raises(PlanUnusable) as caught:
        parse_plan("I cannot help with that.")

    assert caught.value.repairable is True


def test_parse_treats_an_empty_plan_as_an_answer_not_a_defect() -> None:
    answer = json.dumps(_payload(steps=[], summary="the evidence shows a healthy service"))

    with pytest.raises(PlanUnusable) as caught:
        parse_plan(answer)

    # Not repairable: asking again would repeat the same answer and cost another
    # call. The model's explanation is what the human needs to read.
    assert caught.value.repairable is False
    assert "healthy service" in caught.value.detail


# --- the guard rails --------------------------------------------------------


def _usable(payload: dict) -> None:
    require_usable(parse_plan(json.dumps(payload)))


def test_guard_rail_refuses_a_wildcard() -> None:
    with pytest.raises(PlanUnusable, match="wildcard"):
        _usable(_payload(steps=[_step(command="kubectl delete pods -l app=*")]))


def test_guard_rail_refuses_a_command_that_addresses_everything() -> None:
    with pytest.raises(PlanUnusable, match="every object"):
        _usable(_payload(steps=[_step(command="kubectl rollout restart deploy --all")]))


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
                    command="kubectl delete pod checkout-abc123",
                    rollback_command="kubectl rollout undo deploy/checkout",
                )
            ]
        )
    )


def test_guard_rail_refuses_a_step_without_a_target() -> None:
    with pytest.raises(PlanUnusable, match="names no target"):
        _usable(_payload(steps=[_step(target="  ")]))


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

    prompt = build_prompt(incident, evidence)

    assert "NullPointerException in OrderHandler" in prompt.user
    assert "seen x12" in prompt.user
    # The prompt leaves this process for a vendor: raw payloads stay home.
    assert "super-secret-token" not in prompt.user
    assert "super-secret-token" not in prompt.system


# --- the node ---------------------------------------------------------------


def _deps(approval: Any, llm: Any) -> CoreDeps:
    return CoreDeps(approval=approval, llm=llm)


async def test_plan_records_what_the_model_proposed(state: dict, approval_gateway: Any, llm: Any) -> None:
    llm.answers = [GOOD_ANSWER]

    result = await nodes.plan(state, _deps(approval_gateway, llm))

    assert result["phase"] == Phase.PLANNING
    assert result["plan"].steps[0].target == "checkout"
    assert result["hypothesis"].confidence == 0.7
    assert "stub-llm" in result["notes"][0]


async def test_plan_degrades_without_a_model(state: dict, approval_gateway: Any) -> None:
    result = await nodes.plan(state, _deps(approval_gateway, None))

    assert result["plan"] is None
    assert "make llm-login" in result["notes"][0]
    # The reason travels as evidence, so the report a human reads explains it.
    assert result["evidence"][0].kind == EvidenceKind.NOTE


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


async def test_plan_does_not_retry_an_empty_plan(
    state: dict, approval_gateway: Any, llm: Any
) -> None:
    llm.answers = [json.dumps(_payload(steps=[], summary="the service looks healthy"))]

    result = await nodes.plan(state, _deps(approval_gateway, llm))

    assert result["plan"] is None
    assert len(llm.prompts) == 1
    assert "the service looks healthy" in result["notes"][0]
