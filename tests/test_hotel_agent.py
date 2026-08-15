"""Hotel agent: stay splitting, three-call orchestration, LLM fallback."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.agents.hotel import HotelAgent, HotelSearchPlan, split_stay
from app.models.travel import TravelRequest
from app.tools.amadeus import AmadeusClient, HotelSearchError
from tests.test_hotels_tool import (
    HOTEL_LIST_PAYLOAD,
    HOTEL_OFFERS_PAYLOAD,
    RATINGS_PAYLOAD,
)

_IATA = {"mumbai": "BOM", "tokyo": "TYO", "kyoto": "UKY", "paris": "PAR"}


def _handler(
    *,
    list_payload: dict | None = None,
    offers_payload: dict | None = None,
    ratings_status: int = 200,
    calls: dict[str, int] | None = None,
):
    """A mock Amadeus serving all four endpoints the hotel agent touches."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if calls is not None:
            for key in ("token", "locations", "by-city", "hotel-offers", "sentiments"):
                if key in path or (key == "token" and path.endswith("/token")):
                    calls[key] = calls.get(key, 0) + 1

        if path.endswith("/oauth2/token"):
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 1799}
            )
        if "hotels/by-city" in path:
            return httpx.Response(
                200,
                json=list_payload
                if list_payload is not None
                else HOTEL_LIST_PAYLOAD,
            )
        if "reference-data/locations" in path:
            keyword = request.url.params.get("keyword", "").lower()
            code = _IATA.get(keyword)
            data = [{"iataCode": code}] if code else []
            return httpx.Response(200, json={"data": data})
        if "hotel-sentiments" in path:
            if ratings_status != 200:
                return httpx.Response(
                    ratings_status, json={"errors": [{"detail": "no sentiment data"}]}
                )
            return httpx.Response(200, json=RATINGS_PAYLOAD)
        if "hotel-offers" in path:
            return httpx.Response(
                200,
                json=offers_payload
                if offers_payload is not None
                else HOTEL_OFFERS_PAYLOAD,
            )
        return httpx.Response(404, json={"errors": [{"detail": f"unmocked {path}"}]})

    return handle


def _agent(handler, llm: object | None = None) -> HotelAgent:
    client = AmadeusClient(
        client_id="id",
        client_secret="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return HotelAgent(client=client, llm=llm)


# ---------------------------------------------------------------------------
# Stay splitting
# ---------------------------------------------------------------------------


def test_split_stay_divides_nights_in_order() -> None:
    stays = split_stay(["Tokyo", "Kyoto"], date(2026, 10, 10), date(2026, 10, 15))

    assert [s[0] for s in stays] == ["Tokyo", "Kyoto"]
    # 5 nights over 2 cities: the remainder goes to the earlier city.
    assert (stays[0][2] - stays[0][1]).days == 3
    assert (stays[1][2] - stays[1][1]).days == 2
    assert stays[0][1] == date(2026, 10, 10)
    assert stays[1][2] == date(2026, 10, 15)


def test_split_stay_is_contiguous_with_no_gaps() -> None:
    stays = split_stay(
        ["A", "B", "C"], date(2026, 10, 10), date(2026, 10, 17)
    )
    for earlier, later in zip(stays, stays[1:], strict=False):
        assert earlier[2] == later[1], "check-out must equal the next check-in"


def test_split_stay_single_destination_covers_whole_trip() -> None:
    stays = split_stay(["Tokyo"], date(2026, 10, 10), date(2026, 10, 15))
    assert stays == [("Tokyo", date(2026, 10, 10), date(2026, 10, 15))]


def test_split_stay_never_leaves_a_zero_night_booking() -> None:
    """More cities than nights must still give every city a bed."""
    stays = split_stay(["A", "B", "C"], date(2026, 10, 10), date(2026, 10, 11))
    assert len(stays) == 3
    assert all((s[2] - s[1]).days >= 1 for s in stays)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _request(destinations: list[str] | None = None) -> TravelRequest:
    return TravelRequest(
        origin="Mumbai",
        destinations=destinations or ["Paris"],
        departure_date=date(2026, 10, 10),
        return_date=date(2026, 10, 13),
        travelers=2,
        budget=200000,
        currency="INR",
        hotel_stars=4,
        interests=["food"],
    )


def test_agent_returns_ranked_hotels() -> None:
    result = _agent(_handler()).run(_request())

    assert result.raw_count == 2
    assert result.recommendations
    scores = [h.score for h in result.recommendations]
    assert scores == sorted(scores, reverse=True)
    assert all(h.source == "amadeus" for h in result.recommendations)


def test_agent_only_recommends_hotels_the_provider_returned() -> None:
    result = _agent(_handler()).run(_request())
    assert {h.hotel_id for h in result.recommendations} <= {"HLPAR266", "ACPARH29"}


def test_agent_makes_all_three_calls_per_destination() -> None:
    calls: dict[str, int] = {}
    _agent(_handler(calls=calls)).run(_request())

    assert calls.get("by-city") == 1
    assert calls.get("sentiments") == 1
    assert calls.get("hotel-offers") == 1


def test_agent_searches_every_destination() -> None:
    calls: dict[str, int] = {}
    result = _agent(_handler(calls=calls)).run(_request(["Tokyo", "Kyoto"]))

    assert calls.get("by-city") == 2
    assert {h.destination for h in result.recommendations} == {"Tokyo", "Kyoto"}


def test_missing_ratings_do_not_fail_the_search() -> None:
    """Sentiment data is a bonus signal; losing it must not lose the hotels."""
    result = _agent(_handler(ratings_status=500)).run(_request())

    assert result.recommendations, "hotels must still be returned"
    assert any("guest ratings unavailable" in note for note in result.notes)
    assert all(h.rating is None for h in result.recommendations)


def test_star_filter_is_relaxed_rather_than_returning_nothing() -> None:
    """An empty star-filtered city retries unfiltered and says so."""
    state = {"first": True}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "hotels/by-city" in path and "ratings" in request.url.params:
            return httpx.Response(200, json={"data": []})
        return _handler()(request)

    result = _agent(handler).run(_request())
    assert result.recommendations
    assert any("searched without the star filter" in n for n in result.notes)
    # Stars were not confirmed, so none may be claimed.
    assert all(h.stars is None for h in result.recommendations)
    assert state["first"]


def test_empty_city_is_reported_not_hidden() -> None:
    result = _agent(_handler(list_payload={"data": []})).run(_request())

    assert result.recommendations == []
    assert any("no hotels listed" in note for note in result.notes)


def test_no_bookable_offers_is_reported() -> None:
    result = _agent(_handler(offers_payload={"data": []})).run(_request())

    assert result.recommendations == []
    assert any("no bookable hotel offers" in note for note in result.notes)


def test_provider_failure_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 1799}
            )
        raise httpx.ConnectError("down")

    with pytest.raises(HotelSearchError):
        _agent(handler).run(_request())


