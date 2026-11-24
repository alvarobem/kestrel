"""The dependency rule, enforced over the filesystem.

A hexagonal architecture that only lives in a document is a preference. This
module makes it a build failure: it parses every source file, resolves its
imports, and asserts that no module reaches outward.

    domain/       entities and policy. Nothing but the standard library and
                  pydantic. It must stay importable from anywhere, so it may
                  never depend on anything.
    application/  the use cases, the state contract, the ports, the flow. May
                  import `domain` and itself. Never infrastructure, never the
                  process environment, never a vendor SDK.
    adapters/     inbound (HTTP, CLI, Temporal, payload mapping) and outbound
                  (Dynatrace HTTP, LangGraph approval, tracing, checkpointer).
                  May import inward and any framework.
    config.py     settings, read from the environment. Outermost.
    worker.py     the composition root. Outermost, and nothing imports it.

`langgraph` is allowed in `application/graph.py` only: the flow definition is
application logic, and the framework that expresses it is a declared exception.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kestrel"

#: What a layer may import, by the second segment of `kestrel.<segment>`.
INTERNAL = {
    "domain": {"domain"},
    "application": {"domain", "application"},
    "adapters": {"domain", "application", "adapters", "config"},
}

#: Third-party roots a layer may never import. A vendor SDK in the core is how
#: the architecture rots: the day it changes, the use case changes with it.
FORBIDDEN_THIRD_PARTY = {
    "domain": {"fastapi", "httpx", "langfuse", "pydantic_settings", "uvicorn"},
    "application": {"fastapi", "httpx", "langfuse", "pydantic_settings", "uvicorn"},
}

#: The reasoning framework and the durable-execution SDK appear in exactly one
#: module: the flow definition and the per-step execution policy are written in
#: their vocabulary. Declared here so the exception is visible and narrow.
ASSEMBLY_EXEMPT = Path("application/graph.py")
ASSEMBLY_FRAMEWORKS = {"langgraph", "temporalio"}


def _layer_modules(layer: str) -> list[Path]:
    root = SRC / layer
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.py") if path.name != "__init__.py")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _violations(layer: str) -> list[str]:
    allowed = INTERNAL[layer]
    forbidden = FORBIDDEN_THIRD_PARTY.get(layer, set())
    problems: list[str] = []

    for path in _layer_modules(layer):
        relative = path.relative_to(SRC)
        for module in _imports(path):
            root = module.split(".")[0]
            if root == "kestrel":
                segments = module.split(".")
                inner = segments[1] if len(segments) > 1 else ""
                if inner not in allowed:
                    problems.append(f"{relative} imports {module} (layer {layer})")
                continue
            if root in ASSEMBLY_FRAMEWORKS and layer in {"domain", "application"}:
                if layer == "application" and relative == ASSEMBLY_EXEMPT:
                    continue
                problems.append(f"{relative} imports {module} (only {ASSEMBLY_EXEMPT} may)")
                continue
            if root in forbidden:
                problems.append(f"{relative} imports {module} (framework in {layer})")

    return problems


@pytest.mark.parametrize("layer", ["domain", "application", "adapters"])
def test_layer_exists_with_a_package_marker(layer: str) -> None:
    """A layer that is not a package is not a boundary."""
    root = SRC / layer
    assert root.is_dir(), f"missing layer directory: {root}"
    assert (root / "__init__.py").is_file(), f"{root} must be a package"


@pytest.mark.parametrize("layer", ["domain", "application", "adapters"])
def test_imports_point_inward_only(layer: str) -> None:
    assert _violations(layer) == []


def test_domain_has_no_layer_above_it() -> None:
    """The whole point of the innermost layer: nothing above it to depend on."""
    for path in _layer_modules("domain"):
        outside = {
            module
            for module in _imports(path)
            if module.startswith("kestrel.") and not module.startswith("kestrel.domain")
        }
        assert outside == set(), f"{path} imports outside the domain layer"


def test_the_core_declares_its_ports() -> None:
    """The capabilities the core needs are named by the core, not by an adapter."""
    source = (SRC / "application" / "ports.py").read_text(encoding="utf-8")
    assert "class ApprovalGateway(Protocol)" in source
    assert "class RemediationExecutor(Protocol)" in source
    assert "class TelemetryProvider(Protocol)" in source


def test_the_core_does_not_speak_to_the_framework() -> None:
    """`interrupt()` is an adapter detail; the approval use case must not see it."""
    offenders = [
        path.relative_to(SRC)
        for path in [*_layer_modules("application"), *_layer_modules("domain")]
        for module in _imports(path)
        if module.startswith("langgraph.types") or module.startswith("langgraph.checkpoint")
    ]
    assert offenders == []


def test_composition_root_is_outermost() -> None:
    """`worker` wires everything; nothing may import it back."""
    for layer in ("domain", "application", "adapters"):
        for path in _layer_modules(layer):
            assert "kestrel.worker" not in _imports(path), (
                f"{path} imports the composition root"
            )


def test_a_driving_adapter_does_not_import_a_driven_adapter() -> None:
    """`api` talks to the domain and the application, not to a vendor adapter."""
    forbidden = {
        "kestrel.adapters.outbound.dynatrace",
        "kestrel.adapters.outbound.dynatrace_provider",
        "kestrel.adapters.outbound.providers",
        "kestrel.adapters.outbound.tracing",
        "kestrel.adapters.outbound.approval",
        "kestrel.adapters.outbound.checkpoint",
    }
    assert _imports(SRC / "adapters" / "inbound" / "api.py") & forbidden == set()


@pytest.mark.parametrize("adapter", ["api.py", "cli.py"])
def test_the_surfaces_do_not_speak_to_the_engine(adapter: str) -> None:
    """Starting a run is a use case; the SDK stays behind `RunGateway`.

    If `api.py` or `cli.py` ever imports `temporalio` again, the run rules have
    started leaking back into a driving adapter.
    """
    imports = _imports(SRC / "adapters" / "inbound" / adapter)
    assert [module for module in imports if module.startswith("temporalio")] == []


def test_the_control_plane_is_an_input_port() -> None:
    """A driving adapter reaches the rules through `application/incidents.py`."""
    source = (SRC / "adapters" / "inbound" / "api.py").read_text(encoding="utf-8")
    assert "from kestrel.application.incidents import" in source
    for use_case in ("submit_incident", "inspect_incident", "decide_incident"):
        assert use_case in source, f"api.py must drive the {use_case} use case"


#: Vendor names the core must never learn. This is the test behind the telemetry
#: port: connecting another tool is an adapter change, so a vendor noun reaching
#: `domain/` or `application/` means the vocabulary already leaked.
VENDORS = ("dynatrace", "datadog", "cloudwatch", "grafana", "newrelic", "splunk", "elastic")


def _vendor_mentions_in_code(path: Path) -> list[str]:
    """Vendor nouns in identifiers and non-docstring strings.

    A docstring or a comment may explain the design with a concrete tool name;
    *code* may not branch on one, and that is what this catches.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    texts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                texts.append(node.value)
        elif isinstance(node, ast.Name):
            texts.append(node.id)
        elif isinstance(node, ast.arg):
            texts.append(node.arg)
        elif isinstance(node, ast.Attribute):
            texts.append(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            texts.append(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            texts.append(node.name)

    lowered = " ".join(texts).lower()
    return [vendor for vendor in VENDORS if vendor in lowered]


@pytest.mark.parametrize("layer", ["domain", "application"])
def test_the_core_names_no_telemetry_vendor(layer: str) -> None:
    offenders: dict[str, list[str]] = {}
    for path in _layer_modules(layer):
        hits = _vendor_mentions_in_code(path)
        if hits:
            offenders[str(path.relative_to(SRC))] = hits

    assert offenders == {}
