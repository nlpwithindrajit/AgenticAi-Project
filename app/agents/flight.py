"""Flight agent — decides *how* to search, then explains what it picked.

The split follows projectIdea.md §22: the LLM chooses the search strategy and
writes the traveller-facing rationale; every API call, filter, and score is
deterministic Python in `app/tools/flights.py`. The agent never invents a
flight — its recommendations can only come from provider results.

The LLM is optional. With no `ANTHROPIC_API_KEY` the agent falls back to a
deterministic search plan and a rule-based explanation, so the graph still runs
end to end on Amadeus credentials alone.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.config import get_settings
from app.models.travel import FlightOption, TravelRequest
from app.tools.flights import (
    AmadeusClient,
    FlightSearchError,
    filter_flights,
    normalize_offers,
    rank_flights,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You plan flight searches for a travel planner.

You do not search for or invent flights. You choose the search parameters that \
are most likely to surface options matching the traveller's constraints, and \
you say why.

Budget guidance: flights typically consume 30-45% of a trip budget. Set \
max_price accordingly, or leave it null when the budget is generous.

Set non_stop only when the traveller asked for direct flights. Prefer leaving \
it false on long-haul routes where non-stop service may not exist at all — an \
over-narrow search returns nothing, which is worse than a one-stop option."""


class SearchPlan(BaseModel):
    """The LLM's decision about how to run this particular search."""

    non_stop: bool = Field(
        default=False, description="Restrict the provider search to non-stop flights."
    )
    max_results: int = Field(
        default=20, ge=1, le=50, description="How many offers to request."
    )
    max_price: float | None = Field(
        default=None,
        description="Upper bound on total flight cost, in the request currency.",
    )
    reasoning: str = Field(
        default="", description="One sentence on why these parameters were chosen."
    )


# Share of the total trip budget flights may take before we filter them out.
DEFAULT_FLIGHT_BUDGET_SHARE = 0.45


class FlightResult(BaseModel):
    """What the flight node writes back into `TravelState`."""

    recommendations: list[FlightOption] = Field(default_factory=list)
    raw_count: int = 0
    plan: SearchPlan = Field(default_factory=SearchPlan)
    notes: list[str] = Field(default_factory=list)


