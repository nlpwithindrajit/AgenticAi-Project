"""The single shared state every LangGraph node reads and writes.

`TravelState` is the backbone of the workflow. Nodes return *partial* dicts;
LangGraph merges them into the running state.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from app.models.travel import (
    Activity,
    BudgetSummary,
    DayPlan,
    DestinationInfo,
    FlightOption,
    HotelOption,
    Restaurant,
    ReviewResult,
    TransportLeg,
    TravelRequest,
    TripRequirements,
)

# Where the budget loop should route when the plan comes in over budget.
CheaperTarget = Literal["flight", "hotel"]


class TravelState(TypedDict, total=False):
    # --- input -----------------------------------------------------------
    request: TravelRequest
    requirements: TripRequirements

    # --- research --------------------------------------------------------
    destination_info: list[DestinationInfo]

    # --- raw search results (straight from the tools) --------------------
    flight_results: list[dict[str, Any]]
    hotel_results: list[dict[str, Any]]
    activity_results: list[dict[str, Any]]
    restaurant_results: list[dict[str, Any]]

    # --- ranked recommendations (agent output) ---------------------------
    flight_recommendations: list[FlightOption]
    hotel_recommendations: list[HotelOption]
    activities: list[Activity]
    restaurants: list[Restaurant]
    transportation_plan: list[TransportLeg]

    # --- synthesis -------------------------------------------------------
    daily_itinerary: list[DayPlan]
    budget: BudgetSummary
    review: ReviewResult

    # --- loop control ----------------------------------------------------
    # The budget loop and the review loop both re-enter earlier nodes. These
    # counters are the only thing standing between us and an infinite graph.
    budget_retries: int
    review_retries: int
    cheaper_target: CheaperTarget

    # What the next pass should do differently. Without these a replan just
    # re-runs the same search and fails the same way until retries run out.
    cost_pressure: float
    """Multiplier applied to search price caps on a retry; 1.0 = no pressure."""
    replan_guidance: list[str]

    # --- diagnostics -----------------------------------------------------
    errors: list[str]
    trace_id: str


# Hard caps for the two agentic loops.
MAX_BUDGET_RETRIES = 2
MAX_REVIEW_RETRIES = 2


def initial_state(request: TravelRequest, trace_id: str | None = None) -> TravelState:
    """Build the starting state for one `/plan-trip` run."""
    state: TravelState = {
        "request": request,
        "flight_results": [],
        "hotel_results": [],
        "activity_results": [],
        "restaurant_results": [],
        "flight_recommendations": [],
        "hotel_recommendations": [],
        "activities": [],
        "restaurants": [],
        "transportation_plan": [],
        "daily_itinerary": [],
        "budget_retries": 0,
        "review_retries": 0,
        "cost_pressure": 1.0,
        "replan_guidance": [],
        "errors": [],
    }
    if trace_id is not None:
        state["trace_id"] = trace_id
    return state
