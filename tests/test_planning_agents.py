"""Budget, Itinerary and Review agents — and the guards on what an LLM may do."""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.budget import BudgetAgent, SavingsPlan, compute
from app.agents.itinerary import (
    ItineraryAgent,
    ItineraryPlan,
    build_catalogue,
    destinations_by_day,
)
from app.agents.reviewer import (
    LLMReview,
    LLMReviewIssue,
    ReviewAgent,
    guidance_from,
    run_rule_checks,
)
from app.models.travel import (
    Activity,
    DayPlan,
    FlightOption,
    FlightSegment,
    FlightSlice,
    HotelOption,
    ItineraryItem,
    Restaurant,
    ReviewIssue,
    TravelRequest,
)


def _request(**overrides) -> TravelRequest:
    base = {
        "origin": "Mumbai",
        "destinations": ["Tokyo"],
        "departure_date": date(2026, 10, 10),
        "return_date": date(2026, 10, 12),
        "travelers": 2,
        "budget": 200000,
        "currency": "INR",
    }
    base.update(overrides)
    return TravelRequest(**base)


def _flight(price: float) -> FlightOption:
    leg = FlightSlice(
        origin="BOM",
        destination="NRT",
        departure_at="2026-10-10T09:00:00",
        arrival_at="2026-10-10T18:00:00",
        duration_minutes=540,
        segments=[
            FlightSegment(
                carrier_code="AI",
                origin="BOM",
                destination="NRT",
                departure_at="2026-10-10T09:00:00",
                arrival_at="2026-10-10T18:00:00",
            )
        ],
    )
    return FlightOption(
        airline="AI",
        outbound=leg,
        inbound=leg.model_copy(update={"origin": "NRT", "destination": "BOM"}),
        price=price,
        currency="INR",
    )


def _hotel(
    price: float, destination: str = "Tokyo", score: float = 90.0
) -> HotelOption:
    return HotelOption(
        name=f"Hotel {destination} {price:.0f}",
        destination=destination,
        check_in=date(2026, 10, 10),
        check_out=date(2026, 10, 12),
        nights=2,
        price_per_night=price / 2,
        total_price=price,
        currency="INR",
        score=score,
    )


# ---------------------------------------------------------------------------
# Budget agent
# ---------------------------------------------------------------------------


def test_arithmetic_costs_alternatives_once_and_schedules_in_full() -> None:
    """The costing rule the whole project depends on, in one assertion."""
    summary = compute(
        _request(),
        flights=[_flight(60_000), _flight(80_000)],
        hotels=[
            _hotel(30_000, "Tokyo", score=90),
            _hotel(50_000, "Tokyo", score=10),
            _hotel(20_000, "Kyoto", score=80),
        ],
        activities=[
            Activity(
                activity="a", category="c", destination="Tokyo", estimated_cost=1000
            ),
            Activity(
                activity="b", category="c", destination="Tokyo", estimated_cost=2000
            ),
        ],
        restaurants=[
            Restaurant(name="r1", destination="Tokyo", price_estimate=500),
            Restaurant(name="r2", destination="Tokyo", price_estimate=700),
        ],
    )

    assert summary.breakdown.flights == 60_000, "top offer only, not the sum"
    assert summary.breakdown.hotels == 50_000, "best per destination: 30k + 20k"
    assert summary.breakdown.activities == 3_000, "schedules are summed"
    assert summary.breakdown.restaurants == 1_200


def test_arithmetic_handles_an_empty_trip() -> None:
    summary = compute(_request())
    assert summary.estimated_total == 0
    assert not summary.over_budget


def test_over_budget_is_detected() -> None:
    summary = compute(_request(budget=50_000), flights=[_flight(90_000)])
    assert summary.over_budget
    assert summary.remaining < 0


def test_default_savings_targets_the_larger_line() -> None:
    summary = compute(
        _request(budget=50_000), flights=[_flight(90_000)], hotels=[_hotel(1_000)]
    )
    plan = BudgetAgent()._default_plan(summary)

    assert plan.target == "flight"
    assert 0.4 <= plan.reduction_ratio <= 0.99
    assert "Deterministic" in plan.reasoning


