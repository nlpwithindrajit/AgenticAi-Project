"""Flight agent — decides *how* to search, then explains what it picked.

The split follows projectIdea.md §22: the LLM chooses the search strategy and
writes the traveller-facing rationale; every API call, filter, and score is
deterministic Python in `app/tools/flights.py`. The agent never invents a
flight — its recommendations can only come from provider results.

Two providers sit behind the same search step. SerpAPI (Google Flights) is the
default because it returns live fares; Amadeus remains selectable via
`FLIGHT_PROVIDER`. They differ in one way that reaches up into this file:
SerpAPI prices a round trip in two calls, the second keyed by a token from the
first, so completing an itinerary costs a billable search per candidate. That
is why only the top few candidates get their return leg resolved.

The LLM is optional. With no LLM key the agent falls back to a deterministic
search plan and a rule-based explanation, so the graph still runs end to end on
provider credentials alone.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.llm import build_llm
from app.config import get_settings
from app.models.travel import FlightOption, TravelRequest
from app.tools.flights import (
    AmadeusClient,
    FlightSearchError,
    SerpApiClient,
    filter_flights,
    flight_number_id,
    normalize_offers,
    normalize_serpapi_offers,
    rank_flights,
    serpapi_itineraries,
)

logger = logging.getLogger(__name__)

FlightProvider = Literal["serpapi", "amadeus"]

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
        client: AmadeusClient | SerpApiClient | None = None,
        llm: object | None = None,
        provider: FlightProvider | None = None,
    ) -> None:
        self._client = client
        self._llm = llm
        self._provider = provider

    # -- lazily-built collaborators --------------------------------------

    @property
    def provider(self) -> FlightProvider:
        """Which provider this agent searches with.

        An injected client settles it — a caller who handed us a stub SerpAPI
        client wants the SerpAPI path regardless of what the environment says,
        which is what makes the agent testable without credentials.
        """
        if self._provider is None:
            if self._client is not None:
                self._provider = (
                    "serpapi"
                    if isinstance(self._client, SerpApiClient)
                    else "amadeus"
                )
            else:
                self._provider = get_settings().active_flight_provider or "amadeus"
        return self._provider

    @property
    def client(self) -> AmadeusClient | SerpApiClient:
        if self._client is None:
            self._client = (
                SerpApiClient() if self.provider == "serpapi" else AmadeusClient()
            )
        return self._client

    def _get_llm(self):
        self._llm = build_llm(self._llm)
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

    # -- searching -------------------------------------------------------

    def _search_amadeus(
        self, request: TravelRequest, plan: SearchPlan, destination: str
    ) -> list[FlightOption]:
        payload = self.client.search_flights(
            origin=request.origin,
            destination=destination,
            departure_date=request.departure_date,
            return_date=request.return_date,
            adults=request.travelers,
            currency=request.currency,
            non_stop=plan.non_stop,
            max_results=plan.max_results,
        )
        return normalize_offers(payload, travelers=request.travelers)

    def _search_serpapi(
        self, request: TravelRequest, plan: SearchPlan, destination: str
    ) -> list[FlightOption]:
        """Outbound itineraries, priced at the full round-trip fare.

        `plan.max_results` has no counterpart here — Google returns the set it
        considers relevant. The cap still matters on the Amadeus path, so the
        field stays rather than being special-cased away.
        """
        payload = self.client.search_flights(
            origin=request.origin,
            destination=destination,
            departure_date=request.departure_date,
            return_date=request.return_date,
            adults=request.travelers,
            currency=request.currency,
            non_stop=plan.non_stop,
        )
        return normalize_serpapi_offers(
            payload, travelers=request.travelers, currency=request.currency
        )

    def _with_return_leg(
        self,
        request: TravelRequest,
        destination: str,
        option: FlightOption,
        plan: SearchPlan,
        notes: list[str],
    ) -> FlightOption:
        """Resolve one option's return leg. Costs a billable provider search.

        A failure degrades the option to outbound-only rather than dropping it:
        the round-trip price is already correct, so a half-detailed real flight
        still beats losing it.
        """
        token = option.offer_id
        label = flight_number_id(option) or option.airline

        def outbound_only(reason: str | None = None) -> FlightOption:
            if reason:
                notes.append(f"return leg for {label} not priced: {reason}")
            return option.model_copy(update={"offer_id": flight_number_id(option)})

        if not token:
            return outbound_only("provider gave no continuation token")

        try:
            payload = self.client.search_return_legs(
                origin=request.origin,
                destination=destination,
                departure_date=request.departure_date,
                return_date=request.return_date,
                departure_token=token,
                adults=request.travelers,
                currency=request.currency,
                non_stop=plan.non_stop,
            )
        except FlightSearchError as exc:
            logger.warning("return-leg lookup failed for %s: %s", label, exc)
            return outbound_only(str(exc))

        candidates = serpapi_itineraries(payload)
        if not candidates:
            return outbound_only("provider offered no return flights")

        # Cheapest, then quickest. The price quoted here is for this specific
        # outbound+inbound pair, so it supersedes the outbound-only estimate.
        best = min(
            candidates,
            key=lambda it: (it.price, it.slice.duration_minutes or 10**6),
        )
        completed = option.model_copy(
            update={
                "inbound": best.slice,
                "price": best.price,
                "price_per_traveler": (
                    round(best.price / request.travelers, 2)
                    if request.travelers > 0
                    else None
                ),
            }
        )
        # Recomputed after the return leg is attached so the id names both.
        return completed.model_copy(
            update={"offer_id": flight_number_id(completed)}
        )

    def _complete_round_trips(
        self,
        request: TravelRequest,
        destination: str,
        offers: list[FlightOption],
        plan: SearchPlan,
        notes: list[str],
    ) -> list[FlightOption]:
        """Shortlist, then buy the return-leg detail for the shortlist only.

        The ordering is the point. Candidates are ranked while every one of
        them is outbound-only — a fair comparison, since they share the same
        missing half and already carry the true round-trip price. Only then are
        the survivors completed and re-ranked against each other. Ranking a
        mixed set would hand a one-way duration to whichever option happened to
        be enriched last.
        """
        lookups = get_settings().serpapi_return_lookups

        if lookups == 0:
            shortlist = rank_flights(
                offers, preferred_airline=request.preferred_airline, top_n=5
            )
            notes.append(
                "return legs not priced (SERPAPI_RETURN_LOOKUPS=0): options show "
                "the outbound leg only, at the full round-trip price"
            )
            return [
                o.model_copy(update={"offer_id": flight_number_id(o)})
                for o in shortlist
            ]

        shortlist = rank_flights(
            offers, preferred_airline=request.preferred_airline, top_n=lookups
        )
        completed = [
            self._with_return_leg(request, destination, option, plan, notes)
            for option in shortlist
        ]

        whole = [o for o in completed if o.inbound is not None]
        partial = [o for o in completed if o.inbound is None]

        if whole:
            notes.append(
                f"priced complete round trips for the top {len(whole)} of "
                f"{len(offers)} candidates that passed filtering "
                f"({len(shortlist)} extra provider searches)"
            )

        # Complete itineraries rank among themselves and always come first:
        # a partial one has no return duration, so mixing them in would let
        # missing data score as speed.
        ranked = rank_flights(
            whole, preferred_airline=request.preferred_airline, top_n=len(whole)
        )
        if partial:
            ranked += rank_flights(
                partial,
                preferred_airline=request.preferred_airline,
                top_n=len(partial),
            )
        return ranked

    # -- the whole job ---------------------------------------------------

    def run(
        self, request: TravelRequest, *, cost_pressure: float = 1.0
    ) -> FlightResult:
        """Search, filter, rank and explain. Raises FlightSearchError upward.

        `cost_pressure` below 1.0 tightens the price cap — that is how a
        budget replan actually searches cheaper instead of repeating itself.
        """
        notes: list[str] = []
        plan = self.plan_search(request)
        if cost_pressure < 1.0 and plan.max_price is not None:
            plan = plan.model_copy(
                update={"max_price": round(plan.max_price * cost_pressure, 2)}
            )
            notes.append(
                f"budget pressure: flight cap tightened to {plan.max_price:,.0f} "
                f"{request.currency}"
            )
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

        if self.provider == "serpapi":
            offers = self._search_serpapi(request, plan, outbound_to)
        else:
            offers = self._search_amadeus(request, plan, outbound_to)

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

        if self.provider == "serpapi":
            ranked = self._complete_round_trips(
                request, outbound_to, filtered, plan, notes
            )
        else:
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


__all__ = [
    "FlightAgent",
    "FlightProvider",
    "FlightResult",
    "FlightSearchError",
    "SearchPlan",
]
