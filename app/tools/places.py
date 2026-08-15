"""Amadeus activities and points of interest: normalisation and ranking.

Transport lives in `app/tools/amadeus.py`. No LLM calls belong here.

Two endpoints with very different guarantees:

  /v1/shopping/activities          -> name, description, rating, geoCode and a
                                      REAL price. Ranked on its own merits.
  .../locations/pointsOfInterest   -> name, category, tags, rank, geoCode and
                                      **no price at all**.

That price gap is the defining constraint of the Restaurant agent: the venues
are real, but the cost of eating there has to be estimated. Every estimated
figure is flagged (`price_is_estimated`) and carries the basis it came from, so
a reader can never mistake it for a quote.

Results from both endpoints arrive ordered by provider relevance, so relevance
is taken from response order rather than the `rank` field, whose scale is not
guaranteed.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models.travel import Activity, Restaurant
from app.tools.hotels import haversine_km

logger = logging.getLogger(__name__)

# Activity ranking. Documented here because projectIdea.md leaves activity
# weights open, unlike hotels.
ACTIVITY_WEIGHTS = {
    "rating": 0.35,
    "price": 0.30,
    "interest_match": 0.25,
    "proximity": 0.10,
}

RESTAURANT_WEIGHTS = {
    "relevance": 0.40,
    "dietary_match": 0.30,
    "proximity": 0.20,
    "cuisine_match": 0.10,
}

# Per-person cost of one main meal, as a share of the per-person daily budget.
# A coarse heuristic, stated openly rather than dressed up as a quote.
MEAL_COST_SHARE = {
    "relaxed": 0.22,
    "balanced": 0.18,
    "packed": 0.14,
}

# Interest -> words that suggest an activity serves that interest.
INTEREST_KEYWORDS = {
    "food": ("food", "culinary", "cooking", "tasting", "wine", "market", "dining"),
    "culture": ("museum", "temple", "shrine", "heritage", "historic", "gallery"),
    "history": ("historic", "heritage", "castle", "ruins", "war", "ancient"),
    "nature": ("park", "garden", "hike", "mountain", "forest", "nature", "island"),
    "technology": ("tech", "science", "robot", "innovation", "digital", "future"),
    "shopping": ("shopping", "market", "boutique", "mall", "bazaar"),
    "nightlife": ("night", "bar", "club", "pub", "cocktail"),
    "adventure": ("adventure", "kayak", "dive", "climb", "raft", "zip", "safari"),
    "art": ("art", "gallery", "exhibition", "design", "sculpture"),
    "wellness": ("spa", "onsen", "yoga", "wellness", "thermal", "massage"),
}

# Dietary preference -> words that suggest a venue can accommodate it.
DIETARY_KEYWORDS = {
    "vegetarian": ("vegetarian", "veggie", "vegan", "plant"),
    "vegan": ("vegan", "plant"),
    "halal": ("halal",),
    "kosher": ("kosher",),
    "gluten_free": ("gluten", "glutenfree"),
    "seafood": ("seafood", "fish", "sushi"),
}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_activities(
    payload: dict[str, Any],
    destination: str,
    *,
    anchor: tuple[float, float] | None = None,
    fallback_currency: str = "INR",
) -> list[Activity]:
    """Turn a `/v1/shopping/activities` payload into `Activity` objects.

    Activities without a usable price are kept but flagged as estimated at
    zero — dropping them would silently hide free attractions, while pretending
    they cost nothing would understate the budget. The agent decides.
    """
    activities: list[Activity] = []

    for entry in payload.get("data", []):
        name = entry.get("name")
        if not name:
            continue

        price_block = entry.get("price") or {}
        amount = _as_float(price_block.get("amount"))
        geo = entry.get("geoCode") or {}
        latitude = _as_float(geo.get("latitude"))
        longitude = _as_float(geo.get("longitude"))

        distance = None
        if anchor and latitude is not None and longitude is not None:
            distance = round(
                haversine_km(anchor[0], anchor[1], latitude, longitude), 2
            )

        minimum_duration = entry.get("minimumDuration")
        activities.append(
            Activity(
                activity_id=str(entry.get("id")) if entry.get("id") else None,
                activity=name,
                category="activity",
                destination=destination,
                description=entry.get("shortDescription"),
                duration_hours=_parse_duration_hours(minimum_duration),
                estimated_cost=amount if amount is not None else 0.0,
                cost_is_estimated=amount is None,
                currency=price_block.get("currencyCode") or fallback_currency,
                rating=_as_float(entry.get("rating")),
                latitude=latitude,
                longitude=longitude,
                distance_km=distance,
                booking_link=entry.get("bookingLink"),
                source="amadeus",
            )
        )

    return activities


def _parse_duration_hours(value: str | None) -> float | None:
    """Amadeus writes durations as free text such as "2 hours" or "90 minutes"."""
    if not value:
        return None
    lowered = value.strip().lower()
    number = ""
    for char in lowered:
        if char.isdigit() or char == ".":
            number += char
        elif number:
            break
    if not number:
        return None
    try:
        amount = float(number)
    except ValueError:
        return None
    if "min" in lowered:
        return round(amount / 60, 2)
    if "day" in lowered:
        return round(amount * 24, 2)
    return amount


def normalize_points_of_interest(
    payload: dict[str, Any],
    destination: str,
    *,
    anchor: tuple[float, float] | None = None,
    meal_cost: float = 0.0,
    estimate_basis: str | None = None,
    currency: str = "INR",
) -> list[Restaurant]:
    """Turn a points-of-interest payload into `Restaurant` objects.

    Response order is preserved: Amadeus returns these by relevance, and that
    ordering is more dependable than the `rank` field's scale.
    """
    restaurants: list[Restaurant] = []

    for entry in payload.get("data", []):
        name = entry.get("name")
        if not name:
            continue

        geo = entry.get("geoCode") or {}
        latitude = _as_float(geo.get("latitude"))
        longitude = _as_float(geo.get("longitude"))

        distance = None
        if anchor and latitude is not None and longitude is not None:
            distance = round(
                haversine_km(anchor[0], anchor[1], latitude, longitude), 2
            )

        tags = [str(tag).lower() for tag in entry.get("tags") or []]
        restaurants.append(
            Restaurant(
                place_id=str(entry.get("id")) if entry.get("id") else None,
                name=name,
                destination=destination,
                cuisine=_guess_cuisine(tags),
                price_estimate=round(meal_cost, 2),
                price_is_estimated=True,
                estimate_basis=estimate_basis,
                currency=currency,
                latitude=latitude,
                longitude=longitude,
                distance_km=distance,
                tags=tags,
                dietary_tags=_detect_dietary(tags),
                source="amadeus",
            )
        )

    return restaurants


def _guess_cuisine(tags: list[str]) -> str | None:
    """Best-effort cuisine from POI tags; None rather than a guess when unclear."""
    skip = {"restaurant", "restaurants", "food", "sightseeing", "sights"}
    for tag in tags:
        if tag not in skip and len(tag) > 2:
            return tag
    return None


def _detect_dietary(tags: list[str]) -> list[str]:
    """Which dietary preferences the venue's tags suggest it can serve."""
    joined = " ".join(tags)
    return sorted(
        name
        for name, keywords in DIETARY_KEYWORDS.items()
        if any(keyword in joined for keyword in keywords)
    )


