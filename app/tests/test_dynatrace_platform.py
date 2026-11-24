"""The Dynatrace platform transport (Grail DQL) and the evidence it produces.

The path, the header and the polling are the parts that cost real debugging time
against the live tenant, so they are pinned here:

  * the endpoint is `/platform/storage/query/v1/query:execute` — without
    `/query/v1/` the gateway answers `404 No static resource`;
  * the credential is a platform token sent as `Bearer`;
  * DQL is asynchronous: a `RUNNING` answer carries a `requestToken` that must be
    polled, bounded, because an activity cannot hang.

Record shapes in these tests come from the tenant's own response, not from
imagination: a DQL query that names a field which does not exist fails whole.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kestrel.adapters.outbound import providers
from kestrel.adapters.outbound.dynatrace_platform import (
    DynatracePlatformClient,
    DynatracePlatformError,
)
from kestrel.config import Settings
from kestrel.domain.policy import InvestigationScope

PLATFORM_URL = "https://psk34092.apps.dynatrace.com"


def _settings(**overrides: Any) -> Settings:
    base = {
        "dynatrace_platform_url": PLATFORM_URL,
        "dynatrace_platform_token": "dt0s16.PUBLIC.SECRET",
    }
    base.update(overrides)
    return Settings(**base)


def _succeeded(records: list[dict]) -> dict:
    return {
        "state": "SUCCEEDED",
        "progress": 100,
        "result": {"records": records, "types": []},
        "metadata": {"grail": {"queryId": "q-1", "notifications": []}},
    }


def _client(handler: Any, **overrides: Any) -> DynatracePlatformClient:
    return DynatracePlatformClient(
        _settings(**overrides), transport=httpx.MockTransport(handler)
    )


# --- the request shape -----------------------------------------------------


def test_dql_posts_to_the_documented_path_with_a_bearer_token() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=_succeeded([]))

    with _client(handler) as client:
        assert client.execute('fetch logs | limit 1', max_records=7) == []

    assert seen["method"] == "POST"
    assert seen["path"] == "/platform/storage/query/v1/query:execute"
    assert seen["auth"] == "Bearer dt0s16.PUBLIC.SECRET"
    assert '"maxResultRecords":7' in seen["body"]


def test_dql_sends_the_incident_window() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=_succeeded([]))

    with _client(handler) as client:
        client.execute("fetch logs", start="2026-11-24T07:00:00+00:00", end="2026-11-24T09:00:00+00:00")

    assert '"defaultTimeframeStart":"2026-11-24T07:00:00+00:00"' in seen["body"]
    assert '"defaultTimeframeEnd":"2026-11-24T09:00:00+00:00"' in seen["body"]


# --- asynchronous polling --------------------------------------------------


def test_running_query_is_polled_until_it_finishes() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("query:execute"):
            return httpx.Response(
                200, json={"state": "RUNNING", "progress": 20, "requestToken": "tok-1"}
            )
        assert request.url.params["request-token"] == "tok-1"
        return httpx.Response(200, json=_succeeded([{"content": "boom"}]))

    with _client(handler) as client:
        records = client.execute("fetch logs", poll_interval_seconds=0)

    assert records == [{"content": "boom"}]
    assert calls == [
        "/platform/storage/query/v1/query:execute",
        "/platform/storage/query/v1/query:poll",
    ]


def test_a_query_that_never_finishes_gives_up_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"state": "RUNNING", "progress": 10, "requestToken": "tok-1"}
        )

    with (
        _client(handler) as client,
        pytest.raises(DynatracePlatformError, match="still running"),
    ):
        client.execute("fetch logs", poll_attempts=3, poll_interval_seconds=0)


def test_a_failed_query_reports_the_provider_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": "FAILED",
                "metadata": {
                    "grail": {"notifications": [{"message": "field `nope` not found"}]}
                },
            },
        )

    with (
        _client(handler) as client,
        pytest.raises(DynatracePlatformError, match="field `nope` not found"),
    ):
        client.execute("fetch logs")


# --- transport failures ----------------------------------------------------


def test_a_403_is_readable_and_carries_the_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "Token is missing required scope. Use one of: storage:logs:read",
                }
            },
        )

    with (
        _client(handler) as client,
        pytest.raises(DynatracePlatformError) as raised,
    ):
        client.execute("fetch logs")

    assert raised.value.status_code == 403
    assert "storage:logs:read" in str(raised.value)


def test_a_non_json_gateway_body_is_not_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    with (
        _client(handler) as client,
        pytest.raises(DynatracePlatformError, match="non-JSON"),
    ):
        client.execute("fetch logs")


# --- the registry prefers the platform surface -----------------------------


def test_the_provider_is_built_on_the_platform_surface_when_configured() -> None:
    provider = providers.build_telemetry_provider(_settings())

    assert provider is not None
    assert provider.provider_id == "dynatrace"


def test_a_tenant_with_only_classic_credentials_still_gets_a_provider() -> None:
    provider = providers.build_telemetry_provider(
        Settings(
            dynatrace_env_url="https://example.live.dynatrace.com",
            dynatrace_read_token="read-token",
        )
    )

    assert provider is not None


# --- describe never raises -------------------------------------------------


def test_describe_reports_a_missing_configuration_in_one_line() -> None:
    readable, lines = providers.describe_telemetry(Settings())

    assert readable is False
    assert len(lines) == 1
    assert "DYNATRACE_PLATFORM_URL" in lines[0]


def test_describe_reports_a_rejected_credential_without_a_traceback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": 403, "message": "no scope"}})

    settings = _settings()
    import kestrel.adapters.outbound.dynatrace_provider as dynatrace_provider

    original = dynatrace_provider.DynatracePlatformClient
    dynatrace_provider.DynatracePlatformClient = lambda s: original(  # type: ignore[assignment]
        s, transport=httpx.MockTransport(handler)
    )
    try:
        readable, lines = dynatrace_provider.describe(settings)
    finally:
        dynatrace_provider.DynatracePlatformClient = original  # type: ignore[assignment]

    assert readable is False
    assert len(lines) == 1
    assert "403" in lines[0]


# --- scope still filters ---------------------------------------------------


def test_scope_matches_the_tenant_semantic_attributes() -> None:
    assert InvestigationScope(tags=("customer-experience",)).allows(
        ["customer-experience"]
    )
    # The candidate list is what the provider feeds; empty scope stays unrestricted.
    assert InvestigationScope().allows(["anything"]) is True
