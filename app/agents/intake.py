"""Intake agent — turns a sentence into a `TravelRequest`, or asks for what's missing.

`CLAUDE.md` is firm that the agents consume a structured schema rather than
free text, and that is worth keeping: every downstream agent depends on knowing
the dates, the party size and the budget exactly. So this agent does not
replace the schema — it is a *front door* that produces one.

    "5 days in Tokyo and Kyoto from Mumbai, Oct 10-15, 2 people, ₹2,00,000"
        -> TravelRequest(...)                       -> the same graph as the form

Nothing is guessed into a required field. If the budget is absent the agent
says so and asks, rather than inventing a number that would then silently drive
every search and the whole budget loop.

Two extraction paths:

  with an LLM      the sentence is parsed properly, including relative dates
                   and phrasings like "a long weekend"
  without one      a deliberately narrow regex pass handles the obvious shapes
                   and is honest about what it could not read

The fallback exists so the feature is usable with no API key at all, matching
the rest of the project. It is not a substitute for the model, and it says so.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from pydantic import BaseModel, Field, field_validator

from app.agents.llm import build_llm
from app.models.travel import TravelRequest, TripStyle

logger = logging.getLogger(__name__)

# Singular forms — the validator strips a trailing "s" before looking up.
_SPOKEN_CURRENCIES = {
    "rupee": "INR",
    "inr": "INR",
    "dollar": "USD",
    "us dollar": "USD",
    "usd": "USD",
    "euro": "EUR",
    "eur": "EUR",
    "pound": "GBP",
    "pound sterling": "GBP",
    "gbp": "GBP",
    "yen": "JPY",
    "jpy": "JPY",
}

SYSTEM_PROMPT = """You read a traveller's message and fill in a trip request.

Extract only what the traveller actually said or clearly implied. Leave a field \
null when it was not stated — do not invent a budget, a date or a party size. \
Guessing one of those silently changes every search that follows.

Resolve relative dates ("next month", "the first week of June") against the \
reference date you are given. Amounts may be written as "2 lakh", "₹2,00,000" \
or "200k" — normalise them to a plain number, and record the currency \
separately as an ISO 4217 code ("rupees" is INR, "$" is USD).

This is one turn of a conversation, so you are shown what is already known and \
which question the traveller was just asked. Read a bare reply as the answer to \
that question: "London" after "where are you travelling from?" is an origin, \
not a destination. Return only the fields this message adds — repeating what is \
already known is harmless, contradicting it is not.