def estimate_meal_cost(
    budget: float, travelers: int, days: int, trip_style: str
) -> tuple[float, str]:
    """Estimate the cost of one meal for the whole party, plus its basis.

    Returns (cost, basis) so the basis can be attached to every restaurant and
    surfaced to the reader — Amadeus provides no restaurant pricing, and an
    unexplained number would read as a quote.
    """
    share = MEAL_COST_SHARE.get(trip_style, MEAL_COST_SHARE["balanced"])
    per_person_per_day = budget / max(travelers, 1) / max(days, 1)
    cost = per_person_per_day * share * max(travelers, 1)
    basis = (
        f"estimated at {int(share * 100)}% of the per-person daily budget "
        f"for a {trip_style} trip — Amadeus does not price restaurants"
    )
    return round(cost, 2), basis


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _spread(value: float, best: float, worst: float) -> float:
    if worst <= best:
        return 1.0
    return max(0.0, min(1.0, (worst - value) / (worst - best)))


def _interest_score(text: str, interests: list[str] | None) -> float | None:
    """How well a venue's text matches the traveller's stated interests."""
    if not interests:
        return None
    lowered = text.lower()
    matched = 0
    for interest in interests:
        keywords = INTEREST_KEYWORDS.get(interest.strip().lower())
        if not keywords:
            continue
        if any(keyword in lowered for keyword in keywords):
            matched += 1
    considered = sum(
        1 for i in interests if INTEREST_KEYWORDS.get(i.strip().lower())
    )
    if not considered:
        return None
    return matched / considered


