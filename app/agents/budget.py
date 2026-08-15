"""Budget agent — deterministic arithmetic, LLM judgement about what to cut.

The split matters here more than anywhere else in the project: **no total is
ever produced by a model.** `compute()` is plain Python, and it is the only
thing that touches money. What the LLM contributes is the judgement call the
arithmetic cannot make — when a trip comes in over budget, *which* category
should give way, and by how much.

Two costing rules the arithmetic depends on, both learned the hard way:

  flights / hotels  are ALTERNATIVES. Cost the best one (per destination for
                    hotels), never the sum of everything considered.
  activities / restaurants are a SCHEDULE, one per day. Sum them.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.places_base import build_llm
from app.models.travel import (
    Activity,
    BudgetBreakdown,
    BudgetSummary,
    FlightOption,
    HotelOption,
    Restaurant,
    TransportLeg,
    TravelRequest,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You advise a travel planner on where to save money.

You never calculate totals — those are given to you and are authoritative. \
Your job is to choose which single category should absorb a cut, and to say \
why in one sentence.

Only "flight" or "hotel" can be re-searched. Prefer whichever has more room to \
give without wrecking the trip: a traveller who asked for direct flights will \
resent a connection more than a slightly further hotel, and a long stay makes \
hotel savings compound across nights."""

# Only these two have a re-search path in the graph.
SavingsTarget = Literal["flight", "hotel"]


class SavingsPlan(BaseModel):
    """Where the next search should find money, and why."""

    target: SavingsTarget = Field(
        default="hotel", description="Which category to re-search more cheaply."
    )
    reduction_ratio: float = Field(
        default=0.85,
        ge=0.4,
        le=0.99,
        description="Multiply that category's price cap by this on the retry.",
    )
    reasoning: str = Field(
        default="", description="One sentence explaining the choice."
    )


def compute(
    request: TravelRequest,
    *,
    flights: list[FlightOption] | None = None,
    hotels: list[HotelOption] | None = None,
    activities: list[Activity] | None = None,
    restaurants: list[Restaurant] | None = None,
    transport: list[TransportLeg] | None = None,
) -> BudgetSummary:
    """Total the trip. Pure arithmetic — no model is involved, by design."""
    flights = flights or []
    hotels = hotels or []

    # Alternatives, not legs: one flight offer covers every traveller and both
    # directions, so cost the top-ranked one only.
    flight_cost = flights[0].price if flights else 0.0

    # Alternatives per destination: cost the best-scoring stay in each city once.
    best_by_destination: dict[str, HotelOption] = {}
    for hotel in hotels:
        current = best_by_destination.get(hotel.destination)
        if current is None or hotel.score > current.score:
            best_by_destination[hotel.destination] = hotel
    hotel_cost = sum(h.total_price for h in best_by_destination.values())

    breakdown = BudgetBreakdown(
        flights=flight_cost,
        hotels=hotel_cost,
        # Schedules, one per day — summing these is correct.
        activities=sum(a.estimated_cost for a in activities or []),
        restaurants=sum(r.price_estimate for r in restaurants or []),
        transportation=sum(t.estimated_cost for t in transport or []),
        currency=request.currency,
    )
    estimated_total = round(breakdown.estimated_total, 2)

    return BudgetSummary(
        breakdown=breakdown,
        estimated_total=estimated_total,
        budget=request.budget,
        remaining=round(request.budget - estimated_total, 2),
        over_budget=estimated_total > request.budget,
        currency=request.currency,
    )


