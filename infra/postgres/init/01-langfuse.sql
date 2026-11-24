-- Runs once, on an empty Postgres data volume.
-- Postgres exists only to back Langfuse (observability profile).
-- The agent has no application database: Temporal owns durable state.
CREATE DATABASE langfuse;
