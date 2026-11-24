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
    def await_decision(self, *, incident_id: str, plan: object, evidence: list[str]) -> None:
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
    adapter = dynatrace_provider.DynatraceTelemetryProvider(classic=FakeDynatraceClient(_problem()))

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
    adapter = dynatrace_provider.DynatraceTelemetryProvider(classic=FakeDynatraceClient(_problem()))

    found = await adapter.investigate(
        Incident(id="P-123", title="checkout latency"),
        InvestigationScope(tags=("another-team",)),
    )

    assert found == []


async def test_dynatrace_scope_matches_a_tag_value() -> None:
    adapter = dynatrace_provider.DynatraceTelemetryProvider(classic=FakeDynatraceClient(_problem()))

    found = await adapter.investigate(
        Incident(id="P-123", title="checkout latency"),
        InvestigationScope(tags=("payments",)),
    )

    assert len(found) == 1


async def test_dynatrace_404_is_no_evidence_not_an_outage() -> None:
    """A manual or smoke incident is unknown to the tool; that is not a failure."""
    client = FakeDynatraceClient(error=DynatraceError("missing", status_code=404))
    adapter = dynatrace_provider.DynatraceTelemetryProvider(classic=client)

    assert await adapter.investigate(Incident(id="smoke-1", title="t"), InvestigationScope()) == []


async def test_dynatrace_transport_failure_becomes_the_port_error() -> None:
    client = FakeDynatraceClient(error=DynatraceError("boom", status_code=503))
    adapter = dynatrace_provider.DynatraceTelemetryProvider(classic=client)

    with pytest.raises(TelemetryUnavailable) as raised:
        await adapter.investigate(Incident(id="P-1", title="t", affected_entities=["lounge-manager"]), InvestigationScope())

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


# --- the platform surface: real evidence ------------------------------------


class FakePlatformClient:
    """The DQL transport's surface, with no HTTP underneath."""

    def __init__(
        self,
        *,
        problems: list[dict] | None = None,
        logs: list[dict] | None = None,
        metrics: list[dict] | None = None,
        counts: list[int] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.problems = problems or []
        self.logs = logs or []
        self.metrics = metrics or []
        #: Answers for the two count queries, in order: current window, then baseline.
        self.counts = list(counts or [])
        self.errors = errors or {}
        self.queries: list[dict] = []
        self.closed = False

    def execute(self, query: str, *, start=None, end=None, max_records=50, **_kw) -> list[dict]:
        if "davis" in query:
            kind = "davis"
        elif "timeseries" in query:
            kind = "metrics"
        elif "summarize n = count()" in query:
            kind = "count"
        else:
            kind = "logs"
        self.queries.append({"kind": kind, "start": start, "end": end, "query": query})
        if kind in self.errors:
            raise self.errors[kind]
        if kind == "count":
            value = self.counts.pop(0) if self.counts else 0
            return [{"n": str(value)}]
        return {"davis": self.problems, "metrics": self.metrics, "logs": self.logs}[kind]

    def close(self) -> None:
        self.closed = True


def _real_log_record(**overrides: object) -> dict:
    """Shaped from the tenant's actual response: these fields exist."""
    record = {
        "timestamp": "2026-11-24T09:42:11.227000000Z",
        "content": "Request Error: Not allowed level <CLASICA>",
        "loglevel": "ERROR",
        "service.name": "lounge-manager",
        "trace_id": "75d59033700d904161b55b4d2fca5d7d",
        "dt.security_context": "customer-experience",
        "deployment.environment.name": "prod",
    }
    record.update(overrides)
    return record


async def test_platform_investigation_returns_an_alert_and_a_log() -> None:
    platform = FakePlatformClient(
        problems=[{"timestamp": "t1", "davis.title": "checkout errors", "davis.status": "OPEN"}],
        logs=[_real_log_record()],
    )
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(
        Incident(
            id="P-1",
            title="checkout slow",
            affected_entities=["lounge-manager"],
            entity_ids=["SERVICE-1"],
        ),
        InvestigationScope(),
    )

    assert [item.kind for item in found] == [EvidenceKind.ALERT, EvidenceKind.LOG]
    assert {item.provider for item in found} == {"dynatrace"}
    alert, log = found
    assert "checkout errors" in alert.summary
    assert log.external_id == "75d59033700d904161b55b4d2fca5d7d"
    assert log.summary.startswith("lounge-manager:")


async def test_platform_evidence_payload_is_projected_and_small() -> None:
    long_content = "x" * 2000
    platform = FakePlatformClient(logs=[_real_log_record(content=long_content)])
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(Incident(id="P-1", title="t", affected_entities=["lounge-manager"]), InvestigationScope())

    payload = found[0].payload
    assert set(payload) == {"timestamp", "loglevel", "service.name", "content"}
    assert len(payload["content"]) < 600
    assert payload["content"].endswith("…")


async def test_platform_investigation_floors_the_window_of_a_stale_incident() -> None:
    from datetime import UTC, datetime, timedelta

    platform = FakePlatformClient()
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]
    stale = Incident(
        id="P-1",
        title="old",
        affected_entities=["lounge-manager"],
        started_at=datetime.now(UTC) - timedelta(hours=48),
    )

    await provider.investigate(stale, InvestigationScope())

    start = datetime.fromisoformat(platform.queries[0]["start"])
    age = datetime.now(UTC) - start
    # Six hours back at most, never 48.
    assert timedelta(hours=6, minutes=1) > age > timedelta(hours=5, minutes=55)


