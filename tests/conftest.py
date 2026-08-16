from __future__ import annotations

import os
from datetime import date

import pytest

from app.config import Settings, get_settings
from app.models.travel import TravelRequest

# Keys that would turn a unit test into a billable network call.
_LIVE_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AMADEUS_CLIENT_ID",
    "AMADEUS_CLIENT_SECRET",
    # SerpAPI bills per search, so a leaked key here would make `make test`
    # cost money on every run, not merely make it slow.
    "SERPAPI_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
)


@pytest.fixture(autouse=True, scope="session")
def _hermetic_environment():
    """Run the suite as if nothing were configured.

    `Settings` loads a developer's `.env`, so the moment a real key is present
    the agents stop using their deterministic fallbacks and start calling a
    provider — which makes `make test` slow, flaky, billable and dependent on
    someone else's uptime. The suite covers the LLM paths with stub models
    instead, which is the only way those assertions can be deterministic.

    Clearing `os.environ` alone is not enough: pydantic-settings reads the
    `.env` *file* as well, so that source is switched off too.
    """
    original_env = {key: os.environ.pop(key, None) for key in _LIVE_KEYS}
    original_file = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original_file
        for key, value in original_env.items():
            if value is not None:
                os.environ[key] = value
        get_settings.cache_clear()


@pytest.fixture
def sample_request() -> TravelRequest:
    """The worked example from projectIdea.md §4."""
    return TravelRequest(
        origin="Mumbai",
        destinations=["Tokyo", "Kyoto"],
        departure_date=date(2026, 10, 10),
        return_date=date(2026, 10, 15),
        travelers=2,
        budget=200000,
        currency="INR",
        hotel_stars=4,
        direct_flights_only=False,
        interests=["food", "culture", "technology"],
        trip_style="balanced",
    )
