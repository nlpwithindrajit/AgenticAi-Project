# The FastAPI backend, for Amazon ECR -> AWS App Runner.
#
# Two stages so the runtime image carries no build tooling. Dependencies are
# installed from uv.lock with --frozen, so a deploy can never quietly resolve a
# different version than the one the tests ran against.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lock alone, so this layer is cached until the
# lock actually changes — application edits do not trigger a reinstall.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app ./app
RUN uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

# App Runner health checks and Amadeus both need TLS roots.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/app /app/app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production \
    PORT=8000

USER appuser
EXPOSE 8000

# App Runner probes this; failing it is how a bad deploy gets rolled back.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Single worker: App Runner scales by adding instances, and the LangGraph
# workflow holds no cross-request state that a second worker could share.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
