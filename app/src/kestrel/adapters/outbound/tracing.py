"""Langfuse tracing.

Each graph node runs as its own Temporal activity, i.e. in its own process.
A LangChain callback handler cannot cross that boundary, so tracing is done at
the node boundary instead: one trace per node, all grouped into one Langfuse
**session per incident**.

The trade-off is deliberate: a session gives you the full incident timeline in
the UI, and it survives process boundaries without stitching state by hand.
LLM generations inside a node still nest under that node's trace, because they
share the process.

Tracing is opt-in. Without keys, `build_graph()` registers the bare nodes and
nothing here is imported.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

NodeFn = Callable[..., Awaitable[dict]]


def trace_node(name: str, fn: NodeFn) -> NodeFn:
    """Wrap a node so one trace is emitted per execution, grouped by incident."""
    from langfuse import observe, propagate_attributes

    @observe(name=name, capture_input=False, capture_output=False)
    @functools.wraps(fn)
    async def traced(state: dict, *args: Any, **kwargs: Any) -> dict:
        incident = state.get("incident")
        incident_id = getattr(incident, "id", None) or (incident or {}).get("id", "unknown")
        with propagate_attributes(session_id=str(incident_id), tags=["incident", name]):
            result = await fn(state, *args, **kwargs)
        return result

    return traced


def flush_traces() -> None:
    """Flush the background queue before the process exits."""
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        # Tracing is best-effort: a dead Langfuse must never fail a run.
        logger.exception("failed to flush Langfuse traces")
