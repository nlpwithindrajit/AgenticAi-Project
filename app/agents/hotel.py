"""Hotel agent — decides *how* to search, then explains what it picked.

Same split as the Flight agent (projectIdea.md §22): the LLM chooses search
parameters and writes the rationale; `app/tools/hotels.py` does the API work
and the scoring. The agent can only recommend hotels the provider returned.

Amadeus needs three calls per destination — list hotels in the city, price a
subset, and fetch guest ratings — so the agent also decides how many candidates
are worth pricing.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from pydantic import BaseModel, Field

from app.agents.llm import build_llm
from app.models.travel import HotelOption, TravelRequest
from app.tools.amadeus import AmadeusClient, AmadeusError, HotelSearchError
from app.tools.hotels import (
    filter_hotels,
    normalize_hotel_offers,
    parse_hotel_list,
    parse_hotel_ratings,
    rank_hotels,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You plan hotel searches for a travel planner.

You do not search for or invent hotels. You choose the search parameters most \
likely to surface stays matching the traveller's constraints, and you say why.

Budget guidance: hotels typically consume 25-40% of a trip budget across the \
whole stay. Set max_total_price for the whole stay, not per night.

search_radius_km controls how far from the city centre to look. Keep it small \
for travellers who want to be central and larger for budget-led trips, but \
never so small that no hotels are found."""

# Share of the trip budget hotels may take before we filter them out.
DEFAULT_HOTEL_BUDGET_SHARE = 0.40

# How many candidate hotels to price per destination. Each pricing call is
# billable and slow, so this is deliberately bounded.
DEFAULT_CANDIDATES = 20


class HotelSearchPlan(BaseModel):
    """The LLM's decision about how to run this particular search."""

    search_radius_km: int = Field(
        default=15, ge=1, le=50, description="Radius around the city centre."
    )
    max_total_price: float | None = Field(
        default=None,
        description="Upper bound on total accommodation cost for the whole stay.",
    )
    max_distance_km: float | None = Field(
        default=None, description="Reject hotels further than this from the centre."
    )
    min_rating: float | None = Field(
        default=None, ge=0, le=100, description="Minimum guest rating out of 100."
    )
    reasoning: str = Field(
        default="", description="One sentence on why these parameters were chosen."
    )


class HotelResult(BaseModel):
    """What the hotel node writes back into `TravelState`."""

    recommendations: list[HotelOption] = Field(default_factory=list)
    raw_count: int = 0
    plan: HotelSearchPlan = Field(default_factory=HotelSearchPlan)
    notes: list[str] = Field(default_factory=list)


def split_stay(
    destinations: list[str], check_in: date, check_out: date
) -> list[tuple[str, date, date]]:
    """Divide the trip's nights between destinations, in order.

    A 5-night trip over Tokyo + Kyoto becomes Tokyo 3 nights, Kyoto 2 — the
    remainder goes to the earlier destinations rather than being dropped.
    """
    nights = max((check_out - check_in).days, 1)
    count = len(destinations)
    base, remainder = divmod(nights, count)

    stays: list[tuple[str, date, date]] = []
    cursor = check_in
    for index, destination in enumerate(destinations):
        allotted = max(base + (1 if index < remainder else 0), 1)
        end = min(cursor + timedelta(days=allotted), check_out)
        if end <= cursor:
            end = cursor + timedelta(days=1)
        stays.append((destination, cursor, end))
        cursor = end
        if cursor >= check_out and index < count - 1:
            # Fewer nights than destinations — remaining cities get a night each
            # so the traveller is never left without a bed.
            cursor = check_out
    return stays


