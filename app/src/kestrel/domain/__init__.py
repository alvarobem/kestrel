"""Domain layer: the entities and the rules that do not need a runtime.

Nothing here imports a framework, a client or a setting. That is the point: the
model of an incident, a plan and an approval is the one part of this system
that can be read, tested and reused without Temporal, LangGraph, FastAPI or a
Dynatrace tenant.

Import rule (enforced by `tests/test_architecture.py`): this package may import
the standard library, `pydantic` and itself, and nothing else.
"""
