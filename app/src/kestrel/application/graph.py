"""Flow assembly: which step follows which, and how each one runs.

Flow:

    START -> receive -> triage -> investigate -+-> (retry) investigate
                                               +-> plan -> approval_gate -+-> execute -> verify -+-> report
                                                                          +-> report             +-> (retry) investigate
    report -> END

Every node declares how it runs (`execute_in`) and its Temporal activity
options. That declaration is the contract with the plugin: an I/O node must be
an activity, and the routers run inside the workflow and stay deterministic.

This module is the one declared exception to the layer rule: it may import
`langgraph` and `temporalio`, because the flow and the per-step execution policy
are written in their vocabulary. Every other application module may import
neither, and `application/` may never import `kestrel.config`.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import timedelta

from langgraph.graph import END, START, StateGraph
from temporalio.common import RetryPolicy

from kestrel.application import nodes
from kestrel.application.deps import CoreDeps
from kestrel.application.state import IncidentState

GRAPH_NAME = "incident"

#: A use case, as the graphest thing it is: state and dependencies in, a
#: partial update out.
NodeFn = Callable[[IncidentState, CoreDeps], Awaitable[dict]]

#: What the graph itself calls: dependencies already bound.
BoundNode = Callable[[IncidentState], Awaitable[dict]]

#: Cross-cutting wrapping applied by the composition root (tracing, today).
NodeDecorator = Callable[[str, BoundNode], BoundNode]

NODE_FUNCTIONS: dict[str, NodeFn] = {
    "receive": nodes.receive,
    "triage": nodes.triage,
    "investigate": nodes.investigate,
    "plan": nodes.plan,
    "approval_gate": nodes.approval_gate,
    "execute": nodes.execute,
    "verify": nodes.verify,
    "report": nodes.report,
}


def _activity(timeout: timedelta, attempts: int) -> dict:
    return {
        "execute_in": "activity",
        "start_to_close_timeout": timeout,
        "retry_policy": RetryPolicy(maximum_attempts=attempts),
    }


# `execute` is the only node that can change production, so it must never be
# retried automatically: a blind retry of a rolling restart is an incident, not
# a recovery. A failure goes back through verification and a new approval.
ACTIVITY_OPTIONS: dict[str, dict] = {
    "receive": _activity(timedelta(seconds=30), 3),
    "triage": _activity(timedelta(minutes=2), 3),
    "investigate": _activity(timedelta(minutes=3), 3),
    "plan": _activity(timedelta(minutes=3), 2),
    "approval_gate": _activity(timedelta(minutes=1), 1),
    "execute": _activity(timedelta(minutes=10), 1),
    "verify": _activity(timedelta(minutes=3), 3),
    "report": _activity(timedelta(minutes=2), 2),
}


def _bind(name: str, fn: NodeFn, deps: CoreDeps) -> BoundNode:
    """Close a use case over its dependencies, keeping the name the plugin sees."""

    @functools.wraps(fn)
    async def bound(state: IncidentState) -> dict:
        return await fn(state, deps)

    bound.__name__ = name
    return bound


def build_graph(
    deps: CoreDeps,
    *,
    instrumentation: NodeDecorator | None = None,
) -> StateGraph:
    """Build the uncompiled graph the Temporal plugin registers.

    Returns the raw StateGraph, not a compiled one: under Temporal, compilation
    happens inside the workflow (`temporal_graph(GRAPH_NAME).compile(...)`).

    `deps` and `instrumentation` are parameters, not lookups: the composition
    root decides, and this module never reads settings.
    """
    builder = StateGraph(IncidentState)

    for name, fn in NODE_FUNCTIONS.items():
        node_fn: BoundNode = _bind(name, fn, deps)
        if instrumentation is not None:
            node_fn = instrumentation(name, node_fn)
        builder.add_node(name, node_fn, metadata=ACTIVITY_OPTIONS[name])

    builder.add_edge(START, "receive")
    builder.add_edge("receive", "triage")
    builder.add_edge("triage", "investigate")
    builder.add_conditional_edges(
        "investigate",
        nodes.route_after_investigation,
        {"investigate": "investigate", "plan": "plan"},
    )
    builder.add_edge("plan", "approval_gate")
    builder.add_conditional_edges(
        "approval_gate",
        nodes.route_after_approval,
        {"execute": "execute", "report": "report"},
    )
    builder.add_edge("execute", "verify")
    builder.add_conditional_edges(
        "verify",
        nodes.route_after_verify,
        {"report": "report", "investigate": "investigate"},
    )
    builder.add_edge("report", END)

    return builder
