"""Review agent — the quality gate, and the source of the replanning loop.

projectIdea.md §14 splits this into constraint validation (dates, travellers,
budget) and logical validation (two things at once, impossible transfers).

Rules do the first kind well and are kept as the authoritative checks: they are
exact, they cannot hallucinate, and they are what the graph's PASS/FAIL edge
depends on. The LLM does a second pass for the things rules read poorly — a
plan that is individually valid but collectively silly.

The LLM's findings are constrained rather than trusted: each must cite a day
that exists, and anything citing a day we do not have is discarded. A model
cannot fail a trip for a day that was never in it.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.agents.places_base import build_llm
from app.models.travel import (
    Activity,
    BudgetSummary,
    DayPlan,
    FlightOption,
    HotelOption,
    Restaurant,
    ReviewIssue,
    ReviewResult,
    TravelRequest,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You review a finished travel itinerary for problems.

Report only concrete, checkable problems you can see in the itinerary given to \
you, and cite the day number for each. Do not speculate about opening hours, \
prices or availability you were not told about, and do not invent days.

Use severity "error" only for something that makes the trip unworkable — an \
impossible transfer, an activity during a flight, a night with no bed. \
Anything a traveller could live with is a "warning".

If the itinerary looks sound, return no issues at all. Finding nothing is a \
valid and useful result."""


class LLMReviewIssue(BaseModel):
    day: int = Field(description="The day number this problem is on.")
    severity: str = Field(default="warning", description='"error" or "warning".')
    detail: str = Field(description="What is wrong, in one sentence.")


class LLMReview(BaseModel):
    issues: list[LLMReviewIssue] = Field(default_factory=list)


def run_rule_checks(
    request: TravelRequest,
    *,
    itinerary: list[DayPlan] | None = None,
    flights: list[FlightOption] | None = None,
    hotels: list[HotelOption] | None = None,
    activities: list[Activity] | None = None,
    restaurants: list[Restaurant] | None = None,
    budget: BudgetSummary | None = None,
) -> list[ReviewIssue]:
    """The authoritative checks. Exact, deterministic, no model involved."""
    itinerary = itinerary or []
    issues: list[ReviewIssue] = []

    # --- constraint validation ------------------------------------------
    if len(itinerary) != request.duration_days:
        issues.append(
            ReviewIssue(
                check="itinerary_length",
                detail=(
                    f"itinerary covers {len(itinerary)} days, "
                    f"request covers {request.duration_days}"
                ),
            )
        )

    if itinerary:
        if itinerary[0].date != request.departure_date:
            issues.append(
                ReviewIssue(
                    check="start_date",
                    detail="first itinerary day does not match departure_date",
                )
            )
        if itinerary[-1].date != request.return_date:
            issues.append(
                ReviewIssue(
                    check="end_date",
                    detail="last itinerary day does not match return_date",
                )
            )

    if not flights:
        issues.append(
            ReviewIssue(check="flights", detail="no flight recommendations produced")
        )
    if not hotels:
        issues.append(
            ReviewIssue(check="hotels", detail="no hotel recommendations produced")
        )

    if budget is not None and budget.over_budget:
        issues.append(
            ReviewIssue(
                check="budget",
                detail=(
                    f"estimated {budget.estimated_total} {budget.currency} exceeds "
                    f"budget {budget.budget} {budget.currency}"
                ),
            )
        )

    # --- logical validation ---------------------------------------------
    for day in itinerary:
        times = [item.time for item in day.items]
        if len(times) != len(set(times)):
            issues.append(
                ReviewIssue(
                    check="schedule_conflict",
                    detail=f"day {day.day} has two entries at the same time",
                )
            )

    # Every destination the traveller asked for should appear somewhere.
    covered = {day.destination for day in itinerary}
    missing = [d for d in request.destinations if d not in covered]
    if missing:
        issues.append(
            ReviewIssue(
                check="destination_coverage",
                detail=f"itinerary never visits {', '.join(missing)}",
            )
        )

    # A night in a city with no hotel booked there is a real failure.
    hotel_cities = {h.destination for h in hotels or []}
    for day in itinerary[:-1] if itinerary else []:
        if day.destination not in hotel_cities:
            issues.append(
                ReviewIssue(
                    check="accommodation_gap",
                    detail=(
                        f"day {day.day} is in {day.destination} "
                        "with no hotel booked there"
                    ),
                )
            )
            break

    return issues


