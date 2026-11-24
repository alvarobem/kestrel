"""What a use case is allowed to know about the world.

`CoreDeps` is the seam the application uses to stay framework-free. It carries
two different things and that is deliberate:

  * ports — the capabilities the core drives (`approval`, `telemetry`, `llm`,
    `executor`);
  * policy values — decisions already taken somewhere else, frozen here
    (`dry_run`, `scope`, `surface`).

Policy values are values, not lookups: a node that reads `os.environ` is
non-reproducible, and a router that does it diverges from Temporal's replay.
The composition root reads settings once and freezes the outcome here.

`telemetry` is one provider or `None`, never a list: exactly one observability
tool is active per run, so evidence ranking across tools never enters the
picture.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kestrel.application.ports import (
    ApprovalGateway,
    LlmClient,
    RemediationExecutor,
    TelemetryProvider,
)
from kestrel.domain.policy import InvestigationScope, RemediationSurface


@dataclass(frozen=True, slots=True)
class CoreDeps:
    """Everything the use cases need, passed in by the composition root."""

    approval: ApprovalGateway
    #: `DRY_RUN`: commands are rendered and audited, never executed.
    dry_run: bool = True
    #: The observability tool this run may read. `None` means none is configured
    #: (or the configured one is missing credentials), and `investigate` records
    #: that it degraded to the incident payload instead of pretending to have
    #: evidence.
    telemetry: TelemetryProvider | None = None
    #: Policy value: what this run may read, frozen at start.
    scope: InvestigationScope = field(default_factory=InvestigationScope)
    #: Policy value: the vocabulary a plan may name, frozen at start. Undeclared
    #: means the planner has no legal commands and degrades instead of inventing.
    surface: RemediationSurface = field(default_factory=RemediationSurface)
    #: `None` means no isolated runner is available, so `execute` fails closed
    #: when the run is not a dry run.
    executor: RemediationExecutor | None = None
    #: `None` means no model is configured (or the seat token is missing), so a
    #: node that needs reasoning records that it degraded instead of failing.
    llm: LlmClient | None = None
