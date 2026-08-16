"""Flight search: normalisation, filtering and explainable ranking.

Transport lives in `app/tools/amadeus.py` and `app/tools/serpapi.py`; this
module turns whichever provider's raw JSON into `FlightOption` objects and
scores them. Filtering and ranking are provider-agnostic on purpose — once an
offer is normalised, nothing downstream should care where it came from. No LLM
calls belong here; the Flight agent makes the judgement calls.
"""

from __future__ import annotations

import logging
import re
from typing import Any, NamedTuple

from app.models.travel import FlightOption, FlightSegment, FlightSlice
from app.tools.amadeus import AmadeusClient, AmadeusError, FlightSearchError
from app.tools.serpapi import SerpApiClient, SerpApiError

logger = logging.getLogger(__name__)

# ISO-8601 durations as Amadeus emits them: "PT14H15M", "PT7H", "PT45M".
_DURATION_RE = re.compile(r"^P(?:(?P<days>\d+)D)?T?(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?$")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_iso_duration(value: str | None) -> int | None:
    """"PT14H15M" -> 855 minutes. Returns None for anything unparseable."""
    if not value:
        return None
    match = _DURATION_RE.match(value.strip())
    if match is None:
        logger.warning("unparseable ISO-8601 duration from provider: %r", value)
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    total = days * 24 * 60 + hours * 60 + minutes
    return total or None


def _segment_from_amadeus(
    raw: dict[str, Any], carriers: dict[str, str]
) -> FlightSegment:
    departure = raw.get("departure") or {}
    arrival = raw.get("arrival") or {}
    carrier_code = raw.get("carrierCode", "")
    return FlightSegment(
        carrier_code=carrier_code,
        carrier_name=carriers.get(carrier_code),
        flight_number=(
            f"{carrier_code}{raw['number']}" if raw.get("number") else None
        ),
        aircraft=(raw.get("aircraft") or {}).get("code"),
        origin=departure.get("iataCode", ""),
        destination=arrival.get("iataCode", ""),
        departure_at=departure.get("at", ""),
        arrival_at=arrival.get("at", ""),
        duration_minutes=parse_iso_duration(raw.get("duration")),
    )


def _slice_from_amadeus(
    itinerary: dict[str, Any], carriers: dict[str, str]
) -> FlightSlice | None:
    segments = [
        _segment_from_amadeus(seg, carriers) for seg in itinerary.get("segments", [])
    ]
    if not segments:
        return None
    duration = parse_iso_duration(itinerary.get("duration"))
    if duration is None:
        # Fall back to summing the legs; connection waits are then excluded,
        # which is a known under-estimate rather than a silent zero.
        leg_totals = [s.duration_minutes for s in segments if s.duration_minutes]
        duration = sum(leg_totals) if leg_totals else None
    return FlightSlice(
        origin=segments[0].origin,
        destination=segments[-1].destination,
        departure_at=segments[0].departure_at,
        arrival_at=segments[-1].arrival_at,
        duration_minutes=duration,
        segments=segments,
    )


def normalize_offers(
    payload: dict[str, Any], travelers: int = 1
) -> list[FlightOption]:
    """Turn a raw Amadeus flight-offers payload into `FlightOption` objects.

    A round-trip offer carries two itineraries — outbound then return — under a
    single price, so it becomes one `FlightOption`, not two.
    """
    carriers: dict[str, str] = (payload.get("dictionaries") or {}).get("carriers", {})
    options: list[FlightOption] = []

    for offer in payload.get("data", []):
        itineraries = offer.get("itineraries") or []
        slices = [
            s
            for s in (_slice_from_amadeus(i, carriers) for i in itineraries)
            if s
        ]
        if not slices:
            continue

        price_block = offer.get("price") or {}
        raw_total = price_block.get("grandTotal") or price_block.get("total")
        try:
            total = float(raw_total)
        except (TypeError, ValueError):
            logger.warning(
                "skipping offer %s: unusable price %r", offer.get("id"), raw_total
            )
            continue

        validating = offer.get("validatingAirlineCodes") or []
        airline = validating[0] if validating else slices[0].segments[0].carrier_code

        options.append(
            FlightOption(
                offer_id=str(offer.get("id")) if offer.get("id") is not None else None,
                airline=airline,
                airline_name=carriers.get(airline),
                outbound=slices[0],
                inbound=slices[1] if len(slices) > 1 else None,
                price=total,
                price_per_traveler=(
                    round(total / travelers, 2) if travelers > 0 else None
                ),
                currency=price_block.get("currency", "EUR"),
                source="amadeus",
            )
        )

    return options


# ---------------------------------------------------------------------------
# SerpAPI (Google Flights) parsing
# ---------------------------------------------------------------------------


def _iso_timestamp(value: str | None) -> str:
    """"2026-10-10 08:10" -> "2026-10-10T08:10".

    Google Flights separates date and time with a space. Everything
    downstream — `_departure_hour`, the itinerary agent, the UI — reads these
    as ISO-8601, so the space is normalised once, here, rather than each
    reader having to know which provider produced the string.
    """
    if not value:
        return ""
    return value.strip().replace(" ", "T", 1)


