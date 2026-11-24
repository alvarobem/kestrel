# hardening-existing-code

Objective: remove contradictions between the code and its own documented guard rails, without adding any new capability.
Branch: none (this directory is not a git repository; changes are local files only).
Delivery: work units, no PR.
Test runner: `make test` (`docker compose run --rm --no-deps agent-worker pytest -q`).

## Specs

**S1 — No new capability.**
Scope is limited to `mejorar lo que hay ahora sin introducir nada nuevo`: fix a place where the
code contradicts `AGENTS.md`/`docs/PROJECT.md`, remove dead or duplicated code, or add a test for
behavior that already exists. No new endpoint, node, signal, config key, third-party dependency,
or feature. A fix that changes observable behavior must be one of the guard-rail contradictions
named in S2–S6 and must be recorded in `## Log`.

**S2 — The plan that executes is the plan the human approved last.**
`AGENTS.md` invariant 6: "Nothing mutates production without a recorded approval."
Acceptance: with `approvals = [approved(plan A), rejected(plan B)]`, `route_after_approval`
returns `report` and `execute` writes no `ActionRecord`. An earlier approval never authorizes a
later plan.

**S3 — Routers are deterministic.**
`AGENTS.md` invariant 4: routers run inside the Temporal workflow with "no I/O, no clock, no
randomness". Acceptance: no function reachable from a router reads settings or the environment;
bounded-attempt limits come from workflow state, snapshotted when the run starts.
`route_after_investigation` and `route_after_verify` execute N times with identical state and
return identical results.

**S4 — `execute` never records success for work it did not do.**
`AGENTS.md` invariant 8: "Every executed step produces an `ActionRecord`. No silent commands."
Acceptance: when nothing runs (`DRY_RUN=true`), records say `executed=False` and carry no
`exit_code` and no fake stdout. When `DRY_RUN=false` and no executor exists, the step fails closed
with `phase=FAILED` and an explicit note instead of `exit_code=0`.

**S5 — `verify` only reports ok when a real check ran.**
`docs/PROJECT.md` §2.6: verification happens "by asking Dynatrace again rather than trusting an
exit code". Acceptance: no code path derives `Verification.ok` from `DRY_RUN`. A dry run reports a
verification that did not confirm the incident is gone, and `report.resolved` reflects that.

**S6 — One source of truth for the version.**
`error_agent.__version__` equals `project.version` in `app/pyproject.toml`.

**S7 — Every fixed behavior has a regression test, and the suite runs with the stack stopped.**
Acceptance: `make test` passes; each S2–S5 behavior has at least one test that fails on the
pre-change code.

**S8 — Docs match the code.**
If S2–S5 change observable behavior, `AGENTS.md` (Definition of done, invariants) and
`docs/PROJECT.md` are updated in the same work unit.

**S9 — No retry when nothing was applied.**
Consequence of S4+S5: re-investigating a dry run or a fail-closed execution cannot change the
outcome, and would ask a human to approve the same no-op again. Acceptance: `route_after_verify`
returns `report` when every recorded action has `executed=False`, regardless of remaining attempts.

## Tasks

| ID | Spec | Description | Status |
| --- | --- | --- | --- |
| T1 | S2 | Scope approval to the latest plan attempt in `route_after_approval` and `execute` | done |
| T2 | S3 | Snapshot attempt limits into `IncidentState`; remove `get_settings()` from routers | done |
| T3 | S4 | Fail closed in `execute` when no executor is wired; stop fabricating `exit_code`/stdout | done |
| T4 | S5 | Make `verify` honest in dry run; propagate to `report.resolved` | done |
| T5 | S6 | Read `__version__` from package metadata | done |
| T6 | S7 | Extend the guard-rail suite for S2–S5, S9 and router purity | done |
| T7 | S8 | Update `AGENTS.md`, `docs/PROJECT.md` and `.env.example` | done |
| T8 | S1 | Remove `DYNATRACE_WRITE_TOKEN` from the agent container env | done |
| T9 | S1/S7 | `make test`/`lint`/`fmt` run against the working tree, not the baked image | done |
| T10 | S9 | Stop the retry loop when nothing was applied | done |

