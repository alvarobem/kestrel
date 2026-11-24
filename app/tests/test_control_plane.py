"""The control-plane use cases, and the HTTP adapter that drives them.

Two things are being proven here:

  * the rules — one run per incident, decisions only while pending, what a read
    derives — are exercised with a fake `RunGateway`, no engine and no network;
  * the HTTP adapter only translates: an application error becomes a status code
    once, in an exception handler, and the routes contain no engine knowledge.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from kestrel.adapters.inbound.api import app
from kestrel.application.errors import (
    IncidentAlreadyRunning,
    IncidentNotFound,
    NoPendingApproval,
    RunUnavailable,
)
from kestrel.application.incidents import (
    decide_incident,
    inspect_incident,
    submit_incident,
)
from kestrel.application.ports import IncidentRunView, RunHandle
from kestrel.domain.models import Approval, Incident
from kestrel.domain.policy import RunLimits


class FakeRunGateway:
    """In-memory `RunGateway`: full control of every rule, no engine involved."""

    def __init__(self) -> None:
        self.started: list[tuple[Incident, RunLimits]] = []
        self.pending: dict[str, dict | None] = {}
        self.decisions: list[tuple[str, Approval]] = []
        self.failure: Exception | None = None

    async def start(self, incident: Incident, limits: RunLimits) -> RunHandle:
        if self.failure is not None:
            raise self.failure
        self.started.append((incident, limits))
        return RunHandle(
            incident_id=incident.id,
            workflow_id=f"incident-{incident.id}",
            run_id="run-1",
        )

    async def view(self, incident_id: str) -> IncidentRunView:
        if self.failure is not None:
            raise self.failure
        return IncidentRunView(
            incident_id=incident_id,
            workflow_id=f"incident-{incident_id}",
            status="RUNNING",
            run_id="run-1",
            pending_approval=self.pending.get(incident_id),
        )

    async def pending_approval(self, incident_id: str) -> dict | None:
        if self.failure is not None:
            raise self.failure
        return self.pending.get(incident_id)

    async def submit_decision(self, incident_id: str, decision: Approval) -> None:
        self.decisions.append((incident_id, decision))

    async def outcome(self, incident_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        return {}


@pytest.fixture
def gateway() -> FakeRunGateway:
    return FakeRunGateway()


def _incident(incident_id: str = "PD-1") -> Incident:
    return Incident(id=incident_id, title="checkout slow")


# --- the rules, without an engine ------------------------------------------


async def test_submit_incident_passes_the_run_input_as_values(gateway: FakeRunGateway) -> None:
    incident = _incident()
    limits = RunLimits(max_investigation_attempts=5, max_remediation_attempts=1)

    handle = await submit_incident(gateway, incident, limits)

    assert gateway.started == [(incident, limits)]
    assert (handle.incident_id, handle.workflow_id) == ("PD-1", "incident-PD-1")


async def test_submit_incident_surfaces_a_duplicate(gateway: FakeRunGateway) -> None:
    """One incident, one run: the refusal travels as a typed error."""
    gateway.failure = IncidentAlreadyRunning("PD-1")

    with pytest.raises(IncidentAlreadyRunning):
        await submit_incident(gateway, _incident(), RunLimits())


async def test_decide_incident_refuses_when_nothing_is_pending(gateway: FakeRunGateway) -> None:
    """A decision is only valid for the plan the run is actually asking about."""
    with pytest.raises(NoPendingApproval):
        await decide_incident(gateway, "PD-1", Approval(approved=True, actor="oncall"))

    assert gateway.decisions == []


async def test_decide_incident_delivers_the_decision_when_pending(
    gateway: FakeRunGateway,
) -> None:
    gateway.pending["PD-1"] = {"question": "Approve this remediation plan?"}
    decision = Approval(approved=False, actor="oncall", comment="too risky")

    await decide_incident(gateway, "PD-1", decision)

    assert gateway.decisions == [("PD-1", decision)]


async def test_inspect_incident_derives_awaiting_approval(gateway: FakeRunGateway) -> None:
    waiting = await inspect_incident(gateway, "PD-1")
    assert waiting.awaiting_approval is False

    gateway.pending["PD-1"] = {"question": "Approve?"}
    still_waiting = await inspect_incident(gateway, "PD-1")
    assert still_waiting.awaiting_approval is True


# --- the HTTP adapter only translates --------------------------------------


def test_api_accepts_an_incident_and_reports_the_run(gateway: FakeRunGateway) -> None:
    with TestClient(app) as client:
        app.state.runs = gateway

        response = client.post("/incidents", json={"id": "PD-2", "title": "checkout slow"})

    assert response.status_code == 202
    assert response.json() == {
        "incident_id": "PD-2",
        "workflow_id": "incident-PD-2",
        "run_id": "run-1",
    }


def test_api_reports_a_pending_approval(gateway: FakeRunGateway) -> None:
    gateway.pending["PD-4"] = {"question": "Approve this remediation plan?"}

    with TestClient(app) as client:
        app.state.runs = gateway
        body = client.get("/incidents/PD-4").json()

    assert body["awaiting_approval"] is True
    assert body["pending_approval"] == {"question": "Approve this remediation plan?"}
    assert body["status"] == "RUNNING"


def test_api_maps_application_errors_to_status_codes(gateway: FakeRunGateway) -> None:
    with TestClient(app) as client:
        app.state.runs = gateway

        gateway.failure = IncidentAlreadyRunning("PD-1")
        duplicate = client.post("/incidents", json={"id": "PD-1", "title": "t"})
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == "incident already has a run"

        gateway.failure = IncidentNotFound("PD-1")
        assert client.get("/incidents/PD-1").status_code == 404

        gateway.failure = RunUnavailable("connection refused")
        unavailable = client.get("/incidents/PD-1")
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == "temporal unavailable: connection refused"


def test_api_refuses_a_decision_when_nothing_is_pending(gateway: FakeRunGateway) -> None:
    with TestClient(app) as client:
        app.state.runs = gateway

        response = client.post("/incidents/PD-3/resume", json={"approved": True, "actor": "oncall"})

    assert response.status_code == 409
    assert response.json()["detail"] == "incident is not waiting for approval"
    assert gateway.decisions == []


def test_api_forwards_an_approval_as_a_domain_decision(gateway: FakeRunGateway) -> None:
    gateway.pending["PD-5"] = {"question": "Approve?"}

    with TestClient(app) as client:
        app.state.runs = gateway
        response = client.post(
            "/incidents/PD-5/resume",
            json={"approved": True, "actor": "alvaro", "comment": "go ahead"},
        )

    assert response.status_code == 202
    assert response.json() == {"incident_id": "PD-5", "accepted": True}
    incident_id, decision = gateway.decisions[0]
    assert (incident_id, decision.approved, decision.actor, decision.comment) == (
        "PD-5",
        True,
        "alvaro",
        "go ahead",
    )


# --- a sentence is enough to start a run -----------------------------------

REPORT = "ayer no arrancaba el servicio java-micronaut"


def test_api_starts_a_run_from_a_sentence(gateway: FakeRunGateway) -> None:
    """The words become the incident: identity, title and the audit trail."""
    with TestClient(app) as client:
        app.state.runs = gateway
        response = client.post("/incidents/natural", json={"text": REPORT})

    assert response.status_code == 202
    body = response.json()
    assert body["incident_id"].startswith("nl-")
    assert body["workflow_id"] == f"incident-{body['incident_id']}"

    incident = gateway.started[0][0]
    assert incident.source == "human"
    assert incident.raw["text"] == REPORT
    assert incident.description == REPORT
    assert "java-micronaut" in incident.title


def test_api_derives_one_incident_per_report(gateway: FakeRunGateway) -> None:
    """Two submissions of the same words are one incident, so the engine refuses the second."""
    with TestClient(app) as client:
        app.state.runs = gateway
        first = client.post("/incidents/natural", json={"text": REPORT}).json()
        second = client.post("/incidents/natural", json={"text": f"  {REPORT}  "}).json()

    assert first["incident_id"] == second["incident_id"]


def test_api_honours_an_explicit_report_id(gateway: FakeRunGateway) -> None:
    with TestClient(app) as client:
        app.state.runs = gateway
        body = client.post("/incidents/natural", json={"text": REPORT, "id": "PD-77"}).json()

    assert body["workflow_id"] == "incident-PD-77"
    assert gateway.started[0][0].id == "PD-77"


def test_api_refuses_a_report_without_words(gateway: FakeRunGateway) -> None:
    with TestClient(app) as client:
        app.state.runs = gateway
        response = client.post("/incidents/natural", json={"text": "   \n "})

    assert response.status_code == 422
    assert gateway.started == []


# --- webhook ingestion is per provider -------------------------------------
def test_api_maps_a_webhook_payload_through_the_provider_registry(
    gateway: FakeRunGateway,
) -> None:
    with TestClient(app) as client:
        app.state.runs = gateway
        response = client.post(
            "/incidents/webhook/dynatrace",
            json={
                "problemId": "P-9",
                "title": "checkout slow",
                "status": "OPEN",
                "severityLevel": "ERROR",
            },
        )

    assert response.status_code == 202
    body = response.json()
    assert body["incident_id"] == "P-9"
    assert body["workflow_id"] == "incident-P-9"
    started_incident = gateway.started[0][0]
    assert started_incident.source == "dynatrace"


def test_api_refuses_a_webhook_for_an_unknown_provider(gateway: FakeRunGateway) -> None:
    """An unmapped payload must not be guessed into an incident."""
    with TestClient(app) as client:
        app.state.runs = gateway
        response = client.post("/incidents/webhook/datadog", json={"id": "x"})

    assert response.status_code == 404
    assert "datadog" in response.json()["detail"]
    assert gateway.started == []


def test_api_reports_a_payload_that_does_not_fit_the_contract(gateway: FakeRunGateway) -> None:
    with TestClient(app) as client:
        app.state.runs = gateway
        response = client.post("/incidents/webhook/dynatrace", json={"problemId": ["bad"]})

    assert response.status_code == 422
    assert gateway.started == []
