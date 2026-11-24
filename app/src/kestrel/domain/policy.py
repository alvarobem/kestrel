"""Run policy: the bounded-attempt guard rails and the limits a run starts with.

These live in the domain because they are rules, not configuration: a bad
hypothesis must not loop forever, and a replayed history must not diverge
because an operator changed `.env` between restarts. `config.py` only supplies
the operator-facing defaults, and the limits travel with the run inside
`IncidentState`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_INVESTIGATION_ATTEMPTS = 3
DEFAULT_MAX_REMEDIATION_ATTEMPTS = 2


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
