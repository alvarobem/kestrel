"""Run identity and the rest of the orchestration policy.

`workflow_id` is the run identity, not a second correlation id. Deriving it from
the incident id is what makes ingestion idempotent: starting the same incident
twice is rejected by Temporal instead of producing a duplicate remediation.

It lives in the application because the rule is ours; it happens to be spelled
as a Temporal workflow id at the boundary.
"""

from __future__ import annotations

import hashlib


def workflow_id(incident_id: str) -> str:
    """Deterministic run identity: one incident, one run."""
    return f"incident-{incident_id}"


#: Prefix of an id derived from a sentence, so provenance is readable in a
#: workflow id (`incident-nl-3f9a…`) without a second lookup.
REPORT_ID_PREFIX = "nl-"


def natural_incident_id(text: str) -> str:
    """The incident id a report's own words earn.

    The sentence *is* the identity, for the same reason a webhook's `problemId`
    is: a client that retries must not be able to start a second remediation of
    the same report. Case and spacing are normalized so a double-click, a
    trailing newline or a capitalized retry are the same incident; anything else
    is a different sentence and therefore a different report. A caller that
    really needs a second run of the same words says so by supplying an id.

    Pure: no clock, no randomness. Two processes derive the same id.
    """
    normalized = " ".join(text.split()).casefold()
    if not normalized:
        raise ValueError("an empty report has no incident identity")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{REPORT_ID_PREFIX}{digest[:12]}"
