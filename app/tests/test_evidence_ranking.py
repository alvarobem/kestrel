"""Evidence ranking rules: merging, deduplicating and ordering.

Pure domain rules, so they are tested without a provider, a node or a tenant. The
distinctions that matter here are the ones the report and the planner will rely on:

  * a repeated message is one finding about volume, not N findings;
  * a retry re-reads an overlapping window, so counts must not be summed twice;
  * ordering reflects what explains *this* incident, without editing the
    provider's own confidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kestrel.domain.evidence import dedupe, merge_repeated, rank_evidence, signature
from kestrel.domain.models import Evidence, EvidenceKind, Incident


def _log(content: str, *, service: str = "order-handler", trace: str | None = None) -> Evidence:
    return Evidence(
        provider="dynatrace",
        kind=EvidenceKind.LOG,
        summary=f"{service}: {content}",
        external_id=trace,
        payload={"service.name": service, "content": content},
    )


def _incident(**overrides: object) -> Incident:
    payload: dict = {"id": "P-1", "title": "incident"}
    payload.update(overrides)
    return Incident(**payload)


# --- signature -------------------------------------------------------------


def test_the_same_message_from_the_same_service_is_the_same_fact() -> None:
    assert signature(_log("boom")) == signature(_log("boom"))


def test_the_same_message_from_another_service_is_a_different_fact() -> None:
    assert signature(_log("boom")) != signature(_log("boom", service="payments-core"))


def test_a_log_is_identified_by_its_text_not_by_its_trace() -> None:
    """The trace is a handle to one occurrence, not the identity of the finding."""
    assert signature(_log("boom", trace="a")) == signature(_log("boom", trace="b"))


# --- merge within one read -------------------------------------------------


def test_repeated_messages_collapse_and_count() -> None:
    first = _log("DBM-ROP failed", trace="trace-1")
    merged = merge_repeated([first, _log("DBM-ROP failed"), _log("DBM-ROP failed")])

    assert len(merged) == 1
    assert merged[0].occurrences == 3
    assert merged[0].external_id == "trace-1"


def test_merging_keeps_the_last_seen_moment() -> None:
    first = _log("boom")
    first = first.model_copy(update={"collected_at": datetime(2026, 11, 24, 9, 0, tzinfo=UTC)})
    second = _log("boom").model_copy(update={"collected_at": datetime(2026, 11, 24, 10, 0, tzinfo=UTC)})

    merged = merge_repeated([first, second])

    assert merged[0].last_seen == datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def test_distinct_facts_survive_untouched() -> None:
    merged = merge_repeated([_log("a"), _log("b"), _log("c")])

    assert [item.occurrences for item in merged] == [1, 1, 1]
    assert len(merged) == 3


def test_merging_an_empty_read_stays_empty() -> None:
    assert merge_repeated([]) == []


# --- dedupe across reads ---------------------------------------------------


def test_dedupe_keeps_the_larger_count_instead_of_summing() -> None:
    """A retried investigation re-reads the same window; summing would inflate it."""
    one = _log("boom").model_copy(update={"occurrences": 3})
    other = _log("boom").model_copy(update={"occurrences": 7})

    deduped = dedupe([one, other])

    assert len(deduped) == 1
    assert deduped[0].occurrences == 7


# --- ranking ---------------------------------------------------------------


def test_an_alert_outranks_a_log_and_a_note() -> None:
    alert = Evidence(provider="dynatrace", kind=EvidenceKind.ALERT, summary="davis: checkout")
    log = _log("boom")
    note = Evidence(provider="kestrel", kind=EvidenceKind.NOTE, summary="bookkeeping")

    ranked = rank_evidence([note, log, alert], _incident())

    assert [item.kind for item in ranked] == [
        EvidenceKind.ALERT,
        EvidenceKind.LOG,
        EvidenceKind.NOTE,
    ]


def test_evidence_about_the_incident_outranks_unrelated_evidence() -> None:
    about = _log("boom", service="order-handler")
    unrelated = _log("boom", service="other-team")

    ranked = rank_evidence([unrelated, about], _incident(affected_entities=["order-handler"]))

    assert ranked[0].summary.startswith("order-handler:")


def test_an_entity_id_counts_as_relevance() -> None:
    item = Evidence(
        provider="dynatrace",
        kind=EvidenceKind.METRIC,
        summary="health",
        external_id="SERVICE-30CF729466F91717",
    )

    ranked = rank_evidence(
        [item, Evidence(provider="dynatrace", kind=EvidenceKind.METRIC, summary="health")],
        _incident(entity_ids=["SERVICE-30CF729466F91717"]),
    )

    assert ranked[0].external_id == "SERVICE-30CF729466F91717"


def test_repetition_raises_rank_but_is_capped() -> None:
    once = _log("boom", service="a")
    many = _log("boom", service="b").model_copy(update={"occurrences": 50})

    ranked = rank_evidence([once, many], _incident())

    assert ranked[0].summary.startswith("b:")


def test_ranking_respects_the_limit() -> None:
    items = [_log(f"message {index}") for index in range(10)]

    assert len(rank_evidence(items, _incident(), limit=3)) == 3


def test_ranking_never_rewrites_the_provider_confidence() -> None:
    item = Evidence(provider="dynatrace", kind=EvidenceKind.LOG, summary="boom", confidence=0.6)

    rank_evidence([item], _incident())

    assert item.confidence == 0.6


def test_newer_evidence_wins_a_tie() -> None:
    old = _log("boom").model_copy(
        update={"collected_at": datetime.now(UTC) - timedelta(hours=1)}
    )
    new = _log("boom").model_copy(update={"collected_at": datetime.now(UTC)})

    ranked = rank_evidence([old, new], _incident())

    assert ranked[0].collected_at > ranked[1].collected_at
