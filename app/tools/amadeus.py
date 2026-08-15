"""Shared Amadeus Self-Service transport: OAuth2, HTTP, and endpoint calls.

One client serves every Amadeus-backed tool (flights, hotels) so a single token
is cached across the whole request. No LLM calls and no decisions live here —
this module only knows how to talk to Amadeus.

Endpoints used:
  POST /v1/security/oauth2/token                    — client_credentials
  GET  /v1/reference-data/locations                 — place -> IATA code
  GET  /v2/shopping/flight-offers                   — flight search
  GET  /v1/reference-data/locations/hotels/by-city  — hotels in a city
  GET  /v3/shopping/hotel-offers                    — hotel prices
  GET  /v2/e-reputation/hotel-sentiments            — guest ratings

Search only. There is no booking path here, by design.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class AmadeusError(RuntimeError):
    """A provider call failed. Messages never include credentials."""


class FlightSearchError(AmadeusError):
    """Raised when flights cannot be searched."""


class HotelSearchError(AmadeusError):
    """Raised when hotels cannot be searched."""


@contextmanager
def _as(error_type: type[AmadeusError]) -> Iterator[None]:
    """Re-raise a transport-level AmadeusError as a domain-specific one.

    Callers catch `FlightSearchError` / `HotelSearchError`, so a bare
    `AmadeusError` escaping from shared plumbing would slip past them.
    """
    try:
        yield
    except error_type:
        raise
    except AmadeusError as exc:
        raise error_type(str(exc)) from exc


class AmadeusClient:
    """Thin, deterministic wrapper over the Amadeus Self-Service APIs."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self.client_id = client_id or settings.amadeus_client_id
        self.client_secret = client_secret or settings.amadeus_client_secret
        self.base_url = (base_url or settings.amadeus_base_url).rstrip("/")
        self.timeout = timeout or settings.amadeus_timeout_seconds
        self._http = http_client
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._location_cache: dict[str, str] = {}

    # -- plumbing --------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> AmadeusClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _access_token(self) -> str:
        """Fetch and cache an OAuth2 token, refreshing 60s before expiry."""
        if not self.configured:
            raise AmadeusError("Amadeus credentials are not configured")

        if self._token and time.monotonic() < self._token_expires_at:
            return self._token

        try:
            response = self._client().post(
                f"{self.base_url}/v1/security/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError as exc:
            raise AmadeusError(f"could not reach Amadeus: {exc}") from exc

        if response.status_code != 200:
            # Deliberately does not echo the body — it can contain the secret.
            raise AmadeusError(
                f"Amadeus authentication failed (HTTP {response.status_code})"
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AmadeusError("Amadeus returned no access token")

        self._token = token
        self._token_expires_at = time.monotonic() + max(
            int(payload.get("expires_in", 1799)) - 60, 30
        )
        return token

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        token = self._access_token()
        try:
            response = self._client().get(
                f"{self.base_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.RequestError as exc:
            raise AmadeusError(f"could not reach Amadeus: {exc}") from exc

        if response.status_code == 401:
            # Token rejected mid-flight; drop it so the next call re-authenticates.
            self._token = None
            raise AmadeusError("Amadeus rejected the access token")
        if response.status_code >= 400:
            raise AmadeusError(
                f"Amadeus {path} failed (HTTP {response.status_code}): "
                f"{_error_detail(response)}"
            )
        return response.json()

    # -- reference data --------------------------------------------------

    def resolve_location_code(self, place: str) -> str:
        """"Mumbai" -> "BOM". Already-valid IATA codes pass straight through."""
        cleaned = place.strip()
        if len(cleaned) == 3 and cleaned.isalpha():
            return cleaned.upper()

        key = cleaned.lower()
        if key in self._location_cache:
            return self._location_cache[key]

        payload = self._get(
            "/v1/reference-data/locations",
            {"subType": "CITY,AIRPORT", "keyword": cleaned, "page[limit]": 5},
        )
        for entry in payload.get("data", []):
            code = entry.get("iataCode")
            if code:
                self._location_cache[key] = code
                return code

        raise AmadeusError(f"no IATA code found for {place!r}")

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
        max_results: int = 20,
    ) -> dict[str, Any]:
        """Raw Amadeus flight-offers payload. Callers normalise it."""
        with _as(FlightSearchError):
            params: dict[str, Any] = {
                "originLocationCode": self.resolve_location_code(origin),
                "destinationLocationCode": self.resolve_location_code(destination),
                "departureDate": departure_date.isoformat(),
                "adults": adults,
                "currencyCode": currency,
                "max": max_results,
            }
            if return_date is not None:
                params["returnDate"] = return_date.isoformat()
            if non_stop:
                params["nonStop"] = "true"

            return self._get("/v2/shopping/flight-offers", params)

    # -- hotels ----------------------------------------------------------

    def list_hotels_by_city(
        self,
        city_code: str,
        radius_km: int = 20,
        ratings: list[int] | None = None,
        amenities: list[str] | None = None,
    ) -> dict[str, Any]:
        """Candidate hotels in a city.

        Returns identity and geolocation only — this endpoint does not return
        a star rating or an amenity list, so `ratings`/`amenities` act purely
        as server-side filters on which hotels come back.
        """
        with _as(HotelSearchError):
            params: dict[str, Any] = {
                "cityCode": city_code,
                "radius": radius_km,
                "radiusUnit": "KM",
                "hotelSource": "ALL",
            }
            if ratings:
                params["ratings"] = ",".join(str(r) for r in sorted(set(ratings)))
            if amenities:
                params["amenities"] = ",".join(amenities)

            return self._get("/v1/reference-data/locations/hotels/by-city", params)

    def search_hotel_offers(
        self,
        hotel_ids: list[str],
        check_in: date,
        check_out: date,
        adults: int = 1,
        rooms: int = 1,
        currency: str = "INR",
    ) -> dict[str, Any]:
        """Prices for specific hotels. `hotelIds` is required by the API."""
        if not hotel_ids:
            raise HotelSearchError("no hotel ids to price")

        with _as(HotelSearchError):
            return self._get(
                "/v3/shopping/hotel-offers",
                {
                    # The endpoint caps how many ids it accepts per call.
                    "hotelIds": ",".join(hotel_ids[:MAX_HOTEL_IDS_PER_CALL]),
                    "adults": adults,
                    "checkInDate": check_in.isoformat(),
                    "checkOutDate": check_out.isoformat(),
                    "roomQuantity": rooms,
                    "currency": currency,
                },
            )

    def hotel_ratings(self, hotel_ids: list[str]) -> dict[str, Any]:
        """Guest sentiment scores (0-100) for specific hotels.

        Coverage is partial — many hotels have no sentiment data at all, so
        callers must treat a missing rating as unknown, not as zero.
        """
        if not hotel_ids:
            return {"data": []}
        with _as(HotelSearchError):
            return self._get(
                "/v2/e-reputation/hotel-sentiments",
                {"hotelIds": ",".join(hotel_ids[:MAX_HOTEL_IDS_PER_CALL])},
            )


# Amadeus rejects oversized hotelIds lists; keep calls comfortably under it.
MAX_HOTEL_IDS_PER_CALL = 20


def _error_detail(response: httpx.Response) -> str:
    """Pull Amadeus's error text out without dumping the whole body."""
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        return "unreadable error response"
    if not errors:
        return "no error detail"
    first = errors[0]
    return str(first.get("detail") or first.get("title") or first)
