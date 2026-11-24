"""Temporal client factory.

One place that decides how the agent talks to Temporal, so the API, the worker
and the CLI cannot drift (a mismatched data converter produces payloads the
other side cannot read).
"""

from __future__ import annotations

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from kestrel.config import Settings


async def connect(settings: Settings) -> Client:
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        # The graph state carries pydantic models across activity boundaries.
        data_converter=pydantic_data_converter,
    )
