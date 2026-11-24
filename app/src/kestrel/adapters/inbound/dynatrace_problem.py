"""Inbound anti-corruption layer for Dynatrace problem payloads.

A webhook body is an external contract: it changes on the vendor's schedule and
it is full of vendor nouns (`problemId`, `severityLevel`, `managementZones`).
Translating it into our `Incident` belongs to the inbound side, next to the
endpoint that receives it — not inside the outbound HTTP client, which is why
the API used to import the client just to map a payload.

Nothing here talks to the network. The outbound client returns plain data; this
module gives it meaning.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kestrel.domain.models import Incident, IncidentStatus, Severity

_SEVERITY_MAP = {
    "AVAILABILITY": Severity.AVAILABILITY,
    "ERROR": Severity.ERROR,
    "PERFORMANCE": Severity.PERFORMANCE,
    "RESOURCE_CONTENTION": Severity.RESOURCE_CONTENTION,
    "CUSTOM_ALERT": Severity.CUSTOM,
}


def parse_dynatrace_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    # Dynatrace returns epoch milliseconds.
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def problem_to_incident(problem: dict) -> Incident:
    """Map a Dynatrace problem payload onto our normalized incident."""
    affected: list[str] = []
    entity_ids: list[str] = []
    for entity in problem.get("affectedEntities", []) or []:
        if not isinstance(entity, dict):
            continue
        name = entity.get("name")
        if name:
            affected.append(name)
        entity_id = (entity.get("entityId") or {}).get("id")
        if entity_id:
            entity_ids.append(entity_id)

    status = IncidentStatus.OPEN if problem.get("status") == "OPEN" else IncidentStatus.CLOSED

    return Incident(
        id=problem.get("problemId", "unknown"),
        source="dynatrace",
        external_id=problem.get("displayId"),
        title=problem.get("title", "untitled problem"),
        description=problem.get("impactLevel", ""),
        severity=_SEVERITY_MAP.get(problem.get("severityLevel", ""), Severity.UNKNOWN),
        status=status,
        started_at=parse_dynatrace_timestamp(problem.get("startTime")),
        affected_entities=affected,
        # Kept separately on purpose: it is the key the investigation filters on.
        entity_ids=entity_ids,
        management_zones=[
            zone.get("name", "")
            for zone in problem.get("managementZones", []) or []
            if zone.get("name")
        ],
        tags={tag.get("key", ""): tag.get("value", "") for tag in problem.get("tags", []) or []},
        raw=problem,
    )