class HotelAgent:
    """Search -> filter -> rank -> recommend, per destination. No booking."""

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

    def _default_plan(self, request: TravelRequest) -> HotelSearchPlan:
        return HotelSearchPlan(
            search_radius_km=15,
            max_total_price=round(request.budget * DEFAULT_HOTEL_BUDGET_SHARE, 2),
            reasoning=(
                "Deterministic plan: capped accommodation at "
                f"{int(DEFAULT_HOTEL_BUDGET_SHARE * 100)}% of the trip budget."
            ),
        )

    def plan_search(self, request: TravelRequest) -> HotelSearchPlan:
        llm = self._get_llm()
        if llm is None:
            return self._default_plan(request)

        prompt = (
            f"Traveller request:\n"
            f"- Destinations: {', '.join(request.destinations)}\n"
            f"- Dates: {request.departure_date} to {request.return_date} "
            f"({request.nights} nights)\n"
            f"- Travellers: {request.travelers}\n"
            f"- Total trip budget: {request.budget} {request.currency} "
            f"(covers flights, hotels, activities, food and transport)\n"
            f"- Preferred hotel stars: {request.hotel_stars or 'no preference'}\n"
            f"- Interests: {', '.join(request.interests) or 'none stated'}\n"
            f"- Trip style: {request.trip_style}\n\n"
            f"Choose the hotel search parameters."
        )
        try:
            plan = self._get_llm().with_structured_output(HotelSearchPlan).invoke(
                [("system", SYSTEM_PROMPT), ("human", prompt)]
            )
            if isinstance(plan, HotelSearchPlan):
                return plan
            return HotelSearchPlan.model_validate(plan)
        except Exception as exc:
            logger.warning("hotel search planning failed, using defaults: %s", exc)
            return self._default_plan(request)

    # -- explanation -----------------------------------------------------

    def explain(
        self, request: TravelRequest, recommendations: list[HotelOption]
    ) -> list[HotelOption]:
        """Give the best hotel per destination a readable rationale."""
        llm = self._get_llm()
        if llm is None or not recommendations:
            return recommendations

        best_per_destination: dict[str, int] = {}
        for index, hotel in enumerate(recommendations):
            best_per_destination.setdefault(hotel.destination, index)

        updated = list(recommendations)
        for index in best_per_destination.values():
            hotel = updated[index]
            prompt = (
                "Explain in one or two sentences why this hotel is the best "
                "match. Use only the facts given; do not add details.\n\n"
                f"Traveller: {request.travelers} people, "
                f"budget {request.budget} {request.currency}, "
                f"stars wanted: {request.hotel_stars or 'no preference'}, "
                f"interests: {', '.join(request.interests) or 'none'}.\n"
                f"Chosen in {hotel.destination}: {hotel.name}, "
                f"{hotel.total_price} {hotel.currency} for {hotel.nights} "
                f"night(s), {hotel.distance_km} km from centre, "
                f"guest rating {hotel.rating}, "
                f"amenities mentioned: {', '.join(hotel.amenities) or 'none'}, "
                f"score {hotel.score}/100."
            )
            try:
                response = llm.invoke(
                    [("system", SYSTEM_PROMPT), ("human", prompt)]
                )
                text = getattr(response, "content", None)
                if isinstance(text, str) and text.strip():
                    updated[index] = hotel.model_copy(
                        update={"rationale": text.strip()}
                    )
            except Exception as exc:
                logger.warning(
                    "hotel explanation failed, keeping scoring reasons: %s", exc
                )
                break

        return updated

    # -- the whole job ---------------------------------------------------

    def _search_destination(
        self,
        request: TravelRequest,
        plan: HotelSearchPlan,
        destination: str,
        check_in: date,
        check_out: date,
        notes: list[str],
    ) -> list[HotelOption]:
        try:
            city_code = self.client.resolve_location_code(destination)
        except AmadeusError as exc:
            # Shared reference-data lookups raise the base error; the graph
            # catches HotelSearchError, so convert rather than escaping it.
            raise HotelSearchError(
                f"could not resolve {destination!r}: {exc}"
            ) from exc

        listing = self.client.list_hotels_by_city(
            city_code,
            radius_km=plan.search_radius_km,
            ratings=[request.hotel_stars] if request.hotel_stars else None,
        )
        hotels = parse_hotel_list(listing)
        if not hotels and request.hotel_stars:
            # The star filter emptied the city — relax it rather than give up,
            # and stop claiming the results match the requested band.
            notes.append(
                f"no {request.hotel_stars}-star hotels found in {destination}; "
                "searched without the star filter"
            )
            hotels = parse_hotel_list(
                self.client.list_hotels_by_city(
                    city_code, radius_km=plan.search_radius_km
                )
            )
            stars_confirmed = False
        else:
            stars_confirmed = bool(request.hotel_stars)

        if not hotels:
            notes.append(f"no hotels listed in {destination}")
            return []

        hotel_ids = list(hotels)[:DEFAULT_CANDIDATES]

        ratings: dict[str, float] = {}
        try:
            ratings = parse_hotel_ratings(self.client.hotel_ratings(hotel_ids))
        except HotelSearchError as exc:
            # Ratings are a bonus signal; losing them must not lose the search.
            notes.append(f"guest ratings unavailable for {destination} ({exc})")

        offers = self.client.search_hotel_offers(
            hotel_ids,
            check_in=check_in,
            check_out=check_out,
            adults=request.travelers,
            rooms=1,
            currency=request.currency,
        )

        centre = _city_centre(hotels)
        return normalize_hotel_offers(
            offers,
            destination=destination,
            geo_by_hotel={
                hid: (info["latitude"], info["longitude"])
                for hid, info in hotels.items()
                if info.get("latitude") is not None
                and info.get("longitude") is not None
            },
            ratings_by_hotel=ratings,
            requested_stars=request.hotel_stars if stars_confirmed else None,
            city_center=centre,
        )

    def run(
        self, request: TravelRequest, *, cost_pressure: float = 1.0
    ) -> HotelResult:
        """Search every destination, rank per destination, then explain.

        `cost_pressure` below 1.0 tightens the price cap so a budget replan
        searches cheaper rather than repeating the same search.
        """
        notes: list[str] = []
        plan = self.plan_search(request)
        if cost_pressure < 1.0 and plan.max_total_price is not None:
            plan = plan.model_copy(
                update={
                    "max_total_price": round(
                        plan.max_total_price * cost_pressure, 2
                    )
                }
            )
            notes.append(
                f"budget pressure: hotel cap tightened to "
                f"{plan.max_total_price:,.0f} {request.currency}"
            )
        if plan.reasoning:
            notes.append(f"hotel search plan: {plan.reasoning}")

        stays = split_stay(
            request.destinations, request.departure_date, request.return_date
        )
        # Budget applies to the whole stay; each destination gets its share.
        per_destination_cap = (
            plan.max_total_price / len(stays) if plan.max_total_price else None
        )

        recommendations: list[HotelOption] = []
        raw_count = 0

        for destination, check_in, check_out in stays:
            found = self._search_destination(
                request, plan, destination, check_in, check_out, notes
            )
            raw_count += len(found)
            if not found:
                notes.append(f"no bookable hotel offers in {destination}")
                continue

            ranked = rank_hotels(
                filter_hotels(
                    found,
                    max_total_price=per_destination_cap,
                    max_distance_km=plan.max_distance_km,
                    min_rating=plan.min_rating,
                ),
                interests=request.interests,
                requested_stars=request.hotel_stars,
                top_n=3,
            )
            recommendations.extend(ranked)

        recommendations = self.explain(request, recommendations)
        return HotelResult(
            recommendations=recommendations,
            raw_count=raw_count,
            plan=plan,
            notes=notes,
        )


def _city_centre(
    hotels: dict[str, dict[str, object]],
) -> tuple[float, float] | None:
    """Use the mean hotel position as the centre reference.

    Amadeus gives no city-centre coordinate, and hotel density clusters
    centrally, so this is a workable proxy — distances are comparative within
    a city, not absolute geography.
    """
    points = [
        (float(info["latitude"]), float(info["longitude"]))
        for info in hotels.values()
        if info.get("latitude") is not None and info.get("longitude") is not None
    ]
    if not points:
        return None
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


__all__ = ["HotelAgent", "HotelResult", "HotelSearchPlan", "split_stay"]
