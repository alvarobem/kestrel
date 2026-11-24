# AGENTS.md

Instructions for AI agents (and humans) working in this repository.

## What this is

A **stateful multi-agent system for production incident triage and remediation**.
It reads incidents from an observability tool, investigates them, proposes a
remediation plan, waits for human approval, executes through an isolated sandbox
and verifies the outcome. The tool is a plugin: exactly one is active per run,
chosen by `TELEMETRY_PROVIDER`, and Dynatrace is the first adapter.

The split that matters: **Temporal owns the process, LangGraph owns the
reasoning.** Temporal is the only durable state. There is no application
database and no second checkpoint store.

Read `docs/PROJECT.md` before changing anything structural.

## Stack

| Concern            | Choice                                                       |
| ------------------ | ------------------------------------------------------------ |
| Durable execution  | Temporal (dev server locally; Temporal Cloud in production)  |
| Reasoning graph    | LangGraph `StateGraph` via `temporalio-langgraph`            |
| Control plane      | FastAPI — stateless: start runs, read state, deliver signals |
| Worker             | `python -m kestrel.worker`, horizontally scalable            |
| LLM tracing        | Langfuse, opt-in profile (`make up-all`)                     |
| Remediation exec   | Isolated `sandbox` container, opt-in profile                 |
| Packaging          | uv + pyproject, Python 3.12                                  |

## Commands

```bash
cp .env.example .env        # first time
make up                     # core: Temporal + API + worker (3 containers)
make up-all                 # core + Langfuse tracing (+5 containers)
make logs SERVICE=agent-worker
make smoke                  # real run: start, approve, report
make test                   # pytest against the working tree, stack stopped
make lint && make fmt       # ruff, against the working tree
make temporal               # list workflows
make sandbox CMD="kubectl get pods -n checkout"
make reset                  # DESTROYS local data, including workflow history

docker compose up -d --scale agent-worker=3   # scale the thinking
```

Endpoints: API http://localhost:8000/docs, Temporal UI http://localhost:8233,
Langfuse http://localhost:3000 (only with `make up-all`).

`make provider-check` proves the active telemetry provider can be read and shows what it sees.

`make llm-login` authorizes your GitHub Copilot seat once per machine (device
OAuth, no API key); `make llm-check` proves the configured model answers.

## Layout

The tree *is* the architecture: `domain` → `application` → `adapters`, and
imports only ever point inward.

```
app/src/kestrel/
  domain/                  innermost: nothing to depend on
    models.py              entities crossing process boundaries
    evidence.py            merging, dedupe and ranking rules (pure)
    policy.py              RunLimits + bounded-attempt defaults
  application/             use cases; no framework, no settings
    ports.py               ApprovalGateway, TelemetryProvider, RemediationExecutor,
                           RunGateway
    deps.py                CoreDeps: ports + frozen policy values (dry_run, scope)
    nodes.py               one function per lifecycle step
    state.py               IncidentState (Temporal-serializable)
    graph.py               flow: nodes, edges, per-node activity options
    incidents.py           control-plane use cases: submit, inspect, decide
    errors.py              typed application errors (the adapter maps them)
    policy.py              run identity (workflow_id)
  adapters/
    inbound/               the world drives us
      api.py               stateless HTTP control plane
      cli.py               smoke + provider-check
      workflow.py          Temporal workflow: lifecycle, approval wait, signals
      webhooks.py          provider id -> payload mapper (inbound ACL registry)
      dynatrace_problem.py Dynatrace payload -> Incident (one mapper)
    outbound/              we drive the world
      providers.py         provider registry: TELEMETRY_PROVIDER -> adapter
      dynatrace_provider.py Dynatrace as a TelemetryProvider (facts -> Evidence)
      dynatrace_platform.py Dynatrace Grail/DQL transport (.apps + Bearer)
      dynatrace.py         Dynatrace classic API v2 transport (.live + Api-Token)
      approval.py          ApprovalGateway: graph interrupt + auto
      llm.py               LlmClient registry: a personal Copilot seat (device OAuth)
      runs.py              RunGateway over Temporal, engine errors translated
      temporal.py          client + data converter
      tracing.py           per-node Langfuse traces, grouped by incident
      checkpoint.py        LangGraph serializer type allowlist
  config.py                settings, all via env (outermost)
  worker.py                composition root: settings -> deps -> graph
sandbox/                   ephemeral execution image
infra/postgres/init/       one-shot bootstrap (Langfuse database only)
docs/                      project definition
```