Do not ask a question. The next question is chosen from what is still missing \
once your answer is merged in."""

# The four things no downstream agent can work without.
REQUIRED = ("origin", "destinations", "departure_date", "budget")


class TripDraft(BaseModel):
    """A partially-filled request. Every field may legitimately be unknown."""

    origin: str | None = None
    destinations: list[str] = Field(default_factory=list)
    departure_date: date | None = None
    return_date: date | None = None
    travelers: int | None = None
    budget: float | None = None
    currency: str | None = None
    hotel_stars: int | None = None
    direct_flights_only: bool | None = None
    interests: list[str] = Field(default_factory=list)
    dietary_preferences: list[str] = Field(default_factory=list)
    trip_style: TripStyle | None = None

    @field_validator("currency")
    @classmethod
    def _as_currency_code(cls, value: str | None) -> str | None:
        """"rupees" -> "INR". Every price is printed with this string.

        The prompt asks for a code, but a traveller writes "80,000 rupees" and a
        model will happily pass that through, so the guarantee is made here
        rather than hoped for. An unrecognised name is kept as-is: an unfamiliar
        word next to a number is better than a confidently wrong code.
        """
        if value is None:
            return None
        spoken = value.strip().lower().rstrip("s")
        return _SPOKEN_CURRENCIES.get(spoken, value.strip())

    def merge(self, other: TripDraft) -> TripDraft:
        """Later turns win, but never overwrite a known value with a blank."""
        merged = self.model_dump()
        for key, value in other.model_dump().items():
            if value in (None, [], ""):
                continue
            merged[key] = value
        return TripDraft.model_validate(merged)

    @property
    def missing(self) -> list[str]:
        return [
            field
            for field in REQUIRED
            if not getattr(self, field.replace("destinations", "destinations"))
        ]

    def to_request(self) -> TravelRequest | None:
        """A full `TravelRequest`, or None while anything required is unknown."""
        if self.missing:
            return None
        assert self.origin and self.departure_date and self.budget
        departure = self.departure_date
        # A trip with no stated end is treated as a long weekend, which is a
        # visible default rather than a silent one — it is echoed back to the
        # traveller in the confirmation.
        return_date = self.return_date or departure + timedelta(days=4)
        if return_date < departure:
            return_date = departure
        return TravelRequest(
            origin=self.origin,
            destinations=self.destinations,
            departure_date=departure,
            return_date=return_date,
            travelers=self.travelers or 1,
            budget=self.budget,
            currency=self.currency or "INR",
            hotel_stars=self.hotel_stars,
            direct_flights_only=bool(self.direct_flights_only),
            interests=self.interests,
            dietary_preferences=self.dietary_preferences,
            trip_style=self.trip_style or "balanced",
        )


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}
_INTEREST_WORDS = (
    "food", "culture", "history", "nature", "technology",
    "shopping", "nightlife", "adventure", "art", "wellness",
)
_CURRENCIES = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP"}


# Words that look like place names because they are capitalised, but are not.
_NOT_A_PLACE = set(_MONTHS) | {
    "i", "we", "my", "the", "a", "an", "prefer", "budget", "plan",
    "trip", "days", "day", "people", "star", "and", "for", "with",
}


def _place_after(preposition: str, message: str) -> list[str]:
    """Capitalised words following "from"/"to", split on "and" and commas.

    Only capitalised runs are taken, so an ordinary lowercase word ends the
    match — which is what stops "to Tokyo and Kyoto for 2 people from Mumbai"
    from reading the whole tail as one enormous city name. Month names are
    dropped afterwards, since "to Tokyo October 10" would otherwise yield a
    city called October.
    """
    match = re.search(
        rf"\b{preposition}\s+((?:[A-Z][\w'’\-]+)(?:[, ]+(?:and\s+)?[A-Z][\w'’\-]+)*)",
        message,
    )
    if not match:
        return []
    parts = [
        part.strip()
        for part in re.split(r"\s+and\s+|,\s*", match.group(1))
        if part.strip()
    ]
    return [p for p in parts if p.lower() not in _NOT_A_PLACE]


def _parse_amount(text: str) -> tuple[float | None, str | None]:
    """"₹2,00,000", "2 lakh", "200k", "$3000" -> (amount, currency)."""
    currency = next((c for sym, c in _CURRENCIES.items() if sym in text), None)
    for code in ("INR", "USD", "EUR", "GBP", "JPY"):
        if re.search(rf"\b{code}\b", text, re.I):
            currency = code

    lakh = re.search(r"(\d+(?:\.\d+)?)\s*lakh", text, re.I)
    if lakh:
        return float(lakh.group(1)) * 100_000, currency
    crore = re.search(r"(\d+(?:\.\d+)?)\s*crore", text, re.I)
    if crore:
        return float(crore.group(1)) * 10_000_000, currency

    thousand = re.search(r"(\d+(?:\.\d+)?)\s*k\b", text, re.I)
    if thousand:
        return float(thousand.group(1)) * 1_000, currency

    # Budget-ish numbers only — a bare "2 people" must not become the budget.
    money = re.search(
        r"(?:budget|under|around|about|upto|up to|₹|\$|€|£)\s*"
        r"([\d][\d,]{2,})",
        text,
        re.I,
    )
    if money:
        return float(money.group(1).replace(",", "")), currency
    return None, currency


def _parse_dates(text: str, today: date) -> tuple[date | None, date | None]:
    """Handles "Oct 10-15", "10 October", and "2026-10-10"."""
    iso = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        parsed = [date(int(y), int(m), int(d)) for y, m, d in iso[:2]]
        return parsed[0], (parsed[1] if len(parsed) > 1 else None)

    month_range = re.search(
        r"\b(" + "|".join(_MONTHS) + r")\w*\s+(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\b",
        text,
        re.I,
    )
    if month_range:
        month = _MONTHS[month_range.group(1).lower()]
        year = today.year if month >= today.month else today.year + 1
        return (
            date(year, month, int(month_range.group(2))),
            date(year, month, int(month_range.group(3))),
        )

    single = re.search(
        r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\w*\b", text, re.I
    ) or re.search(r"\b(" + "|".join(_MONTHS) + r")\w*\s+(\d{1,2})\b", text, re.I)
    if single:
        groups = single.groups()
        day, name = (
            (groups[0], groups[1]) if groups[0].isdigit() else (groups[1], groups[0])
        )
        month = _MONTHS[name.lower()]
        year = today.year if month >= today.month else today.year + 1
        return date(year, month, int(day)), None
    return None, None


def extract_deterministically(message: str, today: date | None = None) -> TripDraft:
    """A narrow regex pass for use without an LLM. Reads obvious shapes only."""
    today = today or date.today()
    draft = TripDraft()

    origin = _place_after("from", message)
    if origin:
        draft.origin = origin[0]

    draft.destinations = _place_after("to", message) or []

    draft.departure_date, draft.return_date = _parse_dates(message, today)

    nights = re.search(r"\b(\d{1,2})[- ]?(?:day|night)s?\b", message, re.I)
    if nights and draft.departure_date and not draft.return_date:
        draft.return_date = draft.departure_date + timedelta(
            days=max(int(nights.group(1)) - 1, 1)
        )

    people = re.search(
        r"\b(\d{1,2})\s*(?:people|persons?|adults?|travell?ers?|pax)\b", message, re.I
    )
    if people:
        draft.travelers = int(people.group(1))

    draft.budget, draft.currency = _parse_amount(message)

    stars = re.search(r"\b([1-5])[- ]star\b", message, re.I)
    if stars:
        draft.hotel_stars = int(stars.group(1))

    if re.search(r"\b(direct|non-?stop)\b", message, re.I):
        draft.direct_flights_only = True

    draft.interests = [
        w for w in _INTEREST_WORDS if re.search(rf"\b{w}", message, re.I)
    ]

    for style in ("relaxed", "balanced", "packed"):
        if re.search(rf"\b{style}\b", message, re.I):
            draft.trip_style = style  # type: ignore[assignment]

    return draft


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

_QUESTIONS = {
    "origin": "Where are you travelling from?",
    "destinations": "Where would you like to go?",
    "departure_date": "When do you want to leave?",
    "budget": "What is your total budget for the trip?",
}


def _next_question(missing: list[str]) -> str:
    """The one question worth asking, chosen from the *merged* draft.

    Deliberately not the model's own suggestion. The model sees a single
    message, so its idea of what is missing is the gap in that sentence rather
    than the gap in the conversation — which is how the chat used to ask for a
    destination that had been given three turns earlier, forever.
    """
    return _QUESTIONS.get(missing[0], "Could you tell me a little more?")


def _context(known: TripDraft) -> str:
    """What the model needs to read a bare reply correctly."""
    filled = {
        key: value
        for key, value in known.model_dump(mode="json").items()
        if value not in (None, [], "")
    }
    if not filled:
        return "Nothing is known about this trip yet; this is the first message."

    missing = known.missing
    lines = [
        "Already known (do not contradict without being told to):",
        *(f"  {key}: {value}" for key, value in filled.items()),
        f"Still needed: {', '.join(missing) if missing else 'nothing'}.",
    ]
    if missing:
        lines.append(f'You just asked the traveller: "{_next_question(missing)}"')
    return "\n".join(lines)


class IntakeResult(BaseModel):
    draft: TripDraft
    request: TravelRequest | None = None
    reply: str
    missing: list[str] = Field(default_factory=list)
    used_llm: bool = False


class IntakeAgent:
    """Reads a message into a trip request, asking for whatever is missing."""

    def __init__(self, llm: object | None = None) -> None:
        self._llm = llm

    def _get_llm(self):
        self._llm = build_llm(self._llm)
        return self._llm

    def _extract(
        self, message: str, today: date, known: TripDraft
    ) -> tuple[TripDraft, bool]:
        llm = self._get_llm()
        if llm is None:
            return extract_deterministically(message, today), False

        prompt = (
            f"Today is {today.isoformat()}.\n\n"
            f"{_context(known)}\n\n"
            f"Traveller's message:\n{message}"
        )
        try:
            draft = llm.with_structured_output(TripDraft).invoke(
                [("system", SYSTEM_PROMPT), ("human", prompt)]
            )
            if not isinstance(draft, TripDraft):
                draft = TripDraft.model_validate(draft)
            return draft, True
        except Exception as exc:
            logger.warning("intake extraction failed, falling back to rules: %s", exc)
            return extract_deterministically(message, today), False

    def read(
        self,
        message: str,
        *,
        draft: TripDraft | None = None,
        today: date | None = None,
    ) -> IntakeResult:
        """Merge one more message into the draft and report what is still needed."""
        today = today or date.today()
        known = draft or TripDraft()
        fresh, used_llm = self._extract(message, today, known)
        merged = known.merge(fresh)

        missing = merged.missing
        if missing:
            return IntakeResult(
                draft=merged,
                reply=_next_question(missing),
                missing=missing,
                used_llm=used_llm,
            )

        request = merged.to_request()
        assert request is not None
        reply = (
            f"Planning {request.duration_days} days in "
            f"{', '.join(request.destinations)} from {request.origin}, "
            f"{request.departure_date} to {request.return_date}, "
            f"for {request.travelers} "
            f"{'traveller' if request.travelers == 1 else 'travellers'}, "
            f"budget {request.budget:,.0f} {request.currency}."
        )
        if merged.return_date is None:
            # Any default that shapes the search gets said out loud.
            reply += " You didn't give a return date, so I assumed 5 days."
        return IntakeResult(
            draft=merged,
            request=request,
            reply=reply,
            missing=[],
            used_llm=used_llm,
        )


__all__ = [
    "REQUIRED",
    "IntakeAgent",
    "IntakeResult",
    "TripDraft",
    "extract_deterministically",
]
