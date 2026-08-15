"""Langfuse observability.

Milestone 1 provides the seam only: one trace id per `/plan-trip` request, and
a span helper that is a no-op when Langfuse is not configured. Milestone 6
fills in generations, tool calls, token usage and cost — instrument each agent
as it is written, not retroactively.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import get_settings

logger = logging.getLogger(__name__)


def new_trace_id() -> str:
    return str(uuid.uuid4())


def _client():
    """Return a Langfuse client, or None when unconfigured/unavailable."""
    settings = get_settings()
    if not settings.langfuse_enabled:
        return None
    try:
        from langfuse import Langfuse
    except ImportError:  # pragma: no cover - depends on optional install
        logger.warning("Langfuse keys are set but the langfuse package is missing")
        return None
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )


@contextmanager
def span(name: str, **metadata) -> Iterator[None]:
    """Record one agent/tool span. Silently no-ops without Langfuse."""
    client = _client()
    if client is None:
        yield
        return

    observation = client.start_span(name=name, metadata=metadata)
    try:
        yield
    except Exception as exc:  # pragma: no cover - exercised in Milestone 6
        observation.update(level="ERROR", status_message=str(exc))
        raise
    finally:
        observation.end()
        client.flush()
