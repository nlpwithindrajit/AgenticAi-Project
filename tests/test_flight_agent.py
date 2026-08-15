"""Flight agent: orchestration, LLM fallback, and the no-hallucination rule."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.agents.flight import FlightAgent, SearchPlan
from app.models.travel import TravelRequest
from app.tools.flights import AmadeusClient, FlightSearchError
from tests.test_flights_tool import AMADEUS_PAYLOAD

# City -> IATA answers the agent needs before it can search at all.
_IATA = {"mumbai": "BOM", "tokyo": "NRT", "kyoto": "KIX"}


def _with_common_endpoints(handler):
    """Wrap a search handler so auth and IATA lookups are always answered."""

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 1799}
            )
        if "reference-data/locations" in request.url.path:
            keyword = request.url.params.get("keyword", "").lower()
            code = _IATA.get(keyword)
            data = [{"iataCode": code, "name": keyword.upper()}] if code else []
            return httpx.Response(200, json={"data": data})
        return handler(request)

    return wrapped


def _agent(handler, llm: object | None = None) -> FlightAgent:
    client = AmadeusClient(
        client_id="id",
        client_secret="secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(_with_common_endpoints(handler))
        ),
    )
    return FlightAgent(client=client, llm=llm)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=AMADEUS_PAYLOAD)


def test_agent_returns_ranked_recommendations(sample_request: TravelRequest) -> None:
    result = _agent(_ok_handler).run(sample_request)

    assert result.raw_count == 2
    assert result.recommendations
    scores = [f.score for f in result.recommendations]
    assert scores == sorted(scores, reverse=True), "must be ranked best-first"
    assert all(f.source == "amadeus" for f in result.recommendations)


def test_agent_only_recommends_flights_the_provider_returned(
    sample_request: TravelRequest,
) -> None:
    """The core no-hallucination guarantee for this agent."""
    result = _agent(_ok_handler).run(sample_request)

    returned_airlines = {"SQ", "AI"}
    assert {f.airline for f in result.recommendations} <= returned_airlines
    assert len(result.recommendations) <= result.raw_count


def test_agent_returns_nothing_when_provider_has_no_offers(
    sample_request: TravelRequest,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "dictionaries": {}})

    result = _agent(handler).run(sample_request)
    assert result.recommendations == []
    assert any("no usable flight offers" in note for note in result.notes)


def test_provider_failure_propagates(sample_request: TravelRequest) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(FlightSearchError):
        _agent(handler).run(sample_request)


def test_multi_city_limitation_is_recorded_not_hidden(
    sample_request: TravelRequest,
) -> None:
    """Tokyo + Kyoto is priced as one round trip — say so rather than pretend."""
    result = _agent(_ok_handler).run(sample_request)
    assert any("multi-city" in note for note in result.notes)


def test_single_destination_records_no_multi_city_note() -> None:
    request = TravelRequest(
        origin="Mumbai",
        destinations=["Tokyo"],
        departure_date=date(2026, 10, 10),
        return_date=date(2026, 10, 15),
        travelers=2,
        budget=200000,
    )
    result = _agent(_ok_handler).run(request)
    assert not any("multi-city" in note for note in result.notes)


# ---------------------------------------------------------------------------
# LLM planning layer
# ---------------------------------------------------------------------------


class _StubStructured:
    def __init__(self, plan: SearchPlan) -> None:
        self._plan = plan

    def invoke(self, _messages: object) -> SearchPlan:
        return self._plan


class _StubLLM:
    """Minimal stand-in for ChatAnthropic — no network, no API key."""

    def __init__(self, plan: SearchPlan, text: str = "Best value overall.") -> None:
        self.plan = plan
        self.text = text
        self.explained = False

    def with_structured_output(self, _schema: type) -> _StubStructured:
        return _StubStructured(self.plan)

    def invoke(self, _messages: object) -> object:
        self.explained = True
        return type("Msg", (), {"content": self.text})()


class _BrokenLLM:
    def with_structured_output(self, _schema: type) -> object:
        raise RuntimeError("model unavailable")

    def invoke(self, _messages: object) -> object:
        raise RuntimeError("model unavailable")


def test_llm_plan_drives_the_provider_query(sample_request: TravelRequest) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=AMADEUS_PAYLOAD)

    llm = _StubLLM(SearchPlan(non_stop=True, max_results=7, max_price=90_000))
    _agent(handler, llm=llm).run(sample_request)

    assert captured["nonStop"] == "true"
    assert captured["max"] == "7"


def test_llm_writes_the_top_recommendation_rationale(
    sample_request: TravelRequest,
) -> None:
    llm = _StubLLM(SearchPlan(), text="Cheapest non-stop within budget.")
    result = _agent(_ok_handler, llm=llm).run(sample_request)

    assert llm.explained
    assert result.recommendations[0].rationale == "Cheapest non-stop within budget."
    # Runners-up keep their deterministic scoring reasons.
    assert "stop(s)" in result.recommendations[1].rationale


def test_agent_falls_back_to_deterministic_plan_when_llm_fails(
    sample_request: TravelRequest,
) -> None:
    agent = _agent(_ok_handler, llm=_BrokenLLM())
    plan = agent.plan_search(sample_request)

    assert plan.max_price == pytest.approx(200000 * 0.45)
    assert "Deterministic plan" in plan.reasoning


def test_agent_works_with_no_llm_at_all(sample_request: TravelRequest) -> None:
    """No ANTHROPIC_API_KEY must still yield real, ranked flights."""
    result = _agent(_ok_handler).run(sample_request)

    assert result.recommendations
    assert "Deterministic plan" in result.plan.reasoning
    assert all(f.rationale for f in result.recommendations)
