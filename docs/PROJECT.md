# kestrel — project definition

Status: scaffold on Temporal. The structure runs end to end; the intelligence is
stubbed.

## 1. Problem

When a production incident fires, an on-call engineer repeats the same loop under
pressure: read the alert, pull context from Dynatrace, form a hypothesis, decide
on a remediation, execute it, confirm it worked, write it up. That loop is slow,
it depends on who is on call, and the reasoning is lost the moment the incident
closes.

We want an agent system that does the evidence-gathering part automatically,
proposes a remediation with its reasoning attached, and leaves the decision — and
the action — to a human while keeping a complete, replayable audit trail.

## 2. Goal

A locally hosted, stateful multi-agent workflow that, for a real incident coming
from our Dynatrace tenant:

1. **Triages** it — severity, blast radius, affected services, ownership.
2. **Investigates** it — problem details, events, logs and metrics as evidence,
   each item tagged with its source and a confidence value.
3. **Proposes** a ranked hypothesis and a remediation plan with explicit steps,
   risk, reversibility and rollback commands.
4. **Waits** for approval — for minutes or days, surviving restarts and deploys.
5. **Executes** the approved steps inside an isolated, throwaway environment.
6. **Verifies** the incident is actually gone, by asking Dynatrace again rather
   than trusting an exit code.
7. **Reports** what happened, what was tried and what changed.

Success for this phase: the stack comes up with one command, the graph runs end
to end against the real tenant, a run survives killing the whole stack while it
waits for approval, and every run is traced.

## 3. Non-goals (for now)

- Autonomous remediation without human approval.
- Multi-tenant isolation or running the agent inside production.
- Replacing the on-call rotation or the incident tooling.
- A custom UI. The API, the Temporal UI and Langfuse are the interface.

## 4. Architecture decision: why Temporal

The requirement is not "run a graph". It is "run a multi-hour, human-gated,
side-effecting process that must survive restarts and be auditable". That is
durable execution, and LangGraph's checkpointer alone is a partial answer: it
persists state, but you still have to build the worker, the retry policies, the
timers, the signal path and the run inventory.

**Temporal owns durability. LangGraph owns the reasoning.** The official
`temporalio-langgraph` plugin runs each graph node as a Temporal activity, so:

| Need | How it is covered |
| --- | --- |
| Survive a crashed worker | Temporal replays the workflow, re-dispatches the pending activity |
| Survive a full stack restart while waiting for approval | Workflow parked in history, resumed by a signal |
| Retries with timeouts per step | `ACTIVITY_OPTIONS` per node |
| "Wait 10 minutes, re-check Dynatrace" | Temporal timer (no polling, no sleeping thread) |
| Approval from anywhere, later | Signal; pending plan exposed via query |
| Inventory of in-flight incidents | Temporal visibility + UI |
| Audit trail | Workflow event history, immutable |
| Change code with runs parked | Temporal versioning |

The alternative — LangGraph + Redis queue + hand-rolled retries — reaches the
same place with more code and no audit trail. That is what this scaffold
replaced.

## 5. Stack

| Piece | Choice | Why |
| --- | --- | --- |
| Durable execution | Temporal 1.32 (server), SDK 1.36 | Replay-based durability, timers, signals, visibility |
| Local runtime | `temporalio/temporal` dev server | One container = server + UI, SQLite on a volume. Production points `TEMPORAL_ADDRESS` at Temporal Cloud or a cluster |
| Reasoning | LangGraph + `temporalio-langgraph` | Explicit graph, conditional edges, interrupts mapped to signals |
| Control plane | FastAPI | Stateless: start runs, read state, deliver approvals |
| Telemetry | Pluggable provider, Dynatrace first | One adapter is active per run (`TELEMETRY_PROVIDER`); the core never names a vendor |
| Tracing | Langfuse (opt-in profile) | One session per incident, one trace per node |
| Execution | Isolated `sandbox` container | The agent must never touch production from its own process |

### What is deliberately absent

