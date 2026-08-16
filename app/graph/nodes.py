"""LangGraph node implementations.

Each node is a thin adapter: it pulls what it needs out of `TravelState`, hands
it to an agent in `app/agents/`, and writes the result back. The reasoning
lives in the agents; the API work lives in `app/tools/`.

Every node is wrapped in a Langfuse observation by `@traced`, so a trace
mirrors the graph exactly — repeats included, which is what makes a replan loop
legible after the fact.

Search nodes fall back to clearly-labelled `STUB` inventory when Amadeus is not
configured, and record why in `state["errors"]`. Anything user-visible produced
by a stub is prefixed `STUB` on purpose: nothing here should ever be mistaken
for a real search result.
"""

from __future__ import annotations

import logging
from functools import wraps

from app.agents.activity import ActivityAgent
from app.agents.budget import BudgetAgent
from app.agents.flight import FlightAgent
from app.agents.hotel import HotelAgent, split_stay
from app.agents.itinerary import ItineraryAgent
from app.agents.restaurant import RestaurantAgent
from app.agents.reviewer import ReviewAgent, guidance_from
from app.config import get_settings
from app.graph.state import (
    MAX_BUDGET_RETRIES,
    MAX_REVIEW_RETRIES,
    TravelState,
)
from app.models.travel import (
    Activity,
    DestinationInfo,
    FlightOption,
    FlightSegment,
    FlightSlice,
    HotelOption,
    Restaurant,
    TransportLeg,
    TripRequirements,
)
from app.services.langfuse import observe, update_current
from app.tools.amadeus import (
    FlightSearchError,
    HotelSearchError,
    PlacesSearchError,
)
from app.tools.flights import rank_flights
from app.tools.hotels import detect_amenities, rank_hotels
from app.tools.places import estimate_meal_cost

logger = logging.getLogger(__name__)


def traced(name: str, as_type: str = "agent"):
    """Wrap a graph node in a Langfuse observation.

    Applied at the node level rather than inside each agent so the trace
    mirrors the graph exactly — including the repeats a loop produces, which
    is precisely what you want to see when diagnosing a replan.
    """

    def decorate(node):
        @wraps(node)
        def wrapper(state: TravelState) -> TravelState:
            with observe(name, as_type=as_type) as span:
                update = node(state)
                if span is not None:
                    update_current(output=_node_summary(update))
                return update

        return wrapper

    return decorate


