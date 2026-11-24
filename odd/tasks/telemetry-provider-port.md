# telemetry-provider-port

Objective: make the telemetry side of the system provider-neutral so a second observability tool is a new adapter, not a change to the domain; exactly one provider is active per run, and Dynatrace is the first plugin.
Branch: `main` (this directory is a git repository as of the kestrel rename; work commits are local).
Delivery: one work unit, uncommitted (the owner decides the commit). The 400-line heuristic is
exceeded on purpose — 512 insertions / 129 deletions in tracked files plus 5 new files — because the
domain vocabulary change cannot land half-applied.
Test runner: `make test` (`docker compose run --rm --no-deps ... pytest -q`), `make lint`.

## Specs

**S1 — The domain has no vendor nouns.**
`Incident.source` and `Evidence` must express "which tool produced this" and "what kind of fact is
this" without enumerating vendors. Acceptance: no member of `domain/` names Dynatrace, and a test
fails if a vendor name is added back.

**S2 — Exactly one telemetry provider is active per run.**
Selection happens once, in the composition root, from configuration. Acceptance: `TELEMETRY_PROVIDER`
chooses the adapter; an unknown value fails at startup with a typed error instead of degrading
silently; no node selects, constructs or discovers a provider.

**S3 — The provider capability is a port declared by the application.**
The core asks "gather evidence for this incident, inside this scope" and receives `Evidence`.
Acceptance: `TelemetryProvider` is a `Protocol` in `application/ports.py`; `application/` imports no
vendor transport and no framework.