async def test_platform_investigation_applies_the_run_scope() -> None:
    platform = FakePlatformClient(
        logs=[_real_log_record(**{"dt.security_context": "other-team", "service.name": "other"})]
    )
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(
        Incident(id="P-1", title="t", affected_entities=["lounge-manager"]), InvestigationScope(tags=("customer-experience",))
    )

    # Filtered, not empty: the run must be able to tell the two apart.
    assert [item.kind for item in found] == [EvidenceKind.NOTE]
    assert "none matched the run scope" in found[0].summary
    assert "customer-experience" in found[0].summary


async def test_platform_scope_matches_a_semantic_attribute() -> None:
    platform = FakePlatformClient(logs=[_real_log_record()])
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(
        Incident(id="P-1", title="t", affected_entities=["lounge-manager"]),
        InvestigationScope(tags=("customer-experience",)),
    )

    assert [item.kind for item in found if item.kind == EvidenceKind.LOG]


async def test_total_platform_failure_is_a_provider_outage() -> None:
    from kestrel.adapters.outbound.dynatrace_platform import DynatracePlatformError

    boom = DynatracePlatformError("query:execute -> 403: no scope", status_code=403)
    platform = FakePlatformClient(errors={"davis": boom, "logs": boom})
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    with pytest.raises(TelemetryUnavailable) as raised:
        await provider.investigate(Incident(id="P-1", title="t", affected_entities=["lounge-manager"]), InvestigationScope())

    assert "403" in str(raised.value)


async def test_a_partial_platform_failure_keeps_the_evidence_and_records_a_note() -> None:
    from kestrel.adapters.outbound.dynatrace_platform import DynatracePlatformError

    platform = FakePlatformClient(
        logs=[_real_log_record()],
        errors={"davis": DynatracePlatformError("davis query rejected")},
    )
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(Incident(id="P-1", title="t", affected_entities=["lounge-manager"]), InvestigationScope())

    kinds = [item.kind for item in found]
    assert EvidenceKind.LOG in kinds
    assert EvidenceKind.NOTE in kinds
    note = next(item for item in found if item.kind == EvidenceKind.NOTE)
    assert "davis problems unavailable" in note.summary


async def test_the_provider_prefers_the_platform_surface() -> None:
    platform = FakePlatformClient(logs=[_real_log_record()])
    classic = FakeDynatraceClient(_problem())
    provider = dynatrace_provider.DynatraceTelemetryProvider(  # type: ignore[arg-type]
        platform=platform, classic=classic
    )

    await provider.investigate(Incident(id="P-1", title="t", affected_entities=["lounge-manager"]), InvestigationScope())

    assert classic.asked == []
    assert platform.queries


async def test_a_fresh_incident_investigates_a_window_with_context() -> None:
    """The bug a live run caught: a just-created incident had a zero-width window."""
    from datetime import datetime, timedelta

    platform = FakePlatformClient()
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    await provider.investigate(Incident(id="P-1", title="fresh", affected_entities=["lounge-manager"]), InvestigationScope())

    start = datetime.fromisoformat(platform.queries[0]["start"])
    end = datetime.fromisoformat(platform.queries[0]["end"])
    assert end - start > timedelta(minutes=29)
    assert end - start < timedelta(minutes=31)


