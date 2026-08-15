"""Amadeus hotel search: normalisation, filtering and explainable ranking.

Transport lives in `app/tools/amadeus.py`. No LLM calls belong here.

Amadeus splits hotels across three endpoints, and what each returns shapes what
we can honestly score:

  by-city         -> hotelId, name, chain, geoCode. NO star rating, NO amenity
                     list. `ratings` and `amenities` are *filters* only.
  hotel-offers    -> prices, room type and a free-text room description.
  hotel-sentiments-> guest rating 0-100, available for only some hotels.

So a hotel's star rating is never invented, guest rating may be missing, and
amenities can only be read out of the room description text. `rank_hotels`
therefore scores each factor **only when the data exists** and renormalises the
weights over what is present, rather than silently scoring unknowns as zero.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any

from app.models.travel import HotelOption

logger = logging.getLogger(__name__)

# Weights from projectIdea.md §9. Renormalised per hotel over available factors.
RANK_WEIGHTS = {
    "price": 0.30,
    "location": 0.25,
    "rating": 0.20,
    "amenities": 0.15,
    "preferences": 0.10,
}

# Amenity keywords we can actually detect in Amadeus room descriptions.
AMENITY_KEYWORDS = {
    "wifi": ("WIFI", "WI-FI", "INTERNET"),
    "breakfast": ("BREAKFAST",),
    "air_conditioning": ("AIRCON", "AIR CONDITION", "A/C"),
    "parking": ("PARKING",),
    "pool": ("POOL",),
    "gym": ("GYM", "FITNESS"),
    "kitchen": ("KITCHEN", "KITCHENETTE"),
    "refundable": ("REFUNDABLE", "FREE CANCELLATION"),
}

# Which amenities matter to a traveller who listed a given interest.
INTEREST_AMENITIES = {
    "food": ("breakfast", "kitchen"),
    "wellness": ("pool", "gym"),
    "fitness": ("gym", "pool"),
    "family": ("kitchen", "pool"),
    "business": ("wifi", "air_conditioning"),
    "budget": ("breakfast", "kitchen"),
}

# Amenities worth having regardless of stated interests.
BASELINE_AMENITIES = ("wifi", "breakfast", "air_conditioning")


def haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in kilometres."""
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


# Phrases that invert a keyword rather than confirm it. "Non refundable rate"
# contains "REFUNDABLE" but means the opposite, so a bare substring match would
# advertise a non-refundable booking as refundable.
_NEGATIONS = ("NON REFUNDABLE", "NON-REFUNDABLE", "NONREFUNDABLE", "NO REFUND")


def detect_amenities(text: str | None) -> list[str]:
    """Best-effort amenity extraction from an Amadeus room description.

    Amadeus exposes no structured amenity list on these endpoints, so this
    reads the free-text description. Absence here means "not mentioned", not
    "not available" — callers must not present it as a definitive list.
    """
    if not text:
        return []
    upper = text.upper()
    negated = any(phrase in upper for phrase in _NEGATIONS)

    found = []
    for name, keywords in AMENITY_KEYWORDS.items():
        if not any(keyword in upper for keyword in keywords):
            continue
        if name == "refundable" and negated:
            continue
        found.append(name)
    return sorted(found)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _nights(check_in: date, check_out: date) -> int:
    return max((check_out - check_in).days, 1)


