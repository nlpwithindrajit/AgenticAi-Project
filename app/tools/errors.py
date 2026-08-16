"""Provider-neutral tool errors.

These used to live in `app/tools/amadeus.py`, which was fine while Amadeus was
the only provider: `FlightSearchError` subclassing `AmadeusError` said
something true. Now that flights come from SerpAPI, a SerpAPI failure raised as
an `AmadeusError` subclass would be a lie in every traceback — so the domain
errors ("the flight search failed") are separated here from the transport
errors ("this particular provider failed").

The hierarchy is deliberately shallow. Callers care about *which search* broke,
because that decides what to fall back to; they almost never care which vendor
was behind it.
"""

from __future__ import annotations


class TravelProviderError(RuntimeError):
    """Any external travel-data call failed. Messages never include secrets."""


class FlightSearchError(TravelProviderError):
    """Raised when flights cannot be searched, whoever the provider is."""


class HotelSearchError(TravelProviderError):
    """Raised when hotels cannot be searched."""


class PlacesSearchError(TravelProviderError):
    """Raised when activities or points of interest cannot be searched."""


__all__ = [
    "FlightSearchError",
    "HotelSearchError",
    "PlacesSearchError",
    "TravelProviderError",
]
