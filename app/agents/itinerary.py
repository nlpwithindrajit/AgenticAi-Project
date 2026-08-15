"""Itinerary agent — the LLM sequences a fixed catalogue, and nothing else.

projectIdea.md §13 wants the day plan to respect geography, opening hours,
travel time and meal times. That is genuine judgement, and it is the one place
in this project where an LLM clearly beats a rule.

But an itinerary is also the easiest place to hallucinate: a model asked to
"write a day in Tokyo" will happily invent a restaurant. So the agent never
writes an itinerary. It is handed a **catalogue** of items that already exist —
the flight we booked, the hotel we chose, the activity and restaurant the other
agents found — each with an opaque id, and it may only return `(id, time)`
pairs. Anything it returns that is not in the catalogue is dropped and
recorded; if a day survives with nothing, that day falls back to deterministic
times. The model can reorder the day. It cannot add to it.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from pydantic import BaseModel, Field

from app.agents.places_base import build_llm
from app.models.travel import (
    Activity,
    DayPlan,
    FlightOption,
    HotelOption,
    ItineraryItem,
    Restaurant,
    TravelRequest,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You lay out a traveller's day from a fixed list of items.

You may only schedule items from the catalogue you are given, using their ids. \
You cannot add, rename or invent anything — if something seems missing, leave \
it missing.

Order each day sensibly:
- flights anchor the day; nothing may clash with a departure or arrival
- hotel check-in comes after arrival, check-out before the return flight
- meals belong at meal times, and never at the same time as anything else
- leave travel time between items that are far apart
- do not schedule two things at the same time

Times are 24-hour HH:MM."""

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Deterministic fallback times, also used when no LLM is configured.
DEFAULT_TIMES = {
    "flight_out": "09:00",
    "hotel": "19:00",
    "activity": "14:00",
    "meal": "20:00",
    "meal_before_flight": "17:00",
    "flight_home": "20:00",
}


class ScheduleCandidate(BaseModel):
    """One thing that may appear on a given day. Built from real inventory."""

    id: str
    day: int
    title: str
    kind: str
    location: str | None = None
    note: str | None = None
    default_time: str = "12:00"


class ScheduledItem(BaseModel):
    id: str = Field(description="An id from the catalogue. Never invent one.")
    time: str = Field(description="24-hour HH:MM.")


class DaySchedule(BaseModel):
    day: int
    items: list[ScheduledItem] = Field(default_factory=list)


class ItineraryPlan(BaseModel):
    """The LLM's proposed ordering across the whole trip."""

    days: list[DaySchedule] = Field(default_factory=list)
    reasoning: str = Field(default="")


