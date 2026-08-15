"""Activity/POI normalisation, meal-cost estimation, and ranking."""

from __future__ import annotations

import pytest

from app.models.travel import Activity, Restaurant
from app.tools.places import (
    ACTIVITY_WEIGHTS,
    RESTAURANT_WEIGHTS,
    estimate_meal_cost,
    normalize_activities,
    normalize_points_of_interest,
    rank_activities,
    rank_restaurants,
    schedule_across_days,
)

ACTIVITIES_PAYLOAD = {
    "data": [
        {
            "type": "activity",
            "id": "23642",
            "name": "Tokyo Food Tour: Tsukiji Market Tasting",
            "shortDescription": "A culinary walking tour with market tasting.",
            "geoCode": {"latitude": 35.6655, "longitude": 139.7707},
            "rating": "4.8",
            "pictures": ["https://example.test/1.jpg"],
            "bookingLink": "https://example.test/book/23642",
            "minimumDuration": "3 hours",
            "price": {"currencyCode": "INR", "amount": "4500.00"},
        },
        {
            "type": "activity",
            "id": "23643",
            "name": "teamLab Borderless Digital Art Museum",
            "shortDescription": "An immersive digital art and technology museum.",
            "geoCode": {"latitude": 35.6256, "longitude": 139.7756},
            "rating": "4.5",
            "minimumDuration": "90 minutes",
            "price": {"currencyCode": "INR", "amount": "2800.00"},
        },
        {
            # No price published — must be kept and flagged, not dropped.
            "type": "activity",
            "id": "23644",
            "name": "Meiji Shrine Walk",
            "shortDescription": "A free historic shrine and forest walk.",
            "geoCode": {"latitude": 35.6764, "longitude": 139.6993},
            "price": {"currencyCode": "INR"},
        },
        {"type": "activity", "id": "bad", "shortDescription": "no name"},
    ]
}

POI_PAYLOAD = {
    "data": [
        {
            "type": "location",
            "subType": "POINT_OF_INTEREST",
            "id": "9CB40CB5D0",
            "geoCode": {"latitude": 35.6700, "longitude": 139.7600},
            "name": "Sushi Saito",
            "category": "RESTAURANT",
            "rank": 1,
            "tags": ["restaurant", "sushi", "seafood"],
        },
        {
            "type": "location",
            "id": "9CB40CB5D1",
            "geoCode": {"latitude": 35.6900, "longitude": 139.7000},
            "name": "Nagi Shokudo",
            "category": "RESTAURANT",
            "rank": 12,
            "tags": ["restaurant", "vegetarian", "vegan"],
        },
        {"type": "location", "id": "nameless", "category": "RESTAURANT"},
    ]
}


# ---------------------------------------------------------------------------
# Activity normalisation
# ---------------------------------------------------------------------------


def test_activity_normalisation_reads_price_rating_and_duration() -> None:
    activities = normalize_activities(ACTIVITIES_PAYLOAD, destination="Tokyo")
    by_name = {a.activity: a for a in activities}

    tour = by_name["Tokyo Food Tour: Tsukiji Market Tasting"]
    assert tour.estimated_cost == 4500.0
    assert tour.cost_is_estimated is False
    assert tour.currency == "INR"
    assert tour.rating == 4.8
    assert tour.duration_hours == 3.0
    assert tour.booking_link == "https://example.test/book/23642"


def test_minute_and_hour_durations_both_parse() -> None:
    activities = normalize_activities(ACTIVITIES_PAYLOAD, destination="Tokyo")
    by_name = {a.activity: a for a in activities}

    assert by_name["teamLab Borderless Digital Art Museum"].duration_hours == 1.5


def test_activity_without_a_price_is_kept_and_flagged() -> None:
    """Dropping it would hide free attractions; pricing it at 0 would mislead."""
    activities = normalize_activities(ACTIVITIES_PAYLOAD, destination="Tokyo")
    shrine = next(a for a in activities if a.activity == "Meiji Shrine Walk")

    assert shrine.cost_is_estimated is True
    assert shrine.estimated_cost == 0.0


def test_nameless_activity_is_skipped() -> None:
    activities = normalize_activities(ACTIVITIES_PAYLOAD, destination="Tokyo")
    assert len(activities) == 3


def test_activity_distance_uses_the_anchor() -> None:
    anchor = (35.6762, 139.6503)  # Shinjuku-ish
    activities = normalize_activities(
        ACTIVITIES_PAYLOAD, destination="Tokyo", anchor=anchor
    )
    assert all(a.distance_km is not None for a in activities)
    assert all(a.distance_km >= 0 for a in activities)


# ---------------------------------------------------------------------------
# POI normalisation and meal estimation
# ---------------------------------------------------------------------------


def test_poi_normalisation_reads_tags_and_dietary_hints() -> None:
    restaurants = normalize_points_of_interest(POI_PAYLOAD, destination="Tokyo")
    by_name = {r.name: r for r in restaurants}

    assert by_name["Nagi Shokudo"].dietary_tags == ["vegan", "vegetarian"]
    assert "seafood" in by_name["Sushi Saito"].dietary_tags
    assert by_name["Sushi Saito"].cuisine == "sushi"


def test_poi_response_order_is_preserved() -> None:
    """Relevance comes from order, so normalisation must not reorder."""
    restaurants = normalize_points_of_interest(POI_PAYLOAD, destination="Tokyo")
    assert [r.name for r in restaurants] == ["Sushi Saito", "Nagi Shokudo"]


