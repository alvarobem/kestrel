"""Run policy: the bounded-attempt guard rails and the limits a run starts with.

These live in the domain because they are rules, not configuration: a bad
hypothesis must not loop forever, and a replayed history must not diverge
because an operator changed `.env` between restarts. `config.py` only supplies
the operator-facing defaults, and the limits travel with the run inside
`IncidentState`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_INVESTIGATION_ATTEMPTS = 3
DEFAULT_MAX_REMEDIATION_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class InvestigationScope:
    """Which telemetry a run may reason about.

    A governance decision, so it is a value frozen when the run starts, never a
    setting read inside a node: an operator changing the scope must not change
    what an in-flight incident is allowed to read.

    An empty scope means unrestricted. That is the local-development default and
    it is stated here rather than implied, because "no tags" and "every tag" are
    dangerously easy to confuse.

    The matching rule is deliberately coarse for now: a candidate (a management
    zone name, a tag key or a tag value) is in scope when it equals one of the
    configured tags, case-insensitively. Ranking evidence by relevance inside the
    scope is Roadmap Phase 1.
    """

    tags: tuple[str, ...] = ()

    @property
    def unrestricted(self) -> bool:
        return not self.tags

    def allows(self, candidates: Iterable[str]) -> bool:
        if self.unrestricted:
            return True
        wanted = {tag.strip().lower() for tag in self.tags if tag.strip()}
        return any(candidate.strip().lower() in wanted for candidate in candidates if candidate)


@dataclass(frozen=True, slots=True)
class RunLimits:
    """How many times a run may loop, snapshotted when it starts.

    It crosses the process boundary as the workflow input, which is why it can
    serialize itself: a plain `dict[str, int]` is the wire shape, `RunLimits` is
    the type the code reasons about.
    """

    max_investigation_attempts: int = DEFAULT_MAX_INVESTIGATION_ATTEMPTS
    max_remediation_attempts: int = DEFAULT_MAX_REMEDIATION_ATTEMPTS

    def as_workflow_input(self) -> dict[str, int]:
        return {
            "max_investigation_attempts": self.max_investigation_attempts,
            "max_remediation_attempts": self.max_remediation_attempts,
        }

    @classmethod
    def from_workflow_input(cls, raw: Mapping[str, Any] | None) -> RunLimits:
        """Rebuild the limits from the workflow input, defaulting what is missing."""
        data = dict(raw or {})
        return cls(
            max_investigation_attempts=int(
                data.get("max_investigation_attempts", DEFAULT_MAX_INVESTIGATION_ATTEMPTS)
            ),
            max_remediation_attempts=int(
                data.get("max_remediation_attempts", DEFAULT_MAX_REMEDIATION_ATTEMPTS)
            ),
        )