def test_savings_ratio_is_sized_to_close_the_gap() -> None:
    """A token cut would just burn a retry without fixing anything."""
    summary = compute(_request(budget=100_000), flights=[_flight(150_000)])
    plan = BudgetAgent()._default_plan(summary)

    assert 150_000 * plan.reduction_ratio <= 100_000


class _StubStructured:
    def __init__(self, value) -> None:
        self._value = value

    def invoke(self, _messages: object):
        return self._value


class _StubLLM:
    def __init__(self, value, text: str = "Because it fits.") -> None:
        self.value = value
        self.text = text

    def with_structured_output(self, _schema: type) -> _StubStructured:
        return _StubStructured(self.value)

    def invoke(self, _messages: object) -> object:
        return type("Msg", (), {"content": self.text})()


class _BrokenLLM:
    def with_structured_output(self, _schema: type) -> object:
        raise RuntimeError("unavailable")

    def invoke(self, _messages: object) -> object:
        raise RuntimeError("unavailable")


def test_llm_savings_choice_is_used() -> None:
    summary = compute(
        _request(budget=50_000), flights=[_flight(90_000)], hotels=[_hotel(40_000)]
    )
    llm = _StubLLM(SavingsPlan(target="hotel", reduction_ratio=0.7, reasoning="why"))

    plan = BudgetAgent(llm=llm).choose_savings(_request(), summary)
    assert plan.target == "hotel"
    assert plan.reduction_ratio == 0.7


def test_llm_cannot_target_a_category_that_costs_nothing() -> None:
    """Cutting a zero line would waste a retry and never converge."""
    summary = compute(_request(budget=50_000), flights=[_flight(90_000)])
    llm = _StubLLM(SavingsPlan(target="hotel", reduction_ratio=0.7))

    plan = BudgetAgent(llm=llm).choose_savings(_request(), summary)
    assert plan.target == "flight", "must fall back to the line that has money in it"


def test_budget_agent_falls_back_when_the_llm_fails() -> None:
    summary = compute(_request(budget=50_000), flights=[_flight(90_000)])
    plan = BudgetAgent(llm=_BrokenLLM()).choose_savings(_request(), summary)
    assert "Deterministic" in plan.reasoning


# ---------------------------------------------------------------------------
# Itinerary agent
# ---------------------------------------------------------------------------


def _inventory():
    return {
        "flights": [_flight(60_000)],
        "hotels": [_hotel(30_000)],
        "activities": [
            Activity(
                activity="teamLab",
                category="art",
                destination="Tokyo",
                recommended_day=1,
            ),
            Activity(
                activity="Asakusa",
                category="culture",
                destination="Tokyo",
                recommended_day=2,
            ),
        ],
        "restaurants": [
            Restaurant(name="Sushi Saito", destination="Tokyo", recommended_day=1),
            Restaurant(name="Nagi", destination="Tokyo", recommended_day=2),
        ],
    }


def test_catalogue_contains_only_real_inventory() -> None:
    request = _request()
    days = destinations_by_day(
        request.destinations, request.departure_date, request.return_date
    )
    catalogue = build_catalogue(request, days, **_inventory())

    titles = {c.title for items in catalogue.values() for c in items}
    assert "teamLab" in titles
    assert "Sushi Saito" in titles
    assert all(c.id.startswith("d") for items in catalogue.values() for c in items)


def test_itinerary_without_an_llm_uses_default_times() -> None:
    request = _request()
    days = destinations_by_day(
        request.destinations, request.departure_date, request.return_date
    )
    result = ItineraryAgent().build(request, days, **_inventory())

    assert len(result.days) == request.duration_days
    for day in result.days:
        times = [i.time for i in day.items]
        assert times == sorted(times)
        assert len(times) == len(set(times)), "no day may double-book itself"


