"""Domain entities shared by the graph, the API and the persistence layer.

These are the shapes that cross process boundaries (a telemetry provider, HTTP,
the checkpoint store), so they are validated pydantic models rather than dicts.

Nothing here enumerates a vendor. A provider is identified by an opaque string
id (`dynatrace`, `datadog`, ...), so connecting another tool is a new adapter,
never a change to the vocabulary the whole system reasons with.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

#: A provider id: opaque and lowercase. It names *who produced a fact* — a
#: telemetry adapter, but also `kestrel` itself, `human` or `runbook`.
ProviderId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]*$")]


def _now() -> datetime:
    return datetime.now(UTC)


class Severity(StrEnum):
    AVAILABILITY = "availability"
    ERROR = "error"
    PERFORMANCE = "performance"
    RESOURCE_CONTENTION = "resource_contention"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class IncidentStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class Phase(StrEnum):
    RECEIVED = "received"
    TRIAGING = "triaging"
    INVESTIGATING = "investigating"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    FAILED = "failed"


class Incident(BaseModel):
    """Normalized incident, independent of where it came from."""

    model_config = ConfigDict(extra="forbid")

    id: str
    #: Where the incident was ingested from. Provenance, not the tool a run is
    #: configured to investigate with.
    source: ProviderId = "manual"
    external_id: str | None = None
    title: str
    description: str = ""
    severity: Severity = Severity.UNKNOWN
    status: IncidentStatus = IncidentStatus.OPEN
    started_at: datetime = Field(default_factory=_now)
    detected_at: datetime = Field(default_factory=_now)
    #: Human-facing names of the affected entities.
    affected_entities: list[str] = Field(default_factory=list)
    #: Provider-side ids of those entities (`SERVICE-…`). This is the only
    #: reliable join key for reading telemetry about *this* incident: names are
    #: display strings and repeat across a tenant.
    entity_ids: list[str] = Field(default_factory=list)
    management_zones: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    raw: dict = Field(default_factory=dict)


class EvidenceKind(StrEnum):
    """What kind of fact this is, independent of the tool that reported it.

    Every provider maps its own nouns onto these: a Dynatrace "problem", a
    Datadog "monitor" and a CloudWatch "alarm" are all `ALERT`.
    """

    ALERT = "alert"
    EVENT = "event"
    LOG = "log"
    METRIC = "metric"
    NOTE = "note"


class Evidence(BaseModel):
    """One fact gathered while investigating. Every item keeps its provenance."""

    model_config = ConfigDict(extra="forbid")

    #: Who produced this fact: a telemetry provider id, or `kestrel`/`human`/
    #: `runbook` for anything the system or a person contributed directly.
    provider: ProviderId
    kind: EvidenceKind
    summary: str
    #: The provider-side handle, so a report can cite the item it read.
    external_id: str | None = None
    payload: dict = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    collected_at: datetime = Field(default_factory=_now)
    #: How many identical facts this item stands for. Twelve equal log lines are
    #: one finding about volume; `1` means "reported once".
    occurrences: int = Field(default=1, ge=1)
    #: When the last of those occurrences was seen, for a collapsed item.
    last_seen: datetime | None = None


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(default_factory=list)
    falsified_by: str | None = None


class RemediationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int
    description: str
    # Rendered shell command. Always produced, even in DRY_RUN.
    command: str
    target: str
    #: The declared capability this step uses. It is in the audit trail next to
    #: the command so a reviewer reads an intent (`restart_workload`) and not only
    #: a flag soup, and the guard rail checks the command against the vocabulary
    #: that capability declared.
    capability: str | None = None
    reversible: bool = True
    risk: Literal["low", "medium", "high"] = "medium"
    rollback_command: str | None = None


class RemediationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    hypothesis: Hypothesis
    steps: list[RemediationStep] = Field(default_factory=list)
    blast_radius: str = ""
    requires_approval: bool = True


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    actor: str
    comment: str = ""
    at: datetime = Field(default_factory=_now)


class ActionRecord(BaseModel):
    """Audit entry for one executed (or intentionally skipped) step."""

    model_config = ConfigDict(extra="forbid")

    step_order: int
    command: str
    executed: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    at: datetime = Field(default_factory=_now)


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    detail: str
    checked_at: datetime = Field(default_factory=_now)


class IncidentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    summary: str
    root_cause: str | None = None
    actions_taken: list[str] = Field(default_factory=list)
    #: The ranked evidence a human should read first, as short lines.
    top_evidence: list[str] = Field(default_factory=list)
    resolved: bool = False
    follow_ups: list[str] = Field(default_factory=list)