class ItineraryResult(BaseModel):
    days: list[DayPlan] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def build_catalogue(
    request: TravelRequest,
    destinations_by_day: list[str],
    *,
    flights: list[FlightOption] | None = None,
    hotels: list[HotelOption] | None = None,
    activities: list[Activity] | None = None,
    restaurants: list[Restaurant] | None = None,
) -> dict[int, list[ScheduleCandidate]]:
    """Everything that legitimately belongs on each day, keyed by day number.

    This is the whitelist. The LLM cannot schedule anything absent from it.
    """
    flights = flights or []
    chosen = flights[0] if flights else None
    total_days = len(destinations_by_day)

    activity_by_day = {a.recommended_day: a for a in activities or []}
    restaurant_by_day = {r.recommended_day: r for r in restaurants or []}
    hotel_by_destination: dict[str, HotelOption] = {}
    for hotel in hotels or []:
        current = hotel_by_destination.get(hotel.destination)
        if current is None or hotel.score > current.score:
            hotel_by_destination[hotel.destination] = hotel

    catalogue: dict[int, list[ScheduleCandidate]] = {}

    for index, destination in enumerate(destinations_by_day):
        day = index + 1
        is_last = day == total_days
        has_return = is_last and chosen is not None and chosen.inbound is not None
        items: list[ScheduleCandidate] = []

        if day == 1 and chosen is not None:
            items.append(
                ScheduleCandidate(
                    id=f"d{day}-flight-out",
                    day=day,
                    title=(
                        f"Flight {chosen.outbound.origin} to "
                        f"{chosen.outbound.destination}"
                    ),
                    kind="flight",
                    note=(
                        f"departs {chosen.outbound.departure_at}, "
                        f"arrives {chosen.outbound.arrival_at}"
                    ),
                    default_time=DEFAULT_TIMES["flight_out"],
                )
            )
            hotel = hotel_by_destination.get(destination)
            if hotel is not None:
                items.append(
                    ScheduleCandidate(
                        id=f"d{day}-hotel",
                        day=day,
                        title=f"Check in at {hotel.name}",
                        kind="hotel",
                        location=hotel.name,
                        default_time=DEFAULT_TIMES["hotel"],
                    )
                )

        activity = activity_by_day.get(day)
        if activity is not None:
            items.append(
                ScheduleCandidate(
                    id=f"d{day}-activity",
                    day=day,
                    title=activity.activity,
                    kind="activity",
                    location=activity.location,
                    note=(
                        f"about {activity.duration_hours}h"
                        if activity.duration_hours
                        else None
                    ),
                    default_time=DEFAULT_TIMES["activity"],
                )
            )

        restaurant = restaurant_by_day.get(day)
        if restaurant is not None:
            items.append(
                ScheduleCandidate(
                    id=f"d{day}-meal",
                    day=day,
                    title=restaurant.name,
                    kind="meal",
                    location=restaurant.location,
                    default_time=(
                        DEFAULT_TIMES["meal_before_flight"]
                        if has_return
                        else DEFAULT_TIMES["meal"]
                    ),
                )
            )

        if has_return and chosen is not None and chosen.inbound is not None:
            items.append(
                ScheduleCandidate(
                    id=f"d{day}-flight-home",
                    day=day,
                    title=f"Return flight to {chosen.inbound.destination}",
                    kind="flight",
                    note=f"departs {chosen.inbound.departure_at}",
                    default_time=DEFAULT_TIMES["flight_home"],
                )
            )

        catalogue[day] = items

    return catalogue


def _baseline(candidates: list[ScheduleCandidate]) -> list[ItineraryItem]:
    """Deterministic times — used with no LLM, and as the per-day fallback."""
    items = [
        ItineraryItem(
            time=c.default_time, title=c.title, kind=c.kind, location=c.location
        )
        for c in candidates
    ]
    return _resolve_clashes(sorted(items, key=lambda i: i.time))


def _resolve_clashes(items: list[ItineraryItem]) -> list[ItineraryItem]:
    """Push duplicates forward an hour so a day never double-books itself.

    The Review agent fails a plan with two things at the same time, so this is
    the last line of defence before that gate.
    """
    seen: set[str] = set()
    resolved: list[ItineraryItem] = []
    for item in items:
        time = item.time
        while time in seen:
            hour, minute = (int(part) for part in time.split(":"))
            hour = (hour + 1) % 24
            time = f"{hour:02d}:{minute:02d}"
        seen.add(time)
        resolved.append(item.model_copy(update={"time": time}))
    return sorted(resolved, key=lambda i: i.time)


