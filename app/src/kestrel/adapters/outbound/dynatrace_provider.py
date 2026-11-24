"""Dynatrace as one pluggable telemetry provider.

Three modules, three jobs:

  * `dynatrace_platform.py` — platform transport: Grail DQL over `.apps` + Bearer;
  * `dynatrace.py` — classic transport: API v2 over `.live` + `Api-Token`;
  * this module — the provider: which facts become `Evidence`, what is inside the
    run's scope, and how a transport failure maps onto the port contract.

The platform surface wins whenever it is configured. It is the one that can read
Grail, so it yields what an investigation actually needs: the Davis problem that
fired and the error logs the service is writing. The classic surface stays as a
fallback for tenants that only have an API token.

This is the file a second tool copies. Adding Datadog means writing this module
for Datadog and declaring it in `providers.py`; nothing in `domain/` or
`application/` changes.

Log and problem fields are not guessed: they are the ones a live tenant returned.
A DQL query naming a field that does not exist fails the whole query, so a wrong
guess here costs real evidence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from kestrel.adapters.outbound.dynatrace import DynatraceClient, DynatraceError
from kestrel.adapters.outbound.dynatrace_platform import (
    DynatracePlatformClient,
    DynatracePlatformError,
)
from kestrel.application.errors import TelemetryUnavailable
from kestrel.config import Settings
from kestrel.domain.models import Evidence, EvidenceKind, Incident
from kestrel.domain.policy import InvestigationScope

PROVIDER_ID = "dynatrace"

#: Davis problems still open, newest first. Field names from the documented
#: Davis-in-Grail shape (`fetch events | filter event.type == "davis"`).
_DAVIS_DQL = (
    'fetch events | filter event.type == "davis" and davis.status != "CLOSED" '
    "| fields timestamp, davis.title, davis.status | sort timestamp desc | limit {limit}"
)

#: Error logs, newest first. Every field here appeared in a real record from the
#: tenant (`loglevel`, `content`, `service.name`, `trace_id`, `timestamp`).
_ERROR_LOGS_DQL = (
    'fetch logs | filter loglevel == "ERROR" '
    "| fields timestamp, content, loglevel, service.name, trace_id "
    "| sort timestamp desc | limit {limit}"
)

#: What a scope tag may match on, from the semantic attributes tenants emit.
_SCOPE_FIELDS = (
    "dt.security_context",
    "deployment.environment.name",
    "environment",
    "application",
    "product",
    "service.name",
)

#: A stale webhook must not make the run scan a week of telemetry.
_MAX_WINDOW_HOURS = 6
_MAX_PROBLEMS = 5
_MAX_LOGS = 12
_MAX_TEXT = 500


def platform_configured(settings: Settings) -> bool:
    """Whether the Grail surface can be read with the supplied credential."""
    return bool(settings.dynatrace_platform_url and settings.dynatrace_platform_token)


def configured(settings: Settings) -> bool:
    """Whether the classic API can be read at all."""
    has_auth = bool(settings.dynatrace_read_token) or bool(
        settings.dynatrace_client_id and settings.dynatrace_client_secret
    )
    return bool(settings.dynatrace_env_url) and has_auth


def build(settings: Settings) -> DynatraceTelemetryProvider | None:
    """The adapter for this run, or `None` when nothing can be read."""
    platform = DynatracePlatformClient(settings) if platform_configured(settings) else None
    classic = DynatraceClient(settings) if configured(settings) else None
    if platform is None and classic is None:
        return None
    return DynatraceTelemetryProvider(platform=platform, classic=classic)


def describe(settings: Settings) -> tuple[bool, list[str]]:
    """Diagnostic: prove the credential works and show what the tool sees.

    Never raises. A failing credential is reported as one readable line, because
    the operator reads this to find out *why* a run degraded, and a traceback
    hides exactly the message they need.
    """
    if platform_configured(settings):
        return _describe_platform(settings)
    if configured(settings):
        return _describe_classic(settings)
    return False, [
        "dynatrace is not configured. Set DYNATRACE_PLATFORM_URL + DYNATRACE_PLATFORM_TOKEN "
        "(platform, preferred) or DYNATRACE_ENV_URL + a read token (classic)."
    ]


def _describe_platform(settings: Settings) -> tuple[bool, list[str]]:
    try:
        with DynatracePlatformClient(settings) as client:
            records = client.execute("fetch logs | limit 1", max_records=1, poll_attempts=5)
    except DynatracePlatformError as exc:
        return False, [f"platform DQL failed: {exc}"]

    lines = [f"platform DQL ok | `fetch logs | limit 1` returned {len(records)} record(s)"]
    if records:
        record = records[0]
        lines.append(
            f"  latest: {record.get('service.name', '?')} "
            f"[{record.get('loglevel', '?')}] at {record.get('timestamp', '?')}"
        )
    return True, lines


def _describe_classic(settings: Settings) -> tuple[bool, list[str]]:
    try:
        with DynatraceClient(settings) as client:
            problems = client.list_open_problems(lookback="now-2h", page_size=5)
    except DynatraceError as exc:
        return False, [f"classic API failed: {exc}"]

    lines = [f"classic API ok | open problems in the last 2h: {len(problems)}"]
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

    def __init__(
        self,
        *,
        platform: DynatracePlatformClient | None = None,
        classic: DynatraceClient | None = None,
        lookback: str = "now-2h",
    ) -> None:
        self._platform = platform
        self._classic = classic
        self._lookback = lookback

    async def investigate(
        self, incident: Incident, scope: InvestigationScope
    ) -> list[Evidence]:
        if self._platform is not None:
            return await self._investigate_platform(self._platform, incident, scope)
        if self._classic is not None:
            return await self._investigate_classic(self._classic, incident, scope)
        raise TelemetryUnavailable(PROVIDER_ID, "no Dynatrace surface is configured")

    def close(self) -> None:
        if self._platform is not None:
            self._platform.close()
        if self._classic is not None:
            self._classic.close()

    # --- Grail: the surface that can actually explain an incident -----------

    async def _investigate_platform(
        self,
        platform: DynatracePlatformClient,
        incident: Incident,
        scope: InvestigationScope,
    ) -> list[Evidence]:
        start, end = _window(incident)

        problems, problem_error = await asyncio.to_thread(
            _query, platform, _DAVIS_DQL.format(limit=_MAX_PROBLEMS), start, end, _MAX_PROBLEMS
        )
        logs, log_error = await asyncio.to_thread(
            _query, platform, _ERROR_LOGS_DQL.format(limit=_MAX_LOGS), start, end, _MAX_LOGS
        )

        evidence: list[Evidence] = []
        notes: list[Evidence] = []

        for record in problems:
            if scope.allows(_record_scope_candidates(record)):
                evidence.append(_davis_evidence(record))
        for record in logs:
            if scope.allows(_record_scope_candidates(record)):
                evidence.append(_log_evidence(record))

        for label, error in (("davis problems", problem_error), ("error logs", log_error)):
            if error:
                notes.append(
                    Evidence(
                        provider=PROVIDER_ID,
                        kind=EvidenceKind.NOTE,
                        summary=f"{label} unavailable: {error[:300]}",
                        confidence=0.0,
                    )
                )

        if not evidence and (problem_error or log_error):
            # Nothing gathered *and* something failed: that is a provider outage,
            # not an empty tenant, and the run must record it as such.
            raise TelemetryUnavailable(PROVIDER_ID, problem_error or log_error or "no evidence")
        return [*evidence, *notes]

    # --- classic API v2: the fallback --------------------------------------

    async def _investigate_classic(
        self, classic: DynatraceClient, incident: Incident, scope: InvestigationScope
    ) -> list[Evidence]:
        try:
            problem = await asyncio.to_thread(classic.get_problem, incident.id)
        except DynatraceError as exc:
            # 404 means the tool simply does not know this incident (a smoke run,
            # a manual incident, another tenant). That is "no evidence", not an
            # outage, and it must not be reported as one.
            if exc.status_code == 404:
                return []
            raise TelemetryUnavailable(PROVIDER_ID, str(exc)) from exc

        if not scope.allows(_problem_scope_candidates(problem)):
            return []

        return [_problem_evidence(problem)]


def _query(
    platform: DynatracePlatformClient,
    dql: str,
    start: str,
    end: str,
    max_records: int,
) -> tuple[list[dict], str | None]:
    """Run one query; a failure is returned, not raised, so the other query survives."""
    try:
        return platform.execute(dql, start=start, end=end, max_records=max_records), None
    except DynatracePlatformError as exc:
        return [], str(exc)


# --- helpers ---------------------------------------------------------------


def _window(incident: Incident) -> tuple[str, str]:
    """The incident's own time window, floored so a stale payload stays cheap."""
    now = datetime.now(UTC)
    started = incident.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    start = max(started, now - timedelta(hours=_MAX_WINDOW_HOURS))
    return start.isoformat(), now.isoformat()


