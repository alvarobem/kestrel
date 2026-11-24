"""Reading a sentence a person wrote: the question, the parse, the guard rails.

A report typed in the operator's own words ("yesterday the java-micronaut service
would not start") is not an incident yet. The investigation joins telemetry on
`entity_ids` (provider-side handles) or on `affected_entities` (display names), so
a run carrying neither reads nothing — and a run that reads nothing still looks
like an investigation in the report. Turning words into those fields is a model's
job; deciding which of its answers are usable is ours.

Three jobs, together because they share one vocabulary — what a report said:

  * `build_intake_prompt` decides what the model is asked and what it may not do:
    extract what is written, never invent a service, never compute a date from
    "yesterday". The incident's own sentence is the only input; nothing else
    leaves this process.
  * `parse_intake` turns the answer into fields, or refuses with a typed error.
  * the normalizers below are the guard rails: an unknown severity degrades to
    `unknown` instead of failing the run, and an entity list is stripped,
    deduplicated and bounded so it can be used as a query literal.

The rules here are pure: no I/O, no clock, no settings. That is what lets a test
pin them without a model, a network or an environment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kestrel.application.errors import IntakeUnusable
from kestrel.domain.models import Incident, Severity

#: Provenance of a report a person typed. It is half of the contract with the
#: inbound mapper (`adapters/inbound/natural_language.py`): `human` plus
#: `raw["text"]` is what makes an incident a report to be read, and nothing else
#: is. This constant is the shared half; the mapper writes the other one.
REPORT_SOURCE = "human"

#: What the model is told it is doing. Every rule is here because the opposite
#: answer produces a run that reads the wrong tenant slice or nothing at all.
SYSTEM_PROMPT = (
    "You are the intake step of an incident-response agent. You receive one "
    "incident report a person wrote in their own words, and you return the fields "
    "the investigation needs.\n"
    "Rules:\n"
    "- Extract only what the report says. Never invent a service, component, "
    "environment or symptom.\n"
    "- `affected_entities` holds the exact names of the services or components the "
    "reporter named, as identifiers, not as sentences: if they wrote 'the "
    "java-micronaut service', the entity is `java-micronaut`. Return an empty list "
    "when the report names none — an empty list is a useful answer, a guessed name "
    "is not.\n"
    "- `severity` is one of: availability (it was down or would not start), error "
    "(it failed), performance (it was slow), resource_contention (a resource was "
    "exhausted), custom, unknown. Use `unknown` when the report does not say.\n"
    "- `description` is one line describing the symptom, in the reporter's own "
    "terms. Keep relative times as written ('yesterday', 'last night'): do not "
    "convert them to a date.\n"
    "- Reply with a single JSON object and nothing else."
)

#: Shape the answer must have. Spelled out for the model; enforced below.
SHAPE = """{
  "severity": "availability",
  "affected_entities": ["the names the reporter used, or an empty list"],
  "description": "one line: the symptom, in the reporter's own terms"
}"""

#: A report names one service, occasionally a handful. Ten entries is the model
#: writing prose, and every extra name widens the telemetry query for no gain.
MAX_ENTITIES = 5
#: Long enough for a real service name, short enough that a sentence fails the
#: length check instead of becoming a query literal that matches nothing.
MAX_ENTITY_LENGTH = 80
#: `checkout`, `java-micronaut`, `order handler v2` are names. Anything longer is
#: the model reporting a sentence into the field: `service.name == "the java-
#: micronaut service was down"` matches nothing, and a run that carries it does
#: not even record that it had no target.
MAX_ENTITY_WORDS = 6
MAX_DESCRIPTION = 300


class Intake(BaseModel):
    """What a report said, in the vocabulary the investigation uses."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity = Severity.UNKNOWN
    #: Display names of the entities the reporter named. Empty means "the report
    #: named none", which is recorded rather than filled in.
    affected_entities: list[str] = Field(default_factory=list)
    description: str = ""


