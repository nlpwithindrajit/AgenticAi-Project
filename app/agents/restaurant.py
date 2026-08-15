"""Restaurant agent — real venues, estimated prices, one dinner per day.

projectIdea.md §11 is emphatic that this agent must not invent restaurants, so
every venue comes from an Amadeus points-of-interest search. But that endpoint
returns **no pricing at all**, so the cost of eating is estimated from the
traveller's budget and trip style. Those two facts are kept strictly apart:

  venue   -> real, from the provider
  price   -> estimated, flagged `price_is_estimated`, with `estimate_basis`
             spelling out where the number came from

Output is a schedule (one dinner per day), not alternatives, so the Budget
agent sums it directly.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.agents.places_base import build_llm, hotel_anchor_for, resolve_anchor
from app.models.travel import HotelOption, Restaurant, TravelRequest
from app.tools.amadeus import AmadeusClient
from app.tools.places import (
    estimate_meal_cost,
    normalize_points_of_interest,
    rank_restaurants,
    schedule_across_days,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You plan restaurant searches for a travel planner.

You do not search for or invent restaurants — every venue comes from a places \
search. You choose the search parameters, and you say why.

The provider does not return restaurant prices, so cost is estimated \
separately. Never present an estimated price as a quoted one."""

RESTAURANT_CATEGORY = "RESTAURANT"


class RestaurantSearchPlan(BaseModel):
    """The LLM's decision about how to run this particular search."""

    radius_km: int = Field(
        default=3, ge=1, le=20, description="Search radius around the hotel."
    )
    reasoning: str = Field(
        default="", description="One sentence on why these parameters were chosen."
    )


class RestaurantResult(BaseModel):
    """What the restaurant node writes back into `TravelState`."""

    scheduled: list[Restaurant] = Field(default_factory=list)
    """One per day — this is what the Budget agent costs."""
    candidates: list[Restaurant] = Field(default_factory=list)
    raw_count: int = 0
    plan: RestaurantSearchPlan = Field(default_factory=RestaurantSearchPlan)
    notes: list[str] = Field(default_factory=list)


class RestaurantAgent:
    """Search -> filter -> rank -> schedule. Venues real, prices estimated."""

    def __init__(
        self,
        client: AmadeusClient | None = None,
        llm: object | None = None,
    ) -> None:
        self._client = client
        self._llm = llm

    @property
    def client(self) -> AmadeusClient:
        if self._client is None:
            self._client = AmadeusClient()
        return self._client

    def _get_llm(self):
        self._llm = build_llm(self._llm)
        return self._llm

    # -- planning --------------------------------------------------------

    def _default_plan(self) -> RestaurantSearchPlan:
        return RestaurantSearchPlan(
            radius_km=3,
            reasoning="Deterministic plan: walkable radius around the hotel.",
        )

    def plan_search(self, request: TravelRequest) -> RestaurantSearchPlan:
        llm = self._get_llm()
        if llm is None:
            return self._default_plan()

        prompt = (
            f"Traveller request:\n"
            f"- Destinations: {', '.join(request.destinations)}\n"
            f"- Travellers: {request.travelers}\n"
            f"- Dietary preferences: "
            f"{', '.join(request.dietary_preferences) or 'none stated'}\n"
            f"- Interests: {', '.join(request.interests) or 'none stated'}\n"
            f"- Trip style: {request.trip_style}\n\n"
            f"Choose the restaurant search parameters."
        )
        try:
            plan = llm.with_structured_output(RestaurantSearchPlan).invoke(
                [("system", SYSTEM_PROMPT), ("human", prompt)]
            )
            if isinstance(plan, RestaurantSearchPlan):
                return plan
            return RestaurantSearchPlan.model_validate(plan)
        except Exception as exc:
            logger.warning(
                "restaurant search planning failed, using defaults: %s", exc
            )
            return self._default_plan()

    # -- explanation -----------------------------------------------------

    def explain(
        self, request: TravelRequest, scheduled: list[Restaurant]
    ) -> list[Restaurant]:
        llm = self._get_llm()
        if llm is None or not scheduled:
            return scheduled

        listing = "; ".join(
            f"day {r.recommended_day}: {r.name}" for r in scheduled[:8]
        )
        prompt = (
            "Explain in one or two sentences why these restaurants suit the "
            "traveller. Use only the facts given; do not name any restaurant "
            "that is not listed. Make clear that the prices are estimates.\n\n"
            f"Dietary preferences: "
            f"{', '.join(request.dietary_preferences) or 'none stated'}. "
            f"Travellers: {request.travelers}. "
            f"Estimated cost per meal: {scheduled[0].price_estimate} "
            f"{scheduled[0].currency} ({scheduled[0].estimate_basis}).\n"
            f"Chosen: {listing}"
        )
        try:
            response = llm.invoke([("system", SYSTEM_PROMPT), ("human", prompt)])
            text = getattr(response, "content", None)
            if isinstance(text, str) and text.strip():
                first = scheduled[0].model_copy(update={"rationale": text.strip()})
                return [first, *scheduled[1:]]
        except Exception as exc:
            logger.warning(
                "restaurant explanation failed, keeping scoring reasons: %s", exc
            )
        return scheduled

    # -- the whole job ---------------------------------------------------

    def run(
        self,
        request: TravelRequest,
        *,
        days_by_destination: dict[str, list[int]],
        hotels: list[HotelOption] | None = None,
    ) -> RestaurantResult:
        notes: list[str] = []
        plan = self.plan_search(request)
        if plan.reasoning:
            notes.append(f"restaurant search plan: {plan.reasoning}")

        meal_cost, basis = estimate_meal_cost(
            request.budget,
            request.travelers,
            request.duration_days,
            request.trip_style,
        )
        notes.append(f"restaurant prices are estimates: {basis}")

        candidates: list[Restaurant] = []
        scheduled: list[Restaurant] = []
        raw_count = 0

        for destination, days in days_by_destination.items():
            if not days:
                continue

            anchor = resolve_anchor(
                self.client,
                destination,
                hotel_anchor=hotel_anchor_for(destination, hotels),
            )
            payload = self.client.search_points_of_interest(
                anchor[0],
                anchor[1],
                radius_km=plan.radius_km,
                categories=[RESTAURANT_CATEGORY],
            )
            found = normalize_points_of_interest(
                payload,
                destination=destination,
                anchor=anchor,
                meal_cost=meal_cost,
                estimate_basis=basis,
                currency=request.currency,
            )
            raw_count += len(found)
            if not found:
                notes.append(f"no restaurants found near {destination}")
                continue

            ranked = rank_restaurants(
                found,
                dietary_preferences=request.dietary_preferences,
                top_n=max(len(days), 5),
            )
            if request.dietary_preferences and not any(
                r.dietary_tags for r in ranked
            ):
                # Say so rather than implying the venues were vetted.
                notes.append(
                    f"no {', '.join(request.dietary_preferences)} venues could be "
                    f"confirmed from provider tags in {destination}; "
                    "check menus before booking"
                )
            candidates.extend(ranked)

            picked = schedule_across_days(ranked, days)
            if len(picked) < len(days):
                notes.append(
                    f"only {len(picked)} restaurants available for "
                    f"{len(days)} days in {destination}"
                )
            scheduled.extend(picked)

        scheduled.sort(key=lambda r: r.recommended_day or 0)
        scheduled = self.explain(request, scheduled)

        return RestaurantResult(
            scheduled=scheduled,
            candidates=candidates,
            raw_count=raw_count,
            plan=plan,
            notes=notes,
        )


__all__ = ["RestaurantAgent", "RestaurantResult", "RestaurantSearchPlan"]