def _record_scope_candidates(record: dict) -> list[str]:
    return [str(record[field]) for field in _SCOPE_FIELDS if record.get(field)]


def _problem_scope_candidates(problem: dict) -> list[str]:
    """What a scope tag may match on in a classic problem payload."""
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


def _project(record: dict, fields: tuple[str, ...]) -> dict:
    """Keep the state small: it is checkpointed after every node and replayed."""
    projected: dict = {}
    for field in fields:
        value = record.get(field)
        if isinstance(value, str) and len(value) > _MAX_TEXT:
            value = value[:_MAX_TEXT] + "…"
        projected[field] = value
    return projected


def _davis_evidence(record: dict) -> Evidence:
    title = str(record.get("davis.title") or "davis problem")
    status = str(record.get("davis.status") or "UNKNOWN")
    return Evidence(
        provider=PROVIDER_ID,
        kind=EvidenceKind.ALERT,
        summary=f"davis: {title} (status={status})",
        payload=_project(record, ("timestamp", "davis.title", "davis.status")),
        # The tool asserts the problem directly.
        confidence=0.8,
    )


def _log_evidence(record: dict) -> Evidence:
    service = str(record.get("service.name") or "unknown service")
    content = str(record.get("content") or "")
    return Evidence(
        provider=PROVIDER_ID,
        kind=EvidenceKind.LOG,
        summary=f"{service}: {content[:200]}",
        # The trace id is the handle that ties this log to the request that failed.
        external_id=str(record["trace_id"]) if record.get("trace_id") else None,
        payload=_project(record, ("timestamp", "loglevel", "service.name", "content")),
        confidence=0.6,
    )


def _problem_evidence(problem: dict) -> Evidence:
    severity = problem.get("severityLevel", "UNKNOWN")
    status = problem.get("status", "UNKNOWN")
    return Evidence(
        provider=PROVIDER_ID,
        kind=EvidenceKind.ALERT,
        summary=f"{problem.get('title', 'untitled problem')} (severity={severity}, status={status})",
        external_id=problem.get("problemId"),
        payload=problem,
        confidence=0.8,
    )
