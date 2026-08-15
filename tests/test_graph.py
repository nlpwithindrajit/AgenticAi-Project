"""The graph contract: topology, both feedback loops, and termination."""

from __future__ import annotations

from datetime import date

import pytest

from app.graph import nodes
from app.graph.graph import build_graph, plan_trip
from app.graph.state import MAX_BUDGET_RETRIES, initial_state
from app.models.travel import (
    BudgetBreakdown,
    BudgetSummary,
    ReviewIssue,
    ReviewResult,
    TravelRequest,
)


def test_graph_has_every_planned_node() -> None:
    compiled = build_graph().compile()
    expected = {
        "planner",
        "destination",
        "flight",
        "hotel",
        "activity",
        "restaurant",
        "transportation",
        "budget",
        "replan_budget",
        "itinerary",
        "review",
        "replan",
    }
    assert expected <= set(compiled.get_graph().nodes)


def test_end_to_end_run_produces_a_complete_plan(
    sample_request: TravelRequest,
) -> None:
    plan = plan_trip(sample_request)

    assert plan.flight_recommendations, "expected outbound + return flights"
    assert plan.hotel_recommendations
    assert plan.activities
    assert plan.restaurants
    assert plan.transportation_plan
    assert plan.budget is not None
    assert plan.review is not None

    # One itinerary day per day of the trip, in order, starting on departure.
    assert len(plan.daily_itinerary) == sample_request.duration_days
    assert plan.daily_itinerary[0].date == sample_request.departure_date
    assert plan.daily_itinerary[-1].date == sample_request.return_date
    assert [d.day for d in plan.daily_itinerary] == list(
        range(1, sample_request.duration_days + 1)
    )


def test_budget_costs_only_the_chosen_flight_offer(
    sample_request: TravelRequest,
) -> None:
    """Recommendations are alternatives — billing all of them is a real bug."""
    plan = plan_trip(sample_request)

    assert len(plan.flight_recommendations) > 1, "need alternatives to test this"
    assert plan.budget is not None
    assert plan.budget.breakdown.flights == plan.flight_recommendations[0].price
    assert plan.budget.breakdown.flights < sum(
        f.price for f in plan.flight_recommendations
    )


def test_stub_flights_are_labelled_and_explained(
    sample_request: TravelRequest,
) -> None:
    """With no Amadeus credentials the plan must admit the flights are fake."""
    plan = plan_trip(sample_request)

    assert all(f.source == "stub" for f in plan.flight_recommendations)
    assert any("Amadeus not configured" in error for error in plan.errors)


def test_budget_costs_one_hotel_per_destination(
    sample_request: TravelRequest,
) -> None:
    """Hotels are alternatives per city — bill the best in each, once."""
    plan = plan_trip(sample_request)

    assert plan.budget is not None
    by_destination: dict[str, list] = {}
    for hotel in plan.hotel_recommendations:
        by_destination.setdefault(hotel.destination, []).append(hotel)

    assert set(by_destination) == set(sample_request.destinations)
    assert all(len(v) > 1 for v in by_destination.values()), "need alternatives"

    expected = sum(
        max(v, key=lambda h: h.score).total_price for v in by_destination.values()
    )
    assert plan.budget.breakdown.hotels == pytest.approx(expected)
    assert plan.budget.breakdown.hotels < sum(
        h.total_price for h in plan.hotel_recommendations
    )


def test_hotel_stays_cover_the_trip_without_gaps(
    sample_request: TravelRequest,
) -> None:
    plan = plan_trip(sample_request)

    best = {}
    for hotel in plan.hotel_recommendations:
        current = best.get(hotel.destination)
        if current is None or hotel.score > current.score:
            best[hotel.destination] = hotel

    stays = sorted(best.values(), key=lambda h: h.check_in)
    assert stays[0].check_in == sample_request.departure_date
    assert stays[-1].check_out == sample_request.return_date
    for earlier, later in zip(stays, stays[1:], strict=False):
        assert earlier.check_out == later.check_in


def test_stub_hotels_are_labelled(sample_request: TravelRequest) -> None:
    plan = plan_trip(sample_request)

    assert all(h.source == "stub" for h in plan.hotel_recommendations)
    assert any("Amadeus not configured" in e for e in plan.errors)


