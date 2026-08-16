"""SerpAPI Google Flights: transport error semantics and normalisation.

The payloads below are trimmed from real responses (BOM->DXB, October 2026), so
the field names, the `"YYYY-MM-DD HH:MM"` timestamps and the round-trip pricing
model are Google's rather than something invented to make the parser pass.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.tools.errors import FlightSearchError
from app.tools.flights import (
    SerpApiClient,
    SerpApiError,
    flight_number_id,
    normalize_serpapi_offers,
    serpapi_itineraries,
)

# One non-stop and one one-stop itinerary, as returned by `type=1`.
# `price` is the whole round-trip fare for all passengers, and each option
# carries a `departure_token` for the second call.
SERPAPI_PAYLOAD = {
    "search_parameters": {"currency": "INR", "type": "1"},
    "best_flights": [
        {
            "flights": [
                {
                    "departure_airport": {
                        "name": "Chhatrapati Shivaji Maharaj International Airport",
                        "id": "BOM",
                        "time": "2026-10-10 08:10",
                    },
                    "arrival_airport": {
                        "name": "Dubai International Airport",
                        "id": "DXB",
                        "time": "2026-10-10 09:40",
                    },
                    "duration": 180,
                    "airplane": "Airbus A321neo",
                    "airline": "IndiGo",
                    "travel_class": "Economy",
                    "flight_number": "6E 1451",
                }
            ],
            "total_duration": 180,
            "price": 28599,
            "type": "Round trip",
            "departure_token": "TOKEN_INDIGO",
        }
    ],
    "other_flights": [
        {
            "flights": [
                {
                    "departure_airport": {"id": "BOM", "time": "2026-10-10 04:00"},
                    "arrival_airport": {"id": "AUH", "time": "2026-10-10 05:35"},
                    "duration": 185,
                    "airline": "Etihad",
                    "flight_number": "EY 205",
                },
                {
                    "departure_airport": {"id": "AUH", "time": "2026-10-10 07:25"},
                    "arrival_airport": {"id": "DXB", "time": "2026-10-10 08:15"},
                    "duration": 50,
                    "airline": "Emirates",
                    "flight_number": "EK 1",
                },
            ],
            "layovers": [{"duration": 110, "name": "Zayed International", "id": "AUH"}],
            "total_duration": 345,
            "price": 41000,
            "type": "Round trip",
            "departure_token": "TOKEN_ETIHAD",
        }
    ],
}

# The second call: `flights` is the RETURN leg only, `price` is the total for
# the outbound+inbound pair (and here it beats the outbound-only estimate).
SERPAPI_RETURN_PAYLOAD = {
    "search_parameters": {"currency": "INR", "type": "1"},
    "best_flights": [
        {
            "flights": [
                {
                    "departure_airport": {"id": "DXB", "time": "2026-10-17 10:40"},
                    "arrival_airport": {"id": "BOM", "time": "2026-10-17 15:30"},
                    "duration": 200,
                    "airline": "IndiGo",
                    "flight_number": "6E 1452",
                }
            ],
            "total_duration": 200,
            "price": 30100,
            "type": "Round trip",
            "booking_token": "BOOK_ME",
        },
        {
            "flights": [
                {
                    "departure_airport": {"id": "DXB", "time": "2026-10-17 22:05"},
                    "arrival_airport": {"id": "BOM", "time": "2026-10-18 02:55"},
                    "duration": 200,
                    "airline": "IndiGo",
                    "flight_number": "6E 1456",
                }
            ],
            "total_duration": 200,
            "price": 28900,
            "type": "Round trip",
            "booking_token": "BOOK_ME_CHEAPER",
        },
    ],
}


def _client(handler) -> SerpApiClient:
    return SerpApiClient(
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _ok(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_normalises_both_best_and_other_flights() -> None:
    offers = normalize_serpapi_offers(SERPAPI_PAYLOAD, travelers=2)
    assert len(offers) == 2
    assert all(o.source == "serpapi" for o in offers)


def test_timestamps_are_converted_to_iso() -> None:
    """Google separates date and time with a space; everything downstream —
    the departure-hour scoring, the itinerary agent, the UI — reads ISO."""
    outbound = normalize_serpapi_offers(SERPAPI_PAYLOAD)[0].outbound
    assert outbound.departure_at == "2026-10-10T08:10"
    assert outbound.arrival_at == "2026-10-10T09:40"


def test_carrier_code_is_derived_from_the_flight_number() -> None:
    """Google gives an airline *name*; the IATA code is the number's prefix."""
    option = normalize_serpapi_offers(SERPAPI_PAYLOAD)[0]
    assert option.airline == "6E"
    assert option.airline_name == "IndiGo"
    assert option.outbound.segments[0].flight_number == "6E1451"


