"""Operator CLI.

    python -m kestrel.adapters.inbound.cli provider-check
    python -m kestrel.adapters.inbound.cli llm-check
    python -m kestrel.adapters.inbound.cli llm-login
    python -m kestrel.adapters.inbound.cli smoke

`smoke` starts a real workflow against the local Temporal server, waits for the
approval request, approves it and prints the final report. It requires the
agent-worker to be running (`make up`).

It is a driving adapter, so it goes through the same use cases as the HTTP
control plane (`application/incidents.py`) instead of assembling a workflow
start of its own. `provider-check` is a diagnostic: it proves the configured
telemetry provider can be read, which is why it asks the provider registry rather
than naming a vendor. `llm-check` and `llm-login` are the same idea one layer
over: they ask the LLM registry, so the operator never learns whether the seat,
a key or a gateway is behind it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from uuid import uuid4

from kestrel.adapters.outbound.llm import (
    DIAGNOSTIC_PROMPT,
    UnknownLlmProvider,
    build_llm,
    describe_llm,
    sign_in,
    token_path,
)
from kestrel.adapters.outbound.providers import UnknownTelemetryProvider, describe_telemetry
from kestrel.adapters.outbound.runs import TemporalRunGateway
from kestrel.application.errors import LlmUnavailable
from kestrel.application.incidents import decide_incident, submit_incident
from kestrel.config import Settings, get_settings
from kestrel.domain.models import Approval, Incident, Severity

logger = logging.getLogger(__name__)


def cmd_provider_check(settings: Settings) -> int:
    print(f"telemetry provider: {settings.telemetry_provider}")
    try:
        readable, lines = describe_telemetry(settings)
    except UnknownTelemetryProvider as exc:
        print(exc)
        return 2
    for line in lines:
        print(line)
    return 0 if readable else 2


async def cmd_llm_check(settings: Settings) -> int:
    """Prove the configured model answers, with the credential it was given."""
    try:
        usable, lines = describe_llm(settings)
    except UnknownLlmProvider as exc:
        print(exc)
        return 2
    for line in lines:
        print(line)
    if not usable:
        return 2

    client = build_llm(settings)
    if client is None:  # `usable` and no client is a bug in the registry
        print("llm-check failed: the registry reported a usable transport and built none")
        return 2

    started = time.perf_counter()
    try:
        answer = await client.complete(
            system="You are a connectivity diagnostic. Answer in as few words as possible.",
            user=DIAGNOSTIC_PROMPT,
        )
    except LlmUnavailable as exc:
        print(exc)
        print("hint: if the seat is not signed in yet, run `make llm-login` once")
        return 2
    elapsed = time.perf_counter() - started

    print(f"answer:  {answer.strip()}")
    print(f"latency: {elapsed:.1f}s")
    return 0


def cmd_llm_login(settings: Settings) -> int:
    """Authorize the seat once, so no API key exists anywhere in this repo.

    Synchronous on purpose: the device flow waits for a human, and the client
    library refuses to run it inside an event loop. The proof comes after —
    the same `llm-check` that the worker's transport is exercised with.
    """
    print(f"llm provider: {settings.llm_provider}")
    print(f"token target: {token_path(settings)}")
    print("")
    print("Open https://github.com/login/device when the code appears below and")
    print("approve it. This is a one-time step for this machine.")
    print("")
    try:
        sign_in(settings)
    except (UnknownLlmProvider, LlmUnavailable, OSError) as exc:
        print(f"login failed: {exc}")
        return 2
    print("signed in. Verifying the seat actually answers:")
    print("")
    return asyncio.run(cmd_llm_check(settings))


async def cmd_smoke(settings: Settings) -> int:
    runs = TemporalRunGateway(settings)
    incident = Incident(
        # Unique per run: the workflow id is derived from the incident id and
        # duplicates are rejected by design.
        id=f"smoke-{uuid4().hex[:8]}",
        title="smoke test: placeholder incident",
        severity=Severity.UNKNOWN,
        affected_entities=["checkout-service"],
    )

    handle = await submit_incident(runs, incident, settings.run_limits)
    print(f"started {handle.workflow_id}")

    pending = None
    for _ in range(60):
        pending = await runs.pending_approval(incident.id)
        if pending is not None:
            break
        # Stop waiting as soon as the run reached a terminal phase: a run can
        # finish without ever asking, and 30 seconds of silence is a bad way to
        # learn it.
        if (await runs.view(incident.id)).status != "RUNNING":
            break
        await asyncio.sleep(0.5)

    if pending is None:
        return await _explain_no_approval(runs, incident.id)

    print(f"approval requested: {pending.get('plan', {}).get('summary', 'unknown plan')}")
    await decide_incident(runs, incident.id, Approval(approved=True, actor="smoke"))

    result = await runs.outcome(incident.id, timeout_seconds=120)
    report = result.get("report")
    if hasattr(report, "model_dump"):
        report = report.model_dump(mode="json")

    print(f"final phase={result.get('phase')}")
    print(json.dumps(report, indent=2))

    # Smoke proves the plumbing: a run reached a terminal phase and wrote a
    # report. It does not prove the incident was fixed — in dry-run mode the
    # verification is explicitly empty, and the report says resolved=false.
    if not isinstance(report, dict):
        print("smoke failed: the run produced no report")
        return 1
    if report.get("resolved"):
        return 0
    if settings.dry_run:
        print("note: resolved=false — a dry run applies nothing, so there is nothing to verify")
        return 0
    print("smoke failed: the run did not resolve and it was not a dry run")
    return 1


async def _explain_no_approval(runs: TemporalRunGateway, incident_id: str) -> int:
    """Say why nothing reached the human, instead of blaming the stack.

    A run that finds no evidence, or whose planner declines to invent one, has
    nothing to approve — and that is the system working. The operator needs the
    reason, not a stack trace or a 30-second silence.
    """
    view = await runs.view(incident_id)
    if view.status == "RUNNING":
        print("no approval requested and the run is still running; is agent-worker up? (`make up`)")
        return 1

    result = await runs.outcome(incident_id, timeout_seconds=10)
    print(f"the run finished without asking for approval (status={view.status})")
    print(f"final phase={result.get('phase')}")
    for note in result.get("notes") or []:
        print(f"  - {note}")

    report = result.get("report")
    if hasattr(report, "model_dump"):
        report = report.model_dump(mode="json")
    if isinstance(report, dict) and report.get("top_evidence"):
        print("evidence a human should read first:")
        for line in report["top_evidence"]:
            print(f"  {line}")
    return 0 if view.status == "COMPLETED" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kestrel")
    parser.add_argument(
        "command",
        choices=["provider-check", "llm-check", "llm-login", "smoke", "dynatrace-check"],
        help="`dynatrace-check` is a deprecated alias of `provider-check`",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    if args.command in {"provider-check", "dynatrace-check"}:
        return cmd_provider_check(settings)
    if args.command == "llm-check":
        return asyncio.run(cmd_llm_check(settings))
    if args.command == "llm-login":
        return cmd_llm_login(settings)
    return asyncio.run(cmd_smoke(settings))


if __name__ == "__main__":
    sys.exit(main())
