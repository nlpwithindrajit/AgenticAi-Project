"""Environment-driven configuration.

Every external integration is optional at Milestone 1 so the graph runs, and
the test suite passes, with no keys set at all.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "ai-travel-planner"
    environment: str = "local"
    log_level: str = "INFO"

    # CORS origins for the Next.js frontend (Milestone 7).
    # Browser origins allowed to call this API. A missing origin is not a
    # gentle degradation: the browser blocks the request outright and the UI
    # simply cannot talk to the API, so the deployed UI's URL MUST be listed
    # here in production. Both loopback spellings are included because
    # localhost and 127.0.0.1 are distinct origins to a browser.
    allowed_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    @property
    def cors_is_default(self) -> bool:
        """True when nothing was configured — worth warning about off-local."""
        return all("localhost" in o or "127.0.0.1" in o for o in self.allowed_origins)

    # --- LLM ------------------------------------------------------------
    # Either provider drives the agents. `auto` picks whichever key is
    # present, preferring OpenAI when both are, so setting one key is all a
    # deployment needs to do.
    llm_provider: Literal["auto", "openai", "anthropic"] = "auto"

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5"

    # Kept for compatibility with existing .env files that set LLM_MODEL.
    # When set it overrides the chosen provider's model.
    llm_model: str | None = None
    llm_effort: str = "medium"

    @property
    def active_llm_provider(self) -> str | None:
        """Which provider will actually be used, or None when no key is set."""
        if self.llm_provider == "openai":
            return "openai" if self.openai_api_key else None
        if self.llm_provider == "anthropic":
            return "anthropic" if self.anthropic_api_key else None
        if self.openai_api_key:
            return "openai"
        if self.anthropic_api_key:
            return "anthropic"
        return None

    @property
    def active_llm_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        return (
            self.openai_model
            if self.active_llm_provider == "openai"
            else self.anthropic_model
        )

    @property
    def llm_enabled(self) -> bool:
        return self.active_llm_provider is not None

    # --- Amadeus Self-Service (flights, Milestone 2) --------------------
    # Test env has limited inventory; switch the base URL for production.
    amadeus_client_id: str | None = None
    amadeus_client_secret: str | None = None
    amadeus_base_url: str = "https://test.api.amadeus.com"
    amadeus_timeout_seconds: float = 20.0

    @property
    def amadeus_enabled(self) -> bool:
        return bool(self.amadeus_client_id and self.amadeus_client_secret)

    # --- Travel APIs (Milestones 3-4) -----------------------------------
    hotel_api_key: str | None = None
    places_api_key: str | None = None
    maps_api_key: str | None = None

    # --- Langfuse (Milestone 6) -----------------------------------------
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    # Accept LANGFUSE_BASE_URL too: it is what the Langfuse dashboard shows,
    # and a host set under that name would otherwise be silently ignored while
    # traces went to the wrong region.
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_HOST", "LANGFUSE_BASE_URL"),
    )
    # Bounded so an unreachable Langfuse cannot add seconds to a request.
    # Measured: an unroutable host costs ~3s per request at the SDK default.
    langfuse_timeout_seconds: int = 3

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