def test_price_is_the_total_and_per_traveler_divides_it() -> None:
    """Verified against the live API: `price` doubles when adults goes 1 -> 2."""
    option = normalize_serpapi_offers(SERPAPI_PAYLOAD, travelers=2)[0]
    assert option.price == 28599
    assert option.price_per_traveler == round(28599 / 2, 2)


def test_currency_comes_from_the_echoed_search_parameters() -> None:
    option = normalize_serpapi_offers(SERPAPI_PAYLOAD, currency="USD")[0]
    assert option.currency == "INR", "the provider's echo beats the caller's guess"


def test_currency_falls_back_to_the_request_when_not_echoed() -> None:
    payload = {**SERPAPI_PAYLOAD, "search_parameters": {}}
    assert normalize_serpapi_offers(payload, currency="USD")[0].currency == "USD"


def test_connection_becomes_one_stop() -> None:
    connecting = normalize_serpapi_offers(SERPAPI_PAYLOAD)[1]
    assert connecting.outbound.stops == 1
    assert len(connecting.outbound.segments) == 2


def test_mixed_carriers_are_labelled_rather_than_named_after_the_first() -> None:
    connecting = normalize_serpapi_offers(SERPAPI_PAYLOAD)[1]
    assert connecting.airline_name == "Multiple airlines"


def test_slice_spans_origin_to_final_destination() -> None:
    connecting = normalize_serpapi_offers(SERPAPI_PAYLOAD)[1]
    assert connecting.outbound.origin == "BOM"
    assert connecting.outbound.destination == "DXB"
    assert connecting.outbound.duration_minutes == 345


def test_missing_total_duration_sums_legs_and_layovers() -> None:
    option = {
        k: v for k, v in SERPAPI_PAYLOAD["other_flights"][0].items()
        if k != "total_duration"
    }
    itineraries = serpapi_itineraries({"best_flights": [option]})
    # 185 + 50 flying, plus the 110-minute layover.
    assert itineraries[0].slice.duration_minutes == 345


def test_unpriceable_itinerary_is_skipped_not_guessed() -> None:
    payload = {
        "best_flights": [{"flights": [{"flight_number": "XX 1"}], "price": None}]
    }
    assert normalize_serpapi_offers(payload) == []


def test_itinerary_without_segments_is_skipped() -> None:
    assert normalize_serpapi_offers({"best_flights": [{"price": 100}]}) == []


def test_empty_payload_yields_no_offers() -> None:
    assert normalize_serpapi_offers({}) == []


def test_token_is_carried_on_the_offer_id_for_the_second_call() -> None:
    assert normalize_serpapi_offers(SERPAPI_PAYLOAD)[0].offer_id == "TOKEN_INDIGO"


def test_return_payload_parses_as_the_inbound_direction() -> None:
    itineraries = serpapi_itineraries(SERPAPI_RETURN_PAYLOAD)
    assert len(itineraries) == 2
    assert itineraries[0].slice.origin == "DXB"
    assert itineraries[0].slice.destination == "BOM"
    assert itineraries[0].price == 30100


def test_flight_number_id_names_both_directions() -> None:
    offers = normalize_serpapi_offers(SERPAPI_PAYLOAD)
    inbound = serpapi_itineraries(SERPAPI_RETURN_PAYLOAD)[0].slice
    complete = offers[0].model_copy(update={"inbound": inbound})
    assert flight_number_id(complete) == "6E1451-6E1452"


def test_flight_number_id_is_none_when_nothing_is_numbered() -> None:
    payload = {
        "best_flights": [
            {
                "flights": [
                    {
                        "departure_airport": {"id": "BOM", "time": "2026-10-10 08:00"},
                        "arrival_airport": {"id": "DXB", "time": "2026-10-10 09:30"},
                        "duration": 180,
                    }
                ],
                "total_duration": 180,
                "price": 100,
            }
        ]
    }
    assert flight_number_id(normalize_serpapi_offers(payload)[0]) is None


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def test_search_sends_the_google_flights_parameters() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=SERPAPI_PAYLOAD)

    _client(handler).search_flights(
        "Mumbai", "Dubai", date(2026, 10, 10), date(2026, 10, 17), adults=2
    )

    assert seen["engine"] == "google_flights"
    assert seen["departure_id"] == "BOM", "city names must be resolved to IATA"
    assert seen["arrival_id"] == "DXB"
    assert seen["outbound_date"] == "2026-10-10"
    assert seen["return_date"] == "2026-10-17"
    assert seen["adults"] == "2"
    assert seen["type"] == "1", "a return date means a round trip"


