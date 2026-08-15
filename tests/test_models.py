from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.models.travel import BudgetBreakdown, TravelRequest


def test_duration_is_inclusive(sample_request: TravelRequest) -> None:
    # 10 Oct -> 15 Oct is 6 calendar days and 5 nights.
    assert sample_request.duration_days == 6
    assert sample_request.nights == 5


def test_return_date_must_not_precede_departure() -> None:
    with pytest.raises(ValidationError):
        TravelRequest(
            origin="Mumbai",
            destinations=["Tokyo"],
            departure_date=date(2026, 10, 15),
            return_date=date(2026, 10, 10),
            travelers=1,
            budget=100000,
        )


def test_at_least_one_destination_required() -> None:
    with pytest.raises(ValidationError):
        TravelRequest(
            origin="Mumbai",
            destinations=[],
            departure_date=date(2026, 10, 10),
            return_date=date(2026, 10, 15),
            travelers=1,
            budget=100000,
        )


def test_budget_breakdown_totals() -> None:
    breakdown = BudgetBreakdown(
        flights=65000,
        hotels=60000,
        activities=15000,
        restaurants=20000,
        transportation=15000,
    )
    assert breakdown.estimated_total == 175000
