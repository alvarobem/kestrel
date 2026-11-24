# dynatrace-platform-investigation

Objective: make the Dynatrace provider investigate through the platform surface (Grail DQL) instead of the classic
API, so one credential yields real evidence — Davis problems and error logs — and the classic token stops being a
dependency for reading.
Branch: `feat/telemetry-provider-port` (builds on the provider seam).
Delivery: one work unit, committed on the branch. Test runner: `make test`, `make lint`, then a live run against the tenant.

## Specs

**S1 — The DQL transport uses the documented endpoint and authenticates with the platform token.**
Acceptance: `POST https://{env}.apps.dynatrace.com/platform/storage/query/v1/query:execute` with
`Authorization: Bearer`, JSON body carrying `query` and `maxResultRecords`. The old
`/platform/storage/query/execute` path (no `/query/v1/`) must not survive anywhere: it returned a Spring
"no static resource" 404 in the live tenant.

**S2 — DQL is asynchronous and is polled, bounded.**
`query:execute` returns `state` and, when unfinished, a `requestToken` that must be used with
`GET .../query:v1/query:poll?request-token=`. Acceptance: a `RUNNING` first response followed by a
`SUCCEEDED` poll returns the records; a query that never finishes raises `DynatraceError` after a bounded
number of polls instead of hanging the activity.

**S3 — Investigation reads Davis problems and error logs, scoped to the incident window.**
Acceptance: a run produces `Evidence` of kind `ALERT` from `fetch events | filter event.type == "davis"`
and kind `LOG` from `fetch logs | filter loglevel == "ERROR"`, using only fields verified to exist (Davis
fields from Dynatrace docs, log fields from the tenant's own payload). The time window starts at the
incident's `started_at`, capped at 6 hours back so a stale webhook cannot scan a week of data.

**S4 — The state stays small.**
Evidence payloads cross the Temporal data converter and grow workflow history. Acceptance: records are
projected to a fixed field set and text is truncated; at most 5 problems and 12 logs per investigation.

**S5 — The scope filter still applies, post-query.**
Tag matching happens in Python over the fields the tenant actually emits (`dt.security_context`,
`deployment.environment.name`, `environment`, `application`, `product`, `service.name`). Acceptance: with a
scope configured, records outside it are not emitted; with no scope, everything is emitted (documented
default).

**S6 — One active provider, platform preferred.**
Acceptance: when `DYNATRACE_PLATFORM_URL` + `DYNATRACE_PLATFORM_TOKEN` are set the provider investigates over
DQL; when only the classic credentials exist it degrades to the old behaviour; when neither exists
`telemetry` is `None` and the run records why. `TELEMETRY_PROVIDER` and the registry are unchanged.

**S7 — `provider-check` proves the credential and never dumps a traceback.**
Acceptance: it runs `fetch logs | limit 1` against the platform surface, prints the record count and the
analysis window, and on failure prints one readable line + exit code 2.

**S8 — No broken method survives.**
`DynatraceClient.query_logs_dql` cannot work (classic tokens cannot query Grail, and its path was wrong): it
is deleted, not fixed in place. DQL lives in one module.

**S9 — Docs and tests match.**
`AGENTS.md`, `docs/PROJECT.md` §12, `.env.example` and `docker-compose.yml` describe the two surfaces.
`make test` and `make lint` pass; a live `make provider-check` against the tenant is the closing evidence.

**S10 — The investigation is scoped to the incident, or it does not run.**
Acceptance: the log query joins on the incident's entity ids (`dt.entity.service`, falling back to
`service.name`) and optionally on `DYNATRACE_ENVIRONMENT`; an incident with no entity id and no service
name triggers **no query** and records why. Reading the tenant-wide error stream is never evidence.

**S11 — Service metrics are read when the service reports them.**
Acceptance: one `METRIC` item per incident entity from
`timeseries sum(dt.service.request.count), sum(dt.service.request.failure_count),
avg(dt.service.request.response_time)` with `filter: {dt.entity.service == "…"}`; the summary states a
failure ratio and a latest-vs-baseline comparison, both unit-free, and the raw response-time series
travels in the payload without a unit claim. A service with no request metrics produces no item.

**S12 — Evidence is collapsed and ranked before anyone reads it.**
Acceptance: `domain/evidence.py` (pure) provides `signature`, `merge_repeated` (sums repeats within one
read), `dedupe` (keeps the larger count across reads) and `rank_evidence` (kind weight + relevance to the
incident + capped repetition bonus); ranking never mutates `confidence`; `investigate` merges its own
read, and `report` prints the top five ranked items.