# --- the query is scoped to the incident, or there is no query -------------


async def test_an_incident_without_a_target_reads_nothing() -> None:
    """The defect this pins: reading the tenant-wide error stream as "evidence"."""
    platform = FakePlatformClient(logs=[_real_log_record()])
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(Incident(id="P-1", title="nothing"), InvestigationScope())

    assert platform.queries == []
    assert [item.kind for item in found] == [EvidenceKind.NOTE]
    assert "no investigation target" in found[0].summary


async def test_the_log_query_joins_on_the_entity_id() -> None:
    platform = FakePlatformClient()
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    await provider.investigate(
        Incident(id="P-1", title="t", entity_ids=["SERVICE-30CF729466F91717"]),
        InvestigationScope(),
    )

    logs_query = next(q["query"] for q in platform.queries if q["kind"] == "logs")
    assert 'dt.entity.service == "SERVICE-30CF729466F91717"' in logs_query
    assert 'loglevel == "ERROR"' in logs_query


async def test_the_log_query_falls_back_to_the_service_name() -> None:
    platform = FakePlatformClient()
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    await provider.investigate(
        Incident(id="P-1", title="t", affected_entities=["order-handler"]), InvestigationScope()
    )

    logs_query = next(q["query"] for q in platform.queries if q["kind"] == "logs")
    assert 'service.name == "order-handler"' in logs_query


async def test_the_environment_guardrail_is_appended() -> None:
    platform = FakePlatformClient()
    provider = dynatrace_provider.DynatraceTelemetryProvider(  # type: ignore[arg-type]
        platform=platform, environment="prod"
    )

    await provider.investigate(
        Incident(id="P-1", title="t", entity_ids=["SERVICE-1"]), InvestigationScope()
    )

    logs_query = next(q["query"] for q in platform.queries if q["kind"] == "logs")
    assert 'deployment.environment.name == "prod"' in logs_query


async def test_an_entity_name_cannot_break_out_of_the_dql_literal() -> None:
    platform = FakePlatformClient()
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    await provider.investigate(
        Incident(id="P-1", title="t", affected_entities=['evil" or true or "']),
        InvestigationScope(),
    )

    logs_query = next(q["query"] for q in platform.queries if q["kind"] == "logs")
    assert 'service.name == "evil\\" or true or \\""' in logs_query


async def test_a_name_only_join_says_it_may_span_environments() -> None:
    """`SERVICE-…` is environment-scoped; a service name is not."""
    platform = FakePlatformClient(logs=[_real_log_record()])
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(
        Incident(id="P-1", title="t", affected_entities=["order-handler"]), InvestigationScope()
    )

    note = next(item for item in found if item.kind == EvidenceKind.NOTE)
    assert "may span environments" in note.summary


async def test_an_entity_id_join_does_not_add_the_caveat() -> None:
    platform = FakePlatformClient(logs=[_real_log_record()])
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(
        Incident(id="P-1", title="t", entity_ids=["SERVICE-30CF729466F91717"]), InvestigationScope()
    )

    assert [item.kind for item in found] == [EvidenceKind.LOG]


# --- service metrics -------------------------------------------------------


async def test_metric_evidence_reports_ratio_and_trend() -> None:
    platform = FakePlatformClient(
        metrics=[
            {
                "dt.entity.service": "SERVICE-30CF729466F91717",
                "req": [1000.0, 1000.0, 1000.0],
                "fails": [10.0, 10.0, 50.0],
                "rt": [1000.0, 1100.0, 5000.0],
            }
        ]
    )
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(
        Incident(id="P-1", title="t", entity_ids=["SERVICE-30CF729466F91717"]), InvestigationScope()
    )

    metric = next(item for item in found if item.kind == EvidenceKind.METRIC)
    assert "failure ratio 2.33%" in metric.summary
    assert "5x typical" in metric.summary
    assert metric.external_id == "SERVICE-30CF729466F91717"


