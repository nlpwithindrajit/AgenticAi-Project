from __future__ import annotations

from datetime import date

import pytest

from app.models.travel import TravelRequest


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