def rank_activities(
    activities: list[Activity],
    *,
    interests: list[str] | None = None,
    max_cost: float | None = None,
    top_n: int = 10,
) -> list[Activity]:
    """Score activities 0-100 and return the best `top_n`, with reasons.

    rating 35% / price 30% / interest match 25% / proximity 10%, renormalised
    per activity over the factors that actually have data.
    """
    if not activities:
        return []

    affordable = (
        [a for a in activities if a.estimated_cost <= max_cost]
        if max_cost is not None
        else list(activities)
    )
    # Relax rather than return nothing.
    candidates = affordable or list(activities)

    costs = [a.estimated_cost for a in candidates]
    cheapest, dearest = min(costs), max(costs)
    distances = [a.distance_km for a in candidates if a.distance_km is not None]
    nearest, farthest = (min(distances), max(distances)) if distances else (0.0, 0.0)

    scored: list[Activity] = []
    for activity in candidates:
        components: dict[str, float] = {
            "price": _spread(activity.estimated_cost, cheapest, dearest)
        }
        if activity.rating is not None:
            # Amadeus activity ratings are 0-5.
            components["rating"] = max(0.0, min(1.0, activity.rating / 5.0))
        if activity.distance_km is not None and distances:
            components["proximity"] = _spread(activity.distance_km, nearest, farthest)

        interest = _interest_score(
            f"{activity.activity} {activity.description or ''}", interests
        )
        if interest is not None:
            components["interest_match"] = interest

        available = {k: ACTIVITY_WEIGHTS[k] for k in components}
        weight_total = sum(available.values()) or 1.0
        score = sum(components[k] * w for k, w in available.items()) / weight_total

        reasons = []
        if activity.cost_is_estimated:
            reasons.append("no price published")
        else:
            reasons.append(f"{activity.estimated_cost:.0f} {activity.currency}")
        if activity.rating is not None:
            reasons.append(f"rated {activity.rating:.1f}/5")
        if activity.distance_km is not None:
            reasons.append(f"{activity.distance_km:.1f} km away")
        if components.get("interest_match"):
            reasons.append("matches your interests")

        scored.append(
            activity.model_copy(
                update={
                    "score": round(score * 100, 1),
                    "rationale": "; ".join(reasons),
                }
            )
        )

    scored.sort(key=lambda a: (-a.score, a.estimated_cost))
    return scored[:top_n]


def rank_restaurants(
    restaurants: list[Restaurant],
    *,
    dietary_preferences: list[str] | None = None,
    top_n: int = 10,
) -> list[Restaurant]:
    """Score restaurants 0-100 and return the best `top_n`, with reasons.

    relevance 40% / dietary match 30% / proximity 20% / cuisine known 10%.
    Relevance comes from provider response order, which is ordered by relevance
    and does not depend on the `rank` field's undocumented scale.
    """
    if not restaurants:
        return []

    total = len(restaurants)
    distances = [r.distance_km for r in restaurants if r.distance_km is not None]
    nearest, farthest = (min(distances), max(distances)) if distances else (0.0, 0.0)
    wanted = [d.strip().lower() for d in dietary_preferences or []]

    scored: list[Restaurant] = []
    for position, restaurant in enumerate(restaurants):
        components: dict[str, float] = {
            # First result scores 1.0, last scores near 0.
            "relevance": 1.0 - (position / total) if total > 1 else 1.0
        }
        if restaurant.distance_km is not None and distances:
            components["proximity"] = _spread(
                restaurant.distance_km, nearest, farthest
            )
        if wanted:
            matched = sum(1 for d in wanted if d in restaurant.dietary_tags)
            components["dietary_match"] = matched / len(wanted)
        if restaurant.cuisine:
            components["cuisine_match"] = 1.0

        available = {k: RESTAURANT_WEIGHTS[k] for k in components}
        weight_total = sum(available.values()) or 1.0
        score = sum(components[k] * w for k, w in available.items()) / weight_total

        reasons = [
            f"~{restaurant.price_estimate:.0f} {restaurant.currency} estimated"
        ]
        if restaurant.cuisine:
            reasons.append(f"{restaurant.cuisine} cuisine")
        if restaurant.distance_km is not None:
            reasons.append(f"{restaurant.distance_km:.1f} km away")
        if wanted and restaurant.dietary_tags:
            reasons.append("suits " + ", ".join(restaurant.dietary_tags))

        scored.append(
            restaurant.model_copy(
                update={
                    "score": round(score * 100, 1),
                    "rationale": "; ".join(reasons),
                }
            )
        )

    scored.sort(key=lambda r: -r.score)
    return scored[:top_n]


def schedule_across_days(items: list[Any], days: list[int]) -> list[Any]:
    """Assign ranked items to days, best first, one per day.

    Returns a *schedule*, not alternatives — which is why the Budget agent can
    sum activities and restaurants directly, unlike flights and hotels.
    """
    scheduled = []
    for index, day in enumerate(days):
        if index >= len(items):
            break
        scheduled.append(items[index].model_copy(update={"recommended_day": day}))
    return scheduled


__all__ = [
    "ACTIVITY_WEIGHTS",
    "DIETARY_KEYWORDS",
    "INTEREST_KEYWORDS",
    "MEAL_COST_SHARE",
    "RESTAURANT_WEIGHTS",
    "estimate_meal_cost",
    "normalize_activities",
    "normalize_points_of_interest",
    "rank_activities",
    "rank_restaurants",
    "schedule_across_days",
]
