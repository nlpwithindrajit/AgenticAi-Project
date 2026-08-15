"""Hotel normalisation, distance, amenity detection and weighted ranking."""

from __future__ import annotations

from datetime import date

import pytest

from app.models.travel import HotelOption
from app.tools.hotels import (
    RANK_WEIGHTS,
    detect_amenities,
    filter_hotels,
    haversine_km,
    normalize_hotel_offers,
    parse_hotel_list,
    parse_hotel_ratings,
    rank_hotels,
)

HOTEL_LIST_PAYLOAD = {
    "data": [
        {
            "chainCode": "HL",
            "iataCode": "PAR",
            "name": "Hilton Paris Opera",
            "hotelId": "HLPAR266",
            "geoCode": {"latitude": 48.8757, "longitude": 2.32553},
            "address": {"countryCode": "FR"},
        },
        {
            "chainCode": "AC",
            "iataCode": "PAR",
            "name": "Acropolis Hotel Paris",
            "hotelId": "ACPARH29",
            "geoCode": {"latitude": 48.83593, "longitude": 2.24922},
            "address": {"countryCode": "FR"},
        },
        # No hotelId — must be skipped rather than crash.
        {"name": "Broken entry", "geoCode": {"latitude": 1.0, "longitude": 2.0}},
    ]
}

HOTEL_OFFERS_PAYLOAD = {
    "data": [
        {
            "type": "hotel-offers",
            "hotel": {
                "type": "hotel",
                "hotelId": "HLPAR266",
                "chainCode": "HL",
                "name": "Hilton Paris Opera",
                "cityCode": "PAR",
                "latitude": 48.8757,
                "longitude": 2.32553,
            },
            "available": True,
            "offers": [
                {
                    "id": "EXPENSIVE",
                    "checkInDate": "2026-10-10",
                    "checkOutDate": "2026-10-13",
                    "room": {
                        "type": "A07",
                        "typeEstimated": {"category": "SUPERIOR_ROOM"},
                        "description": {
                            "text": "ADVANCE PURCHASE\nSUPERIOR ROOM\nFREE WIFI/AIRCON",
                            "lang": "EN",
                        },
                    },
                    "price": {"currency": "INR", "base": "30000", "total": "33000.00"},
                },
                {
                    "id": "CHEAPEST",
                    "checkInDate": "2026-10-10",
                    "checkOutDate": "2026-10-13",
                    "room": {
                        "type": "A01",
                        "typeEstimated": {"category": "STANDARD_ROOM"},
                        "description": {
                            "text": "STANDARD ROOM\nFREE WIFI\nBREAKFAST INCLUDED",
                            "lang": "EN",
                        },
                    },
                    "price": {"currency": "INR", "total": "27000.00"},
                },
            ],
        },
        {
            "type": "hotel-offers",
            "hotel": {
                "hotelId": "ACPARH29",
                "chainCode": "AC",
                "name": "Acropolis Hotel Paris",
                "latitude": 48.83593,
                "longitude": 2.24922,
            },
            "available": True,
            "offers": [
                {
                    "id": "AC1",
                    "checkInDate": "2026-10-10",
                    "checkOutDate": "2026-10-13",
                    "room": {"typeEstimated": {"category": "STANDARD_ROOM"}},
                    "price": {"currency": "INR", "total": "18000.00"},
                }
            ],
        },
        # Sold out — must not appear.
        {
            "hotel": {"hotelId": "SOLDOUT", "name": "No Rooms Inn"},
            "available": False,
            "offers": [],
        },
    ]
}

