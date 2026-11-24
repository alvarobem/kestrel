"""Errors the application raises when a rule of the application is violated.

Typed on purpose. A driving adapter should not have to inspect an engine's
exception hierarchy to answer "was this a duplicate?" — it catches
`IncidentAlreadyRunning` and picks a status code. The engine-specific exception
never leaves the outbound adapter.
"""

from __future__ import annotations


class ApplicationError(Exception):
    """Base class: something the caller asked for is not allowed or not possible."""


class IncidentAlreadyRunning(ApplicationError):
    """One incident, one run. Ingestion is idempotent, so a duplicate is refused."""

    def __init__(self, incident_id: str) -> None:
        super().__init__(f"incident {incident_id} already has a run")
        self.incident_id = incident_id


class IncidentNotFound(ApplicationError):
    """No run exists for that incident."""

    def __init__(self, incident_id: str) -> None:
        super().__init__(f"no run for incident {incident_id}")
        self.incident_id = incident_id


class NoPendingApproval(ApplicationError):
    """The run is not waiting for a decision, so there is nothing to decide."""

    def __init__(self, incident_id: str) -> None:
        super().__init__(f"incident {incident_id} is not waiting for approval")
        self.incident_id = incident_id


class RunUnavailable(ApplicationError):
    """The engine could not be reached, or the run did not answer in time."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class TelemetryUnavailable(ApplicationError):
    """The active telemetry provider could not answer.

    The port contract names this error, so an adapter translates its own
    transport failure into it and the use case stays free of vendor exception
    hierarchies. `investigate` degrades on it; anything else is a bug and is
    left to propagate.
    """

    def __init__(self, provider_id: str, detail: str) -> None:
        super().__init__(f"{provider_id} telemetry unavailable: {detail}")
        self.provider_id = provider_id
        self.detail = detail


class LlmUnavailable(ApplicationError):
    """The active LLM client could not answer.

    Same shape as `TelemetryUnavailable` and for the same reason: a node that
    cannot reason must degrade with a recorded reason, not invent a plan. The
    adapter translates its own transport or auth failure into this error, so no
    node ever catches an SDK exception.
    """

    def __init__(self, model_id: str, detail: str) -> None:
        super().__init__(f"{model_id} unavailable: {detail}")
        self.model_id = model_id
        self.detail = detail