def test_activities_and_restaurants_are_a_schedule_not_alternatives(
    sample_request: TravelRequest,
) -> None:
    """One per day, no repeats — this is why the Budget agent may sum them."""
    plan = plan_trip(sample_request)
    days = list(range(1, sample_request.duration_days + 1))

    assert [a.recommended_day for a in plan.activities] == days
    assert [r.recommended_day for r in plan.restaurants] == days
    assert len({a.activity for a in plan.activities}) == len(plan.activities)

    assert plan.budget is not None
    assert plan.budget.breakdown.activities == pytest.approx(
        sum(a.estimated_cost for a in plan.activities)
    )
    assert plan.budget.breakdown.restaurants == pytest.approx(
        sum(r.price_estimate for r in plan.restaurants)
    )


def test_restaurant_prices_are_never_presented_as_quotes(
    sample_request: TravelRequest,
) -> None:
    plan = plan_trip(sample_request)

    assert plan.restaurants
    assert all(r.price_is_estimated for r in plan.restaurants)
    assert all(r.estimate_basis for r in plan.restaurants)


def test_notes_are_not_duplicated_by_the_replan_loops(
    sample_request: TravelRequest,
) -> None:
    """A standing condition must not be repeated once per loop iteration."""
    plan = plan_trip(sample_request)
    assert len(plan.errors) == len(set(plan.errors))


def test_every_hotel_score_component_is_exercised(
    sample_request: TravelRequest,
) -> None:
    """A component stuck at zero usually means data was never populated."""
    plan = plan_trip(sample_request)

    for hotel in plan.hotel_recommendations:
        assert {"price", "location", "rating", "amenities"} <= set(
            hotel.score_components
        )
    assert any(
        h.score_components["amenities"] > 0 for h in plan.hotel_recommendations
    ), "amenity detection never fired — is the description being parsed?"


def test_stub_flight_offers_vary_in_stops(sample_request: TravelRequest) -> None:
    """The ranker needs offers that actually differ, or scoring is meaningless."""
    plan = plan_trip(sample_request)
    stop_counts = {f.stops for f in plan.flight_recommendations}
    assert len(stop_counts) > 1


def test_itinerary_days_are_chronological(sample_request: TravelRequest) -> None:
    plan = plan_trip(sample_request)
    for day in plan.daily_itinerary:
        times = [item.time for item in day.items]
        assert times == sorted(times), f"day {day.day} is out of order: {times}"


def test_budget_loop_brings_the_plan_under_budget(
    sample_request: TravelRequest,
) -> None:
    """The stub costing model starts over budget; the loop must resolve it."""
    plan = plan_trip(sample_request)

    assert plan.budget is not None
    assert not plan.budget.over_budget, (
        f"budget loop failed to converge: {plan.budget.estimated_total} > "
        f"{plan.budget.budget}"
    )
    assert any("over budget" in error for error in plan.errors), (
        "expected the budget loop to have fired at least once"
    )
    assert plan.review is not None and plan.review.verdict == "PASS"


def test_budget_router_stops_after_max_retries(
    sample_request: TravelRequest,
) -> None:
    """Out of retries, the graph moves on rather than looping forever."""
    over_budget = BudgetSummary(
        breakdown=BudgetBreakdown(flights=999999),
        estimated_total=999999,
        budget=sample_request.budget,
        remaining=sample_request.budget - 999999,
        over_budget=True,
    )
    state = initial_state(sample_request)
    state["budget"] = over_budget

    state["budget_retries"] = 0
    assert nodes.route_after_budget(state) == "replan_budget"

    state["budget_retries"] = MAX_BUDGET_RETRIES
    assert nodes.route_after_budget(state) == "continue"


def test_review_router_gates_the_output(sample_request: TravelRequest) -> None:
    state = initial_state(sample_request)

    state["review"] = ReviewResult(verdict="PASS")
    assert nodes.route_after_review(state) == "pass"

    state["review"] = ReviewResult(
        verdict="FAIL", issues=[ReviewIssue(check="budget", detail="over")]
    )
    assert nodes.route_after_review(state) == "replan"


def test_review_catches_an_incomplete_plan(sample_request: TravelRequest) -> None:
    """Review is a real gate: an empty plan must not pass."""
    state = initial_state(sample_request)
    result = nodes.review_node(state)["review"]

    assert result.verdict == "FAIL"
    checks = {issue.check for issue in result.issues}
    assert {"flights", "hotels", "itinerary_length"} <= checks


def test_single_day_trip_does_not_divide_by_zero() -> None:
    """A same-day trip means zero nights — the hotel node must still work."""
    request = TravelRequest(
        origin="Mumbai",
        destinations=["Goa"],
        departure_date=date(2026, 10, 10),
        return_date=date(2026, 10, 10),
        travelers=1,
        budget=20000,
    )
    plan = plan_trip(request)

    assert len(plan.daily_itinerary) == 1
    assert plan.hotel_recommendations
    assert plan.budget is not None