# ---------------------------------------------------------------------------
# LLM planning layer
# ---------------------------------------------------------------------------


class _StubStructured:
    def __init__(self, plan: HotelSearchPlan) -> None:
        self._plan = plan

    def invoke(self, _messages: object) -> HotelSearchPlan:
        return self._plan


class _StubLLM:
    def __init__(self, plan: HotelSearchPlan, text: str = "Central and cheap.") -> None:
        self.plan = plan
        self.text = text
        self.explain_calls = 0

    def with_structured_output(self, _schema: type) -> _StubStructured:
        return _StubStructured(self.plan)

    def invoke(self, _messages: object) -> object:
        self.explain_calls += 1
        return type("Msg", (), {"content": self.text})()


class _BrokenLLM:
    def with_structured_output(self, _schema: type) -> object:
        raise RuntimeError("model unavailable")

    def invoke(self, _messages: object) -> object:
        raise RuntimeError("model unavailable")


def test_llm_plan_drives_the_search_radius() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "hotels/by-city" in request.url.path:
            captured.update(dict(request.url.params))
        return _handler()(request)

    llm = _StubLLM(HotelSearchPlan(search_radius_km=7, max_total_price=50_000))
    _agent(handler, llm=llm).run(_request())

    assert captured["radius"] == "7"


def test_llm_explains_the_best_hotel_per_destination() -> None:
    llm = _StubLLM(HotelSearchPlan(), text="Closest to the centre for the price.")
    result = _agent(_handler(), llm=llm).run(_request(["Tokyo", "Kyoto"]))

    explained = [
        h for h in result.recommendations
        if h.rationale == "Closest to the centre for the price."
    ]
    assert len(explained) == 2, "one explanation per destination"
    assert llm.explain_calls == 2


def test_agent_falls_back_to_deterministic_plan_when_llm_fails() -> None:
    plan = _agent(_handler(), llm=_BrokenLLM()).plan_search(_request())

    assert plan.max_total_price == pytest.approx(200000 * 0.40)
    assert "Deterministic plan" in plan.reasoning


def test_agent_works_with_no_llm_at_all() -> None:
    result = _agent(_handler()).run(_request())

    assert result.recommendations
    assert "Deterministic plan" in result.plan.reasoning
    assert all(h.rationale for h in result.recommendations)