def test_llm_may_reorder_the_day() -> None:
    request = _request()
    days = destinations_by_day(
        request.destinations, request.departure_date, request.return_date
    )
    plan = ItineraryPlan(
        days=[
            {
                "day": 1,
                "items": [
                    {"id": "d1-flight-out", "time": "07:30"},
                    {"id": "d1-activity", "time": "15:45"},
                    {"id": "d1-meal", "time": "19:30"},
                    {"id": "d1-hotel", "time": "13:00"},
                ],
            }
        ],
        reasoning="early flight, settle in, then dinner",
    )
    result = ItineraryAgent(llm=_StubLLM(plan)).build(request, days, **_inventory())

    day_one = result.days[0]
    assert [i.time for i in day_one.items] == ["07:30", "13:00", "15:45", "19:30"]
    assert any("early flight" in n for n in result.notes)


def test_invented_items_are_dropped_and_reported() -> None:
    """The central guard: a model may arrange inventory, never add to it."""
    request = _request()
    days = destinations_by_day(
        request.destinations, request.departure_date, request.return_date
    )
    plan = ItineraryPlan(
        days=[
            {
                "day": 1,
                "items": [
                    {"id": "d1-activity", "time": "10:00"},
                    {"id": "d1-secret-michelin-dinner", "time": "20:00"},
                ],
            }
        ]
    )
    result = ItineraryAgent(llm=_StubLLM(plan)).build(request, days, **_inventory())

    titles = [i.title for i in result.days[0].items]
    assert "teamLab" in titles
    assert not any("michelin" in t.lower() for t in titles)
    assert any("invented" in n for n in result.notes)


def test_unusable_times_are_dropped_and_reported() -> None:
    request = _request()
    days = destinations_by_day(
        request.destinations, request.departure_date, request.return_date
    )
    plan = ItineraryPlan(
        days=[
            {
                "day": 1,
                "items": [
                    {"id": "d1-activity", "time": "half past two"},
                    {"id": "d1-meal", "time": "25:99"},
                    {"id": "d1-flight-out", "time": "09:00"},
                ],
            }
        ]
    )
    result = ItineraryAgent(llm=_StubLLM(plan)).build(request, days, **_inventory())

    assert any("unusable times" in n for n in result.notes)
    assert [i.time for i in result.days[0].items] == ["09:00"]


def test_a_day_the_llm_ignores_falls_back_to_defaults() -> None:
    request = _request()
    days = destinations_by_day(
        request.destinations, request.departure_date, request.return_date
    )
    plan = ItineraryPlan(
        days=[{"day": 1, "items": [{"id": "d1-activity", "time": "10:00"}]}]
    )
    result = ItineraryAgent(llm=_StubLLM(plan)).build(request, days, **_inventory())

    assert result.days[1].items, "day 2 must not be left empty"


def test_clashing_times_are_separated() -> None:
    """The Review agent fails same-time entries; this is the last defence."""
    request = _request()
    days = destinations_by_day(
        request.destinations, request.departure_date, request.return_date
    )
    plan = ItineraryPlan(
        days=[
            {
                "day": 1,
                "items": [
                    {"id": "d1-activity", "time": "20:00"},
                    {"id": "d1-meal", "time": "20:00"},
                    {"id": "d1-flight-out", "time": "20:00"},
                ],
            }
        ]
    )
    result = ItineraryAgent(llm=_StubLLM(plan)).build(request, days, **_inventory())

    times = [i.time for i in result.days[0].items]
    assert len(times) == len(set(times))


def test_itinerary_falls_back_when_the_llm_fails() -> None:
    request = _request()
    days = destinations_by_day(
        request.destinations, request.departure_date, request.return_date
    )
    result = ItineraryAgent(llm=_BrokenLLM()).build(request, days, **_inventory())
    assert all(day.items for day in result.days)


# ---------------------------------------------------------------------------
# Review agent
# ---------------------------------------------------------------------------


def _itinerary(request: TravelRequest) -> list[DayPlan]:
    return [
        DayPlan(
            day=index + 1,
            date=request.departure_date.fromordinal(
                request.departure_date.toordinal() + index
            ),
            destination="Tokyo",
            items=[ItineraryItem(time="14:00", title="thing", kind="activity")],
        )
        for index in range(request.duration_days)
    ]


