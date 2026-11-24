"""Temporal worker: the composition root, and the only process that runs nodes.

This is where the architecture is decided at runtime. Everything inward depends
on a port or a frozen policy value; only this module knows which adapter
provides it and which approval channel the operator configured. If you want to
change *how* the system talks to the world, you change it here and nowhere else.

Scale it horizontally and the task queue load-balances across instances:
    docker compose up -d --scale agent-worker=3

Every node runs as an activity, so a crashed worker loses nothing — Temporal
replays the workflow and re-dispatches the pending activity.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from langgraph.graph import StateGraph
from temporalio.common import RetryPolicy
from temporalio.langgraph import LangGraphPlugin
from temporalio.worker import Worker

from kestrel.adapters.inbound.workflow import IncidentWorkflow
from kestrel.adapters.outbound.approval import AutoApprovalGateway, GraphApprovalGateway
from kestrel.adapters.outbound.providers import build_telemetry_provider
from kestrel.adapters.outbound.temporal import connect
from kestrel.adapters.outbound.tracing import flush_traces, trace_node
from kestrel.application.deps import CoreDeps
from kestrel.application.graph import GRAPH_NAME, build_graph
from kestrel.config import Settings, get_settings
from kestrel.domain.policy import InvestigationScope

logger = logging.getLogger(__name__)


def build_deps(settings: Settings) -> CoreDeps:
    """Freeze the operator's decisions into the dependencies of a run.

    Reading `.env` happens here, once, outside the reasoning. From this point on
    the core sees values: `dry_run=True` is a fact of the run, not a lookup that
    could change between two replays. The active telemetry provider is chosen
    here too — and only here — so no node ever names a vendor or builds a client.
    """
    approval = (
        AutoApprovalGateway() if settings.approval_mode == "approved" else GraphApprovalGateway()
    )
    return CoreDeps(
        approval=approval,
        dry_run=settings.dry_run,
        # `None` when the selected provider has no credentials: the run degrades
        # and says so instead of failing to start.
        telemetry=build_telemetry_provider(settings),
        scope=InvestigationScope(tags=tuple(settings.scope_tags)),
        # No isolated executor exists yet, so `execute` fails closed when the
        # run is not a dry run. Wiring one is the sandbox decision (§13).
        executor=None,
    )


def build_app(settings: Settings) -> StateGraph:
    """Assemble the graph the worker registers, with tracing if it is configured."""
    instrumentation = trace_node if settings.tracing_enabled else None
    return build_graph(build_deps(settings), instrumentation=instrumentation)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    client = await connect(settings)
    plugin = LangGraphPlugin(
        graphs={GRAPH_NAME: build_app(settings)},
        # Fallback for any node that does not override it. `execute_in` is
        # deliberately not here: the plugin requires it per node.
        default_activity_options={
            "start_to_close_timeout": timedelta(minutes=2),
            "retry_policy": RetryPolicy(maximum_attempts=3),
        },
    )

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[IncidentWorkflow],
        plugins=[plugin],
    )

    deps = build_deps(settings)
    logger.info(
        "worker ready | temporal=%s namespace=%s queue=%s tracing=%s dry_run=%s approval=%s "
        "telemetry=%s configured=%s scope=%s",
        settings.temporal_address,
        settings.temporal_namespace,
        settings.temporal_task_queue,
        settings.tracing_enabled,
        settings.dry_run,
        settings.approval_mode,
        settings.telemetry_provider,
        deps.telemetry is not None,
        ",".join(deps.scope.tags) or "unrestricted",
    )

    try:
        await worker.run()
    finally:
        flush_traces()


if __name__ == "__main__":
    asyncio.run(main())
