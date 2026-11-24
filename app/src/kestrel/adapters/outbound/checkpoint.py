"""Serializer for the in-workflow checkpointer.

LangGraph still serializes state into its (in-memory) checkpoints even when
Temporal owns durability. Unregistered pydantic types work today but warn that
they will be rejected in a future release, so the types that can appear in
`IncidentState` are declared once, here.

Add every new state type to STATE_TYPES. `tests/test_surface.py` proves that no
type reachable from the state is missing.
"""

from __future__ import annotations

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

STATE_TYPES: list[tuple[str, str]] = [
    ("kestrel.domain.models", name)
    for name in (
        "ActionRecord",
        "Approval",
        "Evidence",
        "EvidenceKind",
        "Hypothesis",
        "Incident",
        "IncidentReport",
        "IncidentStatus",
        "Phase",
        "RemediationPlan",
        "RemediationStep",
        "Severity",
        "Verification",
    )
]


def build_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES)
