"""Domain entities shared by the graph, the API and the persistence layer.

These are the shapes that cross process boundaries (Dynatrace, HTTP, the
checkpoint store), so they are validated pydantic models rather than dicts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    source: Literal["dynatrace", "manual"] = "manual"
    external_id: str | None = None
    title: str
    description: str = ""
    severity: Severity = Severity.UNKNOWN
    status: IncidentStatus = IncidentStatus.OPEN
    started_at: datetime = Field(default_factory=_now)
    detected_at: datetime = Field(default_factory=_now)
    affected_entities: list[str] = Field(default_factory=list)
    management_zones: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    raw: dict = Field(default_factory=dict)


class Evidence(BaseModel):
    """One fact gathered while investigating. Every item keeps its provenance."""

    model_config = ConfigDict(extra="forbid")

    source: Literal[
        "dynatrace_problems", "dynatrace_logs", "dynatrace_metrics", "runbook", "human", "agent"
    ]
    summary: str
    payload: dict = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    collected_at: datetime = Field(default_factory=_now)


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
    resolved: bool = False
    follow_ups: list[str] = Field(default_factory=list)