class FlightAgent:
    """Search -> filter -> rank -> recommend. No booking."""

    def __init__(
        self,
        client: AmadeusClient | None = None,
        llm: object | None = None,
    ) -> None:
        self._client = client
        self._llm = llm

    # -- lazily-built collaborators --------------------------------------

    @property
    def client(self) -> AmadeusClient:
        if self._client is None:
            self._client = AmadeusClient()
        return self._client

    def _get_llm(self):
        """Build the chat model on first use; None when unconfigured."""
        if self._llm is not None:
            return self._llm

        settings = get_settings()
        if not settings.llm_enabled:
            return None

        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:  # pragma: no cover - depends on optional install
            logger.warning(
                "ANTHROPIC_API_KEY is set but langchain-anthropic is missing"
            )
            return None

        # No temperature/top_p: those parameters are rejected outright by the
        # current Claude models, so the request must simply omit them.
        self._llm = ChatAnthropic(
            model=settings.llm_model,
            api_key=settings.anthropic_api_key,
            max_tokens=2048,
        )
        return self._llm

    # -- planning --------------------------------------------------------

    def _default_plan(self, request: TravelRequest) -> SearchPlan:
        return SearchPlan(
            non_stop=request.direct_flights_only,
            max_results=20,
            max_price=round(request.budget * DEFAULT_FLIGHT_BUDGET_SHARE, 2),
            reasoning=(
                "Deterministic plan: capped flights at "
                f"{int(DEFAULT_FLIGHT_BUDGET_SHARE * 100)}% of the trip budget."
            ),
        )

    def plan_search(self, request: TravelRequest) -> SearchPlan:
        """Ask the LLM how to search; fall back to the deterministic plan."""
        llm = self._get_llm()
        if llm is None:
            return self._default_plan(request)

        prompt = (
            f"Traveller request:\n"
            f"- Route: {request.origin} to {', '.join(request.destinations)}\n"
            f"- Dates: {request.departure_date} to {request.return_date} "
            f"({request.duration_days} days)\n"
            f"- Travellers: {request.travelers}\n"
            f"- Total trip budget: {request.budget} {request.currency} "
            f"(covers flights, hotels, activities, food and transport)\n"
            f"- Direct flights requested: {request.direct_flights_only}\n"
            f"- Preferred airline: {request.preferred_airline or 'none'}\n"
            f"- Trip style: {request.trip_style}\n\n"
            f"Choose the flight search parameters."
        )
        try:
            structured = llm.with_structured_output(SearchPlan)
            plan = structured.invoke(
                [("system", SYSTEM_PROMPT), ("human", prompt)]
            )
            if isinstance(plan, SearchPlan):
                return plan
            return SearchPlan.model_validate(plan)
        except Exception as exc:
            logger.warning("flight search planning failed, using defaults: %s", exc)
            return self._default_plan(request)

    # -- explanation -----------------------------------------------------

    def explain(
        self, request: TravelRequest, recommendations: list[FlightOption]
    ) -> list[FlightOption]:
        """Replace the top option's scoring reasons with a readable sentence."""
        if not recommendations:
            return recommendations

        llm = self._get_llm()
        if llm is None:
            return recommendations

        best = recommendations[0]
        prompt = (
            "Explain in one or two sentences why this flight is the best match. "
            "Use only the facts given; do not add details.\n\n"
            f"Traveller: {request.travelers} from {request.origin}, "
            f"budget {request.budget} {request.currency}, "
            f"direct preferred: {request.direct_flights_only}.\n"
            f"Chosen: {best.airline_name or best.airline}, "
            f"{best.price} {best.currency} total, {best.stops} stop(s), "
            f"{best.total_duration_minutes} minutes total travel, "
            f"score {best.score}/100.\n"
            f"It was chosen from {len(recommendations)} ranked options."
        )
        try:
            response = llm.invoke([("system", SYSTEM_PROMPT), ("human", prompt)])
            text = getattr(response, "content", None)
            if isinstance(text, str) and text.strip():
                updated = best.model_copy(update={"rationale": text.strip()})
                return [updated, *recommendations[1:]]
        except Exception as exc:
            logger.warning(
                "flight explanation failed, keeping scoring reasons: %s", exc
            )

        return recommendations

    # -- the whole job ---------------------------------------------------

    def run(self, request: TravelRequest) -> FlightResult:
        """Search, filter, rank and explain. Raises FlightSearchError upward."""
        notes: list[str] = []
        plan = self.plan_search(request)
        if plan.reasoning:
            notes.append(f"search plan: {plan.reasoning}")

        outbound_to = request.destinations[0]
        if request.destinations[-1] != outbound_to:
            # MVP scope: one round trip, priced on the first destination.
            notes.append(
                f"multi-city routing is out of MVP scope; priced as a round trip "
                f"to {outbound_to} rather than returning from "
                f"{request.destinations[-1]}"
            )

        payload = self.client.search_flights(
            origin=request.origin,
            destination=outbound_to,
            departure_date=request.departure_date,
            return_date=request.return_date,
            adults=request.travelers,
            currency=request.currency,
            non_stop=plan.non_stop,
            max_results=plan.max_results,
        )

        offers = normalize_offers(payload, travelers=request.travelers)
        if not offers:
            notes.append("provider returned no usable flight offers")
            return FlightResult(plan=plan, notes=notes)

        filtered = filter_flights(
            offers,
            max_price=plan.max_price,
            direct_only=plan.non_stop or request.direct_flights_only,
            preferred_airline=request.preferred_airline,
        )
        if request.direct_flights_only and all(f.stops > 0 for f in filtered):
            notes.append("no non-stop flights available; showing connecting options")

        ranked = rank_flights(
            filtered, preferred_airline=request.preferred_airline, top_n=5
        )
        ranked = self.explain(request, ranked)

        return FlightResult(
            recommendations=ranked,
            raw_count=len(offers),
            plan=plan,
            notes=notes,
        )


__all__ = ["FlightAgent", "FlightResult", "SearchPlan", "FlightSearchError"]
