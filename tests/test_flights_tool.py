"""Amadeus client, normalization, filtering and ranking — all HTTP mocked."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.models.travel import FlightOption, FlightSegment, FlightSlice
from app.tools.flights import (
    AmadeusClient,
    FlightSearchError,
    filter_flights,
    normalize_offers,
    parse_iso_duration,
    rank_flights,
)

# A trimmed but structurally faithful Amadeus flight-offers payload: one
# round-trip offer carrying two itineraries under a single price.
AMADEUS_PAYLOAD = {
    "meta": {"count": 2},
    "data": [
        {
            "type": "flight-offer",
            "id": "1",
            "oneWay": False,
            "numberOfBookableSeats": 4,
            "itineraries": [
                {
                    "duration": "PT14H15M",
                    "segments": [
                        {
                            "departure": {
                                "iataCode": "BOM",
                                "at": "2026-10-10T02:30:00",
                            },
                            "arrival": {
                                "iataCode": "SIN",
                                "at": "2026-10-10T10:45:00",
                            },
                            "carrierCode": "SQ",
                            "number": "421",
                            "aircraft": {"code": "359"},
                            "duration": "PT5H45M",
                            "id": "1",
                            "numberOfStops": 0,
                        },
                        {
                            "departure": {
                                "iataCode": "SIN",
                                "at": "2026-10-10T13:00:00",
                            },
                            "arrival": {
                                "iataCode": "NRT",
                                "at": "2026-10-10T21:15:00",
                            },
                            "carrierCode": "SQ",
                            "number": "638",
                            "aircraft": {"code": "77W"},
                            "duration": "PT7H15M",
                            "id": "2",
                            "numberOfStops": 0,
                        },
                    ],
                },
                {
                    "duration": "PT13H05M",
                    "segments": [
                        {
                            "departure": {
                                "iataCode": "NRT",
                                "at": "2026-10-15T18:00:00",
                            },
                            "arrival": {
                                "iataCode": "BOM",
                                "at": "2026-10-16T02:05:00",
                            },
                            "carrierCode": "SQ",
                            "number": "639",
                            "aircraft": {"code": "77W"},
                            "duration": "PT13H05M",
                            "id": "3",
                            "numberOfStops": 0,
                        }
                    ],
                },
            ],
            "price": {
                "currency": "INR",
                "total": "118000.00",
                "grandTotal": "121500.00",
            },
            "validatingAirlineCodes": ["SQ"],
        },
        {
            "type": "flight-offer",
            "id": "2",
            "itineraries": [
                {
                    "duration": "PT9H30M",
                    "segments": [
                        {
                            "departure": {
                                "iataCode": "BOM",
                                "at": "2026-10-10T23:50:00",
                            },
                            "arrival": {
                                "iataCode": "NRT",
                                "at": "2026-10-11T11:20:00",
                            },
                            "carrierCode": "AI",
                            "number": "358",
                            "aircraft": {"code": "788"},
                            "duration": "PT9H30M",
                            "id": "1",
                            "numberOfStops": 0,
                        }
                    ],
                }
            ],
            "price": {"currency": "INR", "grandTotal": "96000.00"},
            "validatingAirlineCodes": ["AI"],
        },
    ],
    "dictionaries": {"carriers": {"SQ": "SINGAPORE AIRLINES", "AI": "AIR INDIA"}},
}


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT14H15M", 855),
        ("PT7H", 420),
        ("PT45M", 45),
        ("P1DT2H30M", 1590),
        ("", None),
        (None, None),
        ("not-a-duration", None),
    ],
)
def test_parse_iso_duration(value: str | None, expected: int | None) -> None:
    assert parse_iso_duration(value) == expected


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_round_trip_offer_becomes_one_option() -> None:
    """Two itineraries at one price must not become two recommendations."""
    options = normalize_offers(AMADEUS_PAYLOAD, travelers=2)

    assert len(options) == 2
    round_trip = options[0]
    assert round_trip.inbound is not None
    assert round_trip.price == 121500.00  # grandTotal wins over total
    assert round_trip.price_per_traveler == 60750.00
    assert round_trip.currency == "INR"


def test_normalization_reads_segments_stops_and_carrier_names() -> None:
    round_trip, one_way = normalize_offers(AMADEUS_PAYLOAD, travelers=1)

    assert round_trip.airline == "SQ"
    assert round_trip.airline_name == "SINGAPORE AIRLINES"
    assert round_trip.outbound.origin == "BOM"
    assert round_trip.outbound.destination == "NRT"
    assert round_trip.outbound.stops == 1, "two segments is one connection"
    assert round_trip.outbound.duration_minutes == 855
    assert round_trip.outbound.segments[0].flight_number == "SQ421"
    assert round_trip.outbound.segments[0].aircraft == "359"
    assert round_trip.total_duration_minutes == 855 + 785

    assert one_way.inbound is None
    assert one_way.stops == 0
    assert one_way.airline_name == "AIR INDIA"


def test_offers_with_unusable_price_are_skipped_not_crashed() -> None:
    payload = {
        "data": [
            {"id": "9", "itineraries": AMADEUS_PAYLOAD["data"][1]["itineraries"],
             "price": {"currency": "INR", "grandTotal": "not-a-number"}},
            AMADEUS_PAYLOAD["data"][1],
        ],
        "dictionaries": {"carriers": {}},
    }
    options = normalize_offers(payload)
    assert len(options) == 1


def test_offer_with_no_segments_is_skipped() -> None:
    payload = {
        "data": [
            {
                "id": "9",
                "itineraries": [{"duration": "PT2H", "segments": []}],
                "price": {"grandTotal": "100"},
            }
        ]
    }
    assert normalize_offers(payload) == []


# ---------------------------------------------------------------------------
# Filtering and ranking
# ---------------------------------------------------------------------------


def _option(
    price: float, stops: int, airline: str = "XX", hour: str = "09"
) -> FlightOption:
    segments = [
        FlightSegment(
            carrier_code=airline,
            origin="AAA",
            destination="BBB",
            departure_at=f"2026-10-10T{hour}:00:00",
            arrival_at="2026-10-10T18:00:00",
            duration_minutes=540,
        )
        for _ in range(stops + 1)
    ]
    return FlightOption(
        airline=airline,
        outbound=FlightSlice(
            origin="AAA",
            destination="BBB",
            departure_at=f"2026-10-10T{hour}:00:00",
            arrival_at="2026-10-10T18:00:00",
            duration_minutes=540 + stops * 120,
            segments=segments,
        ),
        price=price,
        currency="INR",
    )


def test_filter_drops_over_budget_options() -> None:
    flights = [_option(50_000, 0), _option(200_000, 0)]
    assert len(filter_flights(flights, max_price=100_000)) == 1


def test_filter_relaxes_rather_than_returning_nothing() -> None:
    """An empty result is worse than a violated preference — say so, don't fail."""
    only_connecting = [_option(50_000, 1), _option(60_000, 2)]
    assert len(filter_flights(only_connecting, direct_only=True)) == 2

    all_expensive = [_option(500_000, 0)]
    assert len(filter_flights(all_expensive, max_price=1_000)) == 1