def normalize_hotel_offers(
    payload: dict[str, Any],
    destination: str,
    *,
    geo_by_hotel: dict[str, tuple[float, float]] | None = None,
    ratings_by_hotel: dict[str, float] | None = None,
    requested_stars: int | None = None,
    city_center: tuple[float, float] | None = None,
) -> list[HotelOption]:
    """Turn a raw `/v3/shopping/hotel-offers` payload into `HotelOption`s.

    A hotel may quote several room offers; we keep its cheapest, since the
    ranker compares hotels against each other, not rooms within a hotel.
    """
    geo_by_hotel = geo_by_hotel or {}
    ratings_by_hotel = ratings_by_hotel or {}
    options: list[HotelOption] = []

    for entry in payload.get("data", []):
        if entry.get("available") is False:
            continue

        hotel = entry.get("hotel") or {}
        hotel_id = hotel.get("hotelId")
        offers = entry.get("offers") or []
        if not offers:
            continue

        priced: list[tuple[float, dict[str, Any]]] = []
        for offer in offers:
            price_block = offer.get("price") or {}
            raw_total = price_block.get("total") or price_block.get("base")
            try:
                priced.append((float(raw_total), offer))
            except (TypeError, ValueError):
                logger.warning(
                    "skipping hotel offer %s: unusable price %r",
                    offer.get("id"),
                    raw_total,
                )
        if not priced:
            continue

        total, offer = min(priced, key=lambda pair: pair[0])
        price_block = offer.get("price") or {}

        try:
            check_in = date.fromisoformat(offer["checkInDate"])
            check_out = date.fromisoformat(offer["checkOutDate"])
        except (KeyError, TypeError, ValueError):
            logger.warning("skipping hotel offer %s: unusable dates", offer.get("id"))
            continue

        nights = _nights(check_in, check_out)
        room = offer.get("room") or {}
        description = (room.get("description") or {}).get("text")

        latitude = hotel.get("latitude")
        longitude = hotel.get("longitude")
        if (latitude is None or longitude is None) and hotel_id in geo_by_hotel:
            latitude, longitude = geo_by_hotel[hotel_id]

        distance = None
        if city_center and latitude is not None and longitude is not None:
            distance = round(
                haversine_km(city_center[0], city_center[1], latitude, longitude), 2
            )

        options.append(
            HotelOption(
                hotel_id=hotel_id,
                offer_id=offer.get("id"),
                name=hotel.get("name") or hotel_id or "Unknown hotel",
                destination=destination,
                chain_code=hotel.get("chainCode"),
                check_in=check_in,
                check_out=check_out,
                nights=nights,
                price_per_night=round(total / nights, 2),
                total_price=round(total, 2),
                currency=price_block.get("currency", "EUR"),
                latitude=latitude,
                longitude=longitude,
                distance_km=distance,
                stars=float(requested_stars) if requested_stars else None,
                rating=ratings_by_hotel.get(hotel_id),
                room_type=(room.get("typeEstimated") or {}).get("category")
                or room.get("type"),
                room_description=description,
                amenities=detect_amenities(description),
                source="amadeus",
            )
        )

    return options


