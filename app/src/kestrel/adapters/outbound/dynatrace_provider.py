"""Dynatrace as one pluggable telemetry provider.

Two modules, two jobs:

  * `dynatrace.py` is transport: HTTP in, plain dicts out, no domain types;
  * this module is the provider: which facts become `Evidence`, what is inside
    the run's scope, and how a transport failure maps onto the port contract.

This is the file a second tool copies. Adding Datadog means writing this module
for Datadog and declaring it in `providers.py`; nothing in `domain/` or
`application/` changes.

Depth is Roadmap Phase 1: related events, DQL logs, metrics and evidence ranking
land here, behind the same port. Today the provider reads the incident's own
record and reports it as one `ALERT`.
"""

from __future__ import annotations

import asyncio

from kestrel.adapters.outbound.dynatrace import DynatraceClient, DynatraceError
from kestrel.application.errors import TelemetryUnavailable
from kestrel.config import Settings
from kestrel.domain.models import Evidence, EvidenceKind, Incident
from kestrel.domain.policy import InvestigationScope

PROVIDER_ID = "dynatrace"


def configured(settings: Settings) -> bool:
    """Whether this tenant can be read with the credentials the operator supplied."""
    has_auth = bool(settings.dynatrace_read_token) or bool(
        settings.dynatrace_client_id and settings.dynatrace_client_secret
    )
    return bool(settings.dynatrace_env_url) and has_auth


def build(settings: Settings) -> DynatraceTelemetryProvider | None:
    """The adapter for this run, or `None` when it cannot be read at all."""
    if not configured(settings):
        return None
    return DynatraceTelemetryProvider(DynatraceClient(settings))


def describe(settings: Settings) -> tuple[bool, list[str]]:
    """Diagnostic lines: proves the credential works and shows what the tool sees.

    The flag says whether this provider is readable at all; the lines are what to
    print either way.
    """
    if not configured(settings):
        return False, ["dynatrace is not configured (DYNATRACE_ENV_URL + read token or OAuth client)"]

    lines: list[str] = []
    with DynatraceClient(settings) as client:
        problems = client.list_open_problems(lookback="now-2h", page_size=5)
        lines.append(f"open problems in the last 2h: {len(problems)}")
        for problem in problems:
            lines.append(
                f"  - {problem.get('problemId', 'unknown')} "
                f"[{problem.get('severityLevel', 'UNKNOWN')}] "
                f"{problem.get('title', 'untitled problem')}"
            )
    return True, lines


class DynatraceTelemetryProvider:
    """Read-only Dynatrace access, mapped onto the neutral evidence vocabulary."""

    provider_id = PROVIDER_ID

    def __init__(self, client: DynatraceClient, *, lookback: str = "now-2h") -> None:
        self._client = client
        self._lookback = lookback

    async def investigate(
        self, incident: Incident, scope: InvestigationScope
    ) -> list[Evidence]:
        """The incident's own problem record, if the tool knows it and scope allows it.

        `DynatraceClient` is a synchronous HTTP client, and this node runs as a
        Temporal activity in an event loop: the call is pushed to a thread rather
        than blocking every other activity in the worker process.
        """
        try:
            problem = await asyncio.to_thread(self._client.get_problem, incident.id)
        except DynatraceError as exc:
            # 404 means the tool simply does not know this incident (a smoke run,
            # a manual incident, another tenant). That is "no evidence", not an
            # outage, and it must not be reported as one.
            if exc.status_code == 404:
                return []
            raise TelemetryUnavailable(PROVIDER_ID, str(exc)) from exc

        if not scope.allows(_scope_candidates(problem)):
            return []

        return [_problem_evidence(problem)]

    def close(self) -> None:
        self._client.close()


def _scope_candidates(problem: dict) -> list[str]:
    """What a scope tag may match on: management zone names, tag keys and values."""
    zones = [
        zone.get("name", "")
        for zone in problem.get("managementZones") or []
        if isinstance(zone, dict)
    ]
    tags: list[str] = []
    for tag in problem.get("tags") or []:
        if isinstance(tag, dict):
            tags.extend([tag.get("key", ""), tag.get("value", "")])
    return [candidate for candidate in [*zones, *tags] if candidate]


def _problem_evidence(problem: dict) -> Evidence:
    severity = problem.get("severityLevel", "UNKNOWN")
    status = problem.get("status", "UNKNOWN")
    return Evidence(
        provider=PROVIDER_ID,
        kind=EvidenceKind.ALERT,
        summary=f"{problem.get('title', 'untitled problem')} (severity={severity}, status={status})",
        external_id=problem.get("problemId"),
        payload=problem,
        # The tool asserts the problem directly; how much it explains the incident
        # is what Phase 1 ranking is for.
        confidence=0.8,
    )