def test_one_way_search_when_no_return_date() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=SERPAPI_PAYLOAD)

    _client(handler).search_flights("BOM", "DXB", date(2026, 10, 10))
    assert seen["type"] == "2"
    assert "return_date" not in seen


def test_non_stop_is_the_only_server_side_filter() -> None:
    """Price and airline stay client-side, where `filter_flights` can relax
    them; SerpAPI would just return nothing instead."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=SERPAPI_PAYLOAD)

    _client(handler).search_flights(
        "BOM", "DXB", date(2026, 10, 10), date(2026, 10, 17), non_stop=True
    )
    assert seen["stops"] == "1"
    assert "max_price" not in seen
    assert "include_airlines" not in seen


def test_return_leg_search_repeats_the_query_with_the_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=SERPAPI_RETURN_PAYLOAD)

    _client(handler).search_return_legs(
        "BOM",
        "DXB",
        date(2026, 10, 10),
        date(2026, 10, 17),
        departure_token="TOKEN_INDIGO",
    )

    # The token identifies the outbound choice, not the search, so the whole
    # original query has to travel with it.
    assert seen["departure_token"] == "TOKEN_INDIGO"
    assert seen["departure_id"] == "BOM"
    assert seen["return_date"] == "2026-10-17"
    assert seen["type"] == "1"


def test_no_results_is_not_an_error() -> None:
    """SerpAPI answers 200 with an `error` string when Google simply had
    nothing — a route nobody flies must not crash the graph."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": "Google Flights hasn't returned any results for this query."
            },
        )

    payload = _client(handler).search_flights("BOM", "DXB", date(2026, 10, 10))
    assert normalize_serpapi_offers(payload) == []


def test_other_errors_at_200_still_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "Something actually broke"})

    with pytest.raises(SerpApiError, match="Something actually broke"):
        _client(handler).search_flights("BOM", "DXB", date(2026, 10, 10))


def test_rejected_key_does_not_leak_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid API key: test-key"})

    with pytest.raises(SerpApiError) as exc:
        _client(handler).search_flights("BOM", "DXB", date(2026, 10, 10))

    assert "test-key" not in str(exc.value)
    assert "rejected the API key" in str(exc.value)


def test_quota_exhaustion_is_named() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "run out of searches"})

    with pytest.raises(SerpApiError, match="rate limit or search quota"):
        _client(handler).search_flights("BOM", "DXB", date(2026, 10, 10))


def test_bad_request_surfaces_the_provider_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "`outbound_date` cannot be in the past."}
        )

    with pytest.raises(SerpApiError, match="cannot be in the past"):
        _client(handler).search_flights("BOM", "DXB", date(2020, 1, 1))


def test_non_json_error_body_does_not_mask_the_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>gateway</html>")

    with pytest.raises(SerpApiError, match="HTTP 502"):
        _client(handler).search_flights("BOM", "DXB", date(2026, 10, 10))


def test_network_failure_becomes_a_flight_search_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    with pytest.raises(SerpApiError, match="could not reach SerpAPI"):
        _client(handler).search_flights("BOM", "DXB", date(2026, 10, 10))


def test_missing_key_raises_before_any_request() -> None:
    client = SerpApiClient(api_key=None)
    assert not client.configured
    with pytest.raises(SerpApiError, match="not configured"):
        client.search_flights("BOM", "DXB", date(2026, 10, 10))


def test_unknown_city_fails_before_spending_a_search() -> None:
    """Resolution failure is not a SerpAPI fault, so it raises the base
    `FlightSearchError` — which the graph catches just the same, without
    having burned a billable search on a query that could not be sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the API with an unresolved place")

    with pytest.raises(FlightSearchError, match="no airport code known"):
        _client(handler).search_flights("Atlantis", "DXB", date(2026, 10, 10))


def test_api_key_is_sent_but_kept_out_of_traced_params() -> None:
    from app.tools.serpapi import _safe

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=SERPAPI_PAYLOAD)

    _client(handler).search_flights("BOM", "DXB", date(2026, 10, 10))
    assert seen["api_key"] == "test-key"

    traced = _safe({"engine": "google_flights", "api_key": "k", "departure_token": "t"})
    assert traced == {"engine": "google_flights"}
