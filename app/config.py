"""Environment-driven configuration.

Every external integration is optional at Milestone 1 so the graph runs, and
the test suite passes, with no keys set at all.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "ai-travel-planner"
    environment: str = "local"
    log_level: str = "INFO"

    # CORS origins for the Next.js frontend (Milestone 7).
    allowed_origins: list[str] = ["http://localhost:3000"]

    # --- LLM (Milestone 2 onwards) --------------------------------------
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5"
    llm_effort: str = "medium"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

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
    langfuse_host: str = "https://cloud.langfuse.com"
    # Bounded so an unreachable Langfuse cannot add seconds to a request.
    # Measured: an unroutable host costs ~3s per request at the SDK default.
    langfuse_timeout_seconds: int = 3

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
