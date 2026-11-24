"""The telemetry seam: neutral evidence, one provider, and the degraded paths.

These tests are the contract a second adapter has to satisfy. They use a fake
provider for the use case and a fake transport for the Dynatrace adapter, so no
test needs a tenant, a credential or a network call.
"""

from __future__ import annotations

from typing import Any

import pytest

from kestrel.adapters.outbound import dynatrace_provider, providers
from kestrel.adapters.outbound.dynatrace import DynatraceError
from kestrel.application import nodes
from kestrel.application.deps import CoreDeps
from kestrel.application.errors import TelemetryUnavailable
from kestrel.application.graph import NODE_FUNCTIONS
from kestrel.config import Settings
from kestrel.domain.models import (
    Approval,
    Evidence,
    EvidenceKind,
    Incident,
    Phase,
    RemediationPlan,
)
from kestrel.domain.policy import InvestigationScope


class _NeverCalled:
    def await_decision(self, *, incident_id: str, plan: object) -> None:
        raise AssertionError("the approval gateway must not be reached on this path")


class FakeDynatraceClient:
    """The transport adapter's surface, with no HTTP underneath."""

    def __init__(self, problem: dict | None = None, error: Exception | None = None) -> None:
        self.problem = problem or {}
        self.error = error
        self.asked: list[str] = []
        self.closed = False

    def get_problem(self, problem_id: str) -> dict:
        self.asked.append(problem_id)
        if self.error is not None:
            raise self.error
        return self.problem

    def close(self) -> None:
        self.closed = True


def _problem(**overrides: object) -> dict:
    payload: dict = {
        "problemId": "P-123",
        "title": "checkout latency",
        "status": "OPEN",
        "severityLevel": "PERFORMANCE",
        "managementZones": [{"name": "checkout"}],
        "tags": [{"key": "team", "value": "payments"}],
    }
    payload.update(overrides)
    return payload


def _deps(**kwargs: object) -> CoreDeps:
    return CoreDeps(approval=_NeverCalled(), **kwargs)  # type: ignore[arg-type]


# --- the use case talks to the port ----------------------------------------


async def test_investigate_records_evidence_from_the_injected_provider(
    state: dict, telemetry: Any
) -> None:
    evidence = [
        Evidence(provider="stub", kind=EvidenceKind.ALERT, summary="checkout is slow", confidence=0.9)
    ]
    telemetry.evidence = evidence

    result = await nodes.investigate(state, _deps(telemetry=telemetry))

    assert result["phase"] == Phase.INVESTIGATING
    assert result["investigation_attempts"] == 1
    assert result["evidence"] == evidence
    assert telemetry.calls[0][0].id == "test-1"


async def test_investigate_passes_the_frozen_scope_to_the_provider(
    state: dict, telemetry: Any
) -> None:
    scope = InvestigationScope(tags=("checkout",))

    await nodes.investigate(state, _deps(telemetry=telemetry, scope=scope))

    assert telemetry.calls[0][1] == scope


async def test_investigate_degrades_without_a_provider(state: dict) -> None:
    result = await nodes.investigate(state, _deps())

    item = result["evidence"][0]
    assert result["investigation_attempts"] == 1
    assert item.provider == "kestrel"
    assert item.kind == EvidenceKind.NOTE
    assert "no telemetry provider" in item.summary
    # Nothing was gathered, so the run must not look like it found something.
    assert item.confidence < 0.6


async def test_investigate_records_a_provider_outage_instead_of_crashing(
    state: dict, telemetry: Any
) -> None:
    telemetry.error = TelemetryUnavailable("stub", "connection refused")

    result = await nodes.investigate(state, _deps(telemetry=telemetry))

    item = result["evidence"][0]
    assert item.kind == EvidenceKind.NOTE
    assert item.confidence == 0.0
    assert "connection refused" in item.summary
    assert "degraded" in result["notes"][0]


async def test_investigate_records_an_empty_answer_as_degraded(
    state: dict, telemetry: Any
) -> None:
    telemetry.evidence = []

    result = await nodes.investigate(state, _deps(telemetry=telemetry))

    item = result["evidence"][0]
    assert item.kind == EvidenceKind.NOTE
    assert item.external_id is None
    assert "no evidence" in item.summary


async def test_investigate_does_not_swallow_a_bug(state: dict, telemetry: Any) -> None:
    """Only the port's typed failure degrades: anything else is a defect to see."""
    telemetry.error = AttributeError("boom")

    with pytest.raises(AttributeError):
        await nodes.investigate(state, _deps(telemetry=telemetry))


