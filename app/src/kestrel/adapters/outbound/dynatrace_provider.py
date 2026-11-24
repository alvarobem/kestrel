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
import re
import statistics
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

#: Error logs, aggregated in the tenant and **scoped to the incident**. The
#: `{filters}` part is built per incident: without a target there is no query at all
#: (see `_log_filters`).
#:
#: It used to fetch the most recent N lines, which is a sample, not a measurement:
#: one noisy error filled the whole sample and a 351-occurrence failure appeared as
#: twelve findings of one occurrence each. Grouping by content counts everything in
#: the window, and the scope fields travel with each group so the run filter still
#: applies. `dt.security_context` is included for that reason; a deployment that
#: scopes on a field outside this list will see log evidence narrowed away, which
#: fails closed.
_LOG_GROUP_DQL = (
    "fetch logs | filter {filters} "
    "| summarize hits = count(), by: "
    "{{content, service.name, dt.entity.service, dt.security_context}} "
    "| sort hits desc | limit {limit}"
)

#: The same filter, counted instead of listed: the volume of this incident's
#: errors in one window. Two of these — the window and the one before it — answer
#: "is this normal?", which no single-window number can.
_LOG_COUNT_DQL = "fetch logs | filter {filters} | summarize n = count()"

#: Service health for the incident's own entities. Verified against the tenant:
#: `filter:` inside `timeseries` works and multi-metric returns one row of arrays.
#: Not every service reports request metrics, so an empty answer is normal and is
#: not evidence of a fault.
_METRICS_DQL = (
    "timeseries req = sum(dt.service.request.count), "
    "fails = sum(dt.service.request.failure_count), "
    "rt = avg(dt.service.request.response_time), "
    "by: {{dt.entity.service}}, filter: {{{entity_filter}}}, "
    'from: "{start}", to: "{end}", interval: 5m'
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
_MAX_WINDOW = timedelta(hours=6)
#: An incident is explained by what happened *before* it surfaced, so the window
#: opens ahead of `started_at`. A freshly created incident would otherwise have a
#: window a few milliseconds wide and the investigation would read nothing —
#: which is exactly what a live run proved.
_CONTEXT_BEFORE = timedelta(minutes=30)
_MAX_PROBLEMS = 5
#: Distinct messages fetched for aggregation. The tenant groups, so this bounds the
#: groups returned, not the lines scanned: one query counts every error in the
#: window instead of sampling the most recent ones.
_MAX_LOG_GROUPS = 200
#: Findings emitted. A human reads five; the planner gets the same handful.
_MAX_LOG_FINDINGS = 5
_MAX_METRICS = 3
_MAX_TEXT = 500
_MAX_POINTS = 12


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
    return DynatraceTelemetryProvider(
        platform=platform,
        classic=classic,
        environment=settings.dynatrace_environment or None,
    )


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
            f"  latest: {record.get('service.name') or 'unknown service'} "
            f"[{record.get('loglevel') or 'n/a'}] "
            f"{str(record.get('content') or '')[:80]}"
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
        environment: str | None = None,
        lookback: str = "now-2h",
    ) -> None:
        self._platform = platform
        self._classic = classic
        #: Optional second guardrail: pin the environment the run may read.
        self._environment = environment
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
        targets = _target_conditions(incident)
        if not targets:
            # Fail closed. Reading the tenant-wide error stream and calling it
            # "evidence for this incident" is worse than reading nothing: it looks
            # like an investigation and it is noise from other teams.
            return [
                Evidence(
                    provider=PROVIDER_ID,
                    kind=EvidenceKind.NOTE,
                    summary=(
                        "no investigation target: the incident carries no entity id or service "
                        "name, so no tenant-wide query was made"
                    ),
                    confidence=0.0,
                )
            ]

        start, end = _window(incident)
        span = end - start
        filters = _log_filters(targets, self._environment)
        metrics_dql = _metrics_dql(incident, start, end)

        problems, problem_error = await asyncio.to_thread(
            _query,
            platform,
            _DAVIS_DQL.format(limit=_MAX_PROBLEMS),
            start.isoformat(),
            end.isoformat(),
            _MAX_PROBLEMS,
        )
        logs, log_error = await asyncio.to_thread(
            _query,
            platform,
            _LOG_GROUP_DQL.format(filters=filters, limit=_MAX_LOG_GROUPS),
            start.isoformat(),
            end.isoformat(),
            _MAX_LOG_GROUPS,
        )
        # The same aggregation over the span immediately before: a code that did
        # not exist yesterday is an incident, whatever its absolute volume.
        previous_logs, previous_log_error = await asyncio.to_thread(
            _query,
            platform,
            _LOG_GROUP_DQL.format(filters=filters, limit=_MAX_LOG_GROUPS),
            (start - span).isoformat(),
            start.isoformat(),
            _MAX_LOG_GROUPS,
        )
        metrics, metrics_error = (
            await asyncio.to_thread(
                _query, platform, metrics_dql, start.isoformat(), end.isoformat(), _MAX_METRICS
            )
            if metrics_dql
            else ([], None)
        )

        # The same filter, counted over the window and over the span before it.
        count_dql = _LOG_COUNT_DQL.format(filters=filters)
        current_rows, current_error = await asyncio.to_thread(
            _query, platform, count_dql, start.isoformat(), end.isoformat(), 1
        )
        previous_rows, previous_error = await asyncio.to_thread(
            _query, platform, count_dql, (start - span).isoformat(), start.isoformat(), 1
        )
        baseline_error = current_error or previous_error

        evidence: list[Evidence] = []
        notes: list[Evidence] = []

        for record in problems:
            if scope.allows(_record_scope_candidates(record)):
                evidence.append(_davis_evidence(record))
        minutes = max(1, int(span.total_seconds() // 60))
        evidence.extend(
            _log_findings(
                [row for row in logs if scope.allows(_record_scope_candidates(row))],
                [row for row in previous_logs if scope.allows(_record_scope_candidates(row))],
                minutes,
            )
        )
        for record in metrics:
            item = _metric_evidence(record)
            if item is not None:
                evidence.append(item)

        if not baseline_error:
            baseline = _baseline_evidence(
                _count(current_rows),
                _count(previous_rows),
                minutes,
            )
            if baseline is not None:
                evidence.append(baseline)

        for label, error in (
            ("davis problems", problem_error),
            ("error logs", log_error),
            ("error log baseline", previous_log_error),
            ("service metrics", metrics_error),
            ("error baseline", baseline_error),
        ):
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

        if not evidence and not notes:
            # The tenant answered and the scope dropped everything. Saying "no
            # evidence" here would send the operator looking at the tenant instead
            # of at the filter that discarded it — the distinction cost a full
            # debugging session once.
            fetched = len(problems) + len(logs)
            if fetched:
                notes.append(
                    Evidence(
                        provider=PROVIDER_ID,
                        kind=EvidenceKind.NOTE,
                        summary=(
                            f"{fetched} record(s) retrieved but none matched the run scope "
                            f"({', '.join(scope.tags)})"
                        ),
                        confidence=0.0,
                    )
                )

        if evidence and not incident.entity_ids:
            # A service name is not environment-scoped: the same name exists in
            # prod and in pre, and a single Dynatrace tenant holds them all. Say
            # so in the evidence instead of letting the report imply a precision
            # the join does not have.
            notes.append(
                Evidence(
                    provider=PROVIDER_ID,
                    kind=EvidenceKind.NOTE,
                    summary=(
                        "matched by service name only (the incident carries no entity id): "
                        "the evidence may span environments"
                    ),
                    confidence=0.0,
                )
            )
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


def _window(incident: Incident) -> tuple[datetime, datetime]:
    """The window an incident is explained by: context before it, up to now.

    Opening at `started_at` is wrong — a synthetic or just-fired incident has no
    history inside it, so the investigation reads an empty slice and reports
    "no evidence" for a tenant full of it. Opening `_CONTEXT_BEFORE` earlier and
    flooring the span at `_MAX_WINDOW` keeps a stale payload cheap.
    """
    now = datetime.now(UTC)
    started = incident.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(min(started, now) - _CONTEXT_BEFORE, now - _MAX_WINDOW), now


def _dql_instant(moment: datetime) -> str:
    """A DQL timestamp literal. `Z`, not `+00:00`: the query parser is stricter."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _metrics_dql(incident: Incident, start: datetime, end: datetime) -> str | None:
    """Service health for the incident's entities, or `None` when there is none to ask about."""
    if not incident.entity_ids:
        return None
    entity_filter = " or ".join(
        f"dt.entity.service == {_dql_string(entity_id)}" for entity_id in incident.entity_ids
    )
    return _METRICS_DQL.format(
        entity_filter=entity_filter, start=_dql_instant(start), end=_dql_instant(end)
    )


def _series(values: object) -> list[float]:
    """The numeric points of a timeseries array, dropping the empty buckets."""
    if not isinstance(values, list):
        return []
    return [float(value) for value in values if isinstance(value, (int, float))]


def _target_conditions(incident: Incident) -> list[str]:
    """How this incident is joined to telemetry: by entity id first, then name.

    An entity id (`SERVICE-30CF729466F91717`) is stable and unique; a service name
    is a display string. Both are accepted because not every producer emits an id,
    but the id is what the query leans on when it exists.
    """
    conditions = [
        f"dt.entity.service == {_dql_string(entity_id)}"
        for entity_id in incident.entity_ids
        if entity_id
    ]
    conditions.extend(
        f"service.name == {_dql_string(name)}"
        for name in incident.affected_entities
        if name
    )
    return conditions


def _dql_string(value: str) -> str:
    """A DQL string literal. Quotes and backslashes are escaped, not trusted."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _log_filters(targets: list[str], environment: str | None) -> str:
    """The error-log predicate, scoped to this incident and optionally to one environment."""
    clauses = ['loglevel == "ERROR"', f"({' or '.join(targets)})"]
    if environment:
        clauses.append(f"deployment.environment.name == {_dql_string(environment)}")
    return " and ".join(clauses)


def _log_dql(targets: list[str], environment: str | None, limit: int) -> str:
    """The error-log query, scoped to this incident."""
    return _LOG_GROUP_DQL.format(filters=_log_filters(targets, environment), limit=limit)


def _count(records: list[dict]) -> int:
    """A `summarize count()` answer, which DQL returns as a string."""
    for row in records:
        try:
            return int(float(row.get("n") or 0))
        except (TypeError, ValueError):
            continue
    return 0


def _baseline_evidence(current: int, previous: int, minutes: int) -> Evidence | None:
    """Error volume now against the same span immediately before.

    One number cannot say whether an incident is a change or the status quo: twelve
    errors are a catastrophe against a baseline of zero and a quiet Tuesday against
    a baseline of forty. The ratio is computed from two counts, so it needs no unit.
    """
    if not current and not previous:
        return None

    if not previous:
        trend = "new (no errors in the preceding window)"
    else:
        ratio = current / previous
        if ratio >= 1.5:
            trend = f"{ratio:.1f}x, rising"
        elif ratio <= 0.67:
            trend = f"{ratio:.1f}x, falling"
        else:
            trend = f"{ratio:.1f}x, steady"

    return Evidence(
        provider=PROVIDER_ID,
        kind=EvidenceKind.METRIC,
        summary=f"error log volume: {current} in {minutes}m vs {previous} before — {trend}",
        payload={"current": current, "previous": previous, "window_minutes": minutes},
        confidence=0.7,
    )


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


def _log_findings(
    rows: list[dict], previous: list[dict], minutes: int
) -> list[Evidence]:
    """One finding per error code, with its own baseline.

    The same failure arrives translated into five languages, so the sentence is not
    the identity: the code inside it is. Grouping by code turns 77 translated
    messages into 12 findings, and comparing each code against the window before it
    answers the only question worth asking at an approval gate — is this new, is it
    worse, or is it the status quo.
    """
    if not rows:
        return []

    before = _hits_by_key(previous)
    groups: dict[str, dict] = {}
    for row in rows:
        content = " ".join(str(row.get("content") or "").split())
        if not content:
            continue
        key = _error_code(content) or content
        group = groups.setdefault(
            key,
            {
                "code": _error_code(content),
                "hits": 0,
                "variants": 0,
                "sample": content,
                "service": str(row.get("service.name") or "unknown service"),
            },
        )
        group["hits"] += _hits(row)
        group["variants"] += 1
        if len(content) > len(group["sample"]):
            group["sample"] = content

    findings: list[Evidence] = []
    for key, group in sorted(groups.items(), key=lambda item: item[1]["hits"], reverse=True)[
        :_MAX_LOG_FINDINGS
    ]:
        label = group["code"] or f'"{_clip(group["sample"], 120)}"'
        detail = [f"{group['hits']:,} time(s) in {minutes}m"]
        if group["variants"] > 1:
            detail.append(f"across {group['variants']} variants")
        detail.append(_baseline_phrase(group["hits"], before.get(key, 0)))
        summary = f"{group['service']}: {label} — {', '.join(detail)}"
        if group["code"]:
            summary += f'; e.g. "{_clip(group["sample"], 140)}"'
        findings.append(
            Evidence(
                provider=PROVIDER_ID,
                kind=EvidenceKind.LOG,
                summary=summary,
                # The code is the stable handle: it is what a reviewer cites and
                # what a retried investigation dedupes on.
                external_id=group["code"],
                payload={
                    "service.name": group["service"],
                    "content": _clip(group["sample"], _MAX_TEXT),
                    "code": group["code"],
                    "hits": group["hits"],
                    "variants": group["variants"],
                    "hits_before": before.get(key, 0),
                    "window_minutes": minutes,
                },
                confidence=0.6,
                occurrences=group["hits"],
            )
        )
    return findings


#: The tenant writes the stable identity of a failure inside the message
#: (`PMT Error; code: PMT_PPM_8009; reason: …`). Seen in a live record, not guessed.
_CODE_PATTERN = re.compile(r"code:\s*([A-Za-z0-9_.-]{3,})")


def _error_code(content: str) -> str | None:
    match = _CODE_PATTERN.search(content)
    return match.group(1) if match else None


def _hits(row: dict) -> int:
    for field in ("hits", "n"):
        if field in row:
            try:
                return int(float(str(row[field])))
            except (TypeError, ValueError):
                return 0
    return 0


def _hits_by_key(rows: list[dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in rows:
        content = " ".join(str(row.get("content") or "").split())
        if not content:
            continue
        key = _error_code(content) or content
        totals[key] = totals.get(key, 0) + _hits(row)
    return totals


def _baseline_phrase(current: int, previous: int) -> str:
    """Is this worse than it was, new, or the status quo?"""
    if previous <= 0:
        return "new in this window" if current > 0 else "no occurrences"
    ratio = current / previous
    if ratio >= 1.5:
        return f"{ratio:.1f}x the {previous:,} before (rising)"
    if ratio <= 0.67:
        return f"{ratio:.1f}x the {previous:,} before (falling)"
    return f"{ratio:.1f}x the {previous:,} before (steady)"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _metric_evidence(record: dict) -> Evidence | None:
    """Failure ratio and trend from the service's own request metrics.

    Everything claimed here is unit-free arithmetic on the arrays the tenant
    returned: a ratio of two counts and a comparison between buckets. The raw
    `rt` series travels in the payload without a unit assertion, because the unit
    is Dynatrace's to define and this module has not verified it.
    """
    requests = _series(record.get("req"))
    failures = _series(record.get("fails"))
    response_times = _series(record.get("rt"))
    if not requests and not failures:
        return None

    total_requests = sum(requests)
    total_failures = sum(failures)
    parts: list[str] = []
    if total_requests:
        parts.append(
            f"failure ratio {total_failures / total_requests * 100:.2f}% "
            f"({total_failures:,.0f} of {total_requests:,.0f})"
        )
    elif total_failures:
        parts.append(f"{total_failures:,.0f} failures")

    if len(failures) >= 2:
        parts.append(f"failures/interval {_pattern(failures)}")
    if len(response_times) >= 2:
        parts.append(f"response time {_pattern(response_times)}")

    if not parts:
        return None

    entity_id = str(record.get("dt.entity.service") or "service")
    return Evidence(
        provider=PROVIDER_ID,
        kind=EvidenceKind.METRIC,
        summary=f"{entity_id}: {'; '.join(parts)}",
        external_id=entity_id,
        payload={
            "req": requests[-_MAX_POINTS:],
            "fails": failures[-_MAX_POINTS:],
            "rt": response_times[-_MAX_POINTS:],
        },
        confidence=0.75,
    )


def _pattern(series: list[float]) -> str:
    """How a series behaves across its intervals, without asserting a unit.

    Measured against the *typical* interval, not the last one. Sampling the last
    interval described a service as "steady" while two of its seven intervals were
    on fire — the trough was simply the one that got sampled. A zero baseline is
    the most violent spike there is, not a missing measurement, which is how the
    earlier comparison managed to call it steady too.
    """
    typical = statistics.median(series)
    peak = max(series)
    intervals = len(series)
    if peak <= 0:
        return "no activity in the window"
    if typical <= 0:
        spikes = sum(1 for value in series if value > 0)
        when = f"in {spikes} of {intervals} intervals" if spikes > 1 else "in one interval"
        return f"peak {_amount(peak)} against a typical 0, {when}"
    if peak <= 2 * typical:
        return f"peak {_amount(peak)} vs typical {_amount(typical)} (steady)"
    above = sum(1 for value in series if value > 2 * typical)
    when = f"in {above} of {intervals} intervals" if above > 1 else "in one interval"
    return f"peak {_amount(peak)}, {peak / typical:.0f}x typical, {when}"


def _amount(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,.1f}"


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
