"""One place where the chat model is built, for all seven agents.

Centralised because two things must be true everywhere and were previously
duplicated per agent:

- **No `temperature` / `top_p`.** Current Claude models reject those parameters
  outright, so the request must simply omit them.
- **Langfuse callbacks attached once.** Every LLM call then lands in the same
  trace as the agent span that made it, without each agent remembering to pass
  them.

Returns None when `ANTHROPIC_API_KEY` is unset — every agent has a
deterministic fallback, so an unconfigured LLM is a supported mode, not an
error.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.services.langfuse import callbacks

logger = logging.getLogger(__name__)


def build_llm(existing: Any | None = None) -> Any | None:
    """The chat model, or None when unconfigured. `existing` short-circuits."""
    if existing is not None:
        return existing

    settings = get_settings()
    if not settings.llm_enabled:
        return None

    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:  # pragma: no cover - depends on optional install
        logger.warning("ANTHROPIC_API_KEY is set but langchain-anthropic is missing")
        return None

    try:
        return ChatAnthropic(
            model=settings.llm_model,
            api_key=settings.anthropic_api_key,
            max_tokens=2048,
            callbacks=callbacks(),
        )
    except Exception as exc:  # pragma: no cover - bad key/model surfaces here
        logger.warning(
            "could not build the chat model, agents will use defaults: %s", exc
        )
        return None


__all__ = ["build_llm"]
