"""Amadeus flight search: normalisation, filtering and explainable ranking.

Transport and auth live in `app/tools/amadeus.py`; this module turns raw
Amadeus flight-offers JSON into `FlightOption` objects and scores them. No LLM
calls belong here — the Flight agent makes the judgement calls.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.models.travel import FlightOption, FlightSegment, FlightSlice
from app.tools.amadeus import AmadeusClient, AmadeusError, FlightSearchError

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
    "filter_flights",
    "normalize_offers",
    "parse_iso_duration",
    "rank_flights",
]
