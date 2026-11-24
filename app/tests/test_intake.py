"""A report in a person's own words: its identity, its reading, and `triage`.

Three things are being proven here:

  * the words are the incident's identity, so a retried submission collides
    instead of starting a second remediation;
  * the model's answer is normalized, not trusted — a name that is prose, a
    severity nobody declared and a repeated entity all lose to the guard rails;
  * a run whose report cannot be read degrades with a recorded reason, and a
    structured incident never pays for a model call it does not need.
"""

from __future__ import annotations

from typing import Any

import pytest

from kestrel.adapters.inbound.natural_language import MAX_REPORT_CHARS, incident_from_text
from kestrel.application import nodes
from kestrel.application.deps import CoreDeps
from kestrel.application.errors import IntakeUnusable, LlmUnavailable
from kestrel.application.intake import build_intake_prompt, parse_intake
from kestrel.application.policy import natural_incident_id
from kestrel.application.state import initial_state
from kestrel.domain.models import Incident, Phase, Severity

#: The report the whole feature exists for: a sentence, a service, a day.
REPORT = "ayer no arrancaba el servicio java-micronaut"

READING = """Here is the JSON you asked for:
{
  "severity": "availability",
  "affected_entities": ["java-micronaut"],
  "description": "the java-micronaut service would not start yesterday"
}
"""


def _report_state(text: str = REPORT) -> dict:
    return dict(initial_state(incident_from_text(text)))


# --- the words are the identity --------------------------------------------


def test_the_same_words_are_the_same_incident() -> None:
    """A double-click, a capitalized retry or a trailing newline is one report."""
    first = natural_incident_id(REPORT)

    assert natural_incident_id(REPORT) == first
    assert natural_incident_id(f"  {REPORT.upper()}  ") == first
    assert natural_incident_id(REPORT + "\n") == first
    assert first.startswith("nl-")


def test_different_words_are_different_incidents() -> None:
    assert natural_incident_id(REPORT) != natural_incident_id("ayer no arrancaba java-micronaut")


def test_an_empty_report_has_no_identity() -> None:
    with pytest.raises(ValueError):
        natural_incident_id("   ")


def test_an_incident_from_text_keeps_the_words_and_names_a_person() -> None:
    incident = incident_from_text(REPORT)

    assert incident.source == "human"
    assert incident.id == natural_incident_id(REPORT)
    assert incident.description == REPORT
    # The sentence travels verbatim: it is the audit trail of what was said.
    assert incident.raw["text"] == REPORT
    assert "java-micronaut" in incident.title


def test_an_explicit_id_wins_over_the_derived_one() -> None:
    """A genuine re-report the day after is a different run, and says so."""
    assert incident_from_text(REPORT, "PD-77").id == "PD-77"


def test_an_empty_report_is_not_an_incident() -> None:
    with pytest.raises(ValueError):
        incident_from_text("  \n ")


