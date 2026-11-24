"""Inbound anti-corruption registry: one payload mapper per telemetry provider.

A webhook body is an external contract owned by a vendor, so translating it into
our `Incident` belongs on the inbound side. That was true for a single tool; with
more than one, the mapping also needs a name, which is what this module adds and
nothing else: no network, no rules, no state.

`POST /incidents/webhook/{provider}` resolves an entry here. An unknown name is a
client error, never a silent default: mapping a Datadog payload as if it were
Dynatrace would produce an incident that looks valid and is nonsense.
"""

from __future__ import annotations

from collections.abc import Callable

from kestrel.adapters.inbound.dynatrace_problem import problem_to_incident
from kestrel.domain.models import Incident

#: A vendor payload, in whatever shape that vendor sends.
PayloadMapper = Callable[[dict], Incident]

INCIDENT_MAPPERS: dict[str, PayloadMapper] = {
    "dynatrace": problem_to_incident,
}


class UnknownIncidentProvider(LookupError):
    """A webhook arrived for a provider that has no payload mapper."""

    def __init__(self, provider_id: str, known: list[str]) -> None:
        super().__init__(
            f"unknown incident provider {provider_id!r} "
            f"(configured mappers: {', '.join(known)})"
        )
        self.provider_id = provider_id


def incident_from_payload(provider_id: str, payload: dict) -> Incident:
    key = (provider_id or "").strip().lower()
    mapper = INCIDENT_MAPPERS.get(key)
    if mapper is None:
        raise UnknownIncidentProvider(key, sorted(INCIDENT_MAPPERS))
    return mapper(payload)
