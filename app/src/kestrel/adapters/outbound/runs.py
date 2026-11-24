"""Implementation of `RunGateway` over Temporal.

The mechanics of a run live here: how it is started, addressed, queried and
resumed, and how the engine's exceptions are translated into the application's
typed errors. What a run *means* — one incident one run, decisions only while
pending — is stated by the port contract and decided in
`application/incidents.py`.

Nothing above this module imports `temporalio`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from kestrel.adapters.inbound.workflow import IncidentWorkflow
from kestrel.adapters.outbound.temporal import connect
from kestrel.application.errors import (
    IncidentAlreadyRunning,
    IncidentNotFound,
    RunUnavailable,
)
from kestrel.application.policy import workflow_id
from kestrel.application.ports import IncidentRunView, RunHandle
from kestrel.config import Settings
from kestrel.domain.models import Approval, Incident
from kestrel.domain.policy import RunLimits


class TemporalRunGateway:
    """Runs are Temporal workflows, one per incident."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Client | None = None

    async def _client_connection(self) -> Client:
        # Lazy on purpose: the control plane is stateless and must start even if
        # the engine is briefly unavailable. The first request reports
        # `RunUnavailable` instead of the process refusing to boot.
        if self._client is None:
            self._client = await connect(self._settings)
        return self._client

    async def _handle(self, incident_id: str) -> WorkflowHandle:
        client = await self._client_connection()
        return client.get_workflow_handle(workflow_id(incident_id))

    @staticmethod
    def _translate(incident_id: str, exc: RPCError) -> Exception:
        """Engine exception -> application error. The only place that knows both."""
        if exc.status == RPCStatusCode.NOT_FOUND:
            return IncidentNotFound(incident_id)
        return RunUnavailable(exc.message)

    async def start(self, incident: Incident, limits: RunLimits) -> RunHandle:
        client = await self._client_connection()
        try:
            handle = await client.start_workflow(
                IncidentWorkflow.run,
                args=[
                    incident.model_dump(mode="json"),
                    # The limits are snapshotted into the run: the routers run
                    # inside the workflow and must not read settings.
                    limits.as_workflow_input(),
                ],
                id=workflow_id(incident.id),
                task_queue=self._settings.temporal_task_queue,
                # Same incident, same run: a duplicate webhook cannot start a
                # second remediation.
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError as exc:
            raise IncidentAlreadyRunning(incident.id) from exc

        return RunHandle(
            incident_id=incident.id,
            workflow_id=handle.id,
            run_id=handle.result_run_id,
        )

    async def view(self, incident_id: str) -> IncidentRunView:
        handle = await self._handle(incident_id)
        try:
            description = await handle.describe()
            pending = await handle.query(IncidentWorkflow.pending_approval)
        except RPCError as exc:
            raise self._translate(incident_id, exc) from exc

        return IncidentRunView(
            incident_id=incident_id,
            workflow_id=handle.id,
            status=description.status.name,
            run_id=description.run_id,
            pending_approval=pending,
        )

    async def pending_approval(self, incident_id: str) -> dict | None:
        handle = await self._handle(incident_id)
        try:
            return await handle.query(IncidentWorkflow.pending_approval)
        except RPCError as exc:
            raise self._translate(incident_id, exc) from exc

    async def submit_decision(self, incident_id: str, decision: Approval) -> None:
        handle = await self._handle(incident_id)
        # The gate parses exactly these three keys; `at` is stamped by the node,
        # not by the caller, so the decision travels without it.
        payload = {
            "approved": decision.approved,
            "actor": decision.actor,
            "comment": decision.comment,
        }
        try:
            await handle.signal(IncidentWorkflow.approve, payload)
        except RPCError as exc:
            raise self._translate(incident_id, exc) from exc

    async def outcome(self, incident_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        handle = await self._handle(incident_id)
        try:
            return await asyncio.wait_for(handle.result(), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise RunUnavailable(
                f"run for incident {incident_id} did not finish within {timeout_seconds:g}s"
            ) from exc
        except RPCError as exc:
            raise self._translate(incident_id, exc) from exc