def test_rule_checks_pass_a_sound_plan() -> None:
    request = _request()
    issues = run_rule_checks(
        request,
        itinerary=_itinerary(request),
        flights=[_flight(1000)],
        hotels=[_hotel(1000)],
    )
    assert [i for i in issues if i.severity == "error"] == []


def test_rule_checks_catch_missing_inventory_and_bad_dates() -> None:
    request = _request()
    issues = run_rule_checks(request, itinerary=[], flights=[], hotels=[])
    checks = {i.check for i in issues}
    assert {"flights", "hotels", "itinerary_length"} <= checks


def test_rule_checks_catch_an_uncovered_destination() -> None:
    request = _request(destinations=["Tokyo", "Kyoto"])
    issues = run_rule_checks(
        request,
        itinerary=_itinerary(request),  # every day is Tokyo
        flights=[_flight(1000)],
        hotels=[_hotel(1000)],
    )
    assert any(i.check == "destination_coverage" for i in issues)


def test_rule_checks_catch_a_night_with_no_hotel() -> None:
    request = _request(destinations=["Tokyo", "Kyoto"])
    itinerary = _itinerary(request)
    itinerary[0].destination = "Kyoto"
    issues = run_rule_checks(
        request,
        itinerary=itinerary,
        flights=[_flight(1000)],
        hotels=[_hotel(1000, "Tokyo")],
    )
    assert any(i.check == "accommodation_gap" for i in issues)


def test_llm_findings_are_added_when_they_cite_a_real_day() -> None:
    request = _request()
    llm = _StubLLM(
        LLMReview(
            issues=[
                LLMReviewIssue(day=1, severity="warning", detail="tight transfer")
            ]
        )
    )
    result = ReviewAgent(llm=llm).review(
        request,
        itinerary=_itinerary(request),
        flights=[_flight(1000)],
        hotels=[_hotel(1000)],
    )
    assert any(i.check == "review_agent" for i in result.issues)
    assert result.verdict == "PASS", "a warning must not fail the trip"


def test_llm_cannot_fail_a_trip_for_a_day_that_does_not_exist() -> None:
    """The guard on the gate: no hallucinated day may block a plan."""
    request = _request()
    llm = _StubLLM(
        LLMReview(
            issues=[LLMReviewIssue(day=99, severity="error", detail="chaos on day 99")]
        )
    )
    result = ReviewAgent(llm=llm).review(
        request,
        itinerary=_itinerary(request),
        flights=[_flight(1000)],
        hotels=[_hotel(1000)],
    )
    assert result.verdict == "PASS"
    assert not any("99" in i.detail for i in result.issues)


def test_llm_errors_on_a_real_day_do_fail_the_trip() -> None:
    request = _request()
    llm = _StubLLM(
        LLMReview(
            issues=[
                LLMReviewIssue(day=1, severity="error", detail="activity during flight")
            ]
        )
    )
    result = ReviewAgent(llm=llm).review(
        request,
        itinerary=_itinerary(request),
        flights=[_flight(1000)],
        hotels=[_hotel(1000)],
    )
    assert result.verdict == "FAIL"


def test_review_survives_a_broken_llm() -> None:
    request = _request()
    result = ReviewAgent(llm=_BrokenLLM()).review(
        request,
        itinerary=_itinerary(request),
        flights=[_flight(1000)],
        hotels=[_hotel(1000)],
    )
    assert result.verdict == "PASS", "rule checks must still stand on their own"


# ---------------------------------------------------------------------------
# Replan guidance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check", "fragment"),
    [
        ("budget", "cheaper"),
        ("flights", "relax flight"),
        ("hotels", "widen the hotel"),
        ("schedule_conflict", "re-time"),
        ("destination_coverage", "cover every"),
    ],
)
def test_each_failure_produces_actionable_guidance(check: str, fragment: str) -> None:
    """Without this the replan repeats the same search and fails identically."""
    guidance = guidance_from([ReviewIssue(check=check, detail="x", severity="error")])
    assert any(fragment in g for g in guidance)


def test_warnings_alone_produce_no_guidance() -> None:
    guidance = guidance_from(
        [ReviewIssue(check="budget", detail="x", severity="warning")]
    )
    assert guidance == []