def test_every_restaurant_price_is_flagged_as_an_estimate() -> None:
    """Amadeus prices no restaurants — a bare number would read as a quote."""
    cost, basis = estimate_meal_cost(200000, 2, 6, "balanced")
    restaurants = normalize_points_of_interest(
        POI_PAYLOAD, destination="Tokyo", meal_cost=cost, estimate_basis=basis
    )

    assert restaurants
    assert all(r.price_is_estimated for r in restaurants)
    assert all(r.estimate_basis and "does not price" in r.estimate_basis
               for r in restaurants)


def test_meal_estimate_scales_with_party_and_style() -> None:
    relaxed, _ = estimate_meal_cost(200000, 2, 6, "relaxed")
    balanced, _ = estimate_meal_cost(200000, 2, 6, "balanced")
    packed, _ = estimate_meal_cost(200000, 2, 6, "packed")

    assert relaxed > balanced > packed

    one, _ = estimate_meal_cost(200000, 1, 6, "balanced")
    two, _ = estimate_meal_cost(200000, 2, 6, "balanced")
    # Per-person budget halves but the party doubles, so the meal cost matches.
    assert one == pytest.approx(two)


def test_meal_estimate_handles_degenerate_inputs() -> None:
    cost, _ = estimate_meal_cost(200000, 0, 0, "balanced")
    assert cost > 0


def test_unknown_trip_style_falls_back_to_balanced() -> None:
    unknown, _ = estimate_meal_cost(200000, 2, 6, "not-a-style")
    balanced, _ = estimate_meal_cost(200000, 2, 6, "balanced")
    assert unknown == balanced


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _activity(
    name: str,
    cost: float,
    *,
    rating: float | None = 4.0,
    distance: float | None = 1.0,
    description: str = "",
) -> Activity:
    return Activity(
        activity=name,
        category="activity",
        destination="Tokyo",
        description=description,
        estimated_cost=cost,
        currency="INR",
        rating=rating,
        distance_km=distance,
    )


def test_activity_ranking_prefers_better_rated_and_cheaper() -> None:
    good = _activity("good", 1_000, rating=4.9, distance=0.5)
    poor = _activity("poor", 9_000, rating=2.0, distance=9.0)

    ranked = rank_activities([poor, good])
    assert ranked[0].activity == "good"
    assert ranked[0].score > ranked[1].score


def test_activity_ranking_respects_interests() -> None:
    food = _activity("A", 1_000, description="a culinary tasting tour")
    tech = _activity("B", 1_000, description="a robot and innovation museum")

    by_food = {
        a.activity: a.score
        for a in rank_activities([food, tech], interests=["food"])
    }
    by_tech = {
        a.activity: a.score
        for a in rank_activities([food, tech], interests=["technology"])
    }

    assert by_food["A"] > by_food["B"]
    assert by_tech["B"] > by_tech["A"]


def test_activity_cost_cap_relaxes_rather_than_emptying() -> None:
    expensive = [_activity("x", 50_000), _activity("y", 60_000)]
    assert len(rank_activities(expensive, max_cost=100)) == 2


def test_activity_ranking_omits_missing_factors() -> None:
    unrated = _activity("u", 1_000, rating=None, distance=None)
    ranked = rank_activities([unrated])
    assert ranked[0].rationale
    assert set(ACTIVITY_WEIGHTS) >= {"rating", "price", "interest_match", "proximity"}


def test_restaurant_ranking_prefers_dietary_matches() -> None:
    veg = Restaurant(
        name="veg",
        destination="Tokyo",
        dietary_tags=["vegetarian"],
        distance_km=1.0,
        currency="INR",
    )
    other = Restaurant(
        name="other", destination="Tokyo", dietary_tags=[], distance_km=1.0,
        currency="INR",
    )

    ranked = rank_restaurants([other, veg], dietary_preferences=["vegetarian"])
    assert ranked[0].name == "veg"


def test_restaurant_relevance_follows_response_order() -> None:
    first = Restaurant(name="first", destination="Tokyo", currency="INR")
    last = Restaurant(name="last", destination="Tokyo", currency="INR")

    ranked = rank_restaurants([first, last])
    assert ranked[0].name == "first"
    assert set(RESTAURANT_WEIGHTS) >= {"relevance", "dietary_match", "proximity"}


def test_ranking_handles_empty_inputs() -> None:
    assert rank_activities([]) == []
    assert rank_restaurants([]) == []


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def test_schedule_assigns_best_items_to_days_in_order() -> None:
    items = [_activity(f"a{i}", 1_000) for i in range(5)]
    scheduled = schedule_across_days(items, [1, 2, 3])

    assert [a.recommended_day for a in scheduled] == [1, 2, 3]
    assert [a.activity for a in scheduled] == ["a0", "a1", "a2"]


def test_schedule_stops_when_items_run_out() -> None:
    scheduled = schedule_across_days([_activity("only", 1_000)], [1, 2, 3])
    assert len(scheduled) == 1
    assert scheduled[0].recommended_day == 1


def test_schedule_never_repeats_an_item() -> None:
    """One venue per day — a duplicate would be a scheduling bug."""
    items = [_activity(f"a{i}", 1_000) for i in range(4)]
    scheduled = schedule_across_days(items, [1, 2, 3, 4])
    assert len({a.activity for a in scheduled}) == 4