## Log

**L1 — user request (verbatim).**
> necesito hacer un flujo para mejorar lo que hay ahora sin introducir nada nuevo, se ha generado
> código sin un plan detallado y la calidad puede no ser buena. Encargate de eso con un flujo ODD

**L2 — audit evidence (verified in code, not assumed).**
- `graph/nodes.py:193,274` — `any(a.approved ...)`: approval of plan A authorizes plan B. Violates S2.
- `graph/nodes.py:265,278` — routers call `get_settings()`: env I/O inside the workflow sandbox. Violates S3.
- `graph/nodes.py:202` — `exit_code=0` and `stdout="(stub executor)"` for a step that never ran. Violates S4.
- `graph/nodes.py:229` — `ok=settings.dry_run`: dry run always "verifies". Violates S5.
- `__init__.py:3` vs `pyproject.toml` — `0.1.0` vs `0.2.0`.
- `config.py:43` + `docker-compose.yml` — write token injected into agent containers, unread by code.

**L3 — open decision D1 (blocks T4/T7).**
Making `verify` honest changes `make smoke`, which `AGENTS.md` requires to reach `phase=resolved`.
Options: (a) honest dry run → smoke reports not-resolved; (b) keep the current smoke outcome and
mark the verification as unverified only in the detail text.

**L4 — RED evidence (before any fix, `make test`).**
Four new guard-rail tests fail on the current code, one per defect:
- `test_rejected_replan_is_not_authorized_by_an_earlier_approval`
- `test_execute_refuses_after_the_latest_decision_rejected_the_plan`
- `test_execute_fails_closed_when_no_executor_is_wired`
- `test_routers_never_read_settings` (fails at `nodes.py:265`)

**L5 — extra finding.**
`make test` and `make lint` run inside `agent-worker` with no source mount, so they test whatever
was baked into the built image. A change is invisible to the suite until someone rebuilds. This is
why the RED run above needed `docker compose build` first, and it is a real feedback-loop defect
for a repository whose whole workflow is "edit code, run the tests". `make fmt` was worse: it
rewrote files inside a throwaway container, so formatting never reached the working tree.

**L6 — decision D1 resolved by the user: honest dry run.**
`verify` reports `ok=false` with "dry run: no change was applied, so nothing was verified", and
`report.resolved` follows it. `make smoke` now asserts a completed run and prints the verdict
instead of asserting a resolution the scaffold cannot produce. Consequence captured as S9/T10: a
failed verification only re-plans when something was actually applied.

**L7 — verification evidence.**
- `make lint` → All checks passed. `ruff format --check` → 18 files already formatted.
- `make test` → 23 passed, 1 skipped (the compose surface test, which lives outside the image).
- `make smoke` → one approval, `final phase=failed`, one action `executed=False`,
  `resolved=false`, note explains the dry run. No retry loop, no second approval.
- API path: `POST /incidents` → `GET /incidents/{id}` shows `awaiting_approval=true`.
- Durability: `make down && make up` while awaiting approval, then `POST .../resume` →
  status `COMPLETED` on the same `run_id`. Snapshot limits survived in history.

**L8 — bug found only by running it.**
`client.start_workflow(fn, arg1, arg2, ...)` raises `TypeError: takes from 2 to 3 positional
arguments`. The Temporal Python SDK takes a single positional argument; two workflow inputs must be
passed as `args=[...]`. Unit tests could not see it because they never construct a `Client`. Fixed
in `api.py` and `cli.py`.

**L9 — remaining recommendation, not applied.**
`APPROVAL_MODE=approved` fabricates an `Approval(actor="auto")` and contradicts invariant 6
("never bypass it, not for a quick test"). It is now unnecessary: the smoke run uses a real signal
in human mode. Removal is a policy call (demo convenience vs. guard rail), so it is left as the
next decision rather than changed silently.
