"""Activity agent — searches, ranks, and schedules one activity per day.

Same split as the Flight and Hotel agents: the LLM picks search parameters and
writes the rationale; `app/tools/places.py` does the API work and the scoring.
The agent can only recommend activities the provider returned.

Unlike flights and hotels, the output is a **schedule** (one activity per day),
not a list of alternatives — so the Budget agent sums it directly. Full
candidate sets go to `activity_results` for anyone who wants them.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.agents.places_base import build_llm, hotel_anchor_for, resolve_anchor
from app.models.travel import Activity, HotelOption, TravelRequest
from app.tools.amadeus import AmadeusClient
from app.tools.places import (
    normalize_activities,
    rank_activities,
    schedule_across_days,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You plan activity searches for a travel planner.

You do not search for or invent activities. You choose the search parameters \
most likely to surface things the traveller will actually enjoy, and you say \
why.

Budget guidance: activities typically consume 5-15% of a trip budget across \
the whole trip. Set max_cost_per_activity for a single activity for the whole \
party, not the entire trip.

radius_km controls how far from the traveller's hotel to look. Keep it small \
in dense cities and larger in spread-out ones, but never so small that nothing \
is found."""

# Share of the trip budget activities may take.
DEFAULT_ACTIVITY_BUDGET_SHARE = 0.12


class ActivitySearchPlan(BaseModel):
    """The LLM's decision about how to run this particular search."""

    radius_km: int = Field(
        default=5, ge=1, le=20, description="Search radius around the hotel."
    )
    max_cost_per_activity: float | None = Field(
        default=None,
        description="Upper bound on one activity's cost for the whole party.",
    )
    reasoning: str = Field(
        default="", description="One sentence on why these parameters were chosen."
    )


class ActivityResult(BaseModel):
    """What the activity node writes back into `TravelState`."""

    scheduled: list[Activity] = Field(default_factory=list)
    """One per day — this is what the Budget agent costs."""
    candidates: list[Activity] = Field(default_factory=list)
    raw_count: int = 0
    plan: ActivitySearchPlan = Field(default_factory=ActivitySearchPlan)
    notes: list[str] = Field(default_factory=list)


class ActivityAgent:
    """Search -> filter -> rank -> schedule. No booking."""

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

    def _default_plan(self, request: TravelRequest) -> ActivitySearchPlan:
        per_day = (
            request.budget
            * DEFAULT_ACTIVITY_BUDGET_SHARE
            / max(request.duration_days, 1)
        )
        return ActivitySearchPlan(
            radius_km=5,
            max_cost_per_activity=round(per_day, 2),
            reasoning=(
                "Deterministic plan: capped activities at "
                f"{int(DEFAULT_ACTIVITY_BUDGET_SHARE * 100)}% of the trip budget, "
                "spread across the trip."
            ),
        )

    def plan_search(self, request: TravelRequest) -> ActivitySearchPlan:
        llm = self._get_llm()
        if llm is None:
            return self._default_plan(request)

        prompt = (
            f"Traveller request:\n"
            f"- Destinations: {', '.join(request.destinations)}\n"
            f"- Trip length: {request.duration_days} days\n"
            f"- Travellers: {request.travelers}\n"
            f"- Total trip budget: {request.budget} {request.currency} "
            f"(covers flights, hotels, activities, food and transport)\n"
            f"- Interests: {', '.join(request.interests) or 'none stated'}\n"
            f"- Trip style: {request.trip_style}\n\n"
            f"Choose the activity search parameters."
        )
        try:
            plan = llm.with_structured_output(ActivitySearchPlan).invoke(
                [("system", SYSTEM_PROMPT), ("human", prompt)]
            )
            if isinstance(plan, ActivitySearchPlan):
                return plan
            return ActivitySearchPlan.model_validate(plan)
        except Exception as exc:
            logger.warning("activity search planning failed, using defaults: %s", exc)
            return self._default_plan(request)

    # -- explanation -----------------------------------------------------

    def explain(
        self, request: TravelRequest, scheduled: list[Activity]
    ) -> list[Activity]:
        """Give the whole schedule one readable summary on its first entry."""
        llm = self._get_llm()
        if llm is None or not scheduled:
            return scheduled

        listing = "; ".join(
            f"day {a.recommended_day}: {a.activity} "
            f"({a.estimated_cost:.0f} {a.currency})"
            for a in scheduled[:8]
        )
        prompt = (
            "Explain in one or two sentences why this set of activities suits "
            "the traveller. Use only the facts given; do not add details or "
            "name any activity that is not listed.\n\n"
            f"Interests: {', '.join(request.interests) or 'none stated'}. "
            f"Trip style: {request.trip_style}. "
            f"Travellers: {request.travelers}.\n"
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
                "activity explanation failed, keeping scoring reasons: %s", exc
            )
        return scheduled

    # -- the whole job ---------------------------------------------------

    def run(
        self,
        request: TravelRequest,
        *,
        days_by_destination: dict[str, list[int]],
        hotels: list[HotelOption] | None = None,
    ) -> ActivityResult:
        notes: list[str] = []
        plan = self.plan_search(request)
        if plan.reasoning:
            notes.append(f"activity search plan: {plan.reasoning}")

        candidates: list[Activity] = []
        scheduled: list[Activity] = []
        raw_count = 0

        for destination, days in days_by_destination.items():
            if not days:
                continue

            anchor = resolve_anchor(
                self.client,
                destination,
                hotel_anchor=hotel_anchor_for(destination, hotels),
            )
            payload = self.client.search_activities(
                anchor[0], anchor[1], radius_km=plan.radius_km
            )
            found = normalize_activities(
                payload,
                destination=destination,
                anchor=anchor,
                fallback_currency=request.currency,
            )
            raw_count += len(found)
            if not found:
                notes.append(f"no activities found near {destination}")
                continue

            ranked = rank_activities(
                found,
                interests=request.interests,
                max_cost=plan.max_cost_per_activity,
                top_n=max(len(days), 5),
            )
            candidates.extend(ranked)

            picked = schedule_across_days(ranked, days)
            if len(picked) < len(days):
                notes.append(
                    f"only {len(picked)} activities available for "
                    f"{len(days)} days in {destination}"
                )
            scheduled.extend(picked)

        scheduled.sort(key=lambda a: a.recommended_day or 0)
        scheduled = self.explain(request, scheduled)

        return ActivityResult(
            scheduled=scheduled,
            candidates=candidates,
            raw_count=raw_count,
            plan=plan,
            notes=notes,
        )


__all__ = ["ActivityAgent", "ActivityResult", "ActivitySearchPlan"]