**S13 — A deterministic investigation is retried only when its provider failed.**
Acceptance: `route_after_investigation` returns `investigate` only when
`investigation_degraded` is set and attempts remain; an empty-but-successful read goes straight to
`plan`, because re-running it re-reads the same window in the same second.

**S14 — Error volume is compared against the preceding equal window.**
Acceptance: the same log filter is counted over the incident window and over the span immediately before
it (same length, no gap); the item reports both counts and a trend that needs no unit; two empty windows
produce no item, and a baseline failure is a NOTE that never costs the logs.

## Out of scope, stated on purpose

Metrics and per-service log filtering. A wrong metric selector returns an empty result silently, and the
per-tenant metric names are not known yet; inventing them would produce evidence that looks real and is
empty. They land in the next slice once we have seen the tenant's metric catalogue.

## Tasks

| ID | Spec | Description | Route | Status |
| --- | --- | --- | --- | --- |
| T1 | S1/S2 | `adapters/outbound/dynatrace_platform.py`: Bearer client, `/query/v1/query:execute`, bounded polling | inline | done |
| T2 | S8 | Delete `DynatraceClient.query_logs_dql`; DQL lives in one module | inline | done |
| T3 | S3/S4/S5 | `dynatrace_provider.py`: Davis + error logs, window, projection, scope | inline | done |
| T4 | S6 | `config.py` + `providers.build`: platform preferred, classic fallback | inline | done |
| T5 | S7 | `provider-check` probes DQL and reports cleanly | inline | done |
| T6 | S9 | Tests with the tenant's real response shape + polling/limits failures | inline | done |
| T7 | S9 | `.env.example`, compose, `AGENTS.md`, `docs/PROJECT.md` | inline | done |
| T8 | S9 | Live run: `provider-check` + `smoke` with the real platform token | inline | done |
| T9 | S10 | Scope the log query to the incident's entities + environment, fail closed without one | inline | done |
| T10 | S11 | Service metrics: failure ratio + trend per entity, absence tolerated | inline | done |
| T11 | S12 | `domain/evidence.py` + merging in `investigate` + ranked `top_evidence` in the report | inline | done |
| T12 | S13 | Retry only on `investigation_degraded`, not on an empty deterministic read | inline | done |
| T13 | S14 | Error-volume baseline against the preceding equal window | inline | done |

## Log

**L1 — user request (verbatim).**
> si, arranca el flujo ODD para implementar todo esto

**L2 — the diagnosis that unblocked it (evidence, not assumption).**
The tenant answered a DQL query with `200` and `{"state":"SUCCEEDED","progress":100,"result":{"records":[…]}}`
against `https://psk34092.apps.dynatrace.com/platform/storage/query/v1/query:execute` with
`Authorization: Bearer <platform token>`. The previous 404 was
`{"error":{"code":404,"message":"No static resource platform/storage/query/execute …"}}` — the path was
missing `/query/v1/`, and the gateway fell through to its static-resource handler. That wrong path was in
our own `dynatrace.py`, so it would have propagated into Phase 1.

The classic `dt0c01` token answers `403 Token is missing required scope` on every read endpoint
(`problems`, `events`, `metrics`, `entities`, and even `clusterversion`, which requires `DataExport`). The
tenant's own log records carry `dt.auth.origin: dt0c01.…`, which identifies it as the ingestion token of the
`customer-experience` pipeline: it is an ingest credential and must not be given read scopes.

**L3 — field names verified against the tenant, not guessed.**
`fetch logs | limit 1` returned `timestamp`, `content`, `loglevel`, `service.name`, `dt.entity.service`,
`trace_id`, `span_id`, `dt.security_context`, `deployment.environment.name`, `environment`, `application`,
`product`. Those are the only log fields this unit queries and projects. An earlier query of mine,
`fetch dt.davis.problems`, was invented; the documented Davis shape is `fetch events | filter
event.type == "davis"`.

**L4 — design decisions.**
- The platform client is synchronous like the classic one; the provider bridges with `asyncio.to_thread`.
  One pattern in the adapter layer beats two.
- Polling is bounded by attempts, not by wall clock: an activity must fail with a reason before its
  `start_to_close_timeout`, and the node already degrades on `TelemetryUnavailable`.
- Evidence payloads are projected and truncated: state is checkpointed after every node and also travels
  through Temporal history, so a raw 50-record log dump is a cost paid on every replay.
- Scope stays a Python filter. Pushing it into DQL would require knowing each tenant's semantic
  attributes; the current rule is coarse and documented as such.

**L5 — verification evidence (live tenant, not fixtures).**
- `make lint` → All checks passed.
- `make test` → 108 passed, 1 skipped (109 collected; the suite was 88 before this unit).
- `make provider-check` →
  `platform DQL ok | fetch logs | limit 1 returned 1 record(s)` against
  `https://psk34092.apps.dynatrace.com`.
