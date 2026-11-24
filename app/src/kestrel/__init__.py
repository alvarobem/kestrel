"""kestrel: stateful multi-agent incident triage and remediation."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # The packaging metadata is the single source of truth. `app/pyproject.toml`
    # is read by the build; this reads what the build produced.
    __version__ = version("kestrel")
except PackageNotFoundError:  # running from a source tree with nothing installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
