"""One place where the chat model is built, for every agent.

Centralised because three things must be true everywhere, and were previously
duplicated per agent:

- **The right provider.** OpenAI or Anthropic, chosen by whichever key is set
  (see `Settings.active_llm_provider`). Agents never know which one they got.
- **No `temperature` on Anthropic.** Current Claude models reject that
  parameter outright, so the request must omit it. OpenAI is happy either way,
  so it is omitted for both and the prompts do the steering.
- **Langfuse callbacks attached once**, so every LLM call lands in the same
  trace as the agent span that made it.

Returns None when no key is configured. Every agent has a deterministic
fallback, so an unconfigured LLM is a supported mode rather than an error.
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
    provider = settings.active_llm_provider
    if provider is None:
        return None

    model = settings.active_llm_model
    try:
        if provider == "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=model,
                api_key=settings.openai_api_key,
                max_completion_tokens=2048,
                callbacks=callbacks(),
            )

        from langchain_anthropic import ChatAnthropic

        # No temperature/top_p: current Claude models reject them outright.
        return ChatAnthropic(
            model=model,
            api_key=settings.anthropic_api_key,
            max_tokens=2048,
            callbacks=callbacks(),
        )
    except ImportError as exc:  # pragma: no cover - depends on optional install
        logger.warning(
            "%s is configured but its LangChain package is missing (%s); "
            "agents will use deterministic defaults",
            provider,
            exc,
        )
        return None
    except Exception as exc:  # pragma: no cover - bad key/model surfaces here
        logger.warning(
            "could not build the %s model %r, agents will use defaults: %s",
            provider,
            model,
            exc,
        )
        return None


__all__ = ["build_llm"]