- `make smoke` → `summary: "…: 13 evidence item(s), 1 action(s), resolved=False"`, and the workflow
  history shows the `investigate` activity returning **12 `LOG` evidence items** from
  `order-handler` with their `trace_id` (`18e7aaf75fa4c6c420a1434fa89ef5f9`, …), collected at
  `2026-11-24T10:02:37Z`. Real telemetry, real trace handles.
- Davis returns 0 rows in this tenant: it has synthetic monitors and logs, no open Davis problems.
  The query shape is the documented one; the emptiness is the tenant's, not the code's.

**L6 — a test-hygiene bug this unit exposed, fixed.**
`Settings()` reads the process environment and `make test` runs *inside the worker container*, which
`docker compose` fills with the operator's `.env`. The moment real credentials appeared in `.env`, four
tests that assert the *unconfigured* path started failing — and worse, `build_deps(Settings())` built a
real client and reached the tenant from a unit test. `conftest.py` now clears `DYNATRACE_*`, `LANGFUSE_*`
and `LLM_*` for every test. The suite is hermetic again, and that is a property worth keeping: a green
run must not depend on which machine it runs on.

**L7 — two bugs that only a live run could find, and the message that prevents the second.**
1. `_window` opened at `incident.started_at`, so a just-created incident had a window a few
   milliseconds wide: the queries ran, matched nothing, and the run reported "no evidence" against a
   tenant full of it. The window now opens `_CONTEXT_BEFORE` (30 min) earlier — an incident is explained
   by what happened before it surfaced — and is still floored at 6 h.
2. `DYNATRACE_SCOPE_TAGS` in the operator's `.env` was `c` (a truncated paste). `InvestigationScope`
   filtered out **every** record the tenant returned, and the evidence said "returned no evidence for
   this incident inside the run scope" — which sends a human to look at the tenant instead of the filter.
   The provider now distinguishes the cases: *retrieved N record(s) but none matched the run scope
   (tags)*. That single message would have collapsed this debugging session to one line.

Both are pinned: one test asserts a fresh incident investigates a non-empty window, and one asserts a
fully-filtered result yields an explicit scope note rather than an empty list.

**L9 — the defect the operator caught: tenant-wide noise.**
The first version filtered only `loglevel == "ERROR"` and the time window, then took the 12 most recent
matches **of the whole tenant**. In a tenant with dozens of services (one writing 10.6M logs) that is
random traffic from other teams, presented as evidence for one incident. The operator saw it immediately
and was right.

Root cause, verified in the code: the webhook payload carries
`affectedEntities[].entityId.id` (for example `SERVICE-30CF729466F91717`) and the inbound ACL threw it
away unless the display name was missing, so the investigation had nothing to join on.

Fixed:
- `Incident.entity_ids` keeps the provider-side id, and the ACL fills it separately from the names.
- The log query becomes `loglevel == "ERROR" and (dt.entity.service == "…" or service.name == "…")`,
  with `deployment.environment.name` pinned when `DYNATRACE_ENVIRONMENT` is set.
- **No target means no query.** The provider returns a `NOTE` explaining it. Fail closed, like `execute`
  does when no executor is wired.
- DQL literals escape quotes and backslashes: a service name is telemetry data, not a trusted string.

Live proof, same tenant, same window:
- before: 12 logs mixed across `order-handler`, `lounge-manager`, `java-micronaut`;
- after `POST /incidents/webhook/dynatrace` with `entityId.id = SERVICE-30CF729466F91717`:
  **12 logs, all `order-handler`**, each with its `trace_id`.
`provider-check` still queries wide on purpose: it is a diagnostic, not an investigation.

**L10 — metrics, after reading the catalogue instead of guessing.**
Probed against the tenant before writing anything: `dt.service.request.count`, `dt.service.request.failure_count`,
`dt.service.request.response_time`, `dt.host.cpu.usage` and `dt.process.cpu.usage` all return data;
`filter: {dt.entity.service == "…"}` inside `timeseries` works, including with several metrics in one call, and
`by: {dt.entity.service}` returns one row per entity; the log-bucket query
(`summarize count(), by: {bin(timestamp, 5m)}` then `sort timestamp`) fails with `FIELD_DOES_NOT_EXIST`, so it
was dropped rather than worked around.

Two findings that shaped the implementation:
- **Request metrics are not universal.** `SERVICE-30CF729466F91717` (`order-handler`) reports no
  `dt.service.request.*` at all while writing thousands of error logs, and 300 other services do. An empty
  metrics answer is therefore normal and produces no item, never a failure note.
