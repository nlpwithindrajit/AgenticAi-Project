"""Flight agent: orchestration, LLM fallback, and the no-hallucination rule."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.agents.flight import FlightAgent, SearchPlan
from app.config import get_settings
from app.models.travel import TravelRequest
from app.tools.flights import AmadeusClient, FlightSearchError, SerpApiClient
from tests.test_flights_tool import AMADEUS_PAYLOAD
from tests.test_serpapi_tool import SERPAPI_PAYLOAD, SERPAPI_RETURN_PAYLOAD

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


# ---------------------------------------------------------------------------
# SerpAPI path — the two-call round trip
# ---------------------------------------------------------------------------


def _serpapi_agent(handler, llm: object | None = None) -> FlightAgent:
    client = SerpApiClient(
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return FlightAgent(client=client, llm=llm)


def _serpapi_handler(
    failing_tokens: set[str] | None = None,
    calls: list[str] | None = None,
):
    """Answers the outbound search, then the per-token return-leg searches."""
    failing = failing_tokens or set()

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("departure_token")
        if calls is not None:
            calls.append(token or "OUTBOUND")
        if token is None:
            return httpx.Response(200, json=SERPAPI_PAYLOAD)
        if token in failing:
            return httpx.Response(500, json={"error": "provider exploded"})
        return httpx.Response(200, json=SERPAPI_RETURN_PAYLOAD)

    return handler


@pytest.fixture
def return_lookups(monkeypatch):
    """Set SERPAPI_RETURN_LOOKUPS for one test, cache-clearing either side."""

    def _set(value: int) -> None:
        monkeypatch.setenv("SERPAPI_RETURN_LOOKUPS", str(value))
        get_settings.cache_clear()

    get_settings.cache_clear()
    yield _set
    monkeypatch.delenv("SERPAPI_RETURN_LOOKUPS", raising=False)
    get_settings.cache_clear()


def test_injected_client_selects_the_provider() -> None:
    """A stub SerpAPI client must win over whatever the environment says."""
    assert _serpapi_agent(_serpapi_handler()).provider == "serpapi"
    assert _agent(_ok_handler).provider == "amadeus"


def test_serpapi_agent_completes_round_trips(
    sample_request: TravelRequest,
) -> None:
    result = _serpapi_agent(_serpapi_handler()).run(sample_request)

    assert result.recommendations
    assert all(f.source == "serpapi" for f in result.recommendations)
    best = result.recommendations[0]
    assert best.inbound is not None, "a round trip must carry its return leg"
    assert best.outbound.origin == "BOM"
    assert best.inbound.origin == "DXB"


def test_return_price_supersedes_the_outbound_estimate(
    sample_request: TravelRequest,
) -> None:
    """The second call prices the specific outbound+inbound pair, and the
    cheapest pairing (28,900) beats the 30,100 shown against the first."""
    result = _serpapi_agent(_serpapi_handler()).run(sample_request)
    priced = [f for f in result.recommendations if f.inbound is not None]

    assert priced
    assert all(f.price == 28900 for f in priced)
    assert all(
        f.price_per_traveler == round(28900 / sample_request.travelers, 2)
        for f in priced
    )


def test_total_duration_counts_both_directions(
    sample_request: TravelRequest,
) -> None:
    best = _serpapi_agent(_serpapi_handler()).run(sample_request).recommendations[0]

    assert best.inbound is not None
    assert best.total_duration_minutes == (
        (best.outbound.duration_minutes or 0) + (best.inbound.duration_minutes or 0)
    )
    assert best.total_duration_minutes > (best.outbound.duration_minutes or 0)


def test_one_return_search_per_shortlisted_option(
    sample_request: TravelRequest,
) -> None:
    """Each completed itinerary costs a billable search — no hidden extras."""
    calls: list[str] = []
    _serpapi_agent(_serpapi_handler(calls=calls)).run(sample_request)

    assert calls[0] == "OUTBOUND"
    assert sorted(calls[1:]) == ["TOKEN_ETIHAD", "TOKEN_INDIGO"]
    assert len(calls) == 3, "one outbound search plus one per shortlisted option"


def test_lookup_budget_caps_the_number_of_second_calls(
    sample_request: TravelRequest, return_lookups
) -> None:
    return_lookups(1)
    calls: list[str] = []
    result = _serpapi_agent(_serpapi_handler(calls=calls)).run(sample_request)

    assert len(calls) == 2, "one outbound search plus a single return lookup"
    assert len(result.recommendations) == 1


def test_zero_lookups_skips_the_second_call_entirely(
    sample_request: TravelRequest, return_lookups
) -> None:
    return_lookups(0)
    calls: list[str] = []
    result = _serpapi_agent(_serpapi_handler(calls=calls)).run(sample_request)

    assert calls == ["OUTBOUND"]
    assert result.recommendations
    assert all(f.inbound is None for f in result.recommendations)
    # The round-trip price is still correct — say what is missing, not more.
    assert any("return legs not priced" in note for note in result.notes)
    assert result.recommendations[0].price == 28599


def test_failed_return_lookup_keeps_the_outbound_option(
    sample_request: TravelRequest,
) -> None:
    """Losing the return detail must not lose a real flight."""
    handler = _serpapi_handler(failing_tokens={"TOKEN_INDIGO", "TOKEN_ETIHAD"})
    result = _serpapi_agent(handler).run(sample_request)

    assert len(result.recommendations) == 2
    assert all(f.inbound is None for f in result.recommendations)
    assert any("not priced" in note for note in result.notes)


def test_complete_itineraries_outrank_partial_ones(
    sample_request: TravelRequest,
) -> None:
    """A missing return leg must not read as a shorter journey."""
    handler = _serpapi_handler(failing_tokens={"TOKEN_INDIGO"})
    result = _serpapi_agent(handler).run(sample_request)

    assert result.recommendations[0].inbound is not None
    assert result.recommendations[-1].inbound is None


def test_offer_id_is_readable_rather_than_a_provider_token(
    sample_request: TravelRequest,
) -> None:
    result = _serpapi_agent(_serpapi_handler()).run(sample_request)
    ids = [f.offer_id for f in result.recommendations]

    assert "6E1451-6E1456" in ids
    assert not any(i and i.startswith("TOKEN_") for i in ids)


def test_non_stop_request_reaches_both_serpapi_calls() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("stops"))
        if request.url.params.get("departure_token") is None:
            return httpx.Response(200, json=SERPAPI_PAYLOAD)
        return httpx.Response(200, json=SERPAPI_RETURN_PAYLOAD)

    request = TravelRequest(
        origin="Mumbai",
        destinations=["Dubai"],
        departure_date=date(2026, 10, 10),
        return_date=date(2026, 10, 17),
        travelers=1,
        budget=200000,
        direct_flights_only=True,
    )
    _serpapi_agent(handler).run(request)

    assert seen and all(value == "1" for value in seen)


def test_price_cap_is_not_sent_to_the_provider(
    sample_request: TravelRequest,
) -> None:
    """`filter_flights` relaxes a cap that would empty the list; SerpAPI's
    server-side filter would just return nothing instead."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        if request.url.params.get("departure_token") is None:
            return httpx.Response(200, json=SERPAPI_PAYLOAD)
        return httpx.Response(200, json=SERPAPI_RETURN_PAYLOAD)

    llm = _StubLLM(SearchPlan(max_price=1.0))
    result = _serpapi_agent(handler, llm=llm).run(sample_request)

    assert "max_price" not in seen
    assert result.recommendations, "an impossible cap must relax, not empty the list"


def test_serpapi_no_results_is_reported_not_raised(
    sample_request: TravelRequest,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": "Google Flights hasn't returned any results for this query."
            },
        )

    result = _serpapi_agent(handler).run(sample_request)
    assert result.recommendations == []
    assert any("no usable flight offers" in note for note in result.notes)


def test_serpapi_outbound_failure_propagates(
    sample_request: TravelRequest,
) -> None:
    """The graph catches this and falls back to STUB inventory."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(FlightSearchError):
        _serpapi_agent(handler).run(sample_request)