RATINGS_PAYLOAD = {
    "data": [
        {"hotelId": "HLPAR266", "overallRating": 88, "numberOfReviews": 1200},
        # ACPARH29 deliberately absent — Amadeus coverage is partial.
    ]
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_hotel_list_skips_entries_without_an_id() -> None:
    hotels = parse_hotel_list(HOTEL_LIST_PAYLOAD)

    assert set(hotels) == {"HLPAR266", "ACPARH29"}
    assert hotels["HLPAR266"]["chain_code"] == "HL"
    assert hotels["HLPAR266"]["latitude"] == pytest.approx(48.8757)


def test_parse_hotel_ratings_is_partial_by_design() -> None:
    ratings = parse_hotel_ratings(RATINGS_PAYLOAD)

    assert ratings == {"HLPAR266": 88.0}
    assert "ACPARH29" not in ratings, "missing rating must stay missing, not become 0"


def test_haversine_matches_known_distance() -> None:
    # Hilton Paris Opera to Acropolis Boulogne is roughly 6 km.
    km = haversine_km(48.8757, 2.32553, 48.83593, 2.24922)
    assert 5.0 < km < 7.5


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FREE WIFI/AIRCON", {"wifi", "air_conditioning"}),
        ("STANDARD ROOM\nFREE WIFI\nBREAKFAST INCLUDED", {"wifi", "breakfast"}),
        ("Non refundable rate", set()),
        (None, set()),
        ("", set()),
    ],
)
def test_detect_amenities(text: str | None, expected: set[str]) -> None:
    assert set(detect_amenities(text)) == expected


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_normalisation_keeps_the_cheapest_room_per_hotel() -> None:
    hotels = normalize_hotel_offers(HOTEL_OFFERS_PAYLOAD, destination="Paris")

    by_id = {h.hotel_id: h for h in hotels}
    assert by_id["HLPAR266"].offer_id == "CHEAPEST"
    assert by_id["HLPAR266"].total_price == 27000.00


def test_normalisation_computes_nights_and_nightly_rate() -> None:
    hotel = normalize_hotel_offers(HOTEL_OFFERS_PAYLOAD, destination="Paris")[0]

    assert hotel.check_in == date(2026, 10, 10)
    assert hotel.check_out == date(2026, 10, 13)
    assert hotel.nights == 3
    assert hotel.price_per_night == 9000.00


def test_unavailable_hotels_are_dropped() -> None:
    hotels = normalize_hotel_offers(HOTEL_OFFERS_PAYLOAD, destination="Paris")
    assert "SOLDOUT" not in {h.hotel_id for h in hotels}


def test_normalisation_attaches_ratings_and_distance() -> None:
    hotels = normalize_hotel_offers(
        HOTEL_OFFERS_PAYLOAD,
        destination="Paris",
        ratings_by_hotel=parse_hotel_ratings(RATINGS_PAYLOAD),
        city_center=(48.8566, 2.3522),
    )
    by_id = {h.hotel_id: h for h in hotels}

    assert by_id["HLPAR266"].rating == 88.0
    assert by_id["ACPARH29"].rating is None
    assert by_id["HLPAR266"].distance_km is not None
    # Hilton Opera is nearer the centre than Acropolis Boulogne.
    assert by_id["HLPAR266"].distance_km < by_id["ACPARH29"].distance_km


def test_star_rating_is_never_invented() -> None:
    """Amadeus returns no per-hotel star rating; we must not fabricate one."""
    unfiltered = normalize_hotel_offers(HOTEL_OFFERS_PAYLOAD, destination="Paris")
    assert all(h.stars is None for h in unfiltered)

    confirmed = normalize_hotel_offers(
        HOTEL_OFFERS_PAYLOAD, destination="Paris", requested_stars=4
    )
    assert all(h.stars == 4.0 for h in confirmed)


def test_offers_with_unusable_price_or_dates_are_skipped() -> None:
    payload = {
        "data": [
            {
                "hotel": {"hotelId": "BAD1", "name": "Bad price"},
                "offers": [
                    {
                        "id": "x",
                        "checkInDate": "2026-10-10",
                        "checkOutDate": "2026-10-11",
                        "price": {"total": "not-a-number"},
                    }
                ],
            },
            {
                "hotel": {"hotelId": "BAD2", "name": "Bad dates"},
                "offers": [{"id": "y", "price": {"total": "100"}}],
            },
        ]
    }
    assert normalize_hotel_offers(payload, destination="Paris") == []


# ---------------------------------------------------------------------------
# Filtering and ranking
# ---------------------------------------------------------------------------