## Hexagonal architecture: the dependency rule

Ports and adapters, enforced by a test rather than by discipline. The rule is
mechanical:

| Layer | May import | May never import |
| --- | --- | --- |
| `domain/` | stdlib, `pydantic`, itself | anything else |
| `application/` | `kestrel.domain`, itself | `kestrel.config`, `kestrel.adapters`, `httpx`, `fastapi`, `temporalio`, `pydantic_settings`, `langfuse` |
| `adapters/` | `domain`, `application`, `config`, any framework | `kestrel.worker` |
| `config.py` | inner layers | — |
| `worker.py` | everything | nothing imports it |

`tests/test_architecture.py` parses every module and fails the build on a
violating import, a missing layer, or `interrupt()` appearing in the core. It is
part of `make test`; treat a failure as a design error, not a test to relax.

**Ports** are declared by the application, never by an adapter:

| Port | Method | Adapter |
| --- | --- | --- |
| `ApprovalGateway` | `await_decision(incident_id, plan) -> Approval` | `GraphApprovalGateway` (LangGraph `interrupt()`), `AutoApprovalGateway` (local smoke) |
| `TelemetryProvider` | `async investigate(incident, scope) -> list[Evidence]` | `DynatraceTelemetryProvider` (`adapters/outbound/dynatrace_provider.py`), chosen by the registry in `adapters/outbound/providers.py` |
| `LlmClient` | `async complete(system, user) -> str` | `CopilotLlmClient` (`adapters/outbound/llm.py`): the developer's own Copilot seat over device OAuth, chosen by the registry in the same module |
| `RemediationExecutor` | `async execute(step) -> ActionRecord` | not wired yet: the sandbox decision (§13 of `docs/PROJECT.md`) |
| `RunGateway` | `start` / `view` / `pending_approval` / `submit_decision` / `outcome` | `TemporalRunGateway` (`adapters/outbound/runs.py`) |

**Input ports** (use cases) are the other direction: a driving adapter calls
`application/incidents.py` — `submit_incident`, `inspect_incident`,
`decide_incident` — and never talks to an engine. All three are plain functions
taking the port as their first argument, so `api.py` and `cli.py` share the
rules instead of each assembling a workflow start. An adapter may *construct*
`TemporalRunGateway` (that is wiring); it may not know what a workflow start
involves.

A use case receives these in `CoreDeps`, together with frozen **policy values**
(`dry_run`, `scope`). Reading `os.environ` from a node is a bug
even when it looks convenient: it makes a run non-reproducible and a replayed
history diverge.

**Where new code goes:**

| You are adding | It goes in |
| --- | --- |
| an entity, a guard-rail rule, a bounded limit | `domain/` |
| a lifecycle step or a new use case | `application/nodes.py` + `graph.py` |
| a capability the core needs | a `Protocol` in `application/ports.py`, implemented in `adapters/` |
| support for another observability tool | `adapters/outbound/<tool>_provider.py` + one entry in `providers.py`; nothing in `domain/` or `application/` |
| a control-plane operation (submit, read, decide) | a use case in `application/incidents.py` |
| a vendor call, a transport, a mapping | `adapters/outbound/` (transport) or `adapters/inbound/` (payload → domain) |
| a setting | `config.py`, then read it in `worker.build_deps` and pass the value inward |
| a surface (HTTP route, CLI verb, signal) | `adapters/inbound/` |

**Declared exemptions**, both visible in the test:

1. `application/graph.py` may import `langgraph` and `temporalio`. The flow and
   the per-step execution policy are application logic written in that
   vocabulary; the module is the seam, and it is the only one.
2. `adapters/inbound/workflow.py` assembles the graph and the checkpointer it
   hosts, because Temporal instantiates it by name and cannot inject anything.

## Invariants (do not break these)