async def test_a_periodic_incident_is_not_reported_as_steady() -> None:
    """The series that lied: a quiet trough used to be the whole truth.

    Sampling the last interval described this service as "steady" while two of its
    seven intervals were failing, and the planner refused the contradiction. The
    peak against the typical interval is what a human can act on.
    """
    platform = FakePlatformClient(
        metrics=[
            {
                "dt.entity.service": "SERVICE-763ED91C8D5B7DE9",
                "req": [5.0, 5.0, 256.0, 5.0, 5.0, 256.0, 2.0],
                "fails": [0.0, 0.0, 30.0, 0.0, 0.0, 30.0, 0.0],
                "rt": [8524.0, 6052.0, 371179.0, 6183.0, 5534.0, 378909.0, 5770.0],
            }
        ]
    )
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(
        Incident(id="P-2", title="t", entity_ids=["SERVICE-763ED91C8D5B7DE9"]), InvestigationScope()
    )

    metric = next(item for item in found if item.kind == EvidenceKind.METRIC)
    assert "steady" not in metric.summary
    assert "peak 30 against a typical 0" in metric.summary
    assert "in 2 of 7 intervals" in metric.summary
    # The response-time spike used to travel only in the payload, invisible to the
    # planner, which sees summaries and nothing else.
    assert "response time" in metric.summary
    assert "61x typical" in metric.summary


async def test_a_spike_from_a_zero_baseline_is_a_spike() -> None:
    # `baseline and latest > 2 * baseline` used to short-circuit to "steady" when
    # the baseline was 0, which is the most violent case there is.
    platform = FakePlatformClient(
        metrics=[
            {
                "dt.entity.service": "SERVICE-AAA",
                "req": [100.0, 100.0, 100.0],
                "fails": [0.0, 0.0, 40.0],
                "rt": [10.0, 10.0, 10.0],
            }
        ]
    )
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(
        Incident(id="P-3", title="t", entity_ids=["SERVICE-AAA"]), InvestigationScope()
    )

    metric = next(item for item in found if item.kind == EvidenceKind.METRIC)
    assert "against a typical 0" in metric.summary


async def test_a_service_without_request_metrics_produces_no_metric_evidence() -> None:
    """Not every service reports request metrics; that is normal, not a fault."""
    platform = FakePlatformClient(metrics=[])
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(
        Incident(id="P-1", title="t", entity_ids=["SERVICE-30CF729466F91717"]), InvestigationScope()
    )

    assert [item for item in found if item.kind == EvidenceKind.METRIC] == []


async def test_the_metrics_query_filters_by_the_incident_entities() -> None:
    platform = FakePlatformClient()
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    await provider.investigate(
        Incident(id="P-1", title="t", entity_ids=["SERVICE-A", "SERVICE-B"]), InvestigationScope()
    )

    query = next(q["query"] for q in platform.queries if q["kind"] == "metrics")
    assert 'dt.entity.service == "SERVICE-A"' in query
    assert 'dt.entity.service == "SERVICE-B"' in query
    assert " or " in query


async def test_a_metrics_failure_does_not_lose_the_logs() -> None:
    from kestrel.adapters.outbound.dynatrace_platform import DynatracePlatformError

    platform = FakePlatformClient(
        logs=[_real_log_record()],
        errors={"metrics": DynatracePlatformError("metrics rejected")},
    )
    provider = dynatrace_provider.DynatraceTelemetryProvider(platform=platform)  # type: ignore[arg-type]

    found = await provider.investigate(
        Incident(id="P-1", title="t", entity_ids=["SERVICE-1"]), InvestigationScope()
    )

    assert [item for item in found if item.kind == EvidenceKind.LOG]
    note = next(item for item in found if item.kind == EvidenceKind.NOTE)
    assert "service metrics unavailable" in note.summary


# --- ranking reaches the report --------------------------------------------


async def test_investigate_collapses_repeated_messages(state: dict, telemetry: Any) -> None:
    """Twelve identical lines are one finding about volume, and state is checkpointed."""
    repeated = [
        Evidence(
            provider="stub",
            kind=EvidenceKind.LOG,
            summary="order-handler: same failure",
            external_id=f"trace-{index}",
            payload={"service.name": "order-handler", "content": "same failure"},
        )
        for index in range(12)
    ]
    telemetry.evidence = repeated

    result = await nodes.investigate(
        state, CoreDeps(approval=_NeverCalled(), telemetry=telemetry)
    )

    assert len(result["evidence"]) == 1
    assert result["evidence"][0].occurrences == 12


