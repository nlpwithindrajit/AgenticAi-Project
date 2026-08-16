"""Shared plumbing for the Activity and Restaurant agents.

Both search around a geographic point, so anchor resolution lives here, as does
the optional LLM wiring both agents use identically.
"""

from __future__ import annotations

import logging

from app.agents.llm import build_llm
from app.tools.amadeus import AmadeusClient, PlacesSearchError, TravelProviderError

logger = logging.getLogger(__name__)

Anchor = tuple[float, float]


def resolve_anchor(
    client: AmadeusClient,
    destination: str,
    *,
    hotel_anchor: Anchor | None = None,
) -> Anchor:
    """Where to search from, in priority order.

    1. The hotel we actually recommended — activities near where the traveller
       sleeps are more useful than activities near an abstract centre, and the
       graph runs the hotel node first, so this is usually available.
    2. The city centre from Amadeus City Search.

    Raises `PlacesSearchError` when neither is obtainable, since searching from
    an arbitrary point would return plausible results for the wrong place.
    """
    if hotel_anchor is not None:
        return hotel_anchor

    try:
        payload = client.search_cities(destination)
    except TravelProviderError as exc:
        raise PlacesSearchError(
            f"could not locate {destination!r}: {exc}"
        ) from exc

    for entry in payload.get("data", []):
        geo = entry.get("geoCode") or {}
        latitude, longitude = geo.get("latitude"), geo.get("longitude")
        if latitude is not None and longitude is not None:
            return (float(latitude), float(longitude))

    raise PlacesSearchError(f"no coordinates found for {destination!r}")


def hotel_anchor_for(
    destination: str, hotels: list[object] | None
) -> Anchor | None:
    """Coordinates of the best-scoring recommended hotel in a destination."""
    candidates = [
        h
        for h in hotels or []
        if getattr(h, "destination", None) == destination
        and getattr(h, "latitude", None) is not None
        and getattr(h, "longitude", None) is not None
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda h: getattr(h, "score", 0.0))
    return (float(best.latitude), float(best.longitude))


__all__ = ["Anchor", "build_llm", "hotel_anchor_for", "resolve_anchor"]