def _hotel(
    name: str,
    price: float,
    *,
    distance: float | None = 1.0,
    rating: float | None = 80.0,
    description: str | None = "FREE WIFI\nBREAKFAST INCLUDED",
    chain: str | None = None,
    stars: float | None = None,
    destination: str = "Paris",
) -> HotelOption:
    from app.tools.hotels import detect_amenities as _detect

    return HotelOption(
        hotel_id=name,
        name=name,
        destination=destination,
        chain_code=chain,
        check_in=date(2026, 10, 10),
        check_out=date(2026, 10, 13),
        nights=3,
        price_per_night=price / 3,
        total_price=price,
        currency="INR",
        distance_km=distance,
        rating=rating,
        stars=stars,
        room_description=description,
        amenities=_detect(description),
    )


def test_filter_drops_over_budget_stays() -> None:
    hotels = [_hotel("cheap", 20_000), _hotel("dear", 200_000)]
    assert len(filter_hotels(hotels, max_total_price=50_000)) == 1


def test_filter_relaxes_rather_than_returning_nothing() -> None:
    hotels = [_hotel("only", 200_000)]
    assert len(filter_hotels(hotels, max_total_price=1_000)) == 1


def test_unrated_hotels_survive_a_rating_floor() -> None:
    """Missing data must not be treated as a bad score."""
    hotels = [
        _hotel("rated", 20_000, rating=90.0),
        _hotel("unrated", 21_000, rating=None),
    ]
    kept = {h.name for h in filter_hotels(hotels, min_rating=85.0)}
    assert kept == {"rated", "unrated"}


def test_ranking_prefers_cheaper_closer_better_rated() -> None:
    good = _hotel("good", 20_000, distance=0.5, rating=95.0)
    bad = _hotel("bad", 90_000, distance=12.0, rating=40.0)

    ranked = rank_hotels([bad, good])
    assert ranked[0].name == "good"
    assert ranked[0].score > ranked[1].score


def test_ranking_explains_itself_with_components() -> None:
    ranked = rank_hotels([_hotel("a", 20_000), _hotel("b", 40_000)])

    top = ranked[0]
    assert top.rationale and "km from centre" in top.rationale
    assert set(top.score_components) <= set(RANK_WEIGHTS)
    assert "price" in top.score_components
    assert all(0.0 <= v <= 1.0 for v in top.score_components.values())


def test_missing_factors_are_omitted_not_zeroed() -> None:
    """An unrated hotel must not be scored as if its rating were zero."""
    unrated = _hotel("unrated", 20_000, rating=None, description=None)
    ranked = rank_hotels([unrated])

    assert "rating" not in ranked[0].score_components
    assert "amenities" not in ranked[0].score_components
    assert "price" in ranked[0].score_components


def test_unrated_hotel_is_not_punished_against_a_rated_one() -> None:
    """Weight renormalisation: identical hotels, one simply lacks rating data."""
    rated_poor = _hotel("rated_poor", 20_000, rating=10.0)
    unrated = _hotel("unrated", 20_000, rating=None)

    ranked = {h.name: h.score for h in rank_hotels([rated_poor, unrated])}
    assert ranked["unrated"] > ranked["rated_poor"]


def test_preferred_chain_and_star_match_lift_the_score() -> None:
    plain = _hotel("plain", 20_000, chain="ZZ", stars=None)
    preferred = _hotel("preferred", 20_000, chain="HL", stars=4.0)

    ranked = rank_hotels(
        [plain, preferred], preferred_chain="hl", requested_stars=4
    )
    assert ranked[0].name == "preferred"


def test_interests_change_which_amenities_count() -> None:
    gym_hotel = _hotel("gym", 20_000, description="FREE WIFI\nFITNESS CENTRE")
    food_hotel = _hotel("food", 20_000, description="FREE WIFI\nBREAKFAST INCLUDED")

    wellness = {
        h.name: h.score
        for h in rank_hotels([gym_hotel, food_hotel], interests=["wellness"])
    }
    food = {
        h.name: h.score
        for h in rank_hotels([gym_hotel, food_hotel], interests=["food"])
    }

    assert wellness["gym"] > wellness["food"]
    assert food["food"] > food["gym"]


def test_ranking_caps_results_and_handles_empty() -> None:
    assert rank_hotels([]) == []
    many = [_hotel(f"h{i}", 20_000 + i * 1_000) for i in range(9)]
    assert len(rank_hotels(many, top_n=4)) == 4
