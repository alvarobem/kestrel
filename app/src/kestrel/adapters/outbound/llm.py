"""The LLM registry, and the Copilot adapter behind it.

`LLM_PROVIDER` picks one entry here, mirroring `providers.py` for telemetry: the
core declares `LlmClient`, this module decides what answers it.

Why a GitHub Copilot seat: phase 2 needs real reasoning and the development loop
must not spend paid API credits. A developer's seat is authorized by device OAuth
— one `make llm-login`, then the token is reused from a volume and there is no
API key anywhere. That is a *development* transport and it is honest about it: a
seat belongs to a person, so a deployment bills an organization instead, through
a GitHub App installation holding the `organization_copilot_requests` permission.
That is a different adapter, not a different value in `.env`.

Two rules this module keeps, both learned the hard way elsewhere:

  * building a client never touches the network. A worker that blocks on OAuth at
    boot is a worker that never starts, and a test that does it hangs;
  * the SDK is imported lazily. `import litellm` costs seconds, the worker imports
    this module on every start, and most runs never ask for a model.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kestrel.application.errors import LlmUnavailable
from kestrel.application.ports import LlmClient
from kestrel.config import Settings

#: Token file name inside `llm_token_dir`. The name is the client library's own:
#: it writes and reads that file, we only decide which directory it lives in.
TOKEN_FILE = "access-token"

#: Environment variable the client library resolves the token directory from.
TOKEN_DIR_ENV = "GITHUB_COPILOT_TOKEN_DIR"


def token_path(settings: Settings) -> Path:
    """Where the seat token is expected. Pure path arithmetic, no I/O."""
    return Path(settings.llm_token_dir).expanduser() / TOKEN_FILE


def seat_ready(settings: Settings) -> bool:
    """Whether this machine holds a seat token. One `stat`, no network."""
    return token_path(settings).is_file()


class CopilotLlmClient:
    """Answers a prompt with the developer's own GitHub Copilot seat."""

    provider_id = "copilot"

    def __init__(self, settings: Settings) -> None:
        self.model_id = settings.llm_model
        self._token_dir = str(Path(settings.llm_token_dir).expanduser())

    async def complete(self, *, system: str, user: str) -> str:
        # The library resolves its token cache from the environment, not from an
        # argument, so the frozen setting is written back here: a container whose
        # HOME differs must not look for the token somewhere else and report a
        # spurious "please log in again".
        os.environ[TOKEN_DIR_ENV] = self._token_dir
        litellm = _sdk(self.model_id)
        try:
            response = await litellm.acompletion(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                timeout=120,
            )
        except Exception as exc:  # the SDK's exception tree stops at this boundary
            raise LlmUnavailable(self.model_id, _one_line(exc)) from exc
        return _answer_text(response, self.model_id)


def _sdk(model_id: str) -> Any:
    """Import the client library on demand, translating a broken install."""
    try:
        import litellm
    except ImportError as exc:  # pragma: no cover - packaging error, not a run
        raise LlmUnavailable(model_id, "the LLM client library is not installed") from exc
    # Otherwise its cost-map banner, per-request chatter and atexit flush lines
    # land in the middle of an operator diagnostic. Errors still surface: they
    # come back as exceptions, not as log lines.
    litellm.suppress_debug_info = True
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    return litellm


def _one_line(exc: Exception) -> str:
    """An exception message fit for one log line and one audit record."""
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _answer_text(response: Any, model_id: str) -> str:
    """The answer, or a typed failure. An empty answer is a failure, not a plan."""
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError) as exc:
        raise LlmUnavailable(model_id, "the provider returned an unexpected response") from exc
    if not isinstance(content, str) or not content.strip():
        raise LlmUnavailable(model_id, "the model returned an empty answer")
    return content


@dataclass(frozen=True, slots=True)
class LlmSpec:
    """Everything the composition root needs to know about one LLM transport."""

    #: `None` when the transport is selected but not authorized yet.
    build: Callable[[Settings], LlmClient | None]
    #: `(usable, lines)`: whether reasoning is available at all, and what to print.
    describe: Callable[[Settings], tuple[bool, list[str]]]


def _build_copilot(settings: Settings) -> LlmClient | None:
    """`None` when the seat is not signed in: the run degrades, it does not fail."""
    if not seat_ready(settings):
        return None
    return CopilotLlmClient(settings)


def _describe_copilot(settings: Settings) -> tuple[bool, list[str]]:
    ready = seat_ready(settings)
    path = token_path(settings)
    state = (
        f"present at {path}"
        if ready
        else f"missing at {path} — run `make llm-login` once"
    )
    return ready, [
        "llm provider: copilot (personal seat, device OAuth)",
        f"llm model:    {settings.llm_model}",
        f"seat token:   {state}",
    ]


LLM_PROVIDERS: dict[str, LlmSpec] = {
    "copilot": LlmSpec(build=_build_copilot, describe=_describe_copilot),
}


class UnknownLlmProvider(RuntimeError):
    """`LLM_PROVIDER` names a transport that has no adapter."""

    def __init__(self, provider_id: str, known: list[str]) -> None:
        super().__init__(
            f"unknown llm provider {provider_id!r} (configured adapters: {', '.join(known)})"
        )
        self.provider_id = provider_id


def llm_spec(settings: Settings) -> LlmSpec:
    provider_id = settings.llm_provider.strip().lower()
    spec = LLM_PROVIDERS.get(provider_id)
    if spec is None:
        raise UnknownLlmProvider(provider_id, sorted(LLM_PROVIDERS))
    return spec


def build_llm(settings: Settings) -> LlmClient | None:
    """The active LLM client for this run, or `None` when it is not authorized."""
    return llm_spec(settings).build(settings)


def describe_llm(settings: Settings) -> tuple[bool, list[str]]:
    """Operator diagnostic for the active LLM transport: `(usable, lines)`."""
    return llm_spec(settings).describe(settings)


#: Prompt the diagnostics send. Short on purpose: it measures reachability and
#: authorization, not reasoning quality — that is what the phase 4 evals are for.
DIAGNOSTIC_PROMPT = "Reply with exactly: kestrel llm ok"


def _sign_in_copilot(settings: Settings) -> str:
    """Run GitHub's device flow and store the seat token. Blocking on purpose.

    This must not run inside an event loop. The client library refuses to, and it
    is right: the flow waits for a human to open a browser, and parked inside an
    activity that would stall every other run on the worker. Signing in is a
    command an operator runs once; the worker only ever reads the token file.

    Returns the token so the caller can say where it went, and proves the seat
    answers by running `llm-check` straight after.
    """
    target = token_path(settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.environ[TOKEN_DIR_ENV] = str(target.parent)

    try:
        # The same class the library uses internally; it prints the device code,
        # polls GitHub and writes the token file we named above.
        from litellm.llms.github_copilot.authenticator import Authenticator

        token = Authenticator().get_access_token()
    except Exception as exc:  # the SDK's exception tree stops at this boundary
        raise LlmUnavailable(settings.llm_model, _one_line(exc)) from exc
    if not token:
        raise LlmUnavailable(settings.llm_model, "GitHub returned no seat token")
    return token


#: Transports that need an interactive authorization step, and how to run it.
SIGN_IN: dict[str, Callable[[Settings], str]] = {
    "copilot": _sign_in_copilot,
}


def sign_in(settings: Settings) -> str:
    """Authorize the active transport once, for this machine."""
    provider_id = settings.llm_provider.strip().lower()
    step = SIGN_IN.get(provider_id)
    if step is None:
        raise UnknownLlmProvider(provider_id, sorted(SIGN_IN))
    return step(settings)