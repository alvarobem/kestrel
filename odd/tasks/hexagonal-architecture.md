# hexagonal-architecture

Objective: make the ports-and-adapters architecture the real, enforced structure of the project instead of an implicit
convention, and document the rule in `AGENTS.md`.
Branch: none (this directory is not a git repository; changes are local files only).
Delivery: work units, no PR.
Test runner: `make test` (`docker compose run --rm --no-deps ... pytest -q`); `make lint`; `make smoke` for the runtime.

## Specs

**S1 — The rule is written down where an agent will read it.**
`AGENTS.md` gains a section that names the four layers, the dependency rule (imports point inward only), the ports and
their adapters, where a new node / client / mapping / setting goes, and which exemptions exist and why.
`docs/PROJECT.md` §6 gains the layer diagram and its dependency rule. Acceptance: the section states the rule
mechanically ("`application/` may not import `error_agent.config`") and not only as a principle.

**S2 — `domain/` is inert.**
`error_agent/domain/` holds the entities (`models.py`) and the run policy (`policy.py`: bounded-attempt defaults and
`RunLimits`). It imports only the standard library, `pydantic`, and `error_agent.domain`.

**S3 — `application/` cannot reach infrastructure.**
`error_agent/application/` holds the use cases (`nodes.py`), the state contract (`state.py`), the port declarations
(`ports.py`), the injected dependencies (`deps.py`) and the flow assembly (`graph.py`). It imports `error_agent.domain`
and itself; it never imports `error_agent.config`, `error_agent.adapters`, `httpx`, `fastapi`, `temporalio`,
`pydantic_settings` or `langfuse`. `langgraph` appears in `graph.py` only, which is the declared seam of the reasoning
runtime. Acceptance: an automated test fails on any violating import.

**S4 — Ports are declared by the application and implemented outside it.**
`approval_gate` asks a `ApprovalGateway`; `execute` asks a `RemediationExecutor`. The core names the capability, the
adapter provides it. No module under `application/` may import `langgraph.types.interrupt`, `httpx` or `subprocess`.
Acceptance: `approval_gate` runs with a fake gateway, with no LangGraph import in the path.

**S5 — One composition root.**
`worker.build_app(settings)` is the only place that turns `Settings` into adapters, `CoreDeps` and a graph. Nothing else
constructs a node map or picks an approval channel. Acceptance: `build_app` works from an explicitly constructed
`Settings` with no environment access and no `get_settings()` cache priming.

**S6 — A driving adapter never imports a driven adapter.**
`api.py` imports the domain, the application, and its sibling inbound modules. The Dynatrace payload → `Incident`
mapping leaves the outbound HTTP client and becomes an inbound anti-corruption module. Acceptance: `api.py` has no
`error_agent.adapters.outbound` import except the Temporal client factory it needs to drive runs.

**S7 — No behaviour change.**
Every guard rail in `AGENTS.md` keeps holding and the observable outcome of a run is identical: same nodes, same edges,
same activity options, `execute` still one attempt, dry run still `resolved=false`, approval still scoped to the latest
decision. Acceptance: the pre-existing guard-rail tests keep passing after being ported to the new seam, and `make smoke`
reports the same verdict shape as before.

**S8 — The rule is a test, not a comment.**
`tests/test_architecture.py` parses every module and asserts the dependency rule, the layer map and the absence of
`interrupt` in the core. It fails when a layer is missing and when an import crosses a boundary.

**S10 — The control plane holds no run rules.**
Starting, reading and deciding a run are use cases of the application
(`application/incidents.py`), reached by the driving adapters through an input port. The run's identity, the
idempotency of ingestion, and the rule that a decision is only legal while the run is waiting are decided there, not
in `api.py`. Acceptance: `decide_incident` refuses a decision for a run that is not pending, with a fake gateway and no
engine; `api.py` and `cli.py` contain no workflow-start assembly and no engine imports.

**S11 — No engine exception escapes the adapter.**
The engine's exceptions are translated into typed application errors (`IncidentAlreadyRunning`, `IncidentNotFound`,
`NoPendingApproval`, `RunUnavailable`) inside `adapters/outbound/runs.py`. Acceptance: the HTTP layer maps application
errors to 409/404/409/503 in one registered handler per error, and a test drives each mapping with a fake gateway.