class ItineraryAgent:
    """Arranges existing inventory into days. Never invents an item."""

    def __init__(self, llm: object | None = None) -> None:
        self._llm = llm

    def _get_llm(self):
        self._llm = build_llm(self._llm)
        return self._llm

    def _propose(
        self, request: TravelRequest, catalogue: dict[int, list[ScheduleCandidate]]
    ) -> ItineraryPlan | None:
        llm = self._get_llm()
        if llm is None:
            return None

        lines: list[str] = []
        for day in sorted(catalogue):
            lines.append(f"Day {day}:")
            for candidate in catalogue[day]:
                detail = " — ".join(
                    part
                    for part in (candidate.title, candidate.location, candidate.note)
                    if part
                )
                lines.append(f"  {candidate.id}  [{candidate.kind}]  {detail}")
        if not lines:
            return None

        prompt = (
            f"Traveller: {request.travelers} people, {request.trip_style} pace, "
            f"interests: {', '.join(request.interests) or 'none stated'}.\n\n"
            f"Catalogue (schedule only these, by id):\n"
            + "\n".join(lines)
            + "\n\nReturn a time for each item you schedule."
        )
        try:
            plan = llm.with_structured_output(ItineraryPlan).invoke(
                [("system", SYSTEM_PROMPT), ("human", prompt)]
            )
            if isinstance(plan, ItineraryPlan):
                return plan
            return ItineraryPlan.model_validate(plan)
        except Exception as exc:
            logger.warning("itinerary planning failed, using default times: %s", exc)
            return None

    def build(
        self,
        request: TravelRequest,
        destinations_by_day: list[str],
        *,
        flights: list[FlightOption] | None = None,
        hotels: list[HotelOption] | None = None,
        activities: list[Activity] | None = None,
        restaurants: list[Restaurant] | None = None,
    ) -> ItineraryResult:
        notes: list[str] = []
        catalogue = build_catalogue(
            request,
            destinations_by_day,
            flights=flights,
            hotels=hotels,
            activities=activities,
            restaurants=restaurants,
        )

        proposal = self._propose(request, catalogue)
        proposed_by_day: dict[int, list[ScheduledItem]] = {}
        if proposal is not None:
            if proposal.reasoning:
                notes.append(f"itinerary plan: {proposal.reasoning}")
            for day_schedule in proposal.days:
                proposed_by_day[day_schedule.day] = day_schedule.items

        invented = 0
        bad_times = 0
        days: list[DayPlan] = []

        for index, destination in enumerate(destinations_by_day):
            day = index + 1
            candidates = catalogue.get(day, [])
            by_id = {c.id: c for c in candidates}

            items: list[ItineraryItem] = []
            for scheduled in proposed_by_day.get(day, []):
                candidate = by_id.get(scheduled.id)
                if candidate is None:
                    # The whole point of the catalogue: refuse unknown items.
                    invented += 1
                    continue
                if not _TIME_RE.match(scheduled.time.strip()):
                    bad_times += 1
                    continue
                items.append(
                    ItineraryItem(
                        time=scheduled.time.strip(),
                        title=candidate.title,
                        kind=candidate.kind,
                        location=candidate.location,
                    )
                )

            if not items and candidates:
                # Either no LLM, or it returned nothing usable for this day.
                items = _baseline(candidates)
            else:
                items = _resolve_clashes(sorted(items, key=lambda i: i.time))

            days.append(
                DayPlan(
                    day=day,
                    date=request.departure_date + timedelta(days=index),
                    destination=destination,
                    items=items,
                )
            )

        if invented:
            notes.append(
                f"dropped {invented} itinerary item(s) the planner invented; "
                "only searched inventory is scheduled"
            )
        if bad_times:
            notes.append(f"dropped {bad_times} itinerary item(s) with unusable times")

        return ItineraryResult(days=days, notes=notes)


def destinations_by_day(
    destinations: list[str], departure: date, return_date: date
) -> list[str]:
    """One destination per day of the trip, in order."""
    days = (return_date - departure).days + 1
    per_destination = max(1, days // len(destinations))

    schedule: list[str] = []
    for destination in destinations:
        schedule.extend([destination] * per_destination)
    while len(schedule) < days:
        schedule.append(destinations[-1])
    return schedule[:days]


__all__ = [
    "DEFAULT_TIMES",
    "ItineraryAgent",
    "ItineraryPlan",
    "ItineraryResult",
    "ScheduleCandidate",
    "build_catalogue",
    "destinations_by_day",
]