**S4 — Scope is a policy value, not a setting read at run time.**
`DYNATRACE_SCOPE_TAGS` today is configured and used by nothing. Acceptance: the scope is frozen into
`CoreDeps` as a value, passed to the provider, and a provider returns no evidence for an incident
outside it. An empty scope means unrestricted (today's behaviour), stated in the docstring.

**S5 — A provider outage degrades the run; a bug does not hide.**
Acceptance: a provider that cannot answer raises `TelemetryUnavailable`, which `investigate` records
as low-confidence evidence with the reason; unexpected exceptions are not swallowed. No provider
configured is still a degraded run, never a crash.

**S6 — Inbound mapping is per provider.**
Acceptance: `POST /incidents/webhook/{provider}` resolves a mapper registry; `dynatrace` keeps working
at the same path; an unknown provider answers 404. The API holds no vendor mapping.

**S7 — Adding a provider costs one module and one registry entry.**
Acceptance: a new provider implements the port in `adapters/outbound/` and is declared in the provider
registry. Nothing in `domain/` or `application/` changes.

**S8 — Existing surfaces and credentials keep working.**
`DYNATRACE_*` env names, `/incidents/webhook/dynatrace`, `make smoke` and the Temporal state contract
stay valid. Acceptance: `make smoke` produces the same verdict shape as before.

**S9 — Docs and tests match the code.**
Acceptance: `AGENTS.md` (invariants, ports table, extending), `docs/PROJECT.md` (§5, §6, §12, §15),
`.env.example` and the `Makefile` describe the provider seam; `make test` and `make lint` pass.

## Tasks

| ID | Spec | Description | Route | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| T1 | S1 | `domain/models.py`: `Incident.source: str`, `EvidenceKind`, neutral `Evidence` | inline | done | `test_telemetry.py` |
| T2 | S4 | `domain/policy.py`: `InvestigationScope` value with `allows()` | inline | done | unit test |
| T3 | S3/S5 | `application/ports.py` + `application/errors.py`: `TelemetryProvider`, `TelemetryUnavailable` | inline | done | `test_the_core_declares_its_ports` |
| T4 | S2/S4 | `application/deps.py`: `telemetry` port + frozen `scope`, drop `dynatrace_configured` | inline | done | `test_application.py` |
| T5 | S5 | `application/nodes.py`: `investigate` through the port, degraded paths, neutral `verify` text | inline | done | `test_application.py` |
| T6 | S1/S4 | `adapters/outbound/dynatrace_provider.py`: problem → neutral `Evidence`, scope filter, 404 = no evidence | inline | done | `test_telemetry.py` |
| T7 | S2/S7 | `adapters/outbound/providers.py`: registry, build/describe, unknown provider error | inline | done | `test_application.py` |
| T8 | S6 | `adapters/inbound/webhooks.py` + `/incidents/webhook/{provider}` | inline | done | `test_control_plane.py` |
| T9 | S2 | `config.py` (`telemetry_provider`), `worker.py`, `api.py` health, `cli.py` `provider-check` | inline | done | `test_application.py` |
| T10 | S9 | Docs, `.env.example`, `Makefile`, `checkpoint.py` allowlist | inline | done | `make lint`/`make test` |

## Log

**L1 — user request (verbatim).**
> si tengo acceso al tenat de Dynatrace, pero antes de esto, me gustaría que esto fuera multiprovedor
> es decir no acoplarse a Dynatrace y poder conectarlo con plugins configurables a otras erramientas
> (Cloudwoch, datadog, grafana...) eso si el rpimer plugin será dynatraca. Esto esta contemplado? igual
> debería ser el primer paso

**L2 — scope decision (verbatim answer).**
> solo uno activo configurable

One active provider per deployment, chosen in configuration. Not a fan-out: no evidence ranking
across tools, no conflicting-severity resolution, no per-provider degradation matrix. That keeps
`route_after_investigation` (confidence-based, bounded) meaningful.

**L3 — audit evidence: where the vendor leaked (read in the code, not assumed).**
- `domain/models.py:53` — `source: Literal["dynatrace", "manual"]`: adding a tool edits the domain.
- `domain/models.py:72-74` — `Evidence.source` enumerates `dynatrace_problems|logs|metrics`: a
  CloudWatch log cannot be expressed without changing the domain.
- `application/deps.py:32` — `dynatrace_configured: bool`: a vendor-named policy value inside the
  application layer.
- `application/nodes.py:114,120,133` — hardcoded `"dynatrace_problems"` and vendor text.
- `api.py:137`, `cli.py:102`, `Makefile:82` — one hardcoded surface per vendor noun.
- `config.py:67` — `dynatrace_configured` lives in settings, so every consumer inherits the coupling.
- `config.py:57` `dynatrace_scope_tags` — configured, **used by nothing** (`grep` outside `config.py`
  returns zero hits). The governance promise in §12 was documentation only.

**L4 — design decisions.**
- The port declares `investigate()` only. `ports.py` states the rule "a port is added when a use case
  needs it, not in anticipation", and `verify` is still a stub; `confirm_resolved()` lands with
  Roadmap Phase 4. What unblocks Phase 4 now is the *neutral vocabulary*, not an unused method.
- `provider_id` is "who produced this fact", so it also covers non-vendor producers (`kestrel`,
  `human`, `runbook`). It is not a `Literal`: a new adapter must not need a domain edit.
- Degradation is expressed as `Evidence(kind=NOTE, confidence=0.0)` rather than a silent success, so
  the audit trail says what could not be gathered and `route_after_investigation` still retries.
- `/health` drops `dynatrace_configured`. It cannot answer "is the active provider configured?" without
  the API constructing an outbound adapter, which is the coupling this change removes. The answer now
  lives where it is per-run and unforgeable: the run's own evidence, plus the worker startup log.
- `dynatrace-check` becomes `provider-check`; the old `make` target stays as a one-line alias so the
  documented muscle memory keeps working.

**L5 — serialized-state consequence (accepted, not hidden).**
`Evidence.source` → `provider` and `Incident.source` `Literal` → `str` change the shape of types that
cross the Temporal data converter. A run parked *before* this change cannot be replayed after it.
`docs/PROJECT.md` §9 already states there is nothing to migrate when the graph changes shape, and
`make reset` wipes the local server. Recorded here so it is a decision, not a surprise.

**L6 — test evidence (honest about what was and was not RED).**
The tests for this unit were written against the new model rather than *before* it, so there is no
honest RED transcript for them and none is claimed. What was verified instead:
- the vendor-noun guard was mutation-probed: adding `_LEAK = "dynatrace"` to
  `application/nodes.py` makes `test_the_core_names_no_telemetry_vendor[application]` fail with
  `{'application/nodes.py': ['dynatrace']}`, and reverting it makes the suite green again;
- the architecture rule was extended, not relaxed: `api.py` may not import
  `adapters/outbound/providers` or the provider adapters;
- `make smoke` caught a real bug the unit suite missed (L8), which is why every node is now
  exercised by one test instead of trusting that the constructors were updated.

**L7 — verification evidence.**
- `make lint` → All checks passed.
- `make test` → 87 passed, 1 skipped (88 collected; was 61/1).
- `make up` (rebuild) → `/health` 200 with `"telemetry_provider":"dynatrace"`; worker logs
  `telemetry=dynatrace configured=False scope=unrestricted`.
- `make provider-check` → `telemetry provider: dynatrace` + "dynatrace is not configured
  (DYNATRACE_ENV_URL + read token or OAuth client)", exit 2. Correct: `.env` has no credentials,
  and the run degrades instead of failing to boot.
- `make smoke` → same verdict shape as before: `phase=failed`, `resolved=false`, one action
  `executed=False`.
- Webhook by provider: `POST /incidents/webhook/datadog` → 404 `unknown incident provider
  'datadog' (configured mappers: dynatrace)`; `POST /incidents/webhook/dynatrace` → 202 and a run;
  `{"problemId": ["bad"]}` → 422.
- Durability with the new serialized shape: with `PD-p1` parked awaiting approval
  (`run_id 01a20cca-…`), `make down && make up`, then `POST /incidents/PD-p1/resume` → `202`,
  status `COMPLETED` on the same `run_id`. The new `Evidence`/`Incident` fields round-trip through
  Temporal.

**L8 — the bug only the runtime found.**
`make smoke` failed with a pydantic `ValidationError`: `triage` still built `Evidence(source="agent")`
after the model moved to `provider`/`kind`. Every unit test stayed green because none of them called
that node. Fixed, and pinned by `test_every_node_runs_with_the_neutral_vocabulary`, which invokes
every entry of `NODE_FUNCTIONS` so a stale constructor cannot survive a vocabulary change again.

**L8 — what is deliberately left for Phase 1.**
The Dynatrace adapter reads the problem detail and maps it to one `Evidence` (kind `ALERT`). Events,
DQL logs, metrics, evidence ranking, per-run caching and 429 backoff stay in Roadmap Phase 1: this
unit makes the seam real, it does not finish the investigation.

**L9 — pending mirror.**
Engram reported `degraded (HTTP)` during this session, so the recovery copy of this document could not
be written. Local file is authoritative; the mirror must be refreshed when Engram answers again.
