"""Runtime configuration: the outermost layer.

Every setting is read from the environment (compose injects it) with a
local-development default, so the module can be imported outside Docker.

Only the composition root (`worker.py`) and the adapters read settings. Nothing
in `domain/` or `application/` does: a use case receives a frozen policy value
in `CoreDeps`, so a run cannot behave differently because someone edited `.env`
between two replays.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from kestrel.domain.policy import (
    DEFAULT_MAX_INVESTIGATION_ATTEMPTS,
    DEFAULT_MAX_REMEDIATION_ATTEMPTS,
    Capability,
    RemediationSurface,
    RunLimits,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "local"
    log_level: str = "INFO"

    # Temporal owns durable execution; this is the only "database" address.
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "incident-workflow"

    # Langfuse reads its own LANGFUSE_* variables. These decide whether the
    # nodes are instrumented at all.
    langfuse_base_url: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None

    # Safety switches. DRY_RUN must stay true until the approval gate and the
    # sandbox policy have been exercised end to end.
    dry_run: bool = True
    approval_mode: Literal["human", "approved"] = "human"

    # Which telemetry adapter reads the incidents. Exactly one is active per
    # run; the adapter registry lives in `adapters/outbound/providers.py`. The
    # credentials below are that provider's own block, so a second tool adds its
    # own names instead of reinterpreting these.
    telemetry_provider: str = "dynatrace"

    # Which LLM adapter reasons for the planning nodes. Exactly one is active
    # per run, chosen by the registry in `adapters/outbound/llm.py` — the same
    # shape as `telemetry_provider`, so the core never names a vendor.
    #
    # `copilot` is the development transport: a developer's own GitHub Copilot
    # seat, authorized once by device OAuth. There is no API key to bill and
    # nothing to paste into `.env`.
    llm_provider: str = "copilot"
    llm_model: str = "github_copilot/gpt-6-luna"
    #: Where the seat token lives. It must match `GITHUB_COPILOT_TOKEN_DIR`,
    #: which compose derives from the same `LLM_TOKEN_DIR` variable: the client
    #: library reads that variable itself, and a mismatch is a silent "please
    #: log in again" on every restart.
    llm_token_dir: str = "/data/copilot"
    #: The gateway path (a company-approved endpoint, open decision §16.7). Not
    #: used by the `copilot` adapter, kept because it is how phase 5 deploys.
    llm_base_url: str | None = None
    llm_api_key: str | None = None

    dynatrace_env_url: str | None = None
    dynatrace_read_token: str | None = None
    dynatrace_write_token: str | None = None
    dynatrace_client_id: str | None = None
    dynatrace_client_secret: str | None = None
    dynatrace_scope_tags: str = ""

    # The platform surface (Grail/DQL): different host, different credential.
    # `https://{env-id}.apps.dynatrace.com` + a platform token (`dt0s16…`, Bearer).
    # Investigation prefers this surface; the classic credentials above remain for
    # anything that still speaks API v2.
    dynatrace_platform_url: str | None = None
    dynatrace_platform_token: str | None = None
    #: Optional narrowing, NOT a requirement. The real environment scope is the
    #: incident's own entity id: `SERVICE-…` exists in exactly one environment, so
    #: the same agent can investigate prod, staging or pre without being pinned to
    #: one. Set this only on a deployment that is deliberately restricted.
    dynatrace_environment: str | None = None

    max_investigation_attempts: int = DEFAULT_MAX_INVESTIGATION_ATTEMPTS
    max_remediation_attempts: int = DEFAULT_MAX_REMEDIATION_ATTEMPTS

    # --- The action space a plan may name -----------------------------------
    # A plan is shell commands, so the model has to know what the environment can
    # reach. Declaring *capabilities* (not verb lists) is what turns "invent a
    # command" into "write a command inside this vocabulary" — and it is what lets
    # one mechanism cover a production restart and a broken build in integration.
    #
    # Format: one capability per `;`, five `|`-separated fields,
    #   id | tool | verbs | must_contain | description
    # with `verbs` and `must_contain` comma-separated. Empty means undeclared,
    # and the planner then degrades instead of guessing: an empty surface is not
    # "anything goes".
    remediation_capabilities: str = ""

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def scope_tags(self) -> list[str]:
        return [tag.strip() for tag in self.dynatrace_scope_tags.split(",") if tag.strip()]

    @property
    def remediation_surface(self) -> RemediationSurface:
        """The declared capabilities as the value a run carries, frozen at start."""
        return RemediationSurface(capabilities=_parse_capabilities(self.remediation_capabilities))

    @property
    def run_limits(self) -> RunLimits:
        """The bounded-attempt limits as the value the run carries.

        One mapping, in the layer that owns the settings, so the API and the CLI
        cannot drift and no use case has to know a field name.
        """
        return RunLimits(
            max_investigation_attempts=self.max_investigation_attempts,
            max_remediation_attempts=self.max_remediation_attempts,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _parse_capabilities(raw: str) -> tuple[Capability, ...]:
    """Parse the capability declaration.

    Deliberately strict and loud: a malformed declaration stops the worker at
    startup, because the alternative is a run that plans nothing and leaves the
    operator reading logs to find out why. Same rule as an unknown telemetry
    provider.
    """
    capabilities: list[Capability] = []
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        fields = [field.strip() for field in entry.split("|", 4)]
        if len(fields) < 2 or not fields[0] or not fields[1]:
            raise ValueError(
                f"REMEDIATION_CAPABILITIES entry {entry!r} must start with `id|tool`"
            )
        capabilities.append(
            Capability(
                id=fields[0],
                tool=fields[1],
                verbs=_csv(fields[2]) if len(fields) > 2 else (),
                must_contain=_csv(fields[3]) if len(fields) > 3 else (),
                description=fields[4] if len(fields) > 4 else "",
            )
        )
    return tuple(capabilities)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())
