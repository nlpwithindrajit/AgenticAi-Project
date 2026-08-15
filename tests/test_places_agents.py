"""Activity and Restaurant agents: anchoring, scheduling, LLM fallback."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.agents.activity import ActivityAgent, ActivitySearchPlan
from app.agents.places_base import hotel_anchor_for, resolve_anchor
from app.agents.restaurant import RestaurantAgent, RestaurantSearchPlan
from app.models.travel import HotelOption, TravelRequest
from app.tools.amadeus import AmadeusClient, PlacesSearchError
from tests.test_places_tool import ACTIVITIES_PAYLOAD, POI_PAYLOAD

CITY_PAYLOAD = {
    "data": [
        {
            "type": "location",
            "subType": "city",
            "name": "Tokyo",
            "iataCode": "TYO",
            "geoCode": {"latitude": 35.6895, "longitude": 139.6917},
        }
    ]
}


def _handler(
    *,
    activities: dict | None = None,
    poi: dict | None = None,
    cities: dict | None = None,
    captured: dict | None = None,
):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/token"):
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 1799}
            )
        if "locations/cities" in path:
            if captured is not None:
                captured["cities"] = captured.get("cities", 0) + 1
            return httpx.Response(
                200, json=cities if cities is not None else CITY_PAYLOAD
            )
        if "shopping/activities" in path:
            if captured is not None:
                captured.update(dict(request.url.params))
            return httpx.Response(
                200, json=activities if activities is not None else ACTIVITIES_PAYLOAD
            )
        if "pointsOfInterest" in path:
            if captured is not None:
                captured.update(dict(request.url.params))
            return httpx.Response(200, json=poi if poi is not None else POI_PAYLOAD)
        return httpx.Response(404, json={"errors": [{"detail": f"unmocked {path}"}]})

    return handle


def _client(handler) -> AmadeusClient:
    return AmadeusClient(
        client_id="id",
        client_secret="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _request(**overrides) -> TravelRequest:
    base = {
        "origin": "Mumbai",
        "destinations": ["Tokyo"],
        "departure_date": date(2026, 10, 10),
        "return_date": date(2026, 10, 13),
        "travelers": 2,
        "budget": 200000,
        "currency": "INR",
        "interests": ["food"],
    }
    base.update(overrides)
    return TravelRequest(**base)


def _hotel(lat: float, lon: float, score: float = 90.0) -> HotelOption:
    return HotelOption(
        name="Test Hotel",
        destination="Tokyo",
        check_in=date(2026, 10, 10),
        check_out=date(2026, 10, 13),
        nights=3,
        price_per_night=1000,
        total_price=3000,
        currency="INR",
        latitude=lat,
        longitude=lon,
        score=score,
    )


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------


def test_anchor_prefers_the_recommended_hotel() -> None:
    captured: dict = {}
    client = _client(_handler(captured=captured))
    anchor = resolve_anchor(client, "Tokyo", hotel_anchor=(35.1, 139.1))

    assert anchor == (35.1, 139.1)
    assert "cities" not in captured, "city lookup should be skipped entirely"


def test_anchor_falls_back_to_city_centre() -> None:
    anchor = resolve_anchor(_client(_handler()), "Tokyo")
    assert anchor == pytest.approx((35.6895, 139.6917))


def test_anchor_failure_is_explicit_rather_than_arbitrary() -> None:
    """Searching from a wrong point returns plausible results for nowhere."""
    with pytest.raises(PlacesSearchError, match="no coordinates"):
        resolve_anchor(_client(_handler(cities={"data": []})), "Atlantis")


def test_hotel_anchor_picks_the_best_scoring_hotel() -> None:
    hotels = [_hotel(1.0, 1.0, score=50.0), _hotel(2.0, 2.0, score=95.0)]
    assert hotel_anchor_for("Tokyo", hotels) == (2.0, 2.0)


def test_hotel_anchor_ignores_other_destinations_and_missing_coords() -> None:
    kyoto = _hotel(1.0, 1.0)
    kyoto.destination = "Kyoto"
    no_coords = _hotel(0.0, 0.0)
    no_coords.latitude = None

    assert hotel_anchor_for("Tokyo", [kyoto, no_coords]) is None
    assert hotel_anchor_for("Tokyo", None) is None


# ---------------------------------------------------------------------------
# Activity agent
# ---------------------------------------------------------------------------


def test_activity_agent_schedules_one_per_day() -> None:
    result = ActivityAgent(client=_client(_handler())).run(
        _request(), days_by_destination={"Tokyo": [1, 2, 3]}
    )

    assert [a.recommended_day for a in result.scheduled] == [1, 2, 3]
    assert len({a.activity for a in result.scheduled}) == 3, "no repeats"
    assert all(a.source == "amadeus" for a in result.scheduled)


def test_activity_agent_only_schedules_what_the_provider_returned() -> None:
    result = ActivityAgent(client=_client(_handler())).run(
        _request(), days_by_destination={"Tokyo": [1, 2, 3]}
    )
    provider_names = {
        "Tokyo Food Tour: Tsukiji Market Tasting",
        "teamLab Borderless Digital Art Museum",
        "Meiji Shrine Walk",
    }
    assert {a.activity for a in result.scheduled} <= provider_names


def test_activity_agent_searches_around_the_hotel() -> None:
    captured: dict = {}
    ActivityAgent(client=_client(_handler(captured=captured))).run(
        _request(),
        days_by_destination={"Tokyo": [1]},
        hotels=[_hotel(35.55, 139.55)],
    )
    assert float(captured["latitude"]) == pytest.approx(35.55)
    assert "cities" not in captured


def test_activity_agent_reports_a_short_schedule() -> None:
    result = ActivityAgent(client=_client(_handler())).run(
        _request(), days_by_destination={"Tokyo": [1, 2, 3, 4, 5, 6, 7]}
    )
    assert len(result.scheduled) < 7
    assert any("only" in note and "activities available" in note
               for note in result.notes)


def test_activity_agent_reports_an_empty_city() -> None:
    result = ActivityAgent(client=_client(_handler(activities={"data": []}))).run(
        _request(), days_by_destination={"Tokyo": [1]}
    )
    assert result.scheduled == []
    assert any("no activities found" in note for note in result.notes)


def test_activity_agent_covers_every_destination() -> None:
    result = ActivityAgent(client=_client(_handler())).run(
        _request(destinations=["Tokyo", "Kyoto"]),
        days_by_destination={"Tokyo": [1, 2], "Kyoto": [3]},
    )
    assert {a.destination for a in result.scheduled} == {"Tokyo", "Kyoto"}
    assert [a.recommended_day for a in result.scheduled] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Restaurant agent
# ---------------------------------------------------------------------------


def test_restaurant_agent_schedules_one_per_day() -> None:
    result = RestaurantAgent(client=_client(_handler())).run(
        _request(), days_by_destination={"Tokyo": [1, 2]}
    )
    assert [r.recommended_day for r in result.scheduled] == [1, 2]
    assert len({r.name for r in result.scheduled}) == 2


def test_restaurant_agent_never_invents_a_venue() -> None:
    """projectIdea.md §11: venues must come from the search, always."""
    result = RestaurantAgent(client=_client(_handler())).run(
        _request(), days_by_destination={"Tokyo": [1, 2]}
    )
    assert {r.name for r in result.scheduled} <= {"Sushi Saito", "Nagi Shokudo"}


def test_restaurant_prices_are_always_flagged_as_estimates() -> None:
    result = RestaurantAgent(client=_client(_handler())).run(
        _request(), days_by_destination={"Tokyo": [1, 2]}
    )
    assert all(r.price_is_estimated for r in result.scheduled)
    assert all(r.estimate_basis for r in result.scheduled)
    assert any("prices are estimates" in note for note in result.notes)


def test_restaurant_agent_requests_only_the_restaurant_category() -> None:
    captured: dict = {}
    RestaurantAgent(client=_client(_handler(captured=captured))).run(
        _request(), days_by_destination={"Tokyo": [1]}
    )
    assert captured["categories"] == "RESTAURANT"


def test_unconfirmable_dietary_needs_are_flagged() -> None:
    """Never imply venues were vetted for a diet when tags cannot show it."""
    poi = {
        "data": [
            {
                "id": "1",
                "name": "Plain Grill",
                "category": "RESTAURANT",
                "geoCode": {"latitude": 35.0, "longitude": 139.0},
                "tags": ["restaurant", "grill"],
            }
        ]
    }
    result = RestaurantAgent(client=_client(_handler(poi=poi))).run(
        _request(dietary_preferences=["halal"]),
        days_by_destination={"Tokyo": [1]},
    )
    assert result.scheduled
    assert any("check menus before booking" in note for note in result.notes)


def test_restaurant_agent_reports_an_empty_city() -> None:
    result = RestaurantAgent(client=_client(_handler(poi={"data": []}))).run(
        _request(), days_by_destination={"Tokyo": [1]}
    )
    assert result.scheduled == []
    assert any("no restaurants found" in note for note in result.notes)


# ---------------------------------------------------------------------------
# LLM layer
# ---------------------------------------------------------------------------


class _StubStructured:
    def __init__(self, plan) -> None:
        self._plan = plan

    def invoke(self, _messages: object):
        return self._plan


class _StubLLM:
    def __init__(self, plan, text: str = "Chosen for your interests.") -> None:
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


def test_llm_plan_drives_the_activity_radius() -> None:
    captured: dict = {}
    llm = _StubLLM(ActivitySearchPlan(radius_km=11, max_cost_per_activity=9_000))
    ActivityAgent(client=_client(_handler(captured=captured)), llm=llm).run(
        _request(), days_by_destination={"Tokyo": [1]}
    )
    assert captured["radius"] == "11"


def test_llm_plan_drives_the_restaurant_radius() -> None:
    captured: dict = {}
    llm = _StubLLM(RestaurantSearchPlan(radius_km=2))
    RestaurantAgent(client=_client(_handler(captured=captured)), llm=llm).run(
        _request(), days_by_destination={"Tokyo": [1]}
    )
    assert captured["radius"] == "2"


def test_llm_explains_the_activity_schedule_once() -> None:
    llm = _StubLLM(ActivitySearchPlan(), text="A food-led few days.")
    result = ActivityAgent(client=_client(_handler()), llm=llm).run(
        _request(), days_by_destination={"Tokyo": [1, 2]}
    )
    assert result.scheduled[0].rationale == "A food-led few days."
    assert llm.explain_calls == 1


def test_agents_fall_back_to_deterministic_plans_when_the_llm_fails() -> None:
    activity_plan = ActivityAgent(
        client=_client(_handler()), llm=_BrokenLLM()
    ).plan_search(_request())
    restaurant_plan = RestaurantAgent(
        client=_client(_handler()), llm=_BrokenLLM()
    ).plan_search(_request())

    assert "Deterministic plan" in activity_plan.reasoning
    assert "Deterministic plan" in restaurant_plan.reasoning


def test_agents_work_with_no_llm_at_all() -> None:
    activities = ActivityAgent(client=_client(_handler())).run(
        _request(), days_by_destination={"Tokyo": [1, 2]}
    )
    restaurants = RestaurantAgent(client=_client(_handler())).run(
        _request(), days_by_destination={"Tokyo": [1, 2]}
    )

    assert activities.scheduled and all(a.rationale for a in activities.scheduled)
    assert restaurants.scheduled and all(r.rationale for r in restaurants.scheduled)