**S12 — One run input, built once.**
`RunLimits` is built from settings in exactly one place (`Settings.run_limits`) and travels to the gateway as a value;
`api.py` and `cli.py` share the same use case instead of duplicating the argument list. Acceptance: only
`adapters/outbound/runs.py` calls `limits.as_workflow_input()`.

**S9 — Entrypoints move with the code.**
`Dockerfile`, `docker-compose.yml`, `Makefile`, `pyproject.toml` scripts and both documents reference the new module
paths, and `make up && make smoke` proves the stack still boots.

## Tasks

| ID | Spec | Description | Route | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| T1 | S8 | Write `tests/test_architecture.py` (dependency rule, layer map, no `interrupt` in core) | inline | done | RED: collection fails, `error_agent.application` missing |
| T2 | S2 | Create `domain/`: move `models.py`, extract `policy.py` (`RunLimits`, attempt defaults) | inline | done | `make lint` + `make test` |
| T3 | S3/S4 | Create `application/`: `ports.py`, `deps.py`, `state.py`, `nodes.py` without `config`/`langgraph` | inline | done | `test_graph.py` green |
| T4 | S3 | Move flow assembly to `application/graph.py` with injected `CoreDeps` + instrumentation hook | inline | done | `test_graph.py::test_graph_compiles` |
| T5 | S4 | `adapters/outbound/approval.py`: `GraphApprovalGateway` (`interrupt`) + `AutoApprovalGateway` | inline | done | `test_application.py` approval tests |
| T6 | S6 | `adapters/inbound/dynatrace_problem.py`: payload → `Incident`; strip domain types from the HTTP client | inline | done | `test_surface.py` |
| T7 | S5 | `worker.build_app(settings)` composition root; `main()` consumes it | inline | done | `test_composition.py` |
| T8 | S3/S6 | Move `api.py`, `cli.py`, `workflow.py` to `adapters/inbound/`, outbound clients to `adapters/outbound/` | inline | done | `make lint` + `make test` |
| T9 | S9 | Update `Dockerfile`, `Makefile`, `pyproject.toml`, `AGENTS.md`, `docs/PROJECT.md` | inline | done | `make up` + `make smoke` |
| T10 | S7 | Port the guard-rail suite to the injected seam; keep every existing assertion | inline | done | 48 passed, 1 skipped |
| T11 | S10/S11 | `application/incidents.py` + `application/errors.py`; `RunGateway` port in `ports.py` | inline | done | `test_control_plane.py` (rules, with a fake gateway) |
| T12 | S11 | `adapters/outbound/runs.py`: `TemporalRunGateway`, lazy client, engine errors translated | inline | done | `test_architecture.py` engine-free rule |
| T13 | S10/S12 | Rewrite `api.py` (routes + exception handlers only) and `cli.py` (same use cases) | inline | done | 202/409/404/503 verified against the running stack |
| T14 | S12 | `Settings.run_limits`; remove the duplicated `RunLimits(...)` assembly | inline | done | `make lint`, `make test` |
| T15 | S10 | Enforce it: `api.py`/`cli.py` may not import `temporalio`; `api.py` must drive the use cases | inline | done | `test_architecture.py` |

## Log

**L1 — user request (verbatim).**
> revisa si se está aplicando arq hexagonal en todo el proyecto, si no añadelo en el agents.md y implementa los cambios
> con un flujo odd

**L2 — audit evidence (read in the code, not assumed).**
- `graph/nodes.py:15` imports `error_agent.config`; `get_settings()` is called at `:103`, `:168`, `:230`, `:263`. The
  application core reads process-wide environment. Nothing can substitute it, and it is the same seam that produced the
  router-determinism bug fixed in `hardening-existing-code`.
- `graph/nodes.py:13` imports `langgraph.types.interrupt`. The approval use case is welded to the reasoning framework:
  `approval_gate` cannot be exercised without LangGraph.
