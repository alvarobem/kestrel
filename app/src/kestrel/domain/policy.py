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
class Capability:
    """One thing a run is allowed to do, and the tool it does it with.

    The unit is the *action*, not the tool. "Restart a workload" is a capability
    of `kubectl`; "re-run the integration pipeline" is a capability of `gh`; "read
    the commit that broke the build" is a capability of `git`. A surface is a list
    of these, so the same mechanism covers an on-call restart in production and a
    service that will not boot in integration — different incident, different
    capability, no second code path.
    """

    #: Stable name a plan cites on each step. It is what the audit trail shows,
    #: so it reads as an intent ("restart_workload") and not as a command.
    id: str
    #: First token of every command that uses this capability.
    tool: str
    #: Verbs allowed right after the tool. Empty means any verb is allowed, which
    #: an operator chooses deliberately by leaving it empty.
    verbs: tuple[str, ...] = ()
    #: Literals the command must contain. This is how a surface pins *where* an
    #: action lands — a namespace, a pipeline file, a branch — without the domain
    #: knowing what any of those are.
    must_contain: tuple[str, ...] = ()
    #: When to use it, in the operator's words. Shown to the model.
    description: str = ""

    def allows_verb(self, verb: str) -> bool:
        if not self.verbs:
            return True
        wanted = {item.strip().lower() for item in self.verbs if item.strip()}
        return verb.strip().lower() in wanted


@dataclass(frozen=True, slots=True)
class RemediationSurface:
    """The action space a run may name, frozen when the run starts.

    A plan is written as shell commands, and it is only reviewable if the reviewer
    knows what the environment can actually reach. Declaring capabilities is how
    the model learns what it may propose — and how the guard rail checks it
    without guessing at the operator's intentions.

    Why a frozen value and not a setting read inside the node: an operator editing
    `.env` must not change what an in-flight incident is allowed to run.

    An empty surface is not "no restrictions". It means the run has no legal
    vocabulary, so planning degrades instead of inventing one.

    Scoping a surface to an environment (prod vs integration) is a deployment
    decision today: each deployment declares its own capabilities. Making it a
    per-incident decision needs the incident to carry its environment first —
    which the codebase half-does already, through the entity id.
    """

    capabilities: tuple[Capability, ...] = ()

    @property
    def declared(self) -> bool:
        return bool(self.capabilities)

    def find(self, capability_id: str) -> Capability | None:
        wanted = capability_id.strip().lower()
        for capability in self.capabilities:
            if capability.id.strip().lower() == wanted:
                return capability
        return None


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
