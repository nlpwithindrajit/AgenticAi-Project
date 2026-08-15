"""LangGraph node implementations.

Milestone 1 status: the graph topology, the shared state, the budget loop and
the review loop are real. The *search* nodes are deterministic placeholders —
they invent clearly-labelled `STUB` inventory so the loops can be exercised
end-to-end without any API keys. Milestones 2-5 replace each stub body with a
reasoning agent (`app/agents/`) driving a deterministic tool client
(`app/tools/`); the signatures and the state contract stay the same.

Anything user-visible produced here is prefixed `STUB` on purpose: no node in
this module should ever be mistaken for a real search result.
"""

from __future__ import annotations

from datetime import timedelta

from app.graph.state import (
    MAX_BUDGET_RETRIES,
    MAX_REVIEW_RETRIES,
    TravelState,
)
from app.models.travel import (
    Activity,
    BudgetBreakdown,
    BudgetSummary,
    DayPlan,
    DestinationInfo,
    FlightOption,
    HotelOption,
    ItineraryItem,
    Restaurant,
    ReviewIssue,
    ReviewResult,
    TransportLeg,
    TripRequirements,
)

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

# How much cheaper the re-search comes back each time the budget loop fires.
_CHEAPER_STEP = 0.25


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


def destination_node(state: TravelState) -> TravelState:
    """Destination agent — enriches each destination with context."""
    request = state["request"]
    info = [
        DestinationInfo(name=name, notes="STUB destination context")
        for name in request.destinations
    ]
    return {"destination_info": info}


def flight_node(state: TravelState) -> TravelState:
    """Flight agent — search -> filter -> rank -> recommend.

    Milestone 2 replaces the body with a real flight-search tool client.
    """
    request = state["request"]
    discount = (
        1.0 - _CHEAPER_STEP * state.get("budget_retries", 0)
        if state.get("cheaper_target") == "flight"
        else 1.0
    )
    total = request.budget * _STUB_BUDGET_SHARE["flights"] * max(discount, 0.3)

    outbound = FlightOption(
        airline="STUB-AIR",
        flight_number="SA-100",
        origin=request.origin,
        destination=request.destinations[0],
        departure=f"{request.departure_date.isoformat()}T09:00",
        arrival=f"{request.departure_date.isoformat()}T18:00",
        duration_minutes=540,
        stops=0 if request.direct_flights_only else 1,
        price=round(total / 2, 2),
        currency=request.currency,
        score=90.0,
        rationale="STUB ranking: placeholder until the flight API is wired up",
    )
    inbound = outbound.model_copy(
        update={
            "flight_number": "SA-101",
            "origin": request.destinations[-1],
            "destination": request.origin,
            "departure": f"{request.return_date.isoformat()}T20:00",
            "arrival": f"{request.return_date.isoformat()}T23:59",
        }
    )
    return {
        "flight_results": [outbound.model_dump(), inbound.model_dump()],
        "flight_recommendations": [outbound, inbound],
    }


