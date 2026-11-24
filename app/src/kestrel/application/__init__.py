"""Application layer: the incident use cases, their state and their ports.

This is where the reasoning lives. It knows what an incident lifecycle needs
(investigate, plan, ask a human, execute, verify) and it declares those needs
as protocols in `ports.py`. It does not know who provides them.

Kept import-light on purpose: the Temporal workflow imports `state` and
`policy`, and anything heavier here would be pulled into the workflow sandbox.

Import rule (enforced by `tests/test_architecture.py`): `kestrel.domain`
and itself only. No `config`, no adapters, no vendor SDK. `langgraph` appears in
`graph.py` alone, because the flow definition is application logic expressed in
that framework.
"""