def test_filter_prefers_requested_airline_when_available() -> None:
    flights = [_option(50_000, 0, "AI"), _option(60_000, 0, "SQ")]
    result = filter_flights(flights, preferred_airline="sq")
    assert [f.airline for f in result] == ["SQ"]


def test_ranking_prefers_cheaper_and_more_direct() -> None:
    cheap_direct = _option(50_000, 0)
    dear_connecting = _option(150_000, 2)
    ranked = rank_flights([dear_connecting, cheap_direct])

    assert ranked[0].price == 50_000
    assert ranked[0].score > ranked[1].score
    assert 0 <= ranked[1].score <= 100


def test_ranking_explains_itself_and_caps_results() -> None:
    ranked = rank_flights([_option(10_000 * i, i % 3) for i in range(1, 9)], top_n=5)

    assert len(ranked) == 5
    assert all(f.rationale for f in ranked), "every recommendation must say why"
    assert "stop(s)" in ranked[0].rationale


def test_ranking_penalises_red_eye_departures() -> None:
    daytime = _option(50_000, 0, hour="09")
    red_eye = _option(50_000, 0, hour="03")
    ranked = rank_flights([red_eye, daytime])
    assert ranked[0].outbound.departure_at.endswith("T09:00:00")


def test_ranking_empty_input() -> None:
    assert rank_flights([]) == []