class _Answer(BaseModel):
    """The model's raw answer, kept permissive so one odd field cannot lose the run."""

    model_config = ConfigDict(extra="ignore")

    severity: str = ""
    affected_entities: list[str] = Field(default_factory=list)
    description: str = ""


@dataclass(frozen=True, slots=True)
class IntakePrompt:
    """The two halves of the question, kept separate so a trace can show both."""

    system: str
    user: str


def build_intake_prompt(incident: Incident, *, limit: int = 2000) -> IntakePrompt:
    """The question, built from the reporter's own words.

    `raw["text"]` is the sentence as typed; `title` and `description` are the same
    words after normalization. The text is clipped because it crosses a process
    and a vendor boundary — a pasted stack trace is not a report, and its tail is
    the part that carries tokens.
    """
    text = reported_text(incident) or incident.title
    user = (
        "REPORT\n" + _clip(text, limit) + "\n\n"
        "REQUIRED SHAPE\n" + SHAPE + "\n\n"
        "Return the JSON object now."
    )
    return IntakePrompt(system=SYSTEM_PROMPT, user=user)


def parse_intake(answer: str) -> Intake:
    """The answer as fields, or a typed refusal.

    Refuses only what cannot be used at all: not a JSON object, or a shape that
    contradicts the contract. A severity this system does not know, or a service
    name that came back as a sentence, are normalized instead — those are the
    model's imprecision, not a reason to lose the incident.
    """
    payload = _json_object(answer)
    try:
        parsed = _Answer.model_validate(payload)
    except ValidationError as exc:
        raise IntakeUnusable(_first_error(exc)) from exc

    return Intake(
        severity=_severity(parsed.severity),
        affected_entities=_entities(parsed.affected_entities),
        description=_clip(" ".join(parsed.description.split()), MAX_DESCRIPTION),
    )


def _severity(value: str) -> Severity:
    """A known severity, or `unknown`. The report is not rejected over a label."""
    normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
    try:
        return Severity(normalized)
    except ValueError:
        return Severity.UNKNOWN


def _entities(values: list[str]) -> list[str]:
    """Useful, non-repeating names in the order the report named them.

    A name longer than a service name is prose — `the service that was down` would
    become a query literal that matches nothing — so it is dropped rather than
    passed on. Deduplication is case-insensitive: one service has one name, however
    the model capitalized it.
    """
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = " ".join(str(value).split())
        if not name or len(name) > MAX_ENTITY_LENGTH or len(name.split()) > MAX_ENTITY_WORDS:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= MAX_ENTITIES:
            break
    return names


def _json_object(answer: str) -> dict:
    """The first JSON object in the text, fences and prose tolerated."""
    start = answer.find("{")
    end = answer.rfind("}")
    if start == -1 or end <= start:
        raise IntakeUnusable("the answer contains no JSON object")
    try:
        payload = json.loads(answer[start : end + 1])
    except json.JSONDecodeError as exc:
        raise IntakeUnusable(f"the answer is not valid JSON ({exc.msg})") from exc
    if not isinstance(payload, dict):
        raise IntakeUnusable("the answer is not a JSON object")
    return payload


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "the answer does not match the required shape"
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"]) or "the answer"
    return f"{where}: {first['msg']}"


def reported_text(incident: Incident) -> str | None:
    """The words a person typed, when this incident came in as a report.

    Both halves are the contract with `adapters/inbound/natural_language.py`: a
    report names `human` as its provenance and carries the sentence verbatim in
    `raw["text"]`. The source check matters — a vendor payload that happens to
    carry a `text` field is still a vendor payload, and spending a model call to
    read telemetry as prose would be a silent waste.
    """
    if incident.source != REPORT_SOURCE:
        return None
    raw = incident.raw if isinstance(incident.raw, dict) else {}
    text = raw.get("text")
    return text if isinstance(text, str) and text.strip() else None


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