No application database (Temporal history is the state). No Redis for the app
(Langfuse's own queue only). No Celery/ARQ. No Postgres checkpointer. No Docker
socket. Each of those was a second source of truth or a second failure mode.

## 6. Architecture

The code is organised as ports and adapters (hexagonal), and the rule is
enforced by a test rather than by review:

```
  domain/        entities and policy        imports: stdlib, pydantic
      ▲                                     never: anything else
  application/   use cases, state, ports    imports: domain
      ▲                                     never: config, adapters, any SDK
  adapters/      in/ (http, cli, temporal,  imports: domain, application,
                 payload mapping)                config, frameworks
                 out/ (dynatrace, approval,
                 tracing, checkpointer)
      ▲
  config.py      settings from env
  worker.py      composition root: settings -> CoreDeps -> graph
```

The application names what it needs (`application/ports.py`: an approval channel,
a telemetry provider, an isolated executor, somewhere for runs to live) and
receives it in `CoreDeps`, together with frozen policy values such as `dry_run`
and the investigation `scope`. Adapters decide how those needs are actually met. That is why a use case can be tested with a fake approval
channel and a fake sandbox, and why `langgraph` appears in exactly one file of
the core: the flow definition.

The same holds in the other direction. A driving adapter — the HTTP control
plane, the operator CLI — calls the use cases in `application/incidents.py`
(`submit_incident`, `inspect_incident`, `decide_incident`) and reaches the engine
only through `RunGateway`. Starting a run is therefore *not* "call
`client.start_workflow`": what the run's identity is, that duplicate ingestion is
refused, and that a decision is only legal while the run is waiting are rules of
the application, stated in the port contract and decided in the use cases. The
Temporal adapter owns the mechanics and translates engine exceptions into typed
application errors; HTTP owns the status codes. `tests/test_architecture.py`
fails the build if `api.py` or `cli.py` imports `temporalio` again.

`tests/test_architecture.py` parses every module, resolves its imports and fails
the build when a layer reaches outward, when a layer stops being a package, or
when `interrupt()` appears in the core. The architecture is a test, not a
convention.

```
        Dynatrace tenant (real)
        problems · events · logs · metrics
                 │  (read-only OAuth client / token)
                 ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  agent-api (stateless)                                       │
  │    POST /incidents            start workflow                 │
  │    GET  /incidents/{id}       status + pending plan (query)   │
  │    POST /incidents/{id}/resume      approve (signal)          │
  └───────────────┬──────────────────────────────────────────────┘
                  │ start / signal / query
                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Temporal  ── durability, timers, retries, audit history      │
  └───────────────┬──────────────────────────────────────────────┘
                  │ activity per node (task queue)
                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  agent-worker × N                                            │
  │    receive → triage → investigate → plan → [APPROVAL]         │
  │                 ▲          │           │        │             │
  │                 └ retry ───┘           │  reject└──► report    │
  │                                        │ approve             │
  │                                        ▼                     │
  │                               execute → verify ─ retry ─┐    │
  │                                        │      │          │   │
  │                                        │      └──────────┘   │
  │                                        └──────► report → END │
  └───────────────┬──────────────────────────────┬───────────────┘
                  │ traces (session = incident)   │ commands
                  ▼                               ▼
              Langfuse                     sandbox container
```

## 7. Agents (roles)

Each role is a graph node, i.e. its own activity with its own timeout, retry
policy and audit entry.

| Node | Responsibility | Needs LLM | Writes |
| --- | --- | --- | --- |
| `receive` | Normalize payload, open the trail | no | state |
| `triage` | Severity, scope, ownership, urgency | yes | `evidence` |
| `investigate` | Pull and rank evidence from the active provider | optional | `evidence`, attempts |
| `plan` | Hypothesis + plan with rollback | yes | `hypothesis`, `plan` |
| `approval_gate` | Suspend via `interrupt()`, record decision | no | `approvals` |
| `execute` | Dispatch approved steps to the sandbox | no | `actions` |
| `verify` | Re-query the active provider for recovery | optional | `verification` |
| `report` | Post-incident summary and follow-ups | yes | `report` |

Cheap model for triage/investigation, stronger one for `plan`/`report`. Model
choice is config, not code.

## 8. Flow and human-in-the-loop

The workflow `IncidentWorkflow` runs the graph and owns the loop:

1. First `ainvoke` runs nodes until `approval_gate` calls `interrupt(plan)`.
2. The graph returns the interrupt; the workflow publishes it through the
   `pending_approval` query and blocks on `workflow.wait_condition`. Durable,
   with no timeout and no polling.
3. `POST /incidents/{id}/resume` sends the `approve` signal (or a rejection).
4. The workflow resumes the graph with `Command(resume=decision)`.
5. A rejected plan goes to `report`. A failed verification goes back to
   `investigate` — a second plan and a second approval — only when something was
   actually applied and attempts remain: re-investigating a dry run, or an
   execution that failed closed, cannot change anything. Approvals accumulate
   across attempts, so only the latest one authorizes the plan on the table.

Attempts are bounded (`max_investigation_attempts`, `max_remediation_attempts`),
snapshotted into state when the run starts so the routers never read settings.
That way a bad hypothesis cannot loop forever, and replayed history cannot
diverge because an operator changed `.env` between restarts.

## 9. Persistence and audit

Temporal's event history is the only durable state. There is no application
database to keep in sync, and nothing to migrate when the graph changes shape.

That also gives the audit trail for free: which user approved which plan, when,
with what comment, and what each activity did — all in history, immutable, and
visible in the Temporal UI.

If a queryable incident registry is ever needed beyond Temporal visibility (e.g.
joining with a ticketing system), add a read model populated from workflow
results — never a second authority.

Local persistence: SQLite file on the `temporal_data` volume. A `make down`
keeps it; `make reset` deletes it. That is why an approval survives a restart.

## 10. Scalability

- **API**: stateless, no ports to pin, scale or replace freely.
- **Workers**: `docker compose up -d --scale agent-worker=3`. Temporal
  load-balances activities across the task queue; add replicas for throughput,
  not for correctness.
- **Per-node concurrency**: activity-level; long Dynatrace calls do not block
  fast nodes.
- **Task queues**: the natural boundary if some nodes ever need a different
  pool (e.g. a GPU queue for the planner).
- **Production**: `TEMPORAL_ADDRESS` → Temporal Cloud or a replicated cluster.
  Nothing else in the app knows where Temporal runs.

The local dev server is a single SQLite-backed container: perfect for
development, explicitly not the scaling story. Scaling is a connection string.

## 11. Observability

Nodes run in separate processes, so a LangChain callback handler cannot span
them. Tracing is therefore at the node boundary: **one trace per node, grouped
into one Langfuse session per incident**. In the UI you open the session and see
the incident timeline in order. LLM generations inside a node nest under that
node's trace because they share the process.

Traces capture no input/output by default (`capture_input=False`): evidence
payloads can be large and secrets must not leak. Add explicit, small updates
where they help.

Langfuse is behind the `observability` profile because it costs four extra
containers (Postgres, Redis, ClickHouse, object storage). `make up` runs the
system; `make up-all` runs the system with tracing, and the agent degrades to
untraced when the keys are absent.

## 12. Telemetry integration: a plugin, Dynatrace first

**The seam.** `TelemetryProvider` is a port: one `async investigate(incident,
scope) -> list[Evidence]`. Exactly one adapter is active per run, chosen by
`TELEMETRY_PROVIDER` from the registry in `adapters/outbound/providers.py`. An
adapter maps its tool's facts onto the neutral `Evidence` vocabulary
(`provider`, `kind`, `summary`, `external_id`, `confidence`), so a second tool is
a new module plus one registry entry — never a change in `domain/` or
`application/`. `tests/test_architecture.py` fails the build if a vendor noun
reaches the core.

**Dynatrace has two surfaces, and they are not interchangeable.**

| Surface | Host | Credential | Gives |
| --- | --- | --- | --- |
| Classic API v2 (`dynatrace.py`) | `*.live.dynatrace.com/api/v2` | `Api-Token dt0c01…` | problems, events, metrics |
| Platform / Grail (`dynatrace_platform.py`) | `*.apps.dynatrace.com/platform` | `Bearer dt0s16…` | DQL: logs, events, metrics, spans, bizevents |

Investigation **prefers the platform surface**, because it is the only one that
can read Grail, and Grail is where the evidence is: the Davis problem that fired
(`fetch events | filter event.type == "davis"`) and the error logs the service is
writing (`fetch logs | filter loglevel == "ERROR"`). The classic surface remains
as a fallback for a tenant that only has an API token, where the incident's own
problem record is reported as one `ALERT`.

Two traps worth knowing, both learned against a live tenant:

- The DQL path is `/platform/storage/query/v1/query:execute`. Without `/query/v1/`
  the gateway answers `404 No static resource …` — it falls through to its static
  handler, so the error looks like a missing file, not a wrong route.
- DQL is asynchronous: `query:execute` returns a `state` and, when unfinished, a
  `requestToken` to poll via `GET …/query:v1/query:poll`. The poll loop is bounded
  by attempts, so an activity fails with a reason instead of hanging.

**Credentials.** `DYNATRACE_PLATFORM_URL` + `DYNATRACE_PLATFORM_TOKEN` select the
platform surface; `DYNATRACE_ENV_URL` + a read token (or OAuth client id/secret)
select the classic one. A read token wins over OAuth when both are set, because
`_auth_headers` checks it first — do not configure both and expect OAuth. An
adapter with no usable credential is not built at all, and the run degrades with a
recorded reason instead of failing to start.

An ingest credential is not a read credential: a token that writes telemetry
(`openpipeline:*:ingest`) answers `403 Token is missing required scope` on every
read endpoint. Do not add read scopes to the token your applications use to emit
telemetry.

**Ingestion.** Polling
`GET /api/v2/problems?problemSelector=status("OPEN")&from=now-2h`, or a
problem-notification webhook pointed at `POST /incidents/webhook/{provider}`.
The provider id selects a payload mapper in `adapters/inbound/webhooks.py`; an
unknown id is a 404, never a guess. Starting the same incident twice is
rejected by Temporal (`REJECT_DUPLICATE`), so a retrying webhook is safe.

**Scoping.** `DYNATRACE_SCOPE_TAGS` narrows investigation to the tags/management
zones this prototype may reason about. It is frozen into `CoreDeps` as an
`InvestigationScope` when the run starts, and the provider returns no evidence
for an incident outside it — an operator editing `.env` does not change what an
in-flight incident may read. Set it: an agent reading every team's telemetry is a
governance problem before it is a technical one.

**Read limits.** Cache evidence per run, never re-query the same window twice,
treat 429 as backoff. Retries are bounded per activity.

## 13. Sandbox — still the open design decision

The agent must not run remediation from its own process, and it must not have
the Docker socket.

| Option | Pros | Cons |
| --- | --- | --- |
| **A. Ephemeral compose container** (scaffolded) | Simple, auditable, no host privileges, per-command lifetime | Needs a host-side runner or executor service to be automatic |
| B. Docker socket in the agent | Automatic | Socket is root-equivalent on the host; one injection bug becomes host takeover |
| C. Kubernetes Job per action | Real isolation, native audit, scales | Needs a cluster |
| D. Hosted sandbox (E2B, microVM) | Strong isolation | Third-party data path, awkward egress to clusters |
| E. Direct subprocess | Trivial | Same container, same credentials, same blast radius as the agent |

**Recommendation:** start with **A**. When execution must be automatic, add a
runner that consumes an action queue — or move to **C** if a cluster exists.
Never **B**.

The container runs with `read_only`, `cap_drop: ALL`, `no-new-privileges`, a
tmpfs and an internal network with no egress. Giving it reachability is a
decision to be made together with short-lived, RBAC-scoped credentials.

## 14. Security and guard rails

- Two Dynatrace credentials, read and write, never in the same code path. The
  write token is not injected into the agent containers until a code path
  consumes it.
- Human approval is a signal plus a persisted `Approval` record. Only the latest
  record authorizes the plan on the table.
- `execute` has `maximum_attempts=1`: remediation is never retried blindly.
- `DRY_RUN=true` by default; commands are rendered and audited, not executed, so
  verification reports "not verified" instead of a resolution it cannot prove.
- `execute` fails closed when no executor is wired. A step that did not run
  carries no exit code and no output.
- Sandbox drops capabilities, runs non-root, no socket, no external network.
- Traces capture no input/output by default; no secrets in prompts or evidence.
- Workflow ids deduplicate ingestion.
- Bounded attempts on every loop.

## 15. Roadmap

**Phase 0 — done (this scaffold).**
Temporal + LangGraph wired end to end, real approval signal, restart-durable
runs, plugin activity options per node, API, CLI smoke, tests, docs. A dry run
reports `resolved=false`: nothing was applied, so nothing was verified.

**Phase 1 — real investigation.** *Partly landed.*
The port, the provider seam and the platform transport exist: a run reads Davis
problems and error logs over DQL, inside the incident's own time window, filtered
by the run's scope, and `make provider-check` proves the credential in one
command. Still to do: metrics (needs the tenant's metric catalogue — a wrong
selector returns an empty result silently), per-service log filtering, evidence
ranking, per-run caching and 429 backoff.

**Phase 2 — real planning.**
LLM hypothesis generation, runbook retrieval, plan validation (reversible,
rollback present, no wildcard targets). Prompts in Langfuse.

**Phase 3 — controlled execution.**
A `RemediationExecutor` adapter calls the sandbox, one action at a time, with
timeouts and captured output, wired in `worker.build_deps`. Still `DRY_RUN` by
default. Add a timer-based verification window.

**Phase 4 — verification and evals.**
`verify` re-queries the active provider (a second method on `TelemetryProvider`).
Langfuse dataset of past incidents to score plan
quality. Temporal search attributes for incident-level queries.

**Phase 5 — production shape.**
Webhook ingestion, Temporal Cloud or a replicated cluster, RBAC-scoped
short-lived credentials, alerting on the agent itself.

## 16. Open decisions

1. **Sandbox executor:** host-side runner vs. Kubernetes Jobs (§13).
2. **Ingestion:** polling vs. webhook for v1.
3. **Approval channel:** API only, or Slack/Teams — the signal path is already
   the right seam for it.
4. **Ownership scope:** which tags/management zones are in bounds, who approves
   expanding them.
5. **Write access:** does the agent ever get a write token, or only produce a
   plan for a separate automation to execute?
6. **Temporal hosting:** Cloud vs. self-hosted.
7. **Model provider:** company-approved gateway vs. direct vendor API.

## 17. Quickstart

```bash
cp .env.example .env       # fill DYNATRACE_ENV_URL + OAuth client (or token)
make up                    # Temporal + API + worker
open http://localhost:8000/docs
make provider-check        # prove the active telemetry provider can be read
make test
make smoke                 # start incident, approve, report

make up-all                # adds Langfuse: http://localhost:3000
docker compose up -d --scale agent-worker=3   # scale the thinking
make reset                 # delete everything, workflow history included
```
