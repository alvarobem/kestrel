"""Use cases: the incident lifecycle, one function per step.

Every node runs as a Temporal activity, so it must be async and it must not
mutate the state it receives: take the state, return a partial update.

What this module may not do is the point of the layering: no `get_settings()`,
no `httpx`, no `interrupt()`. Whatever a node needs from the outside arrives as
a port or a frozen policy value in `CoreDeps` (`application/deps.py`). That is
what lets a test drive the whole lifecycle with a fake approval channel and a
fake executor.

The wiring is real and runnable end to end. `plan` reasons with the injected
model; `triage`, `verify` and `report` are still explicit stubs. Keep the guard
rails: read-only investigation, approval before any non-dry-run action, and one
audit record per executed step.
"""

from __future__ import annotations

from kestrel.application.deps import CoreDeps
from kestrel.application.errors import (
    IntakeUnusable,
    LlmUnavailable,
    PlanUnusable,
    TelemetryUnavailable,
)
from kestrel.application.intake import build_intake_prompt, parse_intake, reported_text
from kestrel.application.planner import build_prompt, parse_plan, repair_tail, require_usable
from kestrel.application.ports import LlmClient
from kestrel.application.state import IncidentState
from kestrel.domain.evidence import dedupe, merge_repeated, select_top
from kestrel.domain.models import (
    ActionRecord,
    Approval,
    Evidence,
    EvidenceKind,
    Incident,
    IncidentReport,
    Phase,
    RemediationPlan,
    StepActor,
    Verification,
)
from kestrel.domain.policy import (
    DEFAULT_MAX_INVESTIGATION_ATTEMPTS,
    DEFAULT_MAX_REMEDIATION_ATTEMPTS,
    RemediationSurface,
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

    A report a person typed arrives as words and nothing else: no entity ids, no
    severity, no display names. The investigation joins telemetry on those fields,
    so a run that skips this step reads nothing and says so three nodes later —
    which is exactly the failure this node exists to prevent. The model reads the
    report once here, and its answer is guard-railed in `application/intake.py`.

    Degrading is deliberate: no model, an unusable answer and a report that names
    no service are all conditions of the world. Each one is recorded, and the
    reporter's own words stay the incident.

    TODO(node): read the ownership map (management zones / tags) as well, so a
    triaged severity also routes to the team that owns the entity.
    """
    incident = state["incident"]
    text = reported_text(incident)
    if text is not None:
        return await _triage_report(incident, text, deps)

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


async def _triage_report(incident: Incident, text: str, deps: CoreDeps) -> dict:
    """Read a human report into the fields the investigation needs."""
    llm = deps.llm
    if llm is None:
        return _triage_degraded(
            "no model is authorized for this run: the report keeps the reporter's words "
            "and names no service, so there is no telemetry target"
        )

    prompt = build_intake_prompt(incident)
    try:
        intake = parse_intake(await llm.complete(system=prompt.system, user=prompt.user))
    except LlmUnavailable as exc:
        return _triage_degraded(f"{llm.model_id} could not read the report: {exc.detail}")
    except IntakeUnusable as exc:
        return _triage_degraded(f"the model's reading of the report is unusable: {exc.detail}")

    # The enrichment travels in state: `investigate` joins telemetry on
    # `affected_entities`, and an empty list is the honest answer when the report
    # named nothing — filled in with a guess it would silently read the wrong slice.
    enriched = incident.model_copy(
        update={
            "severity": intake.severity,
            "affected_entities": intake.affected_entities or incident.affected_entities,
            "description": intake.description or incident.description,
        }
    )
    notes = [
        f"report read by {llm.model_id}: severity={intake.severity.value}, "
        f"entities={', '.join(intake.affected_entities) or 'none named'}"
    ]
    if not intake.affected_entities:
        notes.append(
            "the report names no service: the investigation has no target until one is supplied"
        )
    return {
        "phase": Phase.TRIAGING,
        "incident": enriched,
        "evidence": [
            Evidence(
                provider="kestrel",
                kind=EvidenceKind.NOTE,
                summary=f"triage: {intake.description or text}",
                confidence=0.6 if intake.affected_entities else 0.3,
            )
        ],
        "notes": notes,
    }


def _triage_degraded(reason: str) -> dict:
    """A report nobody could read: recorded, never invented away."""
    return {
        "phase": Phase.TRIAGING,
        "evidence": [
            Evidence(
                provider="kestrel",
                kind=EvidenceKind.NOTE,
                summary=f"triage degraded: {reason}",
                confidence=0.0,
            )
        ],
        "notes": [f"triage degraded: {reason}"],
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
            "investigation_degraded": False,
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
        degraded = True
    else:
        degraded = False

    # One read, so summing repeats is honest: twelve identical lines are one
    # finding about volume, and state is checkpointed after every node.
    found = merge_repeated(found)

    if not found:
        reason = reason or (
            f"{provider.provider_id} returned no evidence for this incident inside the run scope"
        )
        return {
            "phase": Phase.INVESTIGATING,
            "investigation_attempts": attempts,
            "investigation_degraded": degraded,
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
        "investigation_degraded": degraded,
        "evidence": found,
        "notes": [f"{provider.provider_id} returned {len(found)} evidence item(s)"],
    }


async def plan(state: IncidentState, deps: CoreDeps) -> dict:
    """Turn evidence into a hypothesis and a remediation plan.

    The model proposes; this node disposes. Everything crossing that boundary is
    shape-checked and guard-railed in `application/planner.py`, and a proposal
    that does not survive is never shown to a human: `plan` degrades to no plan,
    the run goes straight to `report`, and the reason travels as evidence.

    Degrading instead of failing is deliberate. A model that does not answer, a
    seat that is not signed in and an answer that is not JSON are all conditions
    of the world, and none of them is a reason to lose the incident.

    The retry budget is one, not a loop: a model that produced an unusable answer
    twice produces a third, and each attempt is a paid call. That includes an empty
    plan — the same prompt returned no steps once and a valid plan a minute later,
    so "no steps" is not proof that nothing fits.
    """
    incident = state["incident"]
    evidence = dedupe(state.get("evidence") or [])
    surface = deps.surface
    if not surface.declared:
        # Without a declared vocabulary there is no honest command to write. The
        # model would either refuse (the good case) or invent a deployment name
        # (the case this guard exists for).
        return _no_plan(
            "no remediation capability is declared: set REMEDIATION_CAPABILITIES, "
            "or any command would be a guess"
        )

    llm = deps.llm
    if llm is None:
        return _no_plan(
            "no model is authorized for this run: sign in the seat with `make llm-login`, "
            "or configure one"
        )

    prompt = build_prompt(incident, evidence, surface)
    try:
        proposed = await _propose(llm, prompt.system, prompt.user, surface)
    except LlmUnavailable as exc:
        return _no_plan(f"{llm.model_id} could not answer: {exc.detail}")
    except PlanUnusable as exc:
        if not exc.repairable:
            return _no_plan(exc.detail)
        try:
            proposed = await _propose(
                llm, prompt.system, prompt.user + repair_tail(exc.detail), surface
            )
        except LlmUnavailable as retry_exc:
            return _no_plan(f"{llm.model_id} could not answer: {retry_exc.detail}")
        except PlanUnusable as retry_exc:
            return _no_plan(f"the model produced an unusable plan: {retry_exc.detail}")

    return {
        "phase": Phase.PLANNING,
        "hypothesis": proposed.hypothesis,
        "plan": proposed,
        "notes": [
            f"plan proposed by {llm.model_id}: {len(proposed.steps)} step(s), "
            f"hypothesis confidence={proposed.hypothesis.confidence:.2f}"
        ],
    }


async def _propose(
    llm: LlmClient, system: str, user: str, surface: RemediationSurface
) -> RemediationPlan:
    """One round trip: ask, parse, guard-rail. Raises rather than guessing."""
    plan = parse_plan(await llm.complete(system=system, user=user))
    require_usable(plan, surface)
    return plan


def _no_plan(reason: str) -> dict:
    """No plan on the table: report, instead of asking a human to approve nothing."""
    return {
        "phase": Phase.PLANNING,
        "hypothesis": None,
        "plan": None,
        "evidence": [
            Evidence(
                provider="kestrel",
                kind=EvidenceKind.NOTE,
                summary=f"planning degraded: {reason}",
                confidence=0.0,
            )
        ],
        "notes": [f"plan degraded: {reason}"],
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

    incident = state["incident"]
    evidence = _top_evidence(dedupe(state.get("evidence") or []), incident)
    decision = deps.approval.await_decision(
        incident_id=incident.id, plan=plan, evidence=evidence
    )
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
    agent_steps = [step for step in plan.steps if step.actor is StepActor.AGENT]
    human_steps = [step for step in plan.steps if step.actor is StepActor.HUMAN]

    # A step the agent cannot perform is still part of the plan, and the audit
    # trail has to say so: recording it as executed would be a lie, and dropping it
    # would hide the work the human is now responsible for.
    handed_over = [
        ActionRecord(
            step_order=step.order,
            command=step.description,
            executed=False,
            actor=StepActor.HUMAN,
            note="assigned to a person; this system does not perform it",
        )
        for step in human_steps
    ]

    if deps.dry_run:
        rendered = [
            ActionRecord(
                step_order=step.order,
                command=step.command or "",
                executed=False,
                note="dry run: rendered, not executed",
            )
            for step in agent_steps
        ]
        return {
            "phase": Phase.EXECUTING,
            "actions": _ordered(rendered + handed_over),
            "remediation_attempts": attempts,
            "notes": [
                f"dry run: {len(agent_steps)} command(s) rendered, "
                f"{len(human_steps)} step(s) left to a person"
            ],
        }

    if agent_steps and deps.executor is None:
        # Recording exit_code=0 here would write a fake success into the audit
        # trail. Fail closed instead. Human steps alone do not need an executor.
        return {
            "phase": Phase.FAILED,
            "remediation_attempts": attempts,
            "notes": ["execute blocked: no sandbox executor is wired and DRY_RUN is false"],
        }

    executed = [await deps.executor.execute(step) for step in agent_steps] if agent_steps else []
    return {
        "phase": Phase.EXECUTING,
        "actions": _ordered(executed + handed_over),
        "remediation_attempts": attempts,
        "notes": [
            f"{len(executed)} step(s) executed through the sandbox executor, "
            f"{len(human_steps)} handed to a person"
        ],
    }


def _ordered(records: list[ActionRecord]) -> list[ActionRecord]:
    """Steps are a sequence: the audit trail reads in execution order."""
    return sorted(records, key=lambda record: record.step_order)


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
        f"{a.step_order}. {('[human] ' if a.actor is StepActor.HUMAN else '')}{a.command}"
        f" (executed={a.executed})"
        for a in state.get("actions", [])
    ]
    hypothesis = state.get("hypothesis")
    # Attempts re-read overlapping windows, so collapse by signature without
    # summing counts before deciding what a human should read first.
    evidence = dedupe(state.get("evidence") or [])
    top = _top_evidence(evidence, incident)
    return {
        "phase": Phase.RESOLVED if resolved else Phase.FAILED,
        "report": IncidentReport(
            incident_id=incident.id,
            # The evidence count is in the summary on purpose: it is how a human
            # reads from the CLI whether the investigation actually found anything
            # or merely ran.
            summary=(
                f"{incident.title}: {len(evidence)} evidence item(s), "
                f"{len(actions)} action(s), resolved={resolved}"
            ),
            root_cause=hypothesis.statement if hypothesis else None,
            actions_taken=actions,
            top_evidence=top,
            resolved=resolved,
            follow_ups=["wire real verification and the sandbox executor"],
        ),
        "notes": [f"report generated (resolved={resolved})"],
    }


def _top_evidence(evidence: list[Evidence], incident, *, limit: int = 5) -> list[str]:
    """The short lines a human reads first — in the report and at the approval gate."""
    return [
        f"[{item.kind}] {item.summary}" + (f" (x{item.occurrences})" if item.occurrences > 1 else "")
        for item in select_top(evidence, incident, limit=limit)
    ]


# --- routing ---------------------------------------------------------------
# Conditional edge functions run inside the Temporal workflow, so they must be
# async and deterministic: no I/O, no clock, no randomness. They take the state
# and nothing else, so they cannot grow a dependency even by accident.


async def route_after_investigation(state: IncidentState) -> str:
    """Retry only what a retry can fix.

    The investigation is a bounded, deterministic read: re-running it after an
    empty answer re-reads the same window, in the same second, and its only
    effect is to burn the same scan again (three identical attempts, twice over,
    in the live runs). A retry earns its place when the provider could not answer
    at all — that is `investigation_degraded`, and it is the only reason to loop.
    The attempt bound still applies, so a provider that stays down cannot spin.
    """
    if not state.get("investigation_degraded"):
        return "plan"
    attempts = state.get("investigation_attempts", 0)
    limit = state.get("max_investigation_attempts", DEFAULT_MAX_INVESTIGATION_ATTEMPTS)
    return "investigate" if attempts < limit else "plan"


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