async def test_the_report_lists_the_top_ranked_evidence(state: dict, deps: CoreDeps) -> None:
    state["evidence"] = [
        Evidence(provider="stub", kind=EvidenceKind.NOTE, summary="bookkeeping"),
        Evidence(
            provider="dynatrace",
            kind=EvidenceKind.ALERT,
            summary="davis: checkout failing",
            external_id="P-9",
        ),
        Evidence(
            provider="dynatrace",
            kind=EvidenceKind.LOG,
            summary="order-handler: boom",
            occurrences=4,
        ),
    ]

    result = await nodes.report(state, deps)

    top = result["report"].top_evidence
    assert top[0].startswith("[alert]")
    assert any("(x4)" in line for line in top)
    assert "3 evidence item(s)" in result["report"].summary


# --- baseline: is this normal? ---------------------------------------------


async def test_the_baseline_compares_the_window_to_the_span_before_it() -> None:
    platform = FakePlatformClient(counts=[37, 12])

    found = await dynatrace_provider.DynatraceTelemetryProvider(platform=platform).investigate(  # type: ignore[arg-type]
        Incident(id="P-1", title="t", entity_ids=["SERVICE-1"]), InvestigationScope()
    )

    baseline = next(item for item in found if "error log volume" in item.summary)
    assert "37 in 30m vs 12 before" in baseline.summary
    assert "rising" in baseline.summary
    assert baseline.kind == EvidenceKind.METRIC


async def test_a_baseline_of_zero_is_reported_as_new_errors() -> None:
    platform = FakePlatformClient(counts=[9, 0])

    found = await dynatrace_provider.DynatraceTelemetryProvider(platform=platform).investigate(  # type: ignore[arg-type]
        Incident(id="P-1", title="t", entity_ids=["SERVICE-1"]), InvestigationScope()
    )

    baseline = next(item for item in found if "error log volume" in item.summary)
    assert "new" in baseline.summary


async def test_two_quiet_windows_produce_no_baseline_item() -> None:
    platform = FakePlatformClient(counts=[0, 0])

    found = await dynatrace_provider.DynatraceTelemetryProvider(platform=platform).investigate(  # type: ignore[arg-type]
        Incident(id="P-1", title="t", entity_ids=["SERVICE-1"]), InvestigationScope()
    )

    assert [item for item in found if "error log volume" in item.summary] == []


async def test_the_baseline_queries_the_preceding_span() -> None:
    from datetime import datetime

    platform = FakePlatformClient(counts=[1, 1])

    await dynatrace_provider.DynatraceTelemetryProvider(platform=platform).investigate(  # type: ignore[arg-type]
        Incident(id="P-1", title="t", entity_ids=["SERVICE-1"]), InvestigationScope()
    )

    counts = [q for q in platform.queries if q["kind"] == "count"]
    assert len(counts) == 2
    window_start, window_end = (
        datetime.fromisoformat(counts[0]["start"]),
        datetime.fromisoformat(counts[0]["end"]),
    )
    previous_start, previous_end = (
        datetime.fromisoformat(counts[1]["start"]),
        datetime.fromisoformat(counts[1]["end"]),
    )
    # The baseline ends exactly where the window starts: no gap, no overlap.
    assert previous_end == window_start
    # ...and it is the same length, or the ratio would compare different spans.
    assert previous_end - previous_start == window_end - window_start


async def test_a_baseline_failure_does_not_lose_the_logs() -> None:
    from kestrel.adapters.outbound.dynatrace_platform import DynatracePlatformError

    platform = FakePlatformClient(
        logs=[_real_log_record()], errors={"count": DynatracePlatformError("count rejected")}
    )

    found = await dynatrace_provider.DynatraceTelemetryProvider(platform=platform).investigate(  # type: ignore[arg-type]
        Incident(id="P-1", title="t", entity_ids=["SERVICE-1"]), InvestigationScope()
    )

    assert [item for item in found if item.kind == EvidenceKind.LOG]
    note = next(item for item in found if item.kind == EvidenceKind.NOTE)
    assert "error baseline unavailable" in note.summary
