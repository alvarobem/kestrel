"""Temporal workflow: one run per incident.

This is the piece that makes the system durable. The workflow owns the
lifecycle, the approval wait and the audit trail; the graph (running as
activities) owns the reasoning. Temporal's history is the single source of
truth — there is no second checkpoint store to disagree with it.

It is a driving adapter: the world drives us through signals and queries. It is
instantiated by name by the Temporal runtime, so it cannot receive injected
dependencies; it therefore assembles what it hosts (the graph and the
checkpointer serializer). That is the documented exception to "adapters do not
wire".

Human-in-the-loop:
  * `interrupt()` inside the approval_gate node suspends the graph.
  * The workflow publishes the pending plan through a query.
  * An approval arrives as a signal and resumes the graph with `Command`.

Nothing here does I/O. Anything that touches the network lives in a node, which
runs as an activity.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from temporalio import workflow
from temporalio.langgraph import graph as temporal_graph

with workflow.unsafe.imports_passed_through():
    from kestrel.adapters.outbound.checkpoint import build_serde
    from kestrel.application.graph import GRAPH_NAME
    from kestrel.application.state import initial_state
    from kestrel.domain.models import Incident
    from kestrel.domain.policy import RunLimits


@workflow.defn
class IncidentWorkflow:
    def __init__(self) -> None:
        self._decision: dict | None = None
        self._pending: dict | None = None

    # --- external interface ------------------------------------------------

    @workflow.signal
    async def approve(self, decision: dict) -> None:
        """Deliver the human decision for the plan currently on the table."""
        self._decision = decision

    @workflow.query
    def pending_approval(self) -> dict | None:
        """The plan waiting for a decision, or None."""
        return self._pending

    # --- lifecycle ---------------------------------------------------------

    @workflow.run
    async def run(self, incident: dict, limits: dict | None = None) -> dict:
        app = temporal_graph(GRAPH_NAME).compile(checkpointer=InMemorySaver(serde=build_serde()))
        config = RunnableConfig({"configurable": {"thread_id": workflow.info().workflow_id}})

        # The caller (API or CLI) snapshots the bounded-attempt limits into the
        # input. Reading settings here would be non-deterministic I/O inside the
        # workflow, and the routers depend on these values.
        state = initial_state(
            Incident.model_validate(incident),
            RunLimits.from_workflow_input(limits),
        )

        result = await app.ainvoke(state, config, version="v2")

        # A plan can be rejected and a later attempt can ask again, so handle
        # interrupts in a loop instead of once.
        while result.interrupts:
            self._pending = result.interrupts[0].value
            self._decision = None
            await workflow.wait_condition(lambda: self._decision is not None)

            decision = self._decision
            self._decision = None
            self._pending = None
            result = await app.ainvoke(Command(resume=decision), config, version="v2")

        # The plugin's v2 result reports untouched additive channels as null.
        # The state contract says list, and callers rely on it, so normalize
        # here rather than teaching every consumer about the quirk.
        final = dict(result.value)
        for channel in ("evidence", "approvals", "actions", "notes"):
            if final.get(channel) is None:
                final[channel] = []
        return final
