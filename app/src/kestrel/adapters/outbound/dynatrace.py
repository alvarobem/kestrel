"""Dynatrace read client.

Two independent credentials on purpose:

  * read token / OAuth client  -> problems, events, logs, metrics (read scopes)
  * write token                -> only the remediation automation

Nothing in the investigation path may use the write token. That separation is
what makes "the agent can look but cannot touch" enforceable instead of a
convention.

This is a transport adapter and nothing else: every method returns plain data.
Turning a payload into an `Incident` is the inbound adapter's job
(`adapters/inbound/dynatrace_problem.py`) and turning it into `Evidence` is the
use case's job, so this module never imports the domain.

Endpoints used (verify against your tenant, they are versioned by Dynatrace):
  GET  /api/v2/problems
  GET  /api/v2/problems/{problemId}
  GET  /api/v2/events
  GET  /api/v2/metrics/query
  POST /platform/storage/query/execute          (DQL, Grail platforms)
  POST /sso/oauth2/token                        (client-credentials grant)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from kestrel.config import Settings

logger = logging.getLogger(__name__)

_OAUTH_SCOPE = "problems.read events.read logs.read metrics.read"


class DynatraceError(RuntimeError):
    pass


class DynatraceClient:
    def __init__(self, settings: Settings, *, timeout: float = 20.0) -> None:
        if not settings.dynatrace_env_url:
            raise DynatraceError("DYNATRACE_ENV_URL is not set")
        self._base_url = settings.dynatrace_env_url.rstrip("/")
        self._read_token = settings.dynatrace_read_token
        self._client_id = settings.dynatrace_client_id
        self._client_secret = settings.dynatrace_client_secret
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=httpx.HTTPTransport(retries=2),
            headers={"Accept": "application/json"},
        )
        self._oauth_token: str | None = None
        self._oauth_expires_at: float = 0.0

    # --- auth --------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        if self._read_token:
            return {"Authorization": f"Api-Token {self._read_token}"}
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _access_token(self) -> str:
        if self._oauth_token and time.monotonic() < self._oauth_expires_at - 30:
            return self._oauth_token
        if not (self._client_id and self._client_secret):
            raise DynatraceError("set DYNATRACE_READ_TOKEN or DYNATRACE_CLIENT_ID/SECRET")

        response = self._http.post(
            "/sso/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": _OAUTH_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise DynatraceError(
                f"oauth token request failed: {response.status_code} {response.text[:200]}"
            )

        payload = response.json()
        self._oauth_token = payload["access_token"]
        self._oauth_expires_at = time.monotonic() + float(payload.get("expires_in", 300))
        return self._oauth_token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        response = self._http.get(path, params=params, headers=self._auth_headers())
        if response.status_code == 429:
            raise DynatraceError("rate limited by Dynatrace (429); back off and retry")
        if response.status_code >= 400:
            raise DynatraceError(f"GET {path} -> {response.status_code}: {response.text[:300]}")
        return response.json()

    # --- problems ----------------------------------------------------------

    def list_open_problems(self, *, lookback: str = "now-2h", page_size: int = 50) -> list[dict]:
        payload = self._get(
            "/api/v2/problems",
            params={
                "problemSelector": 'status("OPEN")',
                "from": lookback,
                "pageSize": page_size,
            },
        )
        return payload.get("problems", [])

    def get_problem(self, problem_id: str) -> dict:
        return self._get(f"/api/v2/problems/{problem_id}")

    # --- evidence sources --------------------------------------------------

    def query_events(
        self, *, selector: str, lookback: str = "now-2h", page_size: int = 100
    ) -> list[dict]:
        payload = self._get(
            "/api/v2/events",
            params={"eventSelector": selector, "from": lookback, "pageSize": page_size},
        )
        return payload.get("events", [])

    def query_metric(self, *, metric_selector: str, lookback: str = "now-2h") -> dict:
        return self._get(
            "/api/v2/metrics/query",
            params={"metricSelector": metric_selector, "from": lookback, "resolution": "5m"},
        )

    def query_logs_dql(self, *, query: str, lookback: str = "now-2h", limit: int = 200) -> dict:
        """DQL query over Grail. Requires the platform storage scope."""
        response = self._http.post(
            "/platform/storage/query/execute",
            json={"query": query, "defaultTimeframeStart": lookback, "maxResultRecords": limit},
            headers=self._auth_headers(),
        )
        if response.status_code >= 400:
            raise DynatraceError(f"DQL query failed: {response.status_code} {response.text[:300]}")
        return response.json()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DynatraceClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
