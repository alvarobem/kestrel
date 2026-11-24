"""HTTP control plane: a thin driving adapter.

Stateless on purpose: it starts runs, exposes state and forwards human
decisions. It never runs the graph, so it can be scaled or replaced without
affecting a single in-flight incident.

What does *not* live here any more: the run's identity, the idempotency rule,
whether a decision is allowed, and any knowledge of the engine. Those are the
application's (`application/incidents.py`), reached through `RunGateway`. This
module owns routes, request/response shapes and HTTP status codes — the
translation from an application error to a status is registered once, as an
exception handler.

Endpoints:
    GET  /health
    POST /incidents                     start a run from a normalized incident
    POST /incidents/webhook/{provider}  start a run from that provider's payload
    GET  /incidents/{id}                status, phase and pending approval
    POST /incidents/{id}/resume         approve/reject the pending plan
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from kestrel.adapters.inbound.webhooks import UnknownIncidentProvider, incident_from_payload
from kestrel.adapters.outbound.runs import TemporalRunGateway
from kestrel.application.errors import (
    IncidentAlreadyRunning,
    IncidentNotFound,
    NoPendingApproval,
    RunUnavailable,
)
from kestrel.application.incidents import (
    decide_incident,
    inspect_incident,
    submit_incident,
)
from kestrel.application.ports import RunGateway, RunHandle
from kestrel.config import Settings, get_settings
from kestrel.domain.models import Approval, Incident

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    # The gateway connects lazily: a stateless control plane must be able to
    # boot while the engine is briefly unavailable.
    app.state.runs = TemporalRunGateway(settings)
    app.state.limits = settings.run_limits
    app.state.settings = settings
    logger.info(
        "api ready | temporal=%s namespace=%s tracing=%s",
        settings.temporal_address,
        settings.temporal_namespace,
        settings.tracing_enabled,
    )
    yield


app = FastAPI(title="kestrel", version="0.2.0", lifespan=lifespan)


class ResumeRequest(BaseModel):
    approved: bool
    actor: str = "unknown"
    comment: str = ""


def _runs(request: Request) -> RunGateway:
    return request.app.state.runs


def _handle_payload(handle: RunHandle) -> dict:
    return {
        "incident_id": handle.incident_id,
        "workflow_id": handle.workflow_id,
        "run_id": handle.run_id,
    }


# --- application error -> HTTP status, registered once ---------------------


@app.exception_handler(IncidentAlreadyRunning)
async def _already_running(request: Request, exc: IncidentAlreadyRunning) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "incident already has a run"})


@app.exception_handler(IncidentNotFound)
async def _not_found(request: Request, exc: IncidentNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "incident not found"})


@app.exception_handler(NoPendingApproval)
async def _not_pending(request: Request, exc: NoPendingApproval) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "incident is not waiting for approval"})


@app.exception_handler(RunUnavailable)
async def _unavailable(request: Request, exc: RunUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": f"temporal unavailable: {exc.detail}"})


# --- routes ----------------------------------------------------------------


@app.get("/health")
def health(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    return {
        "status": "ok",
        "env": settings.app_env,
        "dry_run": settings.dry_run,
        "approval_mode": settings.approval_mode,
        "temporal_address": settings.temporal_address,
        "tracing_enabled": settings.tracing_enabled,
        "telemetry_provider": settings.telemetry_provider,
    }


@app.post("/incidents", status_code=202)
async def create_incident(incident: Incident, request: Request) -> dict:
    handle = await submit_incident(_runs(request), incident, request.app.state.limits)
    return _handle_payload(handle)


@app.post("/incidents/webhook/{provider}", status_code=202)
async def create_from_provider(provider: str, payload: dict, request: Request) -> dict:
    try:
        incident = incident_from_payload(provider, payload)
    except UnknownIncidentProvider as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        # A payload the vendor sent that does not fit the contract is a client
        # error, not a server error.
        raise HTTPException(
            status_code=422, detail=f"unsupported {provider} payload: {exc.error_count()} error(s)"
        ) from exc
    handle = await submit_incident(_runs(request), incident, request.app.state.limits)
    return _handle_payload(handle)


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str, request: Request) -> dict:
    view = await inspect_incident(_runs(request), incident_id)
    return {
        "incident_id": view.incident_id,
        "workflow_id": view.workflow_id,
        "status": view.status,
        "run_id": view.run_id,
        "awaiting_approval": view.awaiting_approval,
        "pending_approval": jsonable_encoder(view.pending_approval),
    }


@app.post("/incidents/{incident_id}/resume", status_code=202)
async def resume_incident(incident_id: str, decision: ResumeRequest, request: Request) -> dict:
    approval = Approval(approved=decision.approved, actor=decision.actor, comment=decision.comment)
    await decide_incident(_runs(request), incident_id, approval)
    return {"incident_id": incident_id, "accepted": True}