class BudgetAgent:
    """Totals a trip, and decides where to find money when it doesn't fit."""

    def __init__(self, llm: object | None = None) -> None:
        self._llm = llm

    def _get_llm(self):
        self._llm = build_llm(self._llm)
        return self._llm

    def compute(self, request: TravelRequest, **inventory) -> BudgetSummary:
        return compute(request, **inventory)

    def _default_plan(self, summary: BudgetSummary) -> SavingsPlan:
        """Cut whichever category is simply the larger line."""
        breakdown = summary.breakdown
        target: SavingsTarget = (
            "flight" if breakdown.flights >= breakdown.hotels else "hotel"
        )
        overshoot = summary.estimated_total - summary.budget
        line = breakdown.flights if target == "flight" else breakdown.hotels

        # Ask the retry for enough saving to close the gap, with a little margin.
        ratio = 0.85
        if line > 0:
            ratio = max(0.4, min(0.99, 1.0 - (overshoot * 1.1) / line))

        return SavingsPlan(
            target=target,
            reduction_ratio=round(ratio, 3),
            reasoning=(
                f"Deterministic choice: {target}s are the larger line at "
                f"{line:,.0f} {summary.currency}."
            ),
        )

    def choose_savings(
        self, request: TravelRequest, summary: BudgetSummary
    ) -> SavingsPlan:
        """Decide where the next search should save. Falls back deterministically."""
        llm = self._get_llm()
        if llm is None:
            return self._default_plan(summary)

        breakdown = summary.breakdown
        prompt = (
            f"The trip is over budget and must be re-planned.\n\n"
            f"Budget:    {summary.budget:,.0f} {summary.currency}\n"
            f"Estimated: {summary.estimated_total:,.0f} {summary.currency} "
            f"(over by {summary.estimated_total - summary.budget:,.0f})\n\n"
            f"Breakdown:\n"
            f"  flights        {breakdown.flights:,.0f}\n"
            f"  hotels         {breakdown.hotels:,.0f}\n"
            f"  activities     {breakdown.activities:,.0f}\n"
            f"  restaurants    {breakdown.restaurants:,.0f}\n"
            f"  transport      {breakdown.transportation:,.0f}\n\n"
            f"Traveller: {request.travelers} people, "
            f"{request.duration_days} days, "
            f"direct flights requested: {request.direct_flights_only}, "
            f"hotel stars wanted: {request.hotel_stars or 'no preference'}.\n\n"
            f"Choose the category to re-search and the price-cap multiplier."
        )
        try:
            plan = llm.with_structured_output(SavingsPlan).invoke(
                [("system", SYSTEM_PROMPT), ("human", prompt)]
            )
            if not isinstance(plan, SavingsPlan):
                plan = SavingsPlan.model_validate(plan)
            # Guard: a category with nothing in it cannot give anything back.
            line = (
                breakdown.flights if plan.target == "flight" else breakdown.hotels
            )
            if line <= 0:
                logger.warning(
                    "budget agent chose %s, which costs nothing; using defaults",
                    plan.target,
                )
                return self._default_plan(summary)
            return plan
        except Exception as exc:
            logger.warning("budget savings choice failed, using defaults: %s", exc)
            return self._default_plan(summary)

    def explain(self, request: TravelRequest, summary: BudgetSummary) -> str | None:
        """A sentence on how the money is distributed. None when no LLM."""
        llm = self._get_llm()
        if llm is None:
            return None

        breakdown = summary.breakdown
        prompt = (
            "Summarise this trip budget in one or two sentences for the "
            "traveller. Use only the figures given.\n\n"
            f"Budget {summary.budget:,.0f} {summary.currency}, "
            f"estimated {summary.estimated_total:,.0f}, "
            f"remaining {summary.remaining:,.0f}.\n"
            f"flights {breakdown.flights:,.0f}; hotels {breakdown.hotels:,.0f}; "
            f"activities {breakdown.activities:,.0f}; "
            f"restaurants {breakdown.restaurants:,.0f} (estimated); "
            f"transport {breakdown.transportation:,.0f}."
        )
        try:
            response = llm.invoke([("system", SYSTEM_PROMPT), ("human", prompt)])
            text = getattr(response, "content", None)
            if isinstance(text, str) and text.strip():
                return text.strip()
        except Exception as exc:
            logger.warning("budget explanation failed: %s", exc)
        return None


__all__ = ["BudgetAgent", "SavingsPlan", "SavingsTarget", "compute"]
