"""The telemetry provider registry: the one place a tool is chosen.

`TELEMETRY_PROVIDER` picks an entry here; exactly one provider is active per
run. A provider is a `ProviderSpec`: how to build the adapter from settings, and
how to describe it for the operator diagnostic.

Adding a tool is two steps and no core change:

  1. write `adapters/outbound/<tool>_provider.py` implementing `TelemetryProvider`;
  2. add one entry to `PROVIDERS` below.

A name that no adapter implements fails at startup. Degrading silently because
of a typo would mean a run that investigates nothing and reports no reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from kestrel.adapters.outbound import dynatrace_provider
from kestrel.application.ports import TelemetryProvider
from kestrel.config import Settings


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Everything the composition root needs to know about one tool."""

    #: `None` when the tool is selected but not configured with credentials.
    build: Callable[[Settings], TelemetryProvider | None]
    #: `(readable, lines)`: whether the tool can be read at all, and what to print.
    describe: Callable[[Settings], tuple[bool, list[str]]]


PROVIDERS: dict[str, ProviderSpec] = {
    "dynatrace": ProviderSpec(
        build=dynatrace_provider.build,
        describe=dynatrace_provider.describe,
    ),
}


class UnknownTelemetryProvider(RuntimeError):
    """`TELEMETRY_PROVIDER` names a tool that has no adapter."""

    def __init__(self, provider_id: str, known: list[str]) -> None:
        super().__init__(
            f"unknown telemetry provider {provider_id!r} (configured adapters: {', '.join(known)})"
        )
        self.provider_id = provider_id


def provider_spec(settings: Settings) -> ProviderSpec:
    provider_id = settings.telemetry_provider.strip().lower()
    spec = PROVIDERS.get(provider_id)
    if spec is None:
        raise UnknownTelemetryProvider(provider_id, sorted(PROVIDERS))
    return spec


def build_telemetry_provider(settings: Settings) -> TelemetryProvider | None:
    """The active provider for this run, or `None` when it is not configured."""
    return provider_spec(settings).build(settings)


def describe_telemetry(settings: Settings) -> tuple[bool, list[str]]:
    """Operator diagnostic for the active provider: `(readable, lines)`."""
    return provider_spec(settings).describe(settings)
