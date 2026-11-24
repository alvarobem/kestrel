"""Evidence ranking: turning a bag of facts into an order that means something.

Pure rules, no I/O, no provider knowledge. Two operations, deliberately separate:

  * `merge_repeated` collapses the same message reported N times into one item
    that says N. Twelve identical log lines are one fact about volume, not twelve
    facts.
  * `rank_evidence` orders what is left by how much it explains *this* incident.

Ranking never edits `Evidence.confidence`. That number is the provider's claim
about its own fact ("I assert this problem directly"), and rewriting it to express
our preference would launder an opinion into a measurement. The score is derived,
kept out of the state, and recomputed by whoever needs it — which also means the
planner of Phase 2 can weight evidence differently without migrating anything.

Why these weights, and not a fancier model: they are explainable in one line each,
and every input is something the pipeline already knows. When Phase 2 needs
semantic relevance, that replaces the `_relevance_bonus` function, not the shape.
"""

from __future__ import annotations

from datetime import datetime

from kestrel.domain.models import Evidence, EvidenceKind, Incident

#: How much a kind of fact tends to explain an incident. An alert is someone (or
#: something) asserting a problem; a log is a sentence; a note is our own bookkeeping.
_KIND_WEIGHT: dict[EvidenceKind, float] = {
    EvidenceKind.ALERT: 1.0,
    EvidenceKind.EVENT: 0.6,
    EvidenceKind.METRIC: 0.7,
    EvidenceKind.LOG: 0.5,
    EvidenceKind.NOTE: 0.1,
}

#: A fact that names the incident's own entity explains more than one that does not.
_RELEVANCE_BONUS = 0.3
#: Repetition is signal, but only up to a point: the tenth repeat adds nothing.
_VOLUME_BONUS = 0.2
_VOLUME_CAP = 10


def signature(item: Evidence) -> str:
    """The identity of a fact, for collapsing repeats.

    A log is identified by its service and its text, not by its trace id: two
    occurrences of the same message from the same service are the same finding,
    and the trace id is a handle to one of them.
    """
    if item.kind is EvidenceKind.LOG:
        service = item.payload.get("service.name") or ""
        text = item.payload.get("content") or item.summary
        return f"{item.provider}|log|{service}|{text}"
    return f"{item.provider}|{item.kind}|{item.external_id or item.summary}"


def merge_repeated(items: list[Evidence]) -> list[Evidence]:
    """Collapse identical facts, keeping the first and counting the rest.

    The representative keeps one provider handle (`external_id`) — enough to open
    the trace — while `occurrences` carries the volume. The individual duplicates
    are not preserved: a report quoting twelve identical lines is worse than one
    line that says twelve.
    """
    merged: dict[str, Evidence] = {}
    order: list[str] = []

    for item in items:
        key = signature(item)
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            order.append(key)
            continue
        merged[key] = existing.model_copy(
            update={
                "occurrences": existing.occurrences + item.occurrences,
                "last_seen": _latest(existing, item),
                "external_id": item.external_id or existing.external_id,
            }
        )

    return [merged[key] for key in order]


def dedupe(items: list[Evidence]) -> list[Evidence]:
    """One representative per signature, keeping the larger count.

    Used where the same facts arrive from more than one read — a retried
    investigation re-reads an overlapping window — so counts are *not* summed
    here: a retry would otherwise inflate the volume it reports.
    """
    best: dict[str, Evidence] = {}
    order: list[str] = []

    for item in items:
        key = signature(item)
        existing = best.get(key)
        if existing is None:
            best[key] = item
            order.append(key)
        elif item.occurrences > existing.occurrences:
            best[key] = item

    return [best[key] for key in order]


def rank_evidence(
    items: list[Evidence], incident: Incident, *, limit: int | None = None
) -> list[Evidence]:
    """Order evidence by how much it explains this incident, best first."""
    ranked = sorted(
        items,
        key=lambda item: (_score(item, incident), item.collected_at),
        reverse=True,
    )
    return ranked[:limit] if limit is not None else ranked


def select_top(
    items: list[Evidence],
    incident: Incident,
    *,
    limit: int = 5,
    per_kind: int = 3,
) -> list[Evidence]:
    """The short list a human reads, keeping the kinds mixed.

    Pure ranking is not enough for a human-facing list: a dozen log lines collapse
    into a handful of distinct messages, and those beat everything else, so the
    conclusion-level facts — a metric ratio, a baseline, an alert — fall off the end
    of a top-5. A live run showed exactly that: the approver was shown five log
    lines and no baseline.

    So the list is filled in rank order with a cap per kind. The cap is the point:
    it costs a couple of log variants and buys the fact that changes the decision.
    """
    picked: list[Evidence] = []
    seen: dict[EvidenceKind, int] = {}

    for item in rank_evidence(items, incident):
        if seen.get(item.kind, 0) >= per_kind:
            continue
        picked.append(item)
        seen[item.kind] = seen.get(item.kind, 0) + 1
        if len(picked) >= limit:
            break

    return picked


def _score(item: Evidence, incident: Incident) -> float:
    score = _KIND_WEIGHT.get(item.kind, 0.2)
    if _mentions_incident(item, incident):
        score += _RELEVANCE_BONUS
    score += min(item.occurrences, _VOLUME_CAP) / _VOLUME_CAP * _VOLUME_BONUS
    return score


def _mentions_incident(item: Evidence, incident: Incident) -> bool:
    haystack = " ".join(
        [
            item.summary,
            item.external_id or "",
            *(str(value) for value in item.payload.values()),
        ]
    ).lower()
    for needle in (*incident.entity_ids, *incident.affected_entities):
        if needle and needle.lower() in haystack:
            return True
    return False


def _latest(existing: Evidence, item: Evidence) -> datetime:
    return max(
        moment
        for moment in (existing.last_seen, existing.collected_at, item.collected_at)
        if moment is not None
    )
