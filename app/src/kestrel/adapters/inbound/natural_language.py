"""Inbound anti-corruption layer for a report a person typed.

The other inbound mappers translate a vendor's contract into our `Incident`. This
one translates a human one: a sentence in the operator's own words, with no
identifiers, no severity field and no timestamp anyone verified.

Two rules make it usable without a model in the request path:

  * the incident is *created here and now*, deterministically — the identity comes
    from the words themselves (`application/policy.natural_incident_id`), so a
    retried client gets a refusal instead of a second remediation;
  * the sentence is carried verbatim in `raw["text"]`. Reading it into severity and
    entities is `triage`'s job, inside the run, where it has a retry policy and can
    degrade with a recorded reason. Editing free text at the door would hide both.

Nothing here talks to the network.
"""

from __future__ import annotations

from kestrel.application.intake import REPORT_SOURCE
from kestrel.application.policy import natural_incident_id
from kestrel.domain.models import Incident

#: A report becomes a title in reports and approval messages, so the first line is
#: what a human reads. The whole sentence stays in `description` and `raw`.
_TITLE_LIMIT = 140

#: A report is a sentence or two. The text travels into workflow history and is
#: re-serialized on every state transition, so an unbounded paste is a payload
#: problem wearing a feature's clothes: it is refused at the door with a message
#: instead of failing later inside the engine, where nobody would know why.
MAX_REPORT_CHARS = 4000


def normalized_report(text: str) -> str:
    """The words of a report, or a `ValueError` naming what is wrong with it.

    One rule, called by every surface: the HTTP model, the CLI verb and the
    mapper below. Whitespace is collapsed because a trailing newline is not a
    second incident, and the length is bounded because the sentence is durable.
    """
    words = " ".join(text.split())
    if not words:
        raise ValueError("a report needs some words")
    if len(words) > MAX_REPORT_CHARS:
        raise ValueError(
            f"the report is {len(words)} characters; the limit is {MAX_REPORT_CHARS}. "
            "Report the symptom and the service — a full log dump belongs in the ticketing tool"
        )
    return words


def incident_from_text(text: str, incident_id: str | None = None) -> Incident:
    """One sentence, one incident.

    `incident_id` overrides the derived identity when the caller genuinely wants a
    second run of the same words — a re-report the day after. Without it, the same
    sentence is the same incident, which is what makes submission idempotent.
    """
    words = normalized_report(text)
    return Incident(
        id=incident_id or natural_incident_id(words),
        # Provenance, not the tool the run investigates with. Together with
        # `raw["text"]` it is what `triage` recognizes as a report to read.
        source=REPORT_SOURCE,
        title=_clip(words, _TITLE_LIMIT),
        description=words,
        raw={"text": words},
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
