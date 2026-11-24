"""Operator CLI.

    python -m kestrel.adapters.inbound.cli provider-check
    python -m kestrel.adapters.inbound.cli smoke

`smoke` starts a real workflow against the local Temporal server, waits for the
approval request, approves it and prints the final report. It requires the
agent-worker to be running (`make up`).

It is a driving adapter, so it goes through the same use cases as the HTTP
control plane (`application/incidents.py`) instead of assembling a workflow
start of its own. `provider-check` is a diagnostic: it proves the configured
telemetry provider can be read, which is why it asks the provider registry rather
than naming a vendor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from uuid import uuid4

from kestrel.adapters.outbound.providers import UnknownTelemetryProvider, describe_telemetry
from kestrel.adapters.outbound.runs import TemporalRunGateway
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
        await asyncio.sleep(0.5)

    if pending is None:
        print("no approval requested within 30s; is agent-worker running? (`make up`)")
        return 1

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kestrel")
    parser.add_argument(
        "command",
        choices=["provider-check", "smoke", "dynatrace-check"],
        help="`dynatrace-check` is a deprecated alias of `provider-check`",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    if args.command in {"provider-check", "dynatrace-check"}:
        return cmd_provider_check(settings)
    return asyncio.run(cmd_smoke(settings))


if __name__ == "__main__":
    sys.exit(main())
