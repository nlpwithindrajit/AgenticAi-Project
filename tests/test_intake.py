"""Conversational intake: sentence -> TravelRequest, without guessing."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.agents.intake import (
    IntakeAgent,
    TripDraft,
    extract_deterministically,
)
from app.main import app

TODAY = date(2026, 8, 1)
client = TestClient(app)


# ---------------------------------------------------------------------------
# Deterministic extraction
# ---------------------------------------------------------------------------


def test_reads_the_worked_example_from_the_design_doc() -> None:
    draft = extract_deterministically(
        "Plan a 5-day trip to Tokyo and Kyoto for 2 people from Mumbai, "
        "October 10-15, with a budget of ₹2,00,000. Prefer 4-star hotels "
        "and direct flights.",
        TODAY,
    )

    assert draft.origin == "Mumbai"
    assert draft.destinations == ["Tokyo", "Kyoto"]
    assert draft.departure_date == date(2026, 10, 10)
    assert draft.return_date == date(2026, 10, 15)
    assert draft.travelers == 2
    assert draft.budget == 200000
    assert draft.currency == "INR"
    assert draft.hotel_stars == 4
    assert draft.direct_flights_only is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("budget of ₹2,00,000", 200000),
        ("2 lakh budget", 200000),
        ("around 200k", 200000),
        ("budget $3,500", 3500),
        ("1.5 crore", 15000000),
    ],
)
def test_amount_shapes(text: str, expected: float) -> None:
    assert extract_deterministically(text, TODAY).budget == expected


def test_a_party_size_is_not_mistaken_for_a_budget() -> None:
    """"2 people" must never become a £2 budget."""
    draft = extract_deterministically("a trip for 2 people", TODAY)
    assert draft.travelers == 2
    assert draft.budget is None


def test_iso_dates_and_day_counts() -> None:
    draft = extract_deterministically(
        "from Mumbai to Tokyo on 2026-10-10, 4 days", TODAY
    )
    assert draft.departure_date == date(2026, 10, 10)
    assert draft.return_date == date(2026, 10, 13)


def test_a_past_month_rolls_to_next_year() -> None:
    """In August, "March 3" means next March, not one five months gone."""
    draft = extract_deterministically("leaving March 3", TODAY)
    assert draft.departure_date == date(2027, 3, 3)


def test_the_rule_based_path_needs_an_explicit_preposition() -> None:
    """A documented limit of the fallback, not a bug.

    Without an LLM there is no way to tell a destination from any other
    capitalised word, so a bare "Tokyo" is not read as one. With
    ANTHROPIC_API_KEY set, the model handles this and much looser phrasing.
    """
    assert extract_deterministically("Tokyo next month", TODAY).destinations == []
    assert extract_deterministically("to Tokyo next month", TODAY).destinations == [
        "Tokyo"
    ]


def test_nothing_is_invented_from_an_empty_message() -> None:
    draft = extract_deterministically("hello", TODAY)
    assert draft.budget is None
    assert draft.departure_date is None
    assert draft.origin is None


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------


def test_missing_budget_is_asked_for_not_guessed() -> None:
    """A guessed budget would silently drive every search and the whole loop."""
    result = IntakeAgent().read(
        "Trip to Tokyo from Mumbai on 2026-10-10", today=TODAY
    )

    assert result.request is None
    assert "budget" in result.missing
    assert "budget" in result.reply.lower()


def test_a_follow_up_turn_completes_the_request() -> None:
    agent = IntakeAgent()
    first = agent.read("Trip to Tokyo from Mumbai on 2026-10-10", today=TODAY)
    second = agent.read("budget ₹1,50,000", draft=first.draft, today=TODAY)

    assert second.request is not None
    assert second.request.origin == "Mumbai"
    assert second.request.budget == 150000
    assert second.missing == []


def test_a_later_turn_does_not_erase_an_earlier_answer() -> None:
    agent = IntakeAgent()
    first = agent.read(
        "from Mumbai to Tokyo on 2026-10-10 budget 200k", today=TODAY
    )
    second = agent.read("actually make it 3 people", draft=first.draft, today=TODAY)

    assert second.draft.origin == "Mumbai"
    assert second.draft.budget == 200000
    assert second.draft.travelers == 3


def test_an_assumed_return_date_is_stated_out_loud() -> None:
    """Any default that shapes the search has to be visible."""
    result = IntakeAgent().read(
        "from Mumbai to Tokyo on 2026-10-10, budget 200k", today=TODAY
    )

    assert result.request is not None
    assert "assumed" in result.reply.lower()


def test_the_result_is_a_real_travel_request() -> None:
    """Whatever the front door, the agents still receive the schema."""
    result = IntakeAgent().read(
        "from Mumbai to Tokyo 2026-10-10 to 2026-10-15, 2 people, budget 200k",
        today=TODAY,
    )
    assert result.request is not None
    assert result.request.duration_days == 6


# ---------------------------------------------------------------------------
# The conversation with a model attached
#
# The deterministic path never sets `follow_up`, so these are the only tests
# that exercise how a model's question interacts with the accumulated draft —
# which is exactly where the loop was.
# ---------------------------------------------------------------------------


class _StubStructured:
    def __init__(self, agent: _StubLLM) -> None:
        self._agent = agent

    def invoke(self, messages: object) -> TripDraft:
        self._agent.prompts.append(str(messages))
        return self._agent.value


class _StubLLM:
    """A model that answers with one fixed draft and records what it was asked."""

    def __init__(self, value: TripDraft) -> None:
        self.value = value
        self.prompts: list[str] = []

    def with_structured_output(self, _schema: type) -> _StubStructured:
        return _StubStructured(self)


def test_the_question_is_about_the_draft_not_the_last_message() -> None:
    """The gap that matters is the conversation's, not this one sentence's.

    A model shown only "3rd September" cannot see that the destination is
    already known, so its `follow_up` asks for it again. Echoing that question
    is what made the chat loop: the traveller answers, the draft is already
    full, and the same question comes back.
    """
    known = TripDraft(destinations=["Bali"])
    llm = _StubLLM(TripDraft(departure_date=date(2026, 9, 3)))

    result = IntakeAgent(llm).read("3rd September", draft=known, today=TODAY)

    assert result.draft.destinations == ["Bali"]
    assert result.missing == ["origin", "budget"]
    assert "destination" not in result.reply.lower()
    assert "from" in result.reply.lower()


def test_the_draft_carries_no_question_of_its_own() -> None:
    """The draft is trip facts. A question stored in it is a question that can
    go stale and be replayed a turn later."""
    result = IntakeAgent(_StubLLM(TripDraft(origin="Mumbai"))).read(
        "from Mumbai", today=TODAY
    )

    assert "follow_up" not in result.draft.model_dump()


def test_the_model_is_told_what_is_already_known() -> None:
    """Without the draft, "London" is a coin-flip between origin and destination."""
    llm = _StubLLM(TripDraft(origin="London"))
    known = TripDraft(destinations=["Bali"], departure_date=date(2026, 9, 3))

    IntakeAgent(llm).read("London", draft=known, today=TODAY)

    prompt = llm.prompts[0]
    assert "Bali" in prompt
    assert "2026-09-03" in prompt
    # The unanswered fields have to be nameable, so a bare reply lands in the
    # slot the traveller was actually asked about.
    assert "origin" in prompt and "budget" in prompt


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_chat_asks_before_planning() -> None:
    response = client.post("/chat", json={"message": "I want to go to Tokyo"})
    body = response.json()

    assert response.status_code == 200
    assert body["ready"] is False
    assert body["plan"] is None
    assert body["missing"]


def test_chat_plans_once_the_request_is_complete() -> None:
    response = client.post(
        "/chat",
        json={
            "message": (
                "from Mumbai to Tokyo and Kyoto, 2026-10-10 to 2026-10-15, "
                "2 people, budget ₹2,00,000"
            )
        },
    )
    body = response.json()

    assert body["ready"] is True
    assert body["request"]["origin"] == "Mumbai"
    assert body["plan"] is not None
    assert body["plan"]["review"]["verdict"] in {"PASS", "FAIL"}
    assert len(body["plan"]["daily_itinerary"]) == 6


def test_chat_can_collect_without_planning() -> None:
    """`plan_when_ready=false` keeps a turn cheap while gathering details."""
    response = client.post(
        "/chat",
        json={
            "message": "from Mumbai to Tokyo 2026-10-10, budget 200k",
            "plan_when_ready": False,
        },
    )
    body = response.json()

    assert body["ready"] is True
    assert body["plan"] is None


def test_chat_carries_the_draft_between_turns() -> None:
    first = client.post("/chat", json={"message": "to Tokyo from Mumbai"}).json()
    second = client.post(
        "/chat",
        json={
            "message": "2026-10-10, budget 200k",
            "draft": first["draft"],
            "plan_when_ready": False,
        },
    ).json()

    assert second["ready"] is True
    assert second["request"]["destinations"] == ["Tokyo"]


def test_chat_rejects_an_empty_message() -> None:
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_chat_reports_whether_a_model_was_used() -> None:
    """Without a key this is rule-based, and the response says so."""
    body = client.post(
        "/chat", json={"message": "Tokyo", "plan_when_ready": False}
    ).json()
    assert body["used_llm"] is False


@pytest.mark.parametrize(
    ("spoken", "code"),
    [
        ("rupees", "INR"),
        ("Rupee", "INR"),
        ("dollars", "USD"),
        ("euro", "EUR"),
        ("pounds", "GBP"),
        ("yen", "JPY"),
        ("inr", "INR"),
    ],
)
def test_a_spoken_currency_becomes_a_code(spoken: str, code: str) -> None:
    """"80,000 rupees" reads back as a currency on every screen it reaches."""
    assert TripDraft(currency=spoken).currency == code


def test_an_unknown_currency_is_left_alone() -> None:
    """Better an unfamiliar name than a wrong code on every price."""
    assert TripDraft(currency="Swiss francs").currency == "Swiss francs"


def test_draft_merge_ignores_blanks() -> None:
    base = TripDraft(origin="Mumbai", budget=1000)
    merged = base.merge(TripDraft(origin=None, destinations=["Tokyo"]))

    assert merged.origin == "Mumbai"
    assert merged.budget == 1000
    assert merged.destinations == ["Tokyo"]
