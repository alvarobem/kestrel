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
| T8 | S9 | Live run: `make provider-check` with the real platform token | inline | pending owner token |

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

**L5 — verification evidence.**
- `make lint` → All checks passed.
- `make test` → 107 passed, 1 skipped (108 collected; the suite was 88 before this unit).
- `make provider-check` → pending: needs `DYNATRACE_PLATFORM_URL` + `DYNATRACE_PLATFORM_TOKEN` in `.env`
  (task T8). Nothing in this unit claims a live result it does not have.

**L6 — a test-hygiene bug this unit exposed, fixed.**
`Settings()` reads the process environment and `make test` runs *inside the worker container*, which
`docker compose` fills with the operator's `.env`. The moment real credentials appeared in `.env`, four
tests that assert the *unconfigured* path started failing — and worse, `build_deps(Settings())` built a
real client and reached the tenant from a unit test. `conftest.py` now clears `DYNATRACE_*`, `LANGFUSE_*`
and `LLM_*` for every test. The suite is hermetic again, and that is a property worth keeping: a green
run must not depend on which machine it runs on.

**L7 — pending mirror.**
Engram is unavailable in this session (`could not resolve an authoritative OpenCode runtime session`), so
the recovery copy of this document is not written. The local file is authoritative.
