"""What a use case is allowed to know about the world.

`CoreDeps` is the seam the application uses to stay framework-free. It carries
two different things and that is deliberate:

  * ports — the capabilities the core drives (`approval`, `executor`);
  * policy values — decisions already taken somewhere else, frozen here
    (`dry_run`, `dynatrace_configured`).

Policy values are values, not lookups: a node that reads `os.environ` is
non-reproducible, and a router that does it diverges from Temporal's replay.
The composition root reads settings once and freezes the outcome here.
"""

from __future__ import annotations

from dataclasses import dataclass

from kestrel.application.ports import ApprovalGateway, RemediationExecutor


@dataclass(frozen=True, slots=True)
class CoreDeps:
    """Everything the use cases need, passed in by the composition root."""

    approval: ApprovalGateway
    #: `DRY_RUN`: commands are rendered and audited, never executed.
    dry_run: bool = True
    #: Whether a read-only telemetry source exists for this run. Until the
    #: investigation port lands (Roadmap Phase 1) this is a capability flag,
    #: not a client: the node reports what it could not gather.
    dynatrace_configured: bool = False
    #: `None` means no isolated runner is available, so `execute` fails closed
    #: when the run is not a dry run.
    executor: RemediationExecutor | None = None
