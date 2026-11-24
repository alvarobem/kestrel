"""Adapters layer: everything that talks to the outside world.

Two directions, and the name says which way the dependency points:

    inbound/   the world drives us: HTTP, operator CLI, the Temporal workflow
               that hosts the graph, and the mapping of a vendor payload into a
               domain entity (the inbound anti-corruption layer).
    outbound/  we drive the world: the Dynatrace HTTP client, the approval
               channel, the tracing exporter, the checkpointer serializer, the
               Temporal connection.

Adapters may import `domain`, `application` and any framework. Nothing inward
may import them, which is what keeps the reasoning testable without a container.
`tests/test_architecture.py` enforces it.
"""
