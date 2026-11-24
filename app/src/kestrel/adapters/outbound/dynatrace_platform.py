"""Dynatrace platform transport: Grail DQL over the `.apps` surface.

A second surface, not a second client library: the platform API lives on a
different host, uses a different credential and answers a different dialect.

  * classic (`dynatrace.py`): `https://{env}.live.dynatrace.com/api/v2`, `Api-Token`;
  * platform (this module): `https://{env}.apps.dynatrace.com/platform`, `Bearer`.

The two are not interchangeable, and mixing them produces the most confusing
failure mode available: a `404 No static resource …` from the gateway when the
path is wrong, or a `403 Token is missing required scope` when the credential
belongs to the other surface. The path is therefore a constant here, and it was
verified against a live tenant:

    POST /platform/storage/query/v1/query:execute
    GET  /platform/storage/query/v1/query:poll?request-token=…

DQL is asynchronous. `query:execute` answers with a `state` and, when the query
is still running, a `requestToken` to poll. Polling is bounded by attempts: an
activity must fail with a reason before its `start_to_close_timeout`, never hang.

Plain data only: this module returns records and never imports the domain.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from kestrel.config import Settings

logger = logging.getLogger(__name__)

QUERY_EXECUTE_PATH = "/platform/storage/query/v1/query:execute"
QUERY_POLL_PATH = "/platform/storage/query/v1/query:poll"

#: Terminal states. Anything else means "ask again in a moment".
_FINISHED = {"SUCCEEDED", "FAILED", "CANCELLED"}


class DynatracePlatformError(RuntimeError):
    """Platform transport failure. `status_code` is set when the gateway answered."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DynatracePlatformClient:
    def __init__(
        self,
        settings: Settings,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.dynatrace_platform_url:
            raise DynatracePlatformError("DYNATRACE_PLATFORM_URL is not set")
        if not settings.dynatrace_platform_token:
            raise DynatracePlatformError("DYNATRACE_PLATFORM_TOKEN is not set")
        self._base_url = settings.dynatrace_platform_url.rstrip("/")
        self._token = settings.dynatrace_platform_token
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            # `transport` exists for tests: it is the only way to exercise the
            # real request path without a tenant.
            transport=transport or httpx.HTTPTransport(retries=2),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    def _auth(self) -> dict[str, str]:
        # Platform tokens and OAuth access tokens both use the Bearer realm.
        return {"Authorization": f"Bearer {self._token}"}

    def execute(
        self,
        query: str,
        *,
        start: str | None = None,
        end: str | None = None,
        max_records: int = 50,
        request_timeout_ms: int = 8000,
        poll_attempts: int = 8,
        poll_interval_seconds: float = 1.0,
    ) -> list[dict]:
        """Run one DQL query and return its records.

        Raises `DynatracePlatformError` on a transport failure, a rejected query
        and a query that does not finish within `poll_attempts`.
        """
        body: dict[str, Any] = {
            "query": query,
            "maxResultRecords": max_records,
            "requestTimeoutMilliseconds": request_timeout_ms,
        }
        if start:
            body["defaultTimeframeStart"] = start
        if end:
            body["defaultTimeframeEnd"] = end

        payload = self._send(
            "POST", QUERY_EXECUTE_PATH, json=body, operation="query:execute"
        )
        request_token = payload.get("requestToken")

        for attempt in range(poll_attempts):
            state = str(payload.get("state", "")).upper()
            if state == "SUCCEEDED":
                return _records(payload)
            if state in {"FAILED", "CANCELLED"}:
                raise DynatracePlatformError(f"DQL query {state.lower()}: {_problem(payload)}")
            if not request_token:
                raise DynatracePlatformError(
                    f"DQL query returned state={state or 'unknown'} without a requestToken"
                )

            logger.debug("dql poll %s/%s state=%s", attempt + 1, poll_attempts, state)
            time.sleep(poll_interval_seconds)
            payload = self._send(
                "GET",
                QUERY_POLL_PATH,
                params={"request-token": request_token},
                operation="query:poll",
            )
            request_token = payload.get("requestToken") or request_token

        raise DynatracePlatformError(
            f"DQL query still running after {poll_attempts} polls; giving up"
        )

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        operation: str,
    ) -> dict:
        response = self._http.request(
            method, path, json=json, params=params, headers=self._auth()
        )
        if response.status_code == 429:
            raise DynatracePlatformError(
                "rate limited by Dynatrace (429); back off and retry", status_code=429
            )
        if response.status_code >= 400:
            raise DynatracePlatformError(
                f"{operation} -> {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:  # a non-JSON body from a gateway, not from Grail
            raise DynatracePlatformError(
                f"{operation} returned a non-JSON body: {response.text[:200]}",
                status_code=response.status_code,
            ) from exc

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DynatracePlatformClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _records(payload: dict) -> list[dict]:
    result = payload.get("result") or {}
    records = result.get("records") or []
    return [record for record in records if isinstance(record, dict)]


def _problem(payload: dict) -> str:
    """The provider's own explanation when a query fails, if it gave one."""
    notifications = (payload.get("metadata") or {}).get("grail", {}).get("notifications") or []
    for notification in notifications:
        message = notification.get("message") if isinstance(notification, dict) else None
        if message:
            return str(message)[:300]
    return "no reason reported"
