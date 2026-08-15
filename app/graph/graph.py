"""LangGraph wiring for the travel-planning workflow.

    START -> planner -> destination -> flight -> hotel -> activity
          -> restaurant -> transportation -> budget
                                              |
                          over budget? -> replan_budget -> flight | hotel
                                              |
                                          itinerary -> review
                                                        |
                                              PASS -> END
                                              FAIL -> replan -> planner

The two feedback loops (budget and review) are the point of this project. Both
are bounded by retry counters in `app/graph/state.py`; do not flatten them into
a linear chain.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graph import nodes
from app.graph.state import TravelState, initial_state
from app.models.travel import TravelRequest, TripPlan


def build_graph() -> StateGraph:
    """Assemble the workflow. Call `.compile()` on the result to run it."""
    graph = StateGraph(TravelState)

    graph.add_node("planner", nodes.planner_node)
    graph.add_node("destination", nodes.destination_node)
    graph.add_node("flight", nodes.flight_node)
    graph.add_node("hotel", nodes.hotel_node)
    graph.add_node("activity", nodes.activity_node)
    graph.add_node("restaurant", nodes.restaurant_node)
    graph.add_node("transportation", nodes.transportation_node)
    graph.add_node("budget", nodes.budget_node)
    graph.add_node("replan_budget", nodes.budget_replan_node)
    graph.add_node("itinerary", nodes.itinerary_node)
    graph.add_node("review", nodes.review_node)
    graph.add_node("replan", nodes.replan_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "destination")
    graph.add_edge("destination", "flight")
    graph.add_edge("flight", "hotel")
    graph.add_edge("hotel", "activity")
    graph.add_edge("activity", "restaurant")
    graph.add_edge("restaurant", "transportation")
    graph.add_edge("transportation", "budget")

    # Budget loop: over budget -> re-search the biggest cost category.
    graph.add_conditional_edges(
        "budget",
        nodes.route_after_budget,
        {"continue": "itinerary", "replan_budget": "replan_budget"},
    )
    graph.add_conditional_edges(
        "replan_budget",
        nodes.route_cheaper_target,
        {"flight": "flight", "hotel": "hotel"},
    )

    graph.add_edge("itinerary", "review")

    # Review gate: PASS ends the run, FAIL routes back through the planner.
    graph.add_conditional_edges(
        "review",
        nodes.route_after_review,
        {"pass": END, "replan": "replan"},
    )
    graph.add_edge("replan", "planner")

    return graph


_compiled = None


def get_compiled_graph():
    """Compile once and reuse — compilation is not free per request."""
    global _compiled
    if _compiled is None:
        _compiled = build_graph().compile()
    return _compiled


def plan_trip(request: TravelRequest, trace_id: str | None = None) -> TripPlan:
    """Run the workflow end to end and shape the final state into a TripPlan."""
    final_state = get_compiled_graph().invoke(
        initial_state(request, trace_id=trace_id),
        # Generous ceiling: the bounded loops terminate well before this, but a
        # bad edit should raise rather than hang a request forever.
        {"recursion_limit": 50},
    )
    return to_trip_plan(final_state)


def _dedupe(notes: list[str]) -> list[str]:
    """Collapse identical notes, keeping first-seen order.

    Every replan pass re-runs the search nodes, so a standing condition like
    "Amadeus not configured" would otherwise be repeated once per loop and bury
    the notes that only fired once.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for note in notes:
        if note not in seen:
            seen.add(note)
            unique.append(note)
    return unique


def to_trip_plan(state: TravelState) -> TripPlan:
    return TripPlan(
        request=state["request"],
        destination_info=state.get("destination_info", []),
        flight_recommendations=state.get("flight_recommendations", []),
        hotel_recommendations=state.get("hotel_recommendations", []),
        activities=state.get("activities", []),
        restaurants=state.get("restaurants", []),
        transportation_plan=state.get("transportation_plan", []),
        daily_itinerary=state.get("daily_itinerary", []),
        budget=state.get("budget"),
        review=state.get("review"),
        errors=_dedupe(state.get("errors", [])),
        trace_id=state.get("trace_id"),
    )
