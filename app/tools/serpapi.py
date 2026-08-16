"""SerpAPI Google Flights transport: HTTP, parameters, and error semantics.

This module only knows how to talk to SerpAPI. Normalising the payload into
`FlightOption` objects lives in `app/tools/flights.py`, and every judgement
call lives in the Flight agent — same split as the Amadeus client next door.

Endpoint used:
  GET /search?engine=google_flights                 — flight search

Two things about this API shape the code more than anything else:

**Round trips take two calls.** A `type=1` search returns *outbound* itinerary
options. Each carries `price` — already the total round-trip fare for that
outbound choice, for all passengers — plus a `departure_token`. Feeding that
token back in returns the return-leg options that pair with it. So a complete
round-trip itinerary costs two billable searches, which is why the agent only
resolves return legs for its top few candidates.

**"No results" arrives as HTTP 200.** An unroutable query returns 200 with an
`error` string and no flight arrays, while a bad key returns 401 and a
malformed date returns 400 — both also with `error`. Treating every `error` as
a failure would turn "nothing flies that day" into a crash, so an empty result
is distinguished from a broken request here rather than in the caller.

Search only. There is no booking path here, by design.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from app.config import get_settings
from app.services.langfuse import observe, update_current
from app.tools.airports import resolve_iata
from app.tools.errors import FlightSearchError

logger = logging.getLogger(__name__)


class SerpApiError(FlightSearchError):
    """A SerpAPI call failed. Messages never include the API key."""


# SerpAPI says this, with HTTP 200, when Google simply had nothing to show.
# Not an error condition: the traveller asked for a route nobody flies.
_NO_RESULTS_MARKERS = (
    "hasn't returned any results",
    "has not returned any results",
)

# `type` values in the Google Flights engine.
TYPE_ROUND_TRIP = 1
TYPE_ONE_WAY = 2

# `stops` values. 0 means "any number"; 1 means non-stop only.
STOPS_ANY = 0
STOPS_NONSTOP = 1


class SerpApiClient:
    """Thin, deterministic wrapper over SerpAPI's Google Flights engine."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
        language: str = "en",
    ) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.serpapi_api_key
        self.base_url = (base_url or settings.serpapi_base_url).rstrip("/")
        self.timeout = timeout or settings.serpapi_timeout_seconds
        self.language = language
        self._http = http_client

    # -- plumbing --------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> SerpApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Every provider call is a `tool` observation, so the trace separates
        what an agent decided from what the API actually returned."""
        with observe("serpapi/google_flights", as_type="tool", input=_safe(params)):
            return self._get_traced(params)

    def _get_traced(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise SerpApiError("SerpAPI is not configured")

        try:
            response = self._client().get(
                f"{self.base_url}/search",
                params={**params, "api_key": self.api_key},
            )
        except httpx.RequestError as exc:
            raise SerpApiError(f"could not reach SerpAPI: {exc}") from exc

        payload = _json_or_empty(response)
        message = payload.get("error")

        if response.status_code == 401:
            # Deliberately does not echo the body — it can quote the key back.
            raise SerpApiError("SerpAPI rejected the API key")
        if response.status_code == 429:
            raise SerpApiError("SerpAPI rate limit or search quota exhausted")
        if response.status_code >= 400:
            raise SerpApiError(
                f"SerpAPI search failed (HTTP {response.status_code}): "
                f"{message or 'no error detail'}"
            )

        if message:
            if any(marker in message for marker in _NO_RESULTS_MARKERS):
                logger.info("SerpAPI returned no flights: %s", message)
                update_current(output={"count": 0, "status": 200})
                return payload
            raise SerpApiError(f"SerpAPI search failed: {message}")

        count = len(payload.get("best_flights") or []) + len(
            payload.get("other_flights") or []
        )
        update_current(output={"count": count, "status": response.status_code})
        return payload

    # -- flights ---------------------------------------------------------

    def search_flights(
        self,
        origin: str,
        destination: str,
        departure_date: date,
        return_date: date | None = None,
        adults: int = 1,
        currency: str = "INR",
        non_stop: bool = False,
    ) -> dict[str, Any]:
        """Raw Google Flights payload for the outbound search.

        On a round trip the options returned are outbound itineraries priced at
        the full round-trip fare, each with a `departure_token` for
        `search_return_legs`. Callers normalise the payload.
        """
        params: dict[str, Any] = {
            "engine": "google_flights",
            "departure_id": resolve_iata(origin),
            "arrival_id": resolve_iata(destination),
            "outbound_date": departure_date.isoformat(),
            "adults": adults,
            "currency": currency,
            "hl": self.language,
            "type": TYPE_ROUND_TRIP if return_date else TYPE_ONE_WAY,
        }
        if return_date is not None:
            params["return_date"] = return_date.isoformat()
        if non_stop:
            params["stops"] = STOPS_NONSTOP

        return self._get(params)

    def search_return_legs(
        self,
        origin: str,
        destination: str,
        departure_date: date,
        return_date: date,
        departure_token: str,
        adults: int = 1,
        currency: str = "INR",
        non_stop: bool = False,
    ) -> dict[str, Any]:
        """Return-leg options that pair with one chosen outbound itinerary.

        The whole original query has to be repeated alongside the token —
        the token identifies the outbound choice, not the search.
        """
        params: dict[str, Any] = {
            "engine": "google_flights",
            "departure_id": resolve_iata(origin),
            "arrival_id": resolve_iata(destination),
            "outbound_date": departure_date.isoformat(),
            "return_date": return_date.isoformat(),
            "adults": adults,
            "currency": currency,
            "hl": self.language,
            "type": TYPE_ROUND_TRIP,
            "departure_token": departure_token,
        }
        if non_stop:
            params["stops"] = STOPS_NONSTOP

        return self._get(params)


def _safe(params: dict[str, Any]) -> dict[str, Any]:
    """Query params for a trace, minus anything that authenticates us.

    `departure_token` is dropped too — it is a 300-character blob that buries
    the readable part of the span without telling a reader anything.
    """
    hidden = {"api_key", "departure_token", "booking_token"}
    return {k: v for k, v in params.items() if k.lower() not in hidden}


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    """SerpAPI answers JSON even for errors — but a proxy or an outage may
    not, and a JSONDecodeError there would mask the real status code."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "STOPS_NONSTOP",
    "TYPE_ONE_WAY",
    "TYPE_ROUND_TRIP",
    "SerpApiClient",
    "SerpApiError",
]