# ---------------------------------------------------------------------------
# HTTP client — mocked transport, no network
# ---------------------------------------------------------------------------


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _amadeus(handler) -> AmadeusClient:
    return AmadeusClient(
        client_id="id",
        client_secret="secret",
        base_url="https://test.api.amadeus.com",
        http_client=_mock_client(handler),
    )


def test_token_is_fetched_then_reused() -> None:
    calls = {"token": 0, "search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            calls["token"] += 1
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1799})
        calls["search"] += 1
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json=AMADEUS_PAYLOAD)

    client = _amadeus(handler)
    client.search_flights("BOM", "NRT", date(2026, 10, 10), date(2026, 10, 15))
    client.search_flights("BOM", "NRT", date(2026, 10, 10), date(2026, 10, 15))

    assert calls["search"] == 2
    assert calls["token"] == 1, "token must be cached across calls"


def test_search_sends_expected_query_parameters() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1799})
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=AMADEUS_PAYLOAD)

    _amadeus(handler).search_flights(
        "BOM",
        "NRT",
        date(2026, 10, 10),
        date(2026, 10, 15),
        adults=2,
        currency="INR",
        non_stop=True,
    )

    assert captured["originLocationCode"] == "BOM"
    assert captured["destinationLocationCode"] == "NRT"
    assert captured["departureDate"] == "2026-10-10"
    assert captured["returnDate"] == "2026-10-15"
    assert captured["adults"] == "2"
    assert captured["currencyCode"] == "INR"
    assert captured["nonStop"] == "true"


def test_one_way_search_omits_return_date() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1799})
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=AMADEUS_PAYLOAD)

    _amadeus(handler).search_flights("BOM", "NRT", date(2026, 10, 10))
    assert "returnDate" not in captured


def test_city_name_is_resolved_to_iata_and_cached() -> None:
    lookups = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1799})
        if "reference-data/locations" in request.url.path:
            lookups["count"] += 1
            return httpx.Response(
                200, json={"data": [{"iataCode": "BOM", "name": "MUMBAI"}]}
            )
        return httpx.Response(200, json=AMADEUS_PAYLOAD)

    client = _amadeus(handler)
    assert client.resolve_location_code("Mumbai") == "BOM"
    assert client.resolve_location_code("mumbai") == "BOM"
    assert lookups["count"] == 1, "resolved codes must be cached"


def test_existing_iata_code_skips_the_lookup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1799})
        raise AssertionError("should not call the locations endpoint")

    assert _amadeus(handler).resolve_location_code("bom") == "BOM"


def test_unresolvable_place_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1799})
        return httpx.Response(200, json={"data": []})

    with pytest.raises(FlightSearchError, match="no IATA code"):
        _amadeus(handler).resolve_location_code("Atlantis")


def test_auth_failure_does_not_leak_the_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client", "secret": "secret"})

    with pytest.raises(FlightSearchError) as exc:
        _amadeus(handler).search_flights("BOM", "NRT", date(2026, 10, 10))

    assert "secret" not in str(exc.value)
    assert "authentication failed" in str(exc.value)


def test_api_error_surfaces_amadeus_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1799})
        return httpx.Response(
            400, json={"errors": [{"detail": "Date/Time is in the past"}]}
        )

    with pytest.raises(FlightSearchError, match="Date/Time is in the past"):
        _amadeus(handler).search_flights("BOM", "NRT", date(2020, 1, 1))


def test_network_failure_becomes_flight_search_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    with pytest.raises(FlightSearchError, match="could not reach Amadeus"):
        _amadeus(handler).search_flights("BOM", "NRT", date(2026, 10, 10))


def test_missing_credentials_raises_before_any_request() -> None:
    client = AmadeusClient(client_id=None, client_secret=None)
    assert not client.configured
    with pytest.raises(FlightSearchError, match="not configured"):
        client.search_flights("BOM", "NRT", date(2026, 10, 10))