# --- the Dynatrace adapter -------------------------------------------------


async def test_dynatrace_maps_a_problem_onto_neutral_evidence() -> None:
    adapter = dynatrace_provider.DynatraceTelemetryProvider(FakeDynatraceClient(_problem()))

    found = await adapter.investigate(
        Incident(id="P-123", title="checkout latency"), InvestigationScope()
    )

    assert len(found) == 1
    item = found[0]
    assert item.provider == "dynatrace"
    assert item.kind == EvidenceKind.ALERT
    assert item.external_id == "P-123"
    assert "checkout latency" in item.summary


async def test_dynatrace_returns_nothing_for_an_incident_outside_the_scope() -> None:
    adapter = dynatrace_provider.DynatraceTelemetryProvider(FakeDynatraceClient(_problem()))

    found = await adapter.investigate(
        Incident(id="P-123", title="checkout latency"),
        InvestigationScope(tags=("another-team",)),
    )

    assert found == []


async def test_dynatrace_scope_matches_a_tag_value() -> None:
    adapter = dynatrace_provider.DynatraceTelemetryProvider(FakeDynatraceClient(_problem()))

    found = await adapter.investigate(
        Incident(id="P-123", title="checkout latency"),
        InvestigationScope(tags=("payments",)),
    )

    assert len(found) == 1


async def test_dynatrace_404_is_no_evidence_not_an_outage() -> None:
    """A manual or smoke incident is unknown to the tool; that is not a failure."""
    client = FakeDynatraceClient(error=DynatraceError("missing", status_code=404))
    adapter = dynatrace_provider.DynatraceTelemetryProvider(client)

    assert await adapter.investigate(Incident(id="smoke-1", title="t"), InvestigationScope()) == []


async def test_dynatrace_transport_failure_becomes_the_port_error() -> None:
    client = FakeDynatraceClient(error=DynatraceError("boom", status_code=503))
    adapter = dynatrace_provider.DynatraceTelemetryProvider(client)

    with pytest.raises(TelemetryUnavailable) as raised:
        await adapter.investigate(Incident(id="P-1", title="t"), InvestigationScope())

    assert raised.value.provider_id == "dynatrace"


# --- the registry ----------------------------------------------------------


def test_the_registry_builds_nothing_without_credentials() -> None:
    assert providers.build_telemetry_provider(Settings()) is None


def test_the_registry_builds_the_configured_provider() -> None:
    settings = Settings(
        dynatrace_env_url="https://example.live.dynatrace.com",
        dynatrace_read_token="read-token",
    )

    provider = providers.build_telemetry_provider(settings)

    assert provider is not None
    assert provider.provider_id == "dynatrace"


def test_the_registry_rejects_an_unknown_provider() -> None:
    with pytest.raises(providers.UnknownTelemetryProvider) as raised:
        providers.build_telemetry_provider(Settings(telemetry_provider="nope"))

    assert "nope" in str(raised.value)
    assert "dynatrace" in str(raised.value)


def test_describe_reports_an_unconfigured_provider_without_raising() -> None:
    readable, lines = providers.describe_telemetry(Settings())

    assert readable is False
    assert "not configured" in lines[0]


# --- the domain vocabulary -------------------------------------------------


async def test_every_node_runs_with_the_neutral_vocabulary(
    state: dict, deps: CoreDeps, plan: RemediationPlan
) -> None:
    """A vocabulary change must not leave a node constructing the old shape.

    This is the regression test for the bug only `make smoke` caught: `triage`
    still built `Evidence(source=...)` after the model moved on, and every unit
    test stayed green because none of them called that node.
    """
    state["plan"] = plan
    state["approvals"] = [Approval(approved=True, actor="oncall")]

    for name, node in NODE_FUNCTIONS.items():
        update = await node(state, deps)
        assert isinstance(update, dict), f"{name} must return a partial state update"


def test_incident_source_accepts_any_provider_id_and_rejects_junk() -> None:
    assert Incident(id="1", title="t", source="datadog").source == "datadog"

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Incident(id="1", title="t", source="Not A Provider")


def test_scope_is_unrestricted_until_tags_are_configured() -> None:
    assert InvestigationScope().unrestricted is True
    assert InvestigationScope().allows(["anything"]) is True
    assert InvestigationScope(tags=("checkout",)).allows(["CHECKOUT"]) is True
    assert InvestigationScope(tags=("checkout",)).allows(["payments"]) is False