def _node_summary(update: TravelState) -> dict[str, object]:
    """What a node produced, small enough to read in a trace."""
    summary: dict[str, object] = {}
    for key, value in (update or {}).items():
        if key == "errors":
            continue
        if isinstance(value, list):
            summary[key] = len(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        else:
            summary[key] = type(value).__name__
    return summary


# Share of the user's budget each category is assumed to consume in the stub
# costing model. These deliberately sum to >1.0 so a default request trips the
# budget loop once and then converges — the loop is the point of the project.
_STUB_BUDGET_SHARE = {
    "flights": 0.42,
    "hotels": 0.38,
    "activities": 0.10,
    "restaurants": 0.12,
    "transportation": 0.08,
}

def _destinations_by_day(state: TravelState) -> list[str]:
    """Spread the requested destinations across the trip, one entry per day."""
    request = state["request"]
    days = request.duration_days
    destinations = request.destinations
    per_destination = max(1, days // len(destinations))

    schedule: list[str] = []
    for destination in destinations:
        schedule.extend([destination] * per_destination)
    # Pad or trim to exactly `days` entries.
    while len(schedule) < days:
        schedule.append(destinations[-1])
    return schedule[:days]


# ---------------------------------------------------------------------------
# Agent nodes
# ---------------------------------------------------------------------------


@traced("planner", "agent")
def planner_node(state: TravelState) -> TravelState:
    """Travel Planner — turns the raw request into plan requirements.

    It does not search for anything; it only decides what needs to be found.
    """
    request = state["request"]
    requirements = TripRequirements(
        origin=request.origin,
        destinations=request.destinations,
        duration_days=request.duration_days,
        nights=request.nights,
        travelers=request.travelers,
        requirements={
            "budget": request.budget,
            "currency": request.currency,
            "hotel_stars": request.hotel_stars,
            "preferred_airline": request.preferred_airline,
            "direct_flights_only": request.direct_flights_only,
            "interests": request.interests,
            "dietary_preferences": request.dietary_preferences,
            "trip_style": request.trip_style,
        },
    )
    return {"requirements": requirements}


@traced("destination", "agent")
def destination_node(state: TravelState) -> TravelState:
    """Destination agent — enriches each destination with context."""
    request = state["request"]
    info = [
        DestinationInfo(name=name, notes="STUB destination context")
        for name in request.destinations
    ]
    return {"destination_info": info}


def _stub_flight_offers(state: TravelState) -> list[FlightOption]:
    """Placeholder inventory, used only when Amadeus is not configured.

    Returns three alternatives so the graph exercises the same "pick the top
    ranked offer" path it takes with real data.
    """
    request = state["request"]
    # Same signal the real agent uses, so the budget loop behaves the same way
    # with and without credentials.
    pressure = max(state.get("cost_pressure", 1.0), 0.3)
    best_price = request.budget * _STUB_BUDGET_SHARE["flights"] * pressure
    destination = request.destinations[0]

    def _slice(
        origin: str, dest: str, day: str, stops: int, minutes: int
    ) -> FlightSlice:
        # stops connections means stops + 1 segments.
        return FlightSlice(
            origin=origin,
            destination=dest,
            departure_at=f"{day}T09:00:00",
            arrival_at=f"{day}T18:00:00",
            duration_minutes=minutes,
            segments=[
                FlightSegment(
                    carrier_code="ZZ",
                    carrier_name="STUB Airways",
                    flight_number=f"ZZ{100 + leg}",
                    origin=origin,
                    destination=dest,
                    departure_at=f"{day}T09:00:00",
                    arrival_at=f"{day}T18:00:00",
                    duration_minutes=minutes // (stops + 1),
                )
                for leg in range(stops + 1)
            ],
        )

    offers: list[FlightOption] = []
    for index in range(3):
        # Cheapest option is non-stop; alternatives add connections and time,
        # so the ranker has something real to discriminate on.
        stops = index
        minutes = 540 + index * 90
        price = round(best_price * (1 + 0.12 * index), 2)
        offers.append(
            FlightOption(
                offer_id=f"stub-{index}",
                airline="ZZ",
                airline_name="STUB Airways",
                outbound=_slice(
                    request.origin,
                    destination,
                    request.departure_date.isoformat(),
                    stops,
                    minutes,
                ),
                inbound=_slice(
                    destination,
                    request.origin,
                    request.return_date.isoformat(),
                    stops,
                    minutes,
                ),
                price=price,
                price_per_traveler=round(price / request.travelers, 2),
                currency=request.currency,
                source="stub",
            )
        )

    return offers


@traced("flight-agent", "agent")
def flight_node(state: TravelState) -> TravelState:
    """Flight agent — search -> filter -> rank -> recommend (never book).

    Uses SerpAPI (Google Flights) when its key is configured, or Amadeus when
    `FLIGHT_PROVIDER` selects it. With neither, or when the provider errors, it
    falls back to clearly-labelled STUB inventory and records why in
    `state["errors"]` — the graph keeps running, but nothing silently passes
    fake flights off as a real search.
    """
    request = state["request"]
    errors = list(state.get("errors", []))
    settings = get_settings()

    if settings.flight_search_enabled:
        try:
            result = FlightAgent().run(
                request, cost_pressure=state.get("cost_pressure", 1.0)
            )
            errors.extend(result.notes)
            if result.recommendations:
                return {
                    "flight_results": [
                        f.model_dump() for f in result.recommendations
                    ],
                    "flight_recommendations": result.recommendations,
                    "errors": errors,
                }
            errors.append("flight search returned no offers; using STUB inventory")
        except FlightSearchError as exc:
            errors.append(f"flight search failed ({exc}); using STUB inventory")
        except Exception as exc:  # pragma: no cover - unexpected provider fault
            logger.exception("unexpected flight search failure")
            errors.append(f"flight search error ({exc}); using STUB inventory")
    else:
        errors.append(
            "no flight provider configured (set SERPAPI_API_KEY); "
            "using STUB flight inventory"
        )

    ranked = rank_flights(
        _stub_flight_offers(state),
        preferred_airline=request.preferred_airline,
        top_n=3,
    )
    return {
        "flight_results": [f.model_dump() for f in ranked],
        "flight_recommendations": ranked,
        "errors": errors,
    }


_STUB_ROOM_DESCRIPTION = "STUB ROOM\nFREE WIFI\nBREAKFAST INCLUDED"


def _stub_hotel_offers(state: TravelState) -> list[HotelOption]:
    """Placeholder inventory, used only when Amadeus is not configured.

    Returns three alternatives per destination so the graph exercises the same
    "pick the best per destination" path it takes with real data.
    """
    request = state["request"]
    pressure = max(state.get("cost_pressure", 1.0), 0.3)
    total = request.budget * _STUB_BUDGET_SHARE["hotels"] * pressure
    best_per_destination = total / len(request.destinations)

    hotels: list[HotelOption] = []
    for destination, check_in, check_out in split_stay(
        request.destinations, request.departure_date, request.return_date
    ):
        nights = max((check_out - check_in).days, 1)
        for index in range(3):
            price = round(best_per_destination * (1 + 0.15 * index), 2)
            hotels.append(
                HotelOption(
                    hotel_id=f"STUB{destination[:3].upper()}{index}",
                    name=f"STUB Hotel {destination} #{index + 1}",
                    destination=destination,
                    check_in=check_in,
                    check_out=check_out,
                    nights=nights,
                    price_per_night=round(price / nights, 2),
                    total_price=price,
                    currency=request.currency,
                    distance_km=1.0 + index * 2.5,
                    stars=float(request.hotel_stars) if request.hotel_stars else None,
                    rating=88.0 - index * 9,
                    room_type="STANDARD_ROOM",
                    room_description=_STUB_ROOM_DESCRIPTION,
                    # Derived the same way the real path derives them, so the
                    # amenities score is exercised rather than silently zero.
                    amenities=detect_amenities(_STUB_ROOM_DESCRIPTION),
                    source="stub",
                )
            )

    return hotels


@traced("hotel-agent", "agent")
def hotel_node(state: TravelState) -> TravelState:
    """Hotel agent — search -> filter -> rank -> recommend (never book).

    Ranking weights: price 30%, location 25%, rating 20%, amenities 15%,
    traveller preferences 10% — renormalised per hotel over the factors
    Amadeus actually returned data for.

    Uses Amadeus when configured; otherwise falls back to clearly-labelled
    STUB inventory and records why in `state["errors"]`.
    """
    request = state["request"]
    errors = list(state.get("errors", []))
    settings = get_settings()

    if settings.amadeus_enabled:
        try:
            result = HotelAgent().run(
                request, cost_pressure=state.get("cost_pressure", 1.0)
            )
            errors.extend(result.notes)
            if result.recommendations:
                return {
                    "hotel_results": [h.model_dump() for h in result.recommendations],
                    "hotel_recommendations": result.recommendations,
                    "errors": errors,
                }
            errors.append("hotel search returned no offers; using STUB inventory")
        except HotelSearchError as exc:
            errors.append(f"hotel search failed ({exc}); using STUB inventory")
        except Exception as exc:  # pragma: no cover - unexpected provider fault
            logger.exception("unexpected hotel search failure")
            errors.append(f"hotel search error ({exc}); using STUB inventory")
    else:
        errors.append("Amadeus not configured; using STUB hotel inventory")

    stubs = _stub_hotel_offers(state)
    ranked: list[HotelOption] = []
    for destination in request.destinations:
        ranked.extend(
            rank_hotels(
                [h for h in stubs if h.destination == destination],
                interests=request.interests,
                requested_stars=request.hotel_stars,
                top_n=3,
            )
        )

    return {
        "hotel_results": [h.model_dump() for h in ranked],
        "hotel_recommendations": ranked,
        "errors": errors,
    }


def _days_by_destination(state: TravelState) -> dict[str, list[int]]:
    """{destination: [day numbers spent there]}, in itinerary order."""
    mapping: dict[str, list[int]] = {}
    for index, destination in enumerate(_destinations_by_day(state)):
        mapping.setdefault(destination, []).append(index + 1)
    return mapping


def _stub_activities(state: TravelState) -> list[Activity]:
    """Placeholder inventory, used only when Amadeus is not configured."""
    request = state["request"]
    budget_share = request.budget * _STUB_BUDGET_SHARE["activities"]
    schedule = _destinations_by_day(state)
    per_activity = budget_share / max(len(schedule), 1)

    return [
        Activity(
            activity_id=f"stub-act-{day}",
            activity=f"STUB attraction {day} in {destination}",
            category="activity",
            destination=destination,
            description=f"STUB placeholder activity in {destination} for day {day}",
            duration_hours=2.0,
            estimated_cost=round(per_activity, 2),
            cost_is_estimated=True,
            currency=request.currency,
            rating=4.2,
            recommended_day=day,
            source="stub",
        )
        for day, destination in enumerate(schedule, start=1)
    ]


def _stub_restaurants(state: TravelState) -> list[Restaurant]:
    """Placeholder inventory, used only when Amadeus is not configured."""
    request = state["request"]
    meal_cost, basis = estimate_meal_cost(
        request.budget,
        request.travelers,
        request.duration_days,
        request.trip_style,
    )
    schedule = _destinations_by_day(state)

    return [
        Restaurant(
            place_id=f"stub-rest-{day}",
            name=f"STUB restaurant {day} in {destination}",
            destination=destination,
            meal="dinner",
            price_estimate=meal_cost,
            price_is_estimated=True,
            estimate_basis=basis,
            currency=request.currency,
            dietary_tags=list(request.dietary_preferences),
            recommended_day=day,
            source="stub",
        )
        for day, destination in enumerate(schedule, start=1)
    ]


@traced("activity-agent", "agent")
def activity_node(state: TravelState) -> TravelState:
    """Activity agent — draws candidates from a real places search.

    Writes a *schedule* (one activity per day) to `activities`, and the full
    ranked candidate set to `activity_results`. The Budget agent sums the
    schedule, so these must never be alternatives.
    """
    request = state["request"]
    errors = list(state.get("errors", []))
    settings = get_settings()

    if settings.amadeus_enabled:
        try:
            result = ActivityAgent().run(
                request,
                days_by_destination=_days_by_destination(state),
                hotels=state.get("hotel_recommendations"),
            )
            errors.extend(result.notes)
            if result.scheduled:
                return {
                    "activity_results": [a.model_dump() for a in result.candidates],
                    "activities": result.scheduled,
                    "errors": errors,
                }
            errors.append("activity search returned nothing; using STUB inventory")
        except PlacesSearchError as exc:
            errors.append(f"activity search failed ({exc}); using STUB inventory")
        except Exception as exc:  # pragma: no cover - unexpected provider fault
            logger.exception("unexpected activity search failure")
            errors.append(f"activity search error ({exc}); using STUB inventory")
    else:
        errors.append("Amadeus not configured; using STUB activity inventory")

    stubs = _stub_activities(state)
    return {
        "activity_results": [a.model_dump() for a in stubs],
        "activities": stubs,
        "errors": errors,
    }


@traced("restaurant-agent", "agent")
def restaurant_node(state: TravelState) -> TravelState:
    """Restaurant agent — venues from a places search, prices estimated.

    projectIdea.md §11: this agent must never invent venues. Amadeus returns no
    restaurant pricing, so every `price_estimate` is flagged and carries the
    basis it was derived from.
    """
    request = state["request"]
    errors = list(state.get("errors", []))
    settings = get_settings()

    if settings.amadeus_enabled:
        try:
            result = RestaurantAgent().run(
                request,
                days_by_destination=_days_by_destination(state),
                hotels=state.get("hotel_recommendations"),
            )
            errors.extend(result.notes)
            if result.scheduled:
                return {
                    "restaurant_results": [r.model_dump() for r in result.candidates],
                    "restaurants": result.scheduled,
                    "errors": errors,
                }
            errors.append("restaurant search returned nothing; using STUB inventory")
        except PlacesSearchError as exc:
            errors.append(f"restaurant search failed ({exc}); using STUB inventory")
        except Exception as exc:  # pragma: no cover - unexpected provider fault
            logger.exception("unexpected restaurant search failure")
            errors.append(f"restaurant search error ({exc}); using STUB inventory")
    else:
        errors.append("Amadeus not configured; using STUB restaurant inventory")

    stubs = _stub_restaurants(state)
    return {
        "restaurant_results": [r.model_dump() for r in stubs],
        "restaurants": stubs,
        "errors": errors,
    }


@traced("transportation", "agent")
def transportation_node(state: TravelState) -> TravelState:
    """Approximate local transport: airport -> hotel -> attraction -> food."""
    request = state["request"]
    budget_share = request.budget * _STUB_BUDGET_SHARE["transportation"]
    schedule = _destinations_by_day(state)
    per_day = budget_share / max(len(schedule), 1)

    legs = [
        TransportLeg(
            day=day,
            from_location=f"STUB hotel, {destination}",
            to_location=f"STUB attraction, {destination}",
            mode="public transit",
            duration_minutes=30,
            estimated_cost=round(per_day, 2),
            currency=request.currency,
        )
        for day, destination in enumerate(schedule, start=1)
    ]
    return {"transportation_plan": legs}


@traced("budget-agent", "agent")
def budget_node(state: TravelState) -> TravelState:
    """Budget agent — deterministic totals, LLM judgement on where to save.

    Arithmetic is never delegated to a model. What the agent contributes is the
    choice of which category should absorb a cut when the trip does not fit.
    """
    request = state["request"]
    errors = list(state.get("errors", []))
    agent = BudgetAgent()

    summary = agent.compute(
        request,
        flights=state.get("flight_recommendations"),
        hotels=state.get("hotel_recommendations"),
        activities=state.get("activities"),
        restaurants=state.get("restaurants"),
        transport=state.get("transportation_plan"),
    )

    update: TravelState = {"budget": summary}

    if summary.over_budget:
        plan = agent.choose_savings(request, summary)
        update["cheaper_target"] = plan.target
        # Tighten the cap for the retry, compounding across loop iterations.
        update["cost_pressure"] = round(
            state.get("cost_pressure", 1.0) * plan.reduction_ratio, 4
        )
        errors.append(f"budget: {plan.reasoning}")
        update["errors"] = errors
    else:
        explanation = agent.explain(request, summary)
        if explanation:
            errors.append(f"budget: {explanation}")
            update["errors"] = errors

    return update


@traced("itinerary-agent", "agent")
def itinerary_node(state: TravelState) -> TravelState:
    """Itinerary agent — sequences existing inventory, never invents any.

    The agent may only schedule items from a catalogue built here from real
    recommendations; anything else it returns is dropped and recorded.
    """
    request = state["request"]
    errors = list(state.get("errors", []))

    result = ItineraryAgent().build(
        request,
        _destinations_by_day(state),
        flights=state.get("flight_recommendations"),
        hotels=state.get("hotel_recommendations"),
        activities=state.get("activities"),
        restaurants=state.get("restaurants"),
    )
    errors.extend(result.notes)

    return {"daily_itinerary": result.days, "errors": errors}


@traced("review-agent", "agent")
def review_node(state: TravelState) -> TravelState:
    """Review agent — rule checks decide the verdict; the LLM adds a second pass."""
    request = state["request"]

    result = ReviewAgent().review(
        request,
        itinerary=state.get("daily_itinerary"),
        flights=state.get("flight_recommendations"),
        hotels=state.get("hotel_recommendations"),
        activities=state.get("activities"),
        restaurants=state.get("restaurants"),
        budget=state.get("budget"),
    )
    return {"review": result}


@traced("replan-budget", "span")
def budget_replan_node(state: TravelState) -> TravelState:
    """Entered when the plan is over budget; bumps the loop counter."""
    budget = state.get("budget")
    errors = list(state.get("errors", []))
    if budget is not None:
        errors.append(
            f"over budget by {round(budget.estimated_total - budget.budget, 2)} "
            f"{budget.currency}; re-searching {state.get('cheaper_target')}"
        )
    return {
        "budget_retries": state.get("budget_retries", 0) + 1,
        "errors": errors,
    }


@traced("replan", "span")
def replan_node(state: TravelState) -> TravelState:
    """Entered when the Review agent fails the plan; feeds the planner guidance.

    Without translating the failure into directives the next pass would repeat
    the same search and fail identically until the retry budget ran out.
    """
    review = state.get("review")
    issues = review.issues if review else []
    details = "; ".join(f"{i.check}: {i.detail}" for i in issues)

    errors = list(state.get("errors", []))
    errors.append(f"replan triggered by review failure: {details}")

    guidance = list(state.get("replan_guidance", []))
    fresh = [g for g in guidance_from(issues) if g not in guidance]
    guidance.extend(fresh)
    for item in fresh:
        errors.append(f"replan guidance: {item}")

    update: TravelState = {
        "review_retries": state.get("review_retries", 0) + 1,
        "replan_guidance": guidance,
        "errors": errors,
    }

    # A budget failure means the next search must actually look cheaper.
    if any(i.check == "budget" and i.severity == "error" for i in issues):
        update["cost_pressure"] = round(state.get("cost_pressure", 1.0) * 0.85, 4)

    return update


# ---------------------------------------------------------------------------
# Conditional edge routers
# ---------------------------------------------------------------------------


def route_after_budget(state: TravelState) -> str:
    """Over budget -> re-search the biggest cost category; else carry on."""
    budget = state.get("budget")
    if budget is None or not budget.over_budget:
        return "continue"

    if state.get("budget_retries", 0) >= MAX_BUDGET_RETRIES:
        # Out of retries: continue with the best plan we have and let the
        # Review agent record that it is still over budget.
        return "continue"

    return "replan_budget"


def route_cheaper_target(state: TravelState) -> str:
    """Which search node the budget loop re-enters."""
    return "flight" if state.get("cheaper_target") == "flight" else "hotel"


def route_after_review(state: TravelState) -> str:
    """PASS -> END. FAIL -> replan, until the retry budget runs out."""
    review = state.get("review")
    if review is not None and review.verdict == "PASS":
        return "pass"
    if state.get("review_retries", 0) >= MAX_REVIEW_RETRIES:
        return "pass"  # give up looping; the failed review ships with the plan
    return "replan"
