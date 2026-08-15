"""Langfuse observability — one trace per request, a span per agent and tool.

projectIdea.md §15 wants a single trace per `/plan-trip` request with nested
observations for each agent, each tool call, and each LLM call, carrying
latency, tokens, cost and errors.

Two constraints shape this module:

**It must be free when unconfigured.** Without Langfuse keys every helper here
is a no-op context manager. The graph runs identically; nothing is imported
that isn't needed. That keeps the "works with no keys at all" promise intact.

**It must never break a request.** Observability that takes down the thing it
observes is worse than no observability, so every Langfuse call is wrapped: a
tracing failure is logged and swallowed, never raised into the workflow.

Written against the langfuse 4.x API (`start_as_current_observation`), which
differs from the 2.x/3.x `start_span` shape found in older examples.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from app.config import get_settings

logger = logging.getLogger(__name__)

# Langfuse observation types we use. `agent` and `tool` are what make the
# trace readable: you can see the Flight agent's reasoning separately from the
# Amadeus calls it made.
ObservationType = Literal["span", "agent", "tool", "generation", "chain"]

_client: Any = None
_client_ready = False


def new_trace_id() -> str:
    return str(uuid.uuid4())


def get_client() -> Any:
    """The Langfuse client, or None when unconfigured or unavailable.

    Cached, including the "not available" answer, so an unconfigured
    deployment does not retry an import on every span.
    """
    global _client, _client_ready
    if _client_ready:
        return _client

    _client_ready = True
    settings = get_settings()
    if not settings.langfuse_enabled:
        return None

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            environment=settings.environment,
            # Tracing is best-effort; it must not hold a request open.
            timeout=settings.langfuse_timeout_seconds,
        )
    except Exception as exc:  # pragma: no cover - depends on optional install
        logger.warning("Langfuse is configured but unusable, tracing off: %s", exc)
        _client = None
    return _client


def reset_client() -> None:
    """Drop the cached client. For tests and config changes."""
    global _client, _client_ready
    _client = None
    _client_ready = False


@contextmanager
def observe(
    name: str,
    *,
    as_type: ObservationType = "span",
    input: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Record one observation. A no-op when Langfuse is not configured.

    Yields the observation so callers can attach output, or None when tracing
    is off — callers must tolerate None rather than assume a span exists.
    """
    client = get_client()
    if client is None:
        yield None
        return

    # Entered manually rather than with `with`, so that a failure to *open* the
    # span (fall through untraced) stays distinguishable from a failure *inside*
    # it (record it, then re-raise). Nesting them makes the generator try to
    # yield twice on the error path.
    try:
        manager = client.start_as_current_observation(
            name=name, as_type=as_type, input=input, metadata=metadata
        )
        observation = manager.__enter__()
    except Exception as exc:
        # Tracing must never be the reason a request fails.
        logger.warning("Langfuse span %r failed, continuing untraced: %s", name, exc)
        yield None
        return

    try:
        yield observation
    except Exception as exc:
        # Record the failure on the span, then let it propagate: the workflow's
        # own error handling decides what happens next.
        _safely(observation.update, level="ERROR", status_message=str(exc))
        _close(manager, exc)
        raise
    else:
        _close(manager, None)


def _close(manager: Any, exc: BaseException | None) -> None:
    """Exit a Langfuse span, swallowing anything the tracer throws."""
    try:
        if exc is None:
            manager.__exit__(None, None, None)
        else:
            manager.__exit__(type(exc), exc, exc.__traceback__)
    except Exception as close_exc:
        logger.warning("Langfuse span close failed: %s", close_exc)


@contextmanager
def trace(
    name: str,
    *,
    trace_id: str | None = None,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> Iterator[Any]:
    """The root observation for one request, with trace-level attributes."""
    client = get_client()
    if client is None:
        yield None
        return

    try:
        from langfuse import propagate_attributes
    except Exception:  # pragma: no cover - depends on optional install
        propagate_attributes = None

    attributes = {"trace_name": name, "tags": tags or []}
    if metadata:
        attributes["metadata"] = metadata

    try:
        if propagate_attributes is not None:
            with propagate_attributes(**attributes):
                with observe(name, as_type="chain", input=input) as root:
                    yield root
        else:  # pragma: no cover - very old langfuse
            with observe(name, as_type="chain", input=input) as root:
                yield root
    except Exception as exc:
        if isinstance(exc, KeyboardInterrupt):  # pragma: no cover
            raise
        raise
    finally:
        # A request is short-lived; flush so the trace appears promptly rather
        # than waiting for the background batcher.
        _safely(client.flush)


def langchain_handler() -> Any:
    """A Langfuse callback for LangChain, so LLM calls land in the same trace.

    Returns None when unconfigured; callers pass `callbacks=[]` in that case.
    """
    if get_client() is None:
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception as exc:  # pragma: no cover - depends on optional install
        logger.warning("Langfuse LangChain handler unavailable: %s", exc)
        return None


def callbacks() -> list[Any]:
    """LangChain `callbacks=` list — empty when tracing is off."""
    handler = langchain_handler()
    return [handler] if handler is not None else []


def score(
    name: str,
    value: float | str,
    *,
    comment: str | None = None,
    data_type: str | None = None,
) -> None:
    """Attach an evaluation score to the current trace. Never raises."""
    client = get_client()
    if client is None:
        return
    kwargs: dict[str, Any] = {"name": name, "value": value}
    if comment is not None:
        kwargs["comment"] = comment
    if data_type is not None:
        kwargs["data_type"] = data_type
    _safely(client.score_current_trace, **kwargs)


def update_current(**kwargs: Any) -> None:
    """Attach output or metadata to the span in scope. Never raises."""
    client = get_client()
    if client is None:
        return
    _safely(client.update_current_span, **kwargs)


def _safely(call: Any, *args: Any, **kwargs: Any) -> None:
    """Run a Langfuse call, swallowing anything it throws."""
    try:
        call(*args, **kwargs)
    except Exception as exc:
        logger.warning(
            "Langfuse call %s failed: %s", getattr(call, "__name__", call), exc
        )


# Backwards-compatible alias: `span` was the original name of `observe`.
span = observe


__all__ = [
    "callbacks",
    "get_client",
    "langchain_handler",
    "new_trace_id",
    "observe",
    "reset_client",
    "score",
    "span",
    "trace",
    "update_current",
]