1. **Temporal is the only durable state.** Do not add a Postgres checkpointer, a
   job queue or a second workflow engine. The in-workflow checkpointer is
   `InMemorySaver` and must stay that way.
2. **State is Temporal-serializable.** `IncidentState` crosses activity
   boundaries through the pydantic data converter. No clients, connections,
   closures or ports in state: capabilities travel in `CoreDeps`. Every new
   pydantic type that reaches the state goes into `STATE_TYPES` in
   `adapters/outbound/checkpoint.py`.
3. **Nodes are async, pure and idempotent, and receive their dependencies.**
   They run as activities and can re-run on retry or worker crash. Never mutate
   the input state; return a partial update. A node is
   `async def node(state, deps)` — the graph binds `deps` once, so no node ever
   imports a client or reads a setting.
4. **Routers run inside the workflow.** They must be `async` and deterministic:
   no I/O, no clock, no randomness. This is enforced by Temporal's sandbox and
   their signature (`route_*(state)`, nothing else). Bounded-attempt limits are
   snapshotted into state when the run starts (`RunLimits`), so a router that
   calls `get_settings()` is a bug.
5. **`execute` is never retried automatically.** A blind retry of a rolling
   restart is a second incident. `ACTIVITY_OPTIONS["execute"]` stays at
   `maximum_attempts=1`; a failure goes back through verification and a new
   approval.
6. **Nothing mutates production without a recorded approval.** The approval is a
   Temporal signal and an `Approval` record. Never bypass it, not for a "quick
   test". Approvals accumulate across plan attempts, so only the latest one
   authorizes the plan on the table: a rejected re-plan never executes on the
   strength of an earlier approval.
7. **Read and write credentials are separate.** Investigation uses the OAuth
   client or `DYNATRACE_READ_TOKEN`. `DYNATRACE_WRITE_TOKEN` belongs to the
   remediation path and must never be reachable from an investigation node. It
   is not injected into the agent containers at all until a code path consumes
   it.
8. **Every executed step produces an `ActionRecord`.** No silent commands. A step
   that did not run records `executed=False` with no `exit_code` and no output;
   `execute` fails closed when no executor is wired instead of reporting success.
9. **`DRY_RUN=true` is the default.** Only flip it after the approval gate and
   the sandbox policy have been exercised end to end.
10. **Remediation never runs on the host or in the agent container.** Commands go
    through the sandbox container. Do not mount `/var/run/docker.sock` anywhere.
11. **Secrets never enter the repo or a trace.** Node traces capture no
    input/output (`capture_input=False`); keep it that way.
12. **The API stays stateless.** It must never run a node or hold run state in
    memory; that is what makes it replaceable and the workers scalable.
13. **Imports point inward only.** `domain` -> `application` -> `adapters`.
    Anything the core needs from outside arrives as a port (`application/ports.py`)
    or a frozen policy value (`CoreDeps`), never as an import. One composition
    root (`worker.build_deps` / `build_app`) decides which adapter is used.
    `tests/test_architecture.py` fails the build on a violation.
14. **A driving adapter holds no run rules.** HTTP and CLI own routes, payload
    shapes and status codes; the run's identity, its idempotency, when a
    decision is legal and what an error means are the application's
    (`application/incidents.py`, typed errors in `application/errors.py`). If an
    adapter catches an engine exception, the rule has already leaked.
15. **Telemetry is a port with one active adapter, and the core names no vendor.**
    Exactly one provider is chosen per run in `worker.build_deps` from
    `TELEMETRY_PROVIDER`; `domain/` and `application/` may not reference a vendor
    in code (a test enforces it), and `Evidence.provider` records who produced
    each fact. Adding a tool is a new adapter plus one registry entry. The scope
    is a frozen policy value: a provider returns nothing for an incident outside
    it, and an unconfigured provider degrades the run with a recorded reason
    instead of failing it.