def _carrier_code(flight_number: str | None) -> str:
    """"6E 1451" -> "6E". Google gives the airline name, not its IATA code."""
    if not flight_number:
        return ""
    return flight_number.strip().split()[0].upper()


def _segment_from_serpapi(raw: dict[str, Any]) -> FlightSegment:
    departure = raw.get("departure_airport") or {}
    arrival = raw.get("arrival_airport") or {}
    number = raw.get("flight_number")
    duration = raw.get("duration")
    return FlightSegment(
        carrier_code=_carrier_code(number),
        carrier_name=raw.get("airline"),
        flight_number=number.replace(" ", "") if number else None,
        aircraft=raw.get("airplane"),
        origin=departure.get("id", ""),
        destination=arrival.get("id", ""),
        departure_at=_iso_timestamp(departure.get("time")),
        arrival_at=_iso_timestamp(arrival.get("time")),
        duration_minutes=duration if isinstance(duration, int) else None,
    )


class SerpApiItinerary(NamedTuple):
    """One direction of travel, at the total price of the trip it belongs to.

    `price` is *not* this leg's fare: Google quotes the whole round trip
    against whichever outbound option you are looking at, for all passengers
    together. Keeping the two side by side is what lets the agent swap in a
    return leg and take its price as the new total.
    """

    slice: FlightSlice
    price: float
    token: str | None


def serpapi_itineraries(payload: dict[str, Any]) -> list[SerpApiItinerary]:
    """Pull every itinerary out of a Google Flights payload, in Google's order.

    `best_flights` comes first because Google has already judged those the
    strongest matches; `other_flights` is the long tail. Ranking re-sorts them
    anyway, but a stable, meaningful order matters when the caller only takes
    the first few.
    """
    results: list[SerpApiItinerary] = []

    for option in (payload.get("best_flights") or []) + (
        payload.get("other_flights") or []
    ):
        segments = [_segment_from_serpapi(f) for f in option.get("flights") or []]
        if not segments:
            continue

        raw_price = option.get("price")
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            # Google omits the price on itineraries it cannot currently sell.
            # A guessed price would flow straight into the budget, so skip it.
            logger.warning(
                "skipping SerpAPI itinerary with unusable price %r", raw_price
            )
            continue

        total_duration = option.get("total_duration")
        if not isinstance(total_duration, int):
            legs = [s.duration_minutes for s in segments if s.duration_minutes]
            layovers = [
                lay.get("duration")
                for lay in option.get("layovers") or []
                if isinstance(lay.get("duration"), int)
            ]
            total_duration = sum(legs) + sum(layovers) if legs else None

        results.append(
            SerpApiItinerary(
                slice=FlightSlice(
                    origin=segments[0].origin,
                    destination=segments[-1].destination,
                    departure_at=segments[0].departure_at,
                    arrival_at=segments[-1].arrival_at,
                    duration_minutes=total_duration,
                    segments=segments,
                ),
                price=price,
                token=option.get("departure_token") or option.get("booking_token"),
            )
        )

    return results


def _airline_identity(slice_: FlightSlice) -> tuple[str, str | None]:
    """(code, display name) for a journey that may change carrier mid-way."""
    codes = [s.carrier_code for s in slice_.segments if s.carrier_code]
    names = [s.carrier_name for s in slice_.segments if s.carrier_name]
    code = codes[0] if codes else ""
    if len(set(names)) > 1:
        # Google labels these "Multiple airlines"; naming only the first one
        # would misrepresent an itinerary the traveller has to change carrier on.
        return code, "Multiple airlines"
    return code, names[0] if names else None


def normalize_serpapi_offers(
    payload: dict[str, Any],
    travelers: int = 1,
    currency: str = "INR",
) -> list[FlightOption]:
    """Turn a Google Flights payload into `FlightOption` objects.

    Each option covers the *outbound* direction only; `inbound` stays None
    until the agent resolves a return leg with `departure_token`. The price is
    already the round-trip total, so an un-resolved option costs the budget
    correctly even though its duration counts one direction.

    `offer_id` carries the provider token, which the agent needs for that
    second call and replaces with a readable id before the option is returned.
    """
    resolved_currency = (
        (payload.get("search_parameters") or {}).get("currency") or currency
    )

    options: list[FlightOption] = []
    for itinerary in serpapi_itineraries(payload):
        code, name = _airline_identity(itinerary.slice)
        options.append(
            FlightOption(
                offer_id=itinerary.token,
                airline=code,
                airline_name=name,
                outbound=itinerary.slice,
                price=itinerary.price,
                price_per_traveler=(
                    round(itinerary.price / travelers, 2) if travelers > 0 else None
                ),
                currency=resolved_currency,
                source="serpapi",
            )
        )

    return options