def parse_hotel_list(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`by-city` payload -> {hotelId: {name, chain, lat, lon}}."""
    hotels: dict[str, dict[str, Any]] = {}
    for entry in payload.get("data", []):
        hotel_id = entry.get("hotelId")
        if not hotel_id:
            continue
        geo = entry.get("geoCode") or {}
        hotels[hotel_id] = {
            "name": entry.get("name"),
            "chain_code": entry.get("chainCode"),
            "latitude": geo.get("latitude"),
            "longitude": geo.get("longitude"),
        }
    return hotels


def parse_hotel_ratings(payload: dict[str, Any]) -> dict[str, float]:
    """`hotel-sentiments` payload -> {hotelId: overallRating}. Partial by design."""
    ratings: dict[str, float] = {}
    for entry in payload.get("data", []):
        hotel_id = entry.get("hotelId")
        overall = entry.get("overallRating")
        if hotel_id and isinstance(overall, (int, float)):
            ratings[hotel_id] = float(overall)
    return ratings


# ---------------------------------------------------------------------------
# Filtering and ranking
# ---------------------------------------------------------------------------


def filter_hotels(
    hotels: list[HotelOption],
    *,
    max_total_price: float | None = None,
    max_distance_km: float | None = None,
    min_rating: float | None = None,
) -> list[HotelOption]:
    """Apply hard constraints, relaxing any that would empty the list."""
    results = list(hotels)

    for predicate in (
        (lambda h: max_total_price is None or h.total_price <= max_total_price),
        (
            lambda h: max_distance_km is None
            or h.distance_km is None
            or h.distance_km <= max_distance_km
        ),
        # Unrated hotels survive a rating floor — absent data is not a failure.
        (lambda h: min_rating is None or h.rating is None or h.rating >= min_rating),
    ):
        kept = [h for h in results if predicate(h)]
        if kept:
            results = kept

    return results


def _spread(value: float, best: float, worst: float) -> float:
    """Map onto 0..1 where `best` scores 1.0. Lower input is better."""
    if worst <= best:
        return 1.0
    return max(0.0, min(1.0, (worst - value) / (worst - best)))


# An amenity the traveller effectively asked for counts for more than one we
# merely assume everyone wants.
INTEREST_AMENITY_WEIGHT = 2.0
BASELINE_AMENITY_WEIGHT = 1.0


def _wanted_amenities(interests: list[str] | None) -> dict[str, float]:
    """Desired amenities mapped to how much each one counts."""
    wanted = {name: BASELINE_AMENITY_WEIGHT for name in BASELINE_AMENITIES}
    for interest in interests or []:
        for name in INTEREST_AMENITIES.get(interest.strip().lower(), ()):
            wanted[name] = INTEREST_AMENITY_WEIGHT
    return wanted


def rank_hotels(
    hotels: list[HotelOption],
    *,
    interests: list[str] | None = None,
    preferred_chain: str | None = None,
    requested_stars: int | None = None,
    top_n: int = 5,
) -> list[HotelOption]:
    """Score hotels 0-100 on the projectIdea.md §9 weights, and say why.

    price 30% / location 25% / rating 20% / amenities 15% / preferences 10%.
    A factor with no data for a given hotel is *omitted* and its weight is
    redistributed across the rest, so an unrated hotel is not punished for
    Amadeus lacking sentiment data on it.
    """
    if not hotels:
        return []

    prices = [h.total_price for h in hotels]
    cheapest, dearest = min(prices), max(prices)

    distances = [h.distance_km for h in hotels if h.distance_km is not None]
    nearest, farthest = (min(distances), max(distances)) if distances else (0.0, 0.0)

    wanted = _wanted_amenities(interests)
    chain = preferred_chain.strip().upper() if preferred_chain else None

    scored: list[HotelOption] = []
    for hotel in hotels:
        components: dict[str, float] = {
            "price": _spread(hotel.total_price, cheapest, dearest)
        }

        if hotel.distance_km is not None and distances:
            components["location"] = _spread(hotel.distance_km, nearest, farthest)

        if hotel.rating is not None:
            components["rating"] = max(0.0, min(1.0, hotel.rating / 100.0))

        if hotel.room_description:
            # Only scored when there is a description to read; a hotel with no
            # description is unknown on amenities, not devoid of them.
            possible = sum(wanted.values())
            matched = sum(
                weight for name, weight in wanted.items() if name in hotel.amenities
            )
            components["amenities"] = matched / possible if possible else 0.0

        preference = 0.0
        if chain and hotel.chain_code and hotel.chain_code.upper() == chain:
            preference += 0.6
        if requested_stars and hotel.stars == float(requested_stars):
            preference += 0.4
        if chain or requested_stars:
            components["preferences"] = min(preference, 1.0)

        # Renormalise the weights over the factors we could actually score.
        available = {k: RANK_WEIGHTS[k] for k in components}
        weight_total = sum(available.values()) or 1.0
        score = sum(components[k] * w for k, w in available.items()) / weight_total

        reasons = [
            f"{hotel.total_price:.0f} {hotel.currency} for {hotel.nights} night(s)"
        ]
        if hotel.distance_km is not None:
            reasons.append(f"{hotel.distance_km:.1f} km from centre")
        if hotel.rating is not None:
            reasons.append(f"guest rating {hotel.rating:.0f}/100")
        else:
            reasons.append("no guest rating available")
        if hotel.amenities:
            reasons.append("includes " + ", ".join(hotel.amenities))

        scored.append(
            hotel.model_copy(
                update={
                    "score": round(score * 100, 1),
                    "score_components": {
                        k: round(v, 3) for k, v in components.items()
                    },
                    "rationale": "; ".join(reasons),
                }
            )
        )

    scored.sort(key=lambda h: (-h.score, h.total_price))
    return scored[:top_n]


__all__ = [
    "AMENITY_KEYWORDS",
    "RANK_WEIGHTS",
    "detect_amenities",
    "filter_hotels",
    "haversine_km",
    "normalize_hotel_offers",
    "parse_hotel_list",
    "parse_hotel_ratings",
    "rank_hotels",
]
