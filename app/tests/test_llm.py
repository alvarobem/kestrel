"""The LLM transport: a port the core drives, and a seat the operator signs into.

These tests cover the two rules that make a pluggable reasoning transport safe:

  * an absent credential degrades the run and says why, instead of failing the
    worker at boot;
  * building a client never opens a connection. A worker that blocks on OAuth
    never starts, and a test that does it hangs the suite rather than fail it.

They deliberately never ask a model anything. `make llm-check` is the one place
that spends a real request, and it is run by a human with a signed-in seat.
"""

from __future__ import annotations

import inspect
import socket
from pathlib import Path

import pytest

from kestrel.adapters.outbound.llm import (
    CopilotLlmClient,
    UnknownLlmProvider,
    build_llm,
    describe_llm,
    seat_ready,
    sign_in,
    token_path,
)
from kestrel.application.deps import CoreDeps
from kestrel.application.ports import LlmClient
from kestrel.config import Settings


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Settings pointed at a throwaway token directory."""
    return Settings(llm_token_dir=str(tmp_path), **overrides)  # type: ignore[arg-type]


def _sign_in_locally(tmp_path: Path) -> None:
    """Stand in for the device flow: the token file is all the client checks."""
    token_path(_settings(tmp_path)).parent.mkdir(parents=True, exist_ok=True)
    token_path(_settings(tmp_path)).write_text("gho_test_token", encoding="utf-8")


def test_seat_without_a_token_is_not_ready(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    assert seat_ready(settings) is False
    assert build_llm(settings) is None


def test_unconfigured_seat_degrades_and_names_the_command(tmp_path: Path) -> None:
    usable, lines = describe_llm(_settings(tmp_path))

    assert usable is False
    printed = "\n".join(lines)
    assert "copilot" in printed
    assert "make llm-login" in printed


def test_signed_in_seat_builds_a_client_without_a_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sign_in_locally(tmp_path)

    def _no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("building the client opened a socket")

    monkeypatch.setattr(socket, "socket", _no_network)

    settings = _settings(tmp_path, llm_model="github_copilot/gpt-4.1")
    client = build_llm(settings)

    assert isinstance(client, CopilotLlmClient)
    # The port is the only shape the core knows, and the model id is what the
    # audit trail records as having reasoned about the incident.
    assert isinstance(client, LlmClient)
    assert client.model_id == "github_copilot/gpt-4.1"


def test_diagnostic_reports_the_model_and_the_token_path(tmp_path: Path) -> None:
    _sign_in_locally(tmp_path)

    usable, lines = describe_llm(_settings(tmp_path, llm_model="github_copilot/gpt-4.1"))

    assert usable is True
    printed = "\n".join(lines)
    assert "github_copilot/gpt-4.1" in printed
    assert str(token_path(_settings(tmp_path))) in printed


def test_unknown_transport_fails_loudly(tmp_path: Path) -> None:
    # A typo in `.env` must be an error, not a run that reasons with nothing and
    # reports no reason — the same rule the telemetry registry follows.
    with pytest.raises(UnknownLlmProvider):
        build_llm(_settings(tmp_path, llm_provider="not-a-provider"))


def test_only_an_authorizing_transport_can_sign_in(tmp_path: Path) -> None:
    with pytest.raises(UnknownLlmProvider):
        sign_in(_settings(tmp_path, llm_provider="not-a-provider"))


def test_sign_in_is_synchronous_so_it_can_wait_for_a_human() -> None:
    # GitHub's device flow parks until somebody opens a browser, and the client
    # library refuses to start it inside a running event loop. A coroutine here
    # would silently reintroduce exactly that failure.
    assert not inspect.iscoroutinefunction(sign_in)


def test_core_deps_defaults_to_no_model() -> None:
    # Every existing test double keeps working: the LLM is optional, so a run
    # without one is the default, not an error.
    deps = CoreDeps(approval=None)  # type: ignore[arg-type]

    assert deps.llm is None