def flight_number_id(option: FlightOption) -> str | None:
    """A short, human-readable id: "6E1451-6E1452".

    Replaces the provider token in the returned option — the token is a
    300-character blob that would otherwise end up in every API response and
    every trace for no reader's benefit.
    """
    slices = [option.outbound] + ([option.inbound] if option.inbound else [])
    numbers = [
        segment.flight_number
        for flight_slice in slices
        for segment in flight_slice.segments
        if segment.flight_number
    ]
    return "-".join(numbers) if numbers else None


# ---------------------------------------------------------------------------
# Filtering and ranking — explainable, deterministic
# ---------------------------------------------------------------------------

# Why a flight scores what it does. Kept explicit so the agent can explain it.
RANK_WEIGHTS = {
    "price": 0.40,
    "duration": 0.25,
    "stops": 0.20,
    "departure_time": 0.10,
    "preferred_airline": 0.05,
}


def filter_flights(
    flights: list[FlightOption],
    *,
    max_price: float | None = None,
    direct_only: bool = False,
    preferred_airline: str | None = None,
) -> list[FlightOption]:
    """Drop offers that violate hard constraints.

    Constraints are relaxed rather than returning nothing: if `direct_only`
    eliminates every option, the non-stop preference is dropped and recorded by
    the caller instead of failing the trip.
    """
    results = list(flights)

    if max_price is not None:
        within = [f for f in results if f.price <= max_price]
        if within:
            results = within

    if direct_only:
        nonstop = [f for f in results if f.stops == 0]
        if nonstop:
            results = nonstop

    if preferred_airline:
        wanted = preferred_airline.strip().upper()
        matching = [f for f in results if f.airline.upper() == wanted]
        if matching:
            results = matching

    return results


def _normalize(value: float, best: float, worst: float) -> float:
    """Map value onto 0..1 where `best` scores 1.0. Lower input is better."""
    if worst <= best:
        return 1.0
    return max(0.0, min(1.0, (worst - value) / (worst - best)))


def _departure_hour(flight: FlightOption) -> int | None:
    stamp = flight.outbound.departure_at
    # Amadeus emits local ISO timestamps like "2026-10-10T09:15:00".
    if "T" in stamp:
        try:
            return int(stamp.split("T", 1)[1][:2])
        except ValueError:
            return None
    return None


def rank_flights(
    flights: list[FlightOption],
    *,
    preferred_airline: str | None = None,
    top_n: int = 5,
) -> list[FlightOption]:
    """Score every offer 0-100 and return the best `top_n`, with reasons.

    Weights: price 40%, duration 25%, stops 20%, departure time 10%,
    preferred airline 5%. Scoring is relative to the candidate set, so a score
    means "best of what was available", not an absolute quality rating.
    """
    if not flights:
        return []

    prices = [f.price for f in flights]
    durations = [f.total_duration_minutes or 0 for f in flights]
    cheapest, dearest = min(prices), max(prices)
    quickest, slowest = min(durations), max(durations)
    wanted_airline = preferred_airline.strip().upper() if preferred_airline else None

    scored: list[FlightOption] = []
    for flight in flights:
        price_score = _normalize(flight.price, cheapest, dearest)
        duration_score = _normalize(
            flight.total_duration_minutes or 0, quickest, slowest
        )
        stop_score = _normalize(float(flight.stops), 0.0, 2.0)

        hour = _departure_hour(flight)
        # Civilised departures (06:00-20:00) score full marks.
        time_score = 1.0 if hour is None or 6 <= hour <= 20 else 0.4

        airline_score = (
            1.0 if wanted_airline and flight.airline.upper() == wanted_airline else 0.0
        )

        total = (
            RANK_WEIGHTS["price"] * price_score
            + RANK_WEIGHTS["duration"] * duration_score
            + RANK_WEIGHTS["stops"] * stop_score
            + RANK_WEIGHTS["departure_time"] * time_score
            + RANK_WEIGHTS["preferred_airline"] * airline_score
        )

        reasons = [
            f"price {flight.price:.0f} {flight.currency}",
            f"{flight.stops} stop(s)",
        ]
        if flight.total_duration_minutes:
            hours, mins = divmod(flight.total_duration_minutes, 60)
            reasons.append(f"{hours}h{mins:02d}m total travel")
        if airline_score:
            reasons.append(f"preferred airline {flight.airline}")

        scored.append(
            flight.model_copy(
                update={
                    "score": round(total * 100, 1),
                    "rationale": "; ".join(reasons),
                }
            )
        )

    scored.sort(key=lambda f: (-f.score, f.price))
    return scored[:top_n]


__all__ = [
    "AmadeusClient",
    "AmadeusError",
    "FlightSearchError",
    "RANK_WEIGHTS",
    "SerpApiClient",
    "SerpApiError",
    "SerpApiItinerary",
    "filter_flights",
    "flight_number_id",
    "normalize_offers",
    "normalize_serpapi_offers",
    "parse_iso_duration",
    "rank_flights",
    "serpapi_itineraries",
]