def test_an_over_long_report_is_refused_before_it_becomes_state() -> None:
    """The sentence is re-serialized on every transition, so it has a ceiling."""
    with pytest.raises(ValueError) as excinfo:
        incident_from_text("x" * (MAX_REPORT_CHARS + 1))

    assert str(MAX_REPORT_CHARS) in str(excinfo.value)
    # Whitespace does not buy room: the limit is on the normalized words.
    longest_ok = incident_from_text("a " * (MAX_REPORT_CHARS // 2 - 1) + "a")
    assert len(longest_ok.description) <= MAX_REPORT_CHARS


def test_the_report_ceiling_is_inclusive() -> None:
    """The edge is accepted, so a later `>=` cannot pass this suite unnoticed."""
    exactly = "a" * MAX_REPORT_CHARS

    assert len(incident_from_text(exactly).description) == MAX_REPORT_CHARS
    with pytest.raises(ValueError):
        incident_from_text(exactly + "a")


# --- the model's reading is normalized, not trusted -------------------------


def test_parse_intake_reads_a_fenced_answer() -> None:
    intake = parse_intake(READING)

    assert intake.severity is Severity.AVAILABILITY
    assert intake.affected_entities == ["java-micronaut"]
    assert "would not start" in intake.description


def test_parse_intake_normalizes_a_severity_it_does_not_know() -> None:
    """A label this system never declared is a hint, not a reason to lose the run."""
    assert parse_intake('{"severity": "critical"}').severity is Severity.UNKNOWN
    assert parse_intake('{"severity": "Availability"}').severity is Severity.AVAILABILITY
    assert parse_intake("{}").severity is Severity.UNKNOWN


def test_parse_intake_drops_names_that_could_never_match_a_query() -> None:
    """A service name is an identifier; prose in that field reads nothing."""
    intake = parse_intake(
        '{"affected_entities": ["java-micronaut", "Java-Micronaut", "  ", '
        '"the service that would not start yesterday morning for a very long time indeed", '
        '"order handler v2"]}'
    )

    assert intake.affected_entities == ["java-micronaut", "order handler v2"]


def test_parse_intake_caps_the_entity_list() -> None:
    names = ", ".join(f'"service-{index}"' for index in range(20))

    assert len(parse_intake(f'{{"affected_entities": [{names}]}}').affected_entities) == 5


def test_parse_intake_refuses_an_answer_that_is_not_json() -> None:
    with pytest.raises(IntakeUnusable):
        parse_intake("I cannot help with that")


def test_parse_intake_refuses_a_shape_that_is_not_ours() -> None:
    with pytest.raises(IntakeUnusable):
        parse_intake('{"affected_entities": "java-micronaut"}')


def test_the_prompt_carries_the_report_and_the_shape() -> None:
    prompt = build_intake_prompt(incident_from_text(REPORT))

    assert REPORT in prompt.user
    assert "affected_entities" in prompt.user
    assert "Never invent a service" in prompt.system


# --- triage turns a report into fields the investigation can use ------------


async def test_triage_enriches_a_report_with_the_models_reading(
    llm: Any, approval_gateway: Any
) -> None:
    report = _report_state()
    original = report["incident"]
    llm.answers = [READING]

    result = await nodes.triage(report, CoreDeps(approval=approval_gateway, llm=llm))

    enriched = result["incident"]
    assert result["phase"] is Phase.TRIAGING
    assert enriched.severity is Severity.AVAILABILITY
    # This is the field `investigate` joins telemetry on: without it the run
    # reads nothing, however interesting the sentence was.
    assert enriched.affected_entities == ["java-micronaut"]
    assert "would not start" in enriched.description
    # A node returns a partial update; it never mutates the state it was given.
    assert original.affected_entities == []
    assert result["evidence"][0].provider == "kestrel"


async def test_triage_degrades_without_a_model(approval_gateway: Any) -> None:
    result = await nodes.triage(_report_state(), CoreDeps(approval=approval_gateway))

    assert "incident" not in result
    assert "no model is authorized" in result["notes"][0]
    assert "triage degraded" in result["evidence"][0].summary


async def test_triage_degrades_when_the_model_cannot_answer(
    llm: Any, approval_gateway: Any
) -> None:
    llm.error = LlmUnavailable("stub-llm", "seat signed out")

    result = await nodes.triage(_report_state(), CoreDeps(approval=approval_gateway, llm=llm))

    assert "could not read the report" in result["notes"][0]


async def test_triage_degrades_on_an_answer_it_cannot_use(
    llm: Any, approval_gateway: Any
) -> None:
    llm.answers = ["I would rather not"]

    result = await nodes.triage(_report_state(), CoreDeps(approval=approval_gateway, llm=llm))

    assert "unusable" in result["notes"][0]


async def test_triage_records_a_report_that_names_no_service(
    llm: Any, approval_gateway: Any
) -> None:
    llm.answers = ['{"severity": "error", "affected_entities": [], "description": "it failed"}']

    result = await nodes.triage(_report_state(), CoreDeps(approval=approval_gateway, llm=llm))

    assert result["incident"].affected_entities == []
    assert any("names no service" in note for note in result["notes"])


async def test_triage_leaves_a_structured_incident_away_from_the_model(
    state: dict, llm: Any, approval_gateway: Any
) -> None:
    """A webhook already carries severity and entities: no model call, no change."""
    state["incident"] = Incident(
        id="P-9",
        title="checkout failing",
        source="dynatrace",
        severity=Severity.ERROR,
        affected_entities=["checkout"],
    )

    result = await nodes.triage(state, CoreDeps(approval=approval_gateway, llm=llm))

    assert llm.prompts == []
    assert "incident" not in result
    assert "triage is a stub" in result["notes"][0]


async def test_triage_reads_a_report_only_when_it_is_one(
    state: dict, llm: Any, approval_gateway: Any
) -> None:
    """A vendor payload with a `text` field is telemetry, not a sentence to read."""
    state["incident"] = Incident(
        id="P-10",
        title="checkout failing",
        source="dynatrace",
        affected_entities=["checkout"],
        raw={"text": "looks like a sentence but came from the tool"},
    )

    await nodes.triage(state, CoreDeps(approval=approval_gateway, llm=llm))

    assert llm.prompts == []
