"""The graph contract: topology, both feedback loops, and termination."""

from __future__ import annotations

from datetime import date

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