16. **A plan may only name a declared capability, and it must change something.**
    `REMEDIATION_CAPABILITIES` declares the action space as capabilities — id,
    tool, allowed verbs, the literals that pin where a command lands, and whether
    the capability changes the system or only observes it — frozen into
    `CoreDeps.surface` when the run starts. `application/planner.py` rejects a step
    that names an unknown capability, uses another tool, omits where it lands, or a
    plan whose steps are all `read`: the investigation already read the logs, and
    proposing to read them again spends a human approval on a no-op. An empty
    declaration means no plan, never "any command". This is deliberately not an
    on-call vocabulary: a production restart and a broken build in integration are
    the same mechanism with different capabilities, so no code changes between them.

## Conventions

- Python 3.12, full type hints, `from __future__ import annotations`.
- Pydantic v2 at every boundary.
- Settings only through `config.get_settings()`, and only in the composition
  root and adapters. A use case receives values, not the settings object.
- `workflow_id` is `incident-<incident id>`; it is never a second correlation id.
- Duplicate starts are rejected by Temporal (`REJECT_DUPLICATE`), which is what
  makes webhook ingestion idempotent.
- Tests live in `app/tests/` and cover routing, guard rails, the plugin
  contract (every node declares `execute_in`, `execute` has one attempt) and
  the dependency rule. Test doubles for the ports live in `conftest.py`.
- Commit messages: conventional commits. No AI attribution.

## Definition of done for a graph change

1. `make lint` and `make test` pass, including `test_architecture.py`: a
   change that needs an outward import is a design change, not a test fix.
2. `make smoke` completes a run and prints its verdict. In the default dry run it
   reports `resolved=false` and says why; it only reports resolved once real
   Dynatrace verification exists.
3. With `make up-all`, the Langfuse session for that incident shows one trace
   per node.
4. Restart durability still holds: `make down && make up` while a workflow is
   awaiting approval, then approve it and see `COMPLETED`.
5. `docs/PROJECT.md` updated if the flow, state model or guard rails changed.

## Extending

- **New node**: `async def step(state, deps)` in `application/nodes.py`,
  register it in `NODE_FUNCTIONS` and `ACTIVITY_OPTIONS` in
  `application/graph.py` (they must stay in sync — a test enforces it), extend
  `IncidentState`, then update the flow diagram in both docs.
- **New human decision point**: a signal + query on `IncidentWorkflow`
  (`adapters/inbound/workflow.py`), surfaced through the API or CLI. Never poll
  from the workflow, never use an HTTP callback inside a node.
- **New control-plane operation**: a use case in `application/incidents.py`
  (the rule), a method on `RunGateway` if it needs the engine (the contract),
  the implementation in `adapters/outbound/runs.py` (the mechanics), then the
  route or CLI verb that only translates.
- **New capability the core needs** (telemetry read, execution, notification):
  declare a `Protocol` in `application/ports.py`, add the field to `CoreDeps`,
  implement it in `adapters/outbound/`, and choose it in `worker.build_deps`.
- **Another observability tool**: implement `TelemetryProvider` in
  `adapters/outbound/<tool>_provider.py` (map the vendor's facts onto `Evidence`,
  apply `scope`, translate its transport failure into `TelemetryUnavailable`),
  then add one `ProviderSpec` to `adapters/outbound/providers.py`. Add its
  credentials to `config.py` and its payload mapper to
  `adapters/inbound/webhooks.py`. No change in `domain/` or `application/`.
- **A new signal from an existing provider**: extend that provider's
  `investigate()`, returning plain domain `Evidence`. Depth lands there, not in
  the node: `investigate` never names a vendor.
- **Anything Grail/DQL in Dynatrace**: `adapters/outbound/dynatrace_platform.py`.
  The path is `/platform/storage/query/v1/query:execute` (the `/query/v1/` is not
  optional: without it the gateway answers `404 No static resource`) and the
  response is asynchronous — poll `query:poll` with the `requestToken`, bounded.
  Only query fields you have seen in a real record: an unknown field fails the
  whole query, so a guess costs evidence.
- **Real LLM calls**: inside a node, so they get the activity's timeout and
  retry policy; keep prompts in Langfuse prompt management once stable; model
  output can never choose a command that bypasses the approval gate.
- **Real execution**: implement `RemediationExecutor` in `adapters/outbound/`
  inside the sandbox boundary, wire it in `worker.build_deps`, and it stays
  gated behind approval and `DRY_RUN=false`.
