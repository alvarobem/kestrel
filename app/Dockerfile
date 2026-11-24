# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_HTTP_TIMEOUT=120

WORKDIR /app

# Dependency layer first: only re-resolves when pyproject.toml changes.
# This is a local-development image, so the `dev` extra (pytest, ruff) is
# included. A production build would use `uv sync --no-dev`.
COPY pyproject.toml ./
RUN uv sync --all-extras --no-install-project

COPY src ./src
COPY tests ./tests
RUN uv sync --all-extras

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src

EXPOSE 8000
ENTRYPOINT []
# The API is the default role; agent-worker overrides the command in compose.
CMD ["uvicorn", "kestrel.adapters.inbound.api:app", "--host", "0.0.0.0", "--port", "8000"]
