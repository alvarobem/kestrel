"""Turning evidence into a plan: the question, the parse, and the guard rails.

Three jobs, together because they share one vocabulary — a plan a human is asked
to authorize:

  * `build_prompt` decides what the model is asked and, more importantly, what it
    is not: it sends the incident and the *summaries* of the ranked evidence,
    never the raw payloads. A prompt leaves this process for a third party, and a
    log line can carry a token, a customer id or a stack trace with credentials.
  * `parse_plan` turns text into the domain shape, or refuses. Models wrap JSON in
    prose and fences, and a remediation plan is not something to guess at.
  * `require_usable` is the guard rail the roadmap promised: every step either can
    be undone or carries a rollback, and nothing addresses "everything". A step
    that deletes a namespace is a plan nobody can review.

The rules here are pure: no I/O, no clock, no settings. That is what lets a test
pin the guard rail without a model, a network or an environment.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass

from pydantic import ValidationError

from kestrel.application.errors import PlanUnusable
from kestrel.domain.evidence import select_top
from kestrel.domain.models import Evidence, Incident, RemediationPlan

#: What the model is told it is doing. The rules are the product's, not the
#: prompt's: each one exists because a plan that breaks it is unusable at the
#: approval gate, not because it reads well.
SYSTEM_PROMPT = (
    "You are the planning step of an incident-response agent for a production "
    "system. You receive one incident and the evidence gathered about it, and you "
    "return a hypothesis and a remediation plan.\n"
    "Rules:\n"
    "- Justify the plan with the evidence you were given. Cite the items you rely "
    "on in `supporting_evidence`. Do not invent facts.\n"
    "- Every command names one specific target: no wildcards, no `--all`, no "
    "`-A`. A plan that addresses everything is a plan nobody can review.\n"
    "- A step that cannot be undone must carry a `rollback_command`. If you cannot "
    "write the rollback, do not propose the step.\n"
    "- Never put a credential, token or password in a command.\n"
    "- Prefer the least invasive action that could fix the cause.\n"
    "- If the evidence does not support any action, return an empty `steps` list "
    "and explain why in `summary`. That is a valid and useful answer.\n"
    "- Reply with a single JSON object and nothing else."
)

#: Shape the answer must have. Spelled out for the model; enforced by pydantic.
SHAPE = """{
  "summary": "one line: what this plan does",
  "hypothesis": {
    "statement": "the cause you believe",
    "confidence": 0.0,
    "supporting_evidence": ["which evidence items support it"],
    "falsified_by": "what observation would prove you wrong"
  },
  "steps": [
    {
      "order": 1,
      "description": "what this step does",
      "command": "the exact shell command",
      "target": "the single entity it touches",
      "reversible": true,
      "risk": "low",
      "rollback_command": "the command that undoes it"
    }
  ],
  "blast_radius": "what else could be affected"
}"""

#: Evidence items shown to the model. The approval gate shows five to a human;
#: the model gets a few more because it has to reason, not decide.
EVIDENCE_LIMIT = 8


@dataclass(frozen=True, slots=True)
class PlanPrompt:
    """The two halves of the question, kept separate so a trace can show both."""

    system: str
    user: str


def build_prompt(
    incident: Incident, evidence: list[Evidence], *, limit: int = EVIDENCE_LIMIT
) -> PlanPrompt:
    """The question, built from what the run already knows.

    Only `summary` travels from each evidence item. `payload` stays home on
    purpose: it holds raw log lines and provider records, and this text is about
    to cross a process and a vendor boundary.
    """
    incident_block = [
        f"id: {incident.id}",
        f"title: {incident.title}",
        f"severity: {incident.severity.value}",
        f"entities: {', '.join(incident.affected_entities) or 'not named'}",
    ]
    if incident.description:
        incident_block.append(f"description: {_clip(incident.description, 600)}")

    ranked = select_top(evidence, incident, limit=limit)
    if ranked:
        evidence_block = [
            f"{index}. [{item.kind}] {item.summary}"
            + (f" (seen x{item.occurrences})" if item.occurrences > 1 else "")
            for index, item in enumerate(ranked, start=1)
        ]
    else:
        evidence_block = ["(no evidence was gathered: plan from the incident alone, or state "
                          "that you cannot)"]

    user = (
        "INCIDENT\n" + "\n".join(incident_block) + "\n\n"
        "EVIDENCE (ranked, most useful first)\n" + "\n".join(evidence_block) + "\n\n"
        "REQUIRED SHAPE\n" + SHAPE + "\n\n"
        "Return the JSON object now."
    )
    return PlanPrompt(system=SYSTEM_PROMPT, user=user)


def repair_tail(problem: str) -> str:
    """Appended to the prompt for the single repair attempt."""
    return (
        f"\n\nYour previous answer was rejected: {problem}\n"
        "Return only the corrected JSON object."
    )


def parse_plan(answer: str) -> RemediationPlan:
    """The answer as a plan, or a typed refusal.

    A model that answers "I cannot help with that" is not a bug; it is an answer
    this cannot use, and the caller degrades. What it must never do is produce a
    half-parsed plan with invented defaults.
    """
    payload = _json_object(answer)
    try:
        plan = RemediationPlan.model_validate(payload)
    except ValidationError as exc:
        raise PlanUnusable(_first_error(exc)) from exc

    if not plan.steps:
        # A legitimate answer, not a malformed one: retrying would repeat it and
        # cost another call. The model's own explanation is the useful part.
        raise PlanUnusable(
            f"the evidence did not support any action: {_clip(plan.summary, 200)}",
            repairable=False,
        )

    # The model does not decide whether a human is asked. It also does not decide
    # the numbering: the order is what the executor will follow.
    ordered = [
        step.model_copy(update={"order": index})
        for index, step in enumerate(sorted(plan.steps, key=lambda s: s.order), start=1)
    ]
    return plan.model_copy(update={"steps": ordered, "requires_approval": True})


def require_usable(plan: RemediationPlan) -> None:
    """Refuse a plan that no reviewer could approve with confidence."""
    problems = _problems(plan)
    if problems:
        raise PlanUnusable("; ".join(problems))


def _problems(plan: RemediationPlan) -> list[str]:
    problems: list[str] = []
    for step in plan.steps:
        if not step.target.strip():
            problems.append(f"step {step.order} names no target")
        tokens = _tokens(step.command)
        if "*" in step.command:
            problems.append(f"step {step.order} uses a wildcard")
        elif any(token == "-A" or token.startswith("--all") for token in tokens):
            problems.append(f"step {step.order} addresses every object, not one")
        if not step.reversible and not (step.rollback_command or "").strip():
            problems.append(
                f"step {step.order} cannot be undone and carries no rollback command"
            )
    return problems


def _tokens(command: str) -> list[str]:
    """Shell tokens, so a flag is a flag and not a substring of one."""
    try:
        return shlex.split(command)
    except ValueError:  # unbalanced quotes: fall back to a naive split
        return command.split()


def _json_object(answer: str) -> dict:
    """The first JSON object in the text, fences and prose tolerated."""
    start = answer.find("{")
    end = answer.rfind("}")
    if start == -1 or end <= start:
        raise PlanUnusable("the answer contains no JSON object")
    try:
        payload = json.loads(answer[start : end + 1])
    except json.JSONDecodeError as exc:
        raise PlanUnusable(f"the answer is not valid JSON ({exc.msg})") from exc
    if not isinstance(payload, dict):
        raise PlanUnusable("the answer is not a JSON object")
    return payload


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "the plan does not match the required shape"
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"]) or "the plan"
    return f"{where}: {first['msg']}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