- **The unit is Dynatrace's, not ours.** `response_time` looks like microseconds (146341 ≈ 146 ms), which is
  plausible and unverified. The summary therefore claims only what is arithmetic on the arrays: a failure ratio
  and a latest-vs-baseline comparison. The `rt` series rides in the payload raw.

Live proof: an incident with `entity_ids: ["SERVICE-001301D820D224E8"]` produced
`[metric] dynatrace conf=0.75 :: SERVICE-001301D820D224E8: failure ratio 100.00% (64 of 64); failures/interval 4 vs baseline 10 (steady)`.

**L11 — ranking, and what it fixed in one live run.**
The rules are pure and live in the domain, because they are product judgment about *facts*, not transport.
Deliberate choices:
- ranking is a derived view and does **not** touch `confidence` — the provider's number is a claim about its
  own fact, and a rank is our preference; mixing them hides which is which;
- `merge_repeated` sums repeats **within one read** (honest: one query, one window) while `dedupe` keeps the
  larger count **across reads**, because a retried attempt re-reads an overlapping window and summing would
  inflate the volume it reports. That distinction is the whole reason there are two functions;
- the volume bonus is capped at ten occurrences: after that, more repetition adds nothing.

Live proof on the tenant, incident `PD-rank-1` (`order-handler`):
`order-handler errors: 2 evidence item(s)` with
`[log] order-handler: DBM-ROP Failed to retrieve airline information. (x12)` ranked above the triage note.
Thirteen raw facts became two, and the one that explains the incident is first.

**L12 — change evidence via deployment events: analyzed, then deferred by decision.**
The tenant already ingests GitHub-Actions-borne deployments as `event.type == "CUSTOM_DEPLOYMENT"`, and they
carry everything a change source would need *except* attribution:

- `swe-platform.source_url` = `https://github.com/Iberia-Ent/commercial-management--ancillaries--bonos--services-bonos`
  — the repository, straight from telemetry, so the service→repo mapping question disappears;
- `swe-platform.sha`, `last_merge_commit`, `triggering_actor`, `workflow`, `deployment_outcome`,
  `isRollback`, `deployment.release_stage`, `deployment.version`, `dt.event.correlation_id`.

What blocks it: `affected_entity_ids` is `['ENVIRONMENT-0000000000000001']`. Deployments are attached to the
**environment** entity, never to a service, so no deployment can be attributed to an incident. Measured
volume makes that fatal rather than cosmetic: 8 `Production`, 11 `Preproduction`, 14 `Integration` deploys in
one 30-minute window. Feeding those to the planner would repeat exactly the defect fixed in L9 — data that
looks like evidence and is not.

DQL lessons worth keeping (each one cost a probe): `arrayContains(...)` does not exist, `"x" in field` is a
parse error, and a projected field containing dots or hyphens must be backticked
(`` `swe-platform.source_url` ``), otherwise DQL reads it as a subtraction and returns `None` silently.

Decision: **no deployment evidence for now.** The fix belongs in one place — the `swe-platform` integration
should send the service or process group in the `entitySelector` instead of the environment — and it would
make the change correlatable for Davis too, not only for us. Until then the investigation stays at Davis
problems, scoped error logs and service metrics, and no `ChangeSource` port is added. When attribution
arrives, the repo and the sha are already in the event, so GitHub enrichment becomes one `compare` call.

**L13 — two things a live run proved at once.**
- The retry rule was burning work: an empty successful read went back through `investigate` up to
  `max_investigation_attempts` (3), re-querying the same window in the same second. `route_after_investigation`
  now loops only on `investigation_degraded` (the provider could not answer), still bounded by the snapshot.
  Verified on `PD-base-1`: **1** investigate activity, where the earlier runs produced 3.
- The baseline changed the reading of the incident. `error log volume: 27446 in 30m vs 27633 before — 1.0x,
  steady`: the twelve sampled error lines were not a spike but the service's permanent state. That is the
  difference between "something broke" and "this is how it always is", and it is the first evidence in the
  pipeline that can tell them apart. Both counts come from the same filter, and the two windows are adjacent
  and equal in length — a test asserts exactly that, because a longer or overlapping baseline would silently
  produce a meaningless ratio.

**Honest gap:** traces were evaluated and rejected for now (`fetch spans` returns 0 rows in 24h; the services
run with `OTEL_TRACES_EXPORTER=none`), so the link from a log's `trace_id` to the failing downstream call is
not available in this tenant.

**L8 — pending mirror.**
Engram is unavailable in this session (`could not resolve an authoritative OpenCode runtime session`), so
the recovery copy of this document is not written. The local file is authoritative.
