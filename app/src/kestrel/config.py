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

    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "gpt-4.1-mini"

    dynatrace_env_url: str | None = None
    dynatrace_read_token: str | None = None
    dynatrace_write_token: str | None = None
    dynatrace_client_id: str | None = None
    dynatrace_client_secret: str | None = None
    dynatrace_scope_tags: str = ""

    max_investigation_attempts: int = DEFAULT_MAX_INVESTIGATION_ATTEMPTS
    max_remediation_attempts: int = DEFAULT_MAX_REMEDIATION_ATTEMPTS

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def dynatrace_configured(self) -> bool:
        has_auth = bool(self.dynatrace_read_token) or bool(
            self.dynatrace_client_id and self.dynatrace_client_secret
        )
        return bool(self.dynatrace_env_url) and has_auth

    @property
    def scope_tags(self) -> list[str]:
        return [tag.strip() for tag in self.dynatrace_scope_tags.split(",") if tag.strip()]

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