def hotel_node(state: TravelState) -> TravelState:
    """Hotel agent — search -> filter -> rank -> recommend.

    Ranking weights (Milestone 3): price 30%, location 25%, rating 20%,
    amenities 15%, traveler preferences 10%.
    """
    request = state["request"]
    discount = (
        1.0 - _CHEAPER_STEP * state.get("budget_retries", 0)
        if state.get("cheaper_target") == "hotel"
        else 1.0
    )
    total = request.budget * _STUB_BUDGET_SHARE["hotels"] * max(discount, 0.3)

    nights = max(request.nights, 1)
    per_destination_total = total / len(request.destinations)
    nights_per_destination = max(1, nights // len(request.destinations))

    per_night = round(per_destination_total / nights_per_destination, 2)

    hotels: list[HotelOption] = []
    check_in = request.departure_date
    for destination in request.destinations:
        check_out = check_in + timedelta(days=nights_per_destination)
        hotels.append(
            HotelOption(
                name=f"STUB Hotel {destination}",
                destination=destination,
                check_in=check_in,
                check_out=check_out,
                stars=float(request.hotel_stars or 4),
                rating=8.5,
                price_per_night=per_night,
                total_price=round(per_destination_total, 2),
                currency=request.currency,
                location="STUB city centre",
                amenities=["wifi", "breakfast"],
                score=88.0,
                rationale="STUB ranking: placeholder until the hotel API is wired up",
            )
        )
        check_in = check_out

    return {
        "hotel_results": [hotel.model_dump() for hotel in hotels],
        "hotel_recommendations": hotels,
    }


def activity_node(state: TravelState) -> TravelState:
    """Activity agent — draws candidates from a places search tool."""
    request = state["request"]
    budget_share = request.budget * _STUB_BUDGET_SHARE["activities"]
    schedule = _destinations_by_day(state)
    per_activity = budget_share / max(len(schedule), 1)

    activities = [
        Activity(
            activity=f"STUB attraction in {destination}",
            category=(request.interests[0] if request.interests else "sightseeing"),
            destination=destination,
            duration_hours=2.0,
            estimated_cost=round(per_activity, 2),
            currency=request.currency,
            recommended_day=day,
        )
        for day, destination in enumerate(schedule, start=1)
    ]
    return {
        "activity_results": [activity.model_dump() for activity in activities],
        "activities": activities,
    }


def restaurant_node(state: TravelState) -> TravelState:
    """Restaurant agent — must draw from search results, never invent venues."""
    request = state["request"]
    budget_share = request.budget * _STUB_BUDGET_SHARE["restaurants"]
    schedule = _destinations_by_day(state)
    per_meal = budget_share / max(len(schedule), 1)

    restaurants = [
        Restaurant(
            name=f"STUB restaurant in {destination}",
            destination=destination,
            meal="dinner",
            price_estimate=round(per_meal, 2),
            currency=request.currency,
            rating=4.4,
            dietary_tags=list(request.dietary_preferences),
            recommended_day=day,
        )
        for day, destination in enumerate(schedule, start=1)
    ]
    return {
        "restaurant_results": [r.model_dump() for r in restaurants],
        "restaurants": restaurants,
    }


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


def budget_node(state: TravelState) -> TravelState:
    """Budget agent — totals every category and compares against the budget."""
    request = state["request"]

    breakdown = BudgetBreakdown(
        flights=sum(f.price for f in state.get("flight_recommendations", [])),
        hotels=sum(h.total_price for h in state.get("hotel_recommendations", [])),
        activities=sum(a.estimated_cost for a in state.get("activities", [])),
        restaurants=sum(r.price_estimate for r in state.get("restaurants", [])),
        transportation=sum(
            t.estimated_cost for t in state.get("transportation_plan", [])
        ),
        currency=request.currency,
    )
    estimated_total = round(breakdown.estimated_total, 2)
    over_budget = estimated_total > request.budget

    summary = BudgetSummary(
        breakdown=breakdown,
        estimated_total=estimated_total,
        budget=request.budget,
        remaining=round(request.budget - estimated_total, 2),
        over_budget=over_budget,
        currency=request.currency,
    )

    update: TravelState = {"budget": summary}
    if over_budget:
        # Send the loop at whichever category is actually the biggest spend.
        update["cheaper_target"] = (
            "flight" if breakdown.flights >= breakdown.hotels else "hotel"
        )
    return update


def itinerary_node(state: TravelState) -> TravelState:
    """Itinerary agent — turns the inventory into a day-by-day schedule."""
    request = state["request"]
    schedule = _destinations_by_day(state)
    activities = {a.recommended_day: a for a in state.get("activities", [])}
    restaurants = {r.recommended_day: r for r in state.get("restaurants", [])}
    flights = state.get("flight_recommendations", [])

    days: list[DayPlan] = []
    for index, destination in enumerate(schedule):
        day_number = index + 1
        day_date = request.departure_date + timedelta(days=index)
        is_last_day = day_number == len(schedule)
        has_return_flight = is_last_day and len(flights) > 1
        items: list[ItineraryItem] = []

        if day_number == 1 and flights:
            items.append(
                ItineraryItem(
                    time="09:00",
                    title=f"Flight {flights[0].origin} to {flights[0].destination}",
                    kind="flight",
                )
            )
            items.append(
                ItineraryItem(time="19:00", title="Hotel check-in", kind="hotel")
            )

        activity = activities.get(day_number)
        if activity is not None:
            items.append(
                ItineraryItem(
                    time="14:00",
                    title=activity.activity,
                    kind="activity",
                    location=activity.location,
                )
            )

        restaurant = restaurants.get(day_number)
        if restaurant is not None:
            # Eat earlier on departure day so dinner clears the return flight.
            items.append(
                ItineraryItem(
                    time="17:00" if has_return_flight else "20:00",
                    title=restaurant.name,
                    kind="meal",
                )
            )

        if has_return_flight:
            items.append(
                ItineraryItem(
                    time="20:00",
                    title=f"Return flight to {flights[-1].destination}",
                    kind="flight",
                )
            )

        days.append(
            DayPlan(
                day=day_number,
                date=day_date,
                destination=destination,
                items=sorted(items, key=lambda item: item.time),
            )
        )

    return {"daily_itinerary": days}


def review_node(state: TravelState) -> TravelState:
    """Review agent — the quality gate. These checks are real, not stubbed."""
    request = state["request"]
    issues: list[ReviewIssue] = []

    itinerary = state.get("daily_itinerary", [])
    if len(itinerary) != request.duration_days:
        issues.append(
            ReviewIssue(
                check="itinerary_length",
                detail=(
                    f"itinerary covers {len(itinerary)} days, "
                    f"request covers {request.duration_days}"
                ),
            )
        )

    if itinerary:
        if itinerary[0].date != request.departure_date:
            issues.append(
                ReviewIssue(
                    check="start_date",
                    detail="first itinerary day does not match departure_date",
                )
            )
        if itinerary[-1].date != request.return_date:
            issues.append(
                ReviewIssue(
                    check="end_date",
                    detail="last itinerary day does not match return_date",
                )
            )

    if not state.get("flight_recommendations"):
        issues.append(
            ReviewIssue(check="flights", detail="no flight recommendations produced")
        )
    if not state.get("hotel_recommendations"):
        issues.append(
            ReviewIssue(check="hotels", detail="no hotel recommendations produced")
        )

    budget = state.get("budget")
    if budget is not None and budget.over_budget:
        issues.append(
            ReviewIssue(
                check="budget",
                detail=(
                    f"estimated {budget.estimated_total} {budget.currency} exceeds "
                    f"budget {budget.budget} {budget.currency}"
                ),
            )
        )

    for day in itinerary:
        times = [item.time for item in day.items]
        if len(times) != len(set(times)):
            issues.append(
                ReviewIssue(
                    check="schedule_conflict",
                    detail=f"day {day.day} has two entries at the same time",
                )
            )

    blocking = [issue for issue in issues if issue.severity == "error"]
    verdict = "FAIL" if blocking else "PASS"
    return {"review": ReviewResult(verdict=verdict, issues=issues)}


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


def replan_node(state: TravelState) -> TravelState:
    """Entered when the Review agent fails the plan; feeds back to the planner."""
    review = state.get("review")
    details = (
        "; ".join(f"{i.check}: {i.detail}" for i in review.issues) if review else ""
    )
    errors = list(state.get("errors", []))
    errors.append(f"replan triggered by review failure: {details}")
    return {
        "review_retries": state.get("review_retries", 0) + 1,
        "errors": errors,
    }


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
