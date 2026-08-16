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

    # --- SerpAPI Google Flights (flight search) --------------------------
    serpapi_api_key: str | None = None
    serpapi_base_url: str = "https://serpapi.com"
    # SerpAPI scrapes Google on demand; searches routinely take 5-15s, so a
    # short timeout fails healthy requests rather than protecting anything.
    serpapi_timeout_seconds: float = 30.0

    # Pricing a round trip needs a second billable search per candidate (see
    # app/tools/serpapi.py), so only this many of the ranked candidates get
    # their return leg resolved — and that is also how many recommendations
    # come back. 0 skips the second call entirely: cheaper, but the results
    # then show the outbound leg only.
    serpapi_return_lookups: int = Field(default=3, ge=0, le=10)

    # Which provider answers flight searches. `auto` prefers SerpAPI when its
    # key is set, since Amadeus's free tier returns test inventory at prices
    # that are not live.
    flight_provider: Literal["auto", "serpapi", "amadeus"] = "auto"

    @property
    def serpapi_enabled(self) -> bool:
        return bool(self.serpapi_api_key)

    @property
    def active_flight_provider(self) -> Literal["serpapi", "amadeus"] | None:
        """Which provider will actually run, or None when none is usable.

        An explicitly-chosen provider is never silently swapped for the other:
        that would make a deployment quietly search somewhere the operator did
        not pick. It reports None instead, and the graph falls back to stubs.
        """
        if self.flight_provider == "serpapi":
            return "serpapi" if self.serpapi_enabled else None
        if self.flight_provider == "amadeus":
            return "amadeus" if self.amadeus_enabled else None
        if self.serpapi_enabled:
            return "serpapi"
        if self.amadeus_enabled:
            return "amadeus"
        return None

    @property
    def flight_search_enabled(self) -> bool:
        return self.active_flight_provider is not None

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
