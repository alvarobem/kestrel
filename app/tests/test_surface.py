"""Surface tests: invariants that span more than one file.

Some rules in `AGENTS.md` are invisible to a unit test because nothing compares
two files. These do: packaging metadata against the imported package, the
compose file against the credential policy, and the state model against the
serializer allowlist.
"""

from __future__ import annotations

import tomllib
import types
from pathlib import Path
from typing import Annotated, get_args, get_origin, get_type_hints

import pytest

import kestrel
import kestrel.domain.models as models
from kestrel.adapters.outbound.checkpoint import STATE_TYPES
from kestrel.application.state import IncidentState

# `app/tests/` in a checkout and `/app/tests/` in the image: parents[1] is the
# application directory in both.
APP_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = APP_DIR.parent / "docker-compose.yml"


def test_package_version_matches_pyproject() -> None:
    """One source of truth for the version: it drifted once already."""
    pyproject = tomllib.loads((APP_DIR / "pyproject.toml").read_text())
    assert kestrel.__version__ == pyproject["project"]["version"]


@pytest.mark.skipif(
    not COMPOSE_FILE.exists(), reason="docker-compose.yml is outside the test image"
)
def test_write_token_is_not_injected_into_agent_containers() -> None:
    """AGENTS.md invariant 7: the write credential never reaches the agent path."""
    text = COMPOSE_FILE.read_text()
    agent_env = text.split("x-agent-env:", 1)[1].split("x-langfuse-env:", 1)[0]
    assert "DYNATRACE_WRITE_TOKEN" not in agent_env


def _reachable_model_names() -> set[str]:
    """Every `kestrel.domain.models` class reachable from an `IncidentState` field."""
    found: set[str] = set()

    def visit(tp: object) -> None:
        if get_origin(tp) is Annotated:
            visit(get_args(tp)[0])
            return
        if get_origin(tp) in (list, types.UnionType):
            for arg in get_args(tp):
                visit(arg)
            return
        if isinstance(tp, type) and tp.__module__ == models.__name__:
            found.add(tp.__name__)

    for annotation in get_type_hints(IncidentState, include_extras=True).values():
        visit(annotation)
    return found


def test_serde_declares_every_type_that_can_enter_state() -> None:
    """AGENTS.md: a new state type must be added to STATE_TYPES or it silently drifts."""
    declared = {name for _, name in STATE_TYPES}
    assert _reachable_model_names() <= declared
