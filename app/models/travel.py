"""Pydantic schemas shared by the API layer and the LangGraph workflow.

The frontend collects a structured `TravelRequest` (never free text) and every
agent returns structured objects rather than prose.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

TripStyle = Literal["relaxed", "balanced", "packed"]


class TravelRequest(BaseModel):
    """The structured trip request submitted by the user."""

    origin: str
    destinations: list[str] = Field(min_length=1)

    departure_date: date
    return_date: date

    travelers: int = Field(ge=1)

    budget: float = Field(gt=0)
    currency: str = "INR"

    hotel_stars: int | None = Field(default=None, ge=1, le=5)
    preferred_airline: str | None = None

    direct_flights_only: bool = False

    interests: list[str] = Field(default_factory=list)
    dietary_preferences: list[str] = Field(default_factory=list)

    trip_style: TripStyle = "balanced"

    @model_validator(mode="after")
    def _return_after_departure(self) -> TravelRequest:
        if self.return_date < self.departure_date:
            raise ValueError("return_date must not be before departure_date")
        return self

    @property
    def duration_days(self) -> int:
        """Inclusive trip length in days (a same-day trip is 1 day)."""
        return (self.return_date - self.departure_date).days + 1

    @property
    def nights(self) -> int:
        return (self.return_date - self.departure_date).days


class TripRequirements(BaseModel):
    """Output of the Travel Planner agent — the plan, not the search results."""

    origin: str
    destinations: list[str]
    duration_days: int
    nights: int
    travelers: int
    requirements: dict = Field(default_factory=dict)


class DestinationInfo(BaseModel):
    name: str
    country: str | None = None
    iata_codes: list[str] = Field(default_factory=list)
    currency: str | None = None
    notes: str | None = None


class FlightOption(BaseModel):
    """A ranked flight recommendation. `score` explains why it was chosen."""

    airline: str
    flight_number: str | None = None
    origin: str
    destination: str
    departure: str
    arrival: str
    duration_minutes: int | None = None
    stops: int = 0
    price: float
    currency: str = "INR"
    score: float = 0.0
    rationale: str | None = None


class HotelOption(BaseModel):
    name: str
    destination: str
    check_in: date
    check_out: date
    stars: float | None = None
    rating: float | None = None
    price_per_night: float
    total_price: float
    currency: str = "INR"
    location: str | None = None
    amenities: list[str] = Field(default_factory=list)
    score: float = 0.0
    rationale: str | None = None


class Activity(BaseModel):
    activity: str
    category: str
    destination: str
    duration_hours: float | None = None
    estimated_cost: float = 0.0
    currency: str = "INR"
    location: str | None = None
    recommended_day: int | None = None


class Restaurant(BaseModel):
    name: str
    destination: str
    cuisine: str | None = None
    meal: Literal["breakfast", "lunch", "dinner"] = "dinner"
    price_estimate: float = 0.0
    currency: str = "INR"
    rating: float | None = None
    location: str | None = None
    dietary_tags: list[str] = Field(default_factory=list)
    recommended_day: int | None = None


class TransportLeg(BaseModel):
    day: int
    from_location: str
    to_location: str
    mode: str
    duration_minutes: int | None = None
    estimated_cost: float = 0.0
    currency: str = "INR"


class ItineraryItem(BaseModel):
    time: str
    title: str
    kind: Literal["transit", "activity", "meal", "hotel", "flight", "free"] = "activity"
    location: str | None = None
    notes: str | None = None


class DayPlan(BaseModel):
    day: int
    date: date
    destination: str
    items: list[ItineraryItem] = Field(default_factory=list)


class BudgetBreakdown(BaseModel):
    flights: float = 0.0
    hotels: float = 0.0
    activities: float = 0.0
    restaurants: float = 0.0
    transportation: float = 0.0
    currency: str = "INR"

    @property
    def estimated_total(self) -> float:
        return (
            self.flights
            + self.hotels
            + self.activities
            + self.restaurants
            + self.transportation
        )


class BudgetSummary(BaseModel):
    breakdown: BudgetBreakdown
    estimated_total: float
    budget: float
    remaining: float
    over_budget: bool
    currency: str = "INR"


class ReviewIssue(BaseModel):
    severity: Literal["error", "warning"] = "error"
    check: str
    detail: str


class ReviewResult(BaseModel):
    verdict: Literal["PASS", "FAIL"]
    issues: list[ReviewIssue] = Field(default_factory=list)


class TripPlan(BaseModel):
    """The final response returned by `/plan-trip`."""

    request: TravelRequest
    destination_info: list[DestinationInfo] = Field(default_factory=list)
    flight_recommendations: list[FlightOption] = Field(default_factory=list)
    hotel_recommendations: list[HotelOption] = Field(default_factory=list)
    activities: list[Activity] = Field(default_factory=list)
    restaurants: list[Restaurant] = Field(default_factory=list)
    transportation_plan: list[TransportLeg] = Field(default_factory=list)
    daily_itinerary: list[DayPlan] = Field(default_factory=list)
    budget: BudgetSummary | None = None
    review: ReviewResult | None = None
    errors: list[str] = Field(default_factory=list)
    trace_id: str | None = None
