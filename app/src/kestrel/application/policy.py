"""Run identity and the rest of the orchestration policy.

`workflow_id` is the run identity, not a second correlation id. Deriving it from
the incident id is what makes ingestion idempotent: starting the same incident
twice is rejected by Temporal instead of producing a duplicate remediation.

It lives in the application because the rule is ours; it happens to be spelled
as a Temporal workflow id at the boundary.
"""

from __future__ import annotations


def workflow_id(incident_id: str) -> str:
    """Deterministic run identity: one incident, one run."""
    return f"incident-{incident_id}"