class ReviewAgent:
    """Gates the plan. Rules decide; the LLM adds what rules read poorly."""

    def __init__(self, llm: object | None = None) -> None:
        self._llm = llm

    def _get_llm(self):
        self._llm = build_llm(self._llm)
        return self._llm

    def _llm_issues(
        self, request: TravelRequest, itinerary: list[DayPlan]
    ) -> list[ReviewIssue]:
        llm = self._get_llm()
        if llm is None or not itinerary:
            return []

        valid_days = {day.day for day in itinerary}
        lines: list[str] = []
        for day in itinerary:
            lines.append(f"Day {day.day} ({day.date}) — {day.destination}")
            for item in day.items:
                lines.append(f"  {item.time}  [{item.kind}]  {item.title}")

        prompt = (
            f"Traveller: {request.travelers} people, {request.trip_style} pace, "
            f"{request.duration_days} days, "
            f"{request.origin} to {', '.join(request.destinations)}.\n\n"
            + "\n".join(lines)
        )
        try:
            review = llm.with_structured_output(LLMReview).invoke(
                [("system", SYSTEM_PROMPT), ("human", prompt)]
            )
            if not isinstance(review, LLMReview):
                review = LLMReview.model_validate(review)
        except Exception as exc:
            logger.warning("LLM review pass failed, keeping rule checks: %s", exc)
            return []

        issues: list[ReviewIssue] = []
        for finding in review.issues:
            if finding.day not in valid_days:
                # Cannot fail a trip for a day that was never in it.
                logger.warning(
                    "discarding review finding for non-existent day %s", finding.day
                )
                continue
            severity = "error" if finding.severity.lower() == "error" else "warning"
            issues.append(
                ReviewIssue(
                    severity=severity,
                    check="review_agent",
                    detail=f"day {finding.day}: {finding.detail}",
                )
            )
        return issues

    def review(
        self,
        request: TravelRequest,
        *,
        itinerary: list[DayPlan] | None = None,
        flights: list[FlightOption] | None = None,
        hotels: list[HotelOption] | None = None,
        activities: list[Activity] | None = None,
        restaurants: list[Restaurant] | None = None,
        budget: BudgetSummary | None = None,
    ) -> ReviewResult:
        issues = run_rule_checks(
            request,
            itinerary=itinerary,
            flights=flights,
            hotels=hotels,
            activities=activities,
            restaurants=restaurants,
            budget=budget,
        )
        issues.extend(self._llm_issues(request, itinerary or []))

        blocking = [issue for issue in issues if issue.severity == "error"]
        return ReviewResult(verdict="FAIL" if blocking else "PASS", issues=issues)


def guidance_from(issues: list[ReviewIssue]) -> list[str]:
    """Turn review failures into instructions the next pass can act on.

    Without this the replan loop just re-runs the same search and fails the
    same way until the retry budget is gone.
    """
    guidance: list[str] = []
    checks = {issue.check for issue in issues if issue.severity == "error"}

    if "budget" in checks:
        guidance.append("search cheaper options: the previous plan was over budget")
    if "flights" in checks:
        guidance.append("relax flight constraints: no flights were found")
    if "hotels" in checks or "accommodation_gap" in checks:
        guidance.append("widen the hotel search: a night had no bed booked")
    if "schedule_conflict" in checks:
        guidance.append("re-time the itinerary: two items clashed")
    if "destination_coverage" in checks:
        guidance.append("cover every requested destination in the itinerary")

    return guidance


__all__ = ["LLMReview", "ReviewAgent", "guidance_from", "run_rule_checks"]