- No `Protocol` or interface exists anywhere in the tree. The documented next step (`nodes.py:128`, "wire
  DynatraceClient into the investigation node") would put `httpx` in the core.
- `api.py:29` imports `error_agent.clients.dynatrace.problem_to_incident`: a driving adapter importing a driven adapter.
- `config.py:77` `workflow_limits(settings)` is application policy living in the settings module; `api.py:76` and
  `cli.py:56` each assemble part of the run input, so a third adapter would drift.
- `clients/dynatrace.py:154-202` mixes two jobs: HTTP transport and the inbound anti-corruption layer (`_SEVERITY_MAP`,
  `parse_dynatrace_timestamp`, `problem_to_incident`). Because the mapping lives in the outbound client, `api.py` cannot
  use it without importing the client.
- `graph/build.py` is the closest thing to a composition root but it only wraps tracing; `worker.py` and `build.py` split
  the wiring, and `build_graph()` reads settings itself (`build.py:70`).

**L3 — scope decision (recorded, not silent).**
Physical restructure over a "Protocols-only" patch. A dependency rule that the package layout does not express is not
enforced, it is a comment. The blast radius is bounded and mechanical: 4 entrypoint references (`Dockerfile`,
`Makefile` ×2, `pyproject.toml`) plus imports. No behaviour change, verified by the pre-existing guard-rail suite and by
`make smoke` after a rebuild.

**L4 — audit verdict.**
Partly applied, structurally not. The *names* were hexagonal (`clients/` as driven adapters, `api`/`cli` as driving
adapters, `models.py` as entities) and the *dependency rule was inverted in the two places that matter*: the core read
settings, and it imported the reasoning framework. No port existed, so no adapter could be substituted.

**L5 — design decisions.**
- `CoreDeps` carries policy values (`dry_run`, `dynatrace_configured`) and port implementations. Policy as an immutable
  value is correct hexagonal; reading `os.environ` from the core is not.
- `APPROVAL_MODE=approved` stops being a branch inside the guard rail and becomes a wiring choice in the composition
  root (`AutoApprovalGateway`). Same behaviour, but the bypass is now visible where wiring lives. The guard-rail
  contradiction flagged as L9 in `hardening-existing-code` is therefore *more* visible, not less.
- `RemediationExecutor` is declared, never wired. `execute` keeps its exact fail-closed behaviour while `DRY_RUN` is
  true, and refuses to invent a success when it is false. No real execution is added.
- No `TelemetryReader` port yet: `investigate` would be its only caller and it is still a stub. Declaring an interface
  with no implementation and no caller is speculation, not architecture. It lands with Roadmap Phase 1.
- `langgraph` is allowed in `application/graph.py` and nowhere else. The flow definition *is* application logic; the
  framework expressing it is a declared, tested exception.

**L6 — RED evidence.**
The new tests were written first. `make test` before any source change: collection fails with
`ModuleNotFoundError: No module named 'error_agent.application'` (raised from `conftest.py:12`, then from every test
written against the injected seam). The layers the rule describes did not exist; that is the honest RED for S2/S3/S8.

**L7 — verification evidence.**
- `make lint` → All checks passed. `ruff format --check` → 30 files already formatted.
- `make test` → **48 passed, 1 skipped in 0.83s** (the skip is the compose surface test, which has no
  `docker-compose.yml` inside the image).
- `make up` (rebuild, new entrypoint paths) → temporal healthy, `worker ready | ... dry_run=True approval=human`,
  `GET /health` 200.
- `make smoke` → `started incident-smoke-…`, `approval requested: placeholder plan`, `final phase=failed`,
  `resolved=false`, one action `executed=False`, dry-run note. Identical verdict shape to `hardening-existing-code`.
- Webhook path (new inbound ACL): `POST /incidents/webhook/dynatrace` with a real-shaped problem →
  `GET /incidents/PD-arch-1` shows `awaiting_approval=true` and the plan targeting `checkout-service`, i.e. the mapping
  moved out of the HTTP client without changing what the endpoint produces.
- Rejection path (new approval port): `POST /incidents/PD-arch-1/resume {"approved": false}` after the restart →
  status `COMPLETED`, no execution.
- Durability still holds: `make down && make up` while `PD-arch-1` awaited approval, same `run_id`
  `01a20c99-…`, query still shows the pending plan, signal resumes it.
- **Mutation proof** for S8: appending `from error_agent.config import get_settings` to `application/nodes.py` makes
  `test_imports_point_inward_only[application]` fail with `application/nodes.py imports error_agent.config (layer
  application)`; appending `from langgraph.types import interrupt` makes it fail with `(only application/graph.py may)`.
  Both reverted, suite green again. The rule is enforced, not documented.

**L8 — what the audit found, in one line.**
Hexagonal in vocabulary, not in dependency direction: the *names* were ports and adapters (`clients/`, `api/`, `cli/`,
`models.py`), but the core read `os.environ`, imported the reasoning framework, and no port existed anywhere, so no
adapter could ever be substituted. Fixed: the core now receives `CoreDeps`, and `tests/test_architecture.py` makes a
regression a build failure.

**L9 — called out, not changed.** `APPROVAL_MODE=approved` still records an approval nobody gave. It is now a
`AutoApprovalGateway` chosen in `worker.build_deps`, i.e. visible at startup instead of hidden inside the gate. Removing
it is still a policy call (demo convenience vs. invariant 6) and is left open.

**L11 — user correction (verbatim).**
> pero en la capa de api, estoy viendo logica de negocio como es iniciar un workflow. Eso debería ir en aplicación
> verdad? revisa este tipo de cosas para que la arquitectura sea correcta

**L12 — the correction was right, and the audit had missed it.**
The first pass moved the *value* (`RunLimits`) and left the *decision* in the adapter: `api.py::_start` still assembled
the workflow input, chose the run identity, set the id-reuse policy, mapped `WorkflowAlreadyStartedError` to 409 and
carried the "resume only when pending" rule as a query + 409. `cli.py` duplicated the same assembly. That is business
logic in a driving adapter, exactly as reported. Fixing it also removed the last duplication between the two surfaces.

**L13 — design decisions for the control plane.**
- Ports in both directions, and both are needed: `RunGateway` (driven, mechanics) and
  `application/incidents.py` (input, rules). Without the input port the HTTP layer would call a driven port directly and
  the rules would have nowhere to live but the adapter or the engine.
- `RunGateway.start` raising `IncidentAlreadyRunning` puts the idempotency rule in the *contract*; the adapter enforces
  it with `REJECT_DUPLICATE` because that is the primitive it has. `submit_decision` only delivers, so "only a waiting
  run can be decided" stays a use-case rule.
- Error translation lives in one place per direction: engine → typed error in the adapter, typed error → status code in
  FastAPI exception handlers. No `try/except` in the routes.
- The HTTP routes are now three lines each; the only `try/except` left is the 422 for a malformed vendor payload, which
  is genuinely an adapter concern.
- Deliberate behaviour change, noted: the API's Temporal client connects lazily. Startup no longer crash-loops when the
  engine is briefly unavailable; the first request answers 503 through the pre-existing `RunUnavailable` path. The
  response contract is unchanged (verified below).

**L14 — verification after the correction.**
- `make lint` → All checks passed; `ruff format` → 34 files formatted.
- `make test` → **61 passed, 1 skipped in 1.14s** (was 48 before the control plane went through use cases; the 13 new
  tests cover the three use cases, the four error→status mappings and the view derivation).
- `make up` (rebuild) → `/health` 200.
- Running stack: `POST /incidents/webhook/dynatrace` → 202 + `workflow_id`; `GET /incidents/PD-hex-2` → 200
  `awaiting_approval=true`; duplicate submit → **409** `incident already has a run`; resume of an unknown id → **404**;
  resume of the pending run → **202** then `COMPLETED`. Same statuses and payloads as before the refactor.
- `make smoke` → `final phase=failed`, `resolved=false`, one action `executed=False`, now going through
  `submit_incident` + `decide_incident` instead of its own workflow assembly.

**L10 — remaining recommendation, not applied.**
No version control: work-unit commits are impossible, so this document records the work unit boundaries instead of commit
identities. `git init` is a repository-level decision, not part of this change.
