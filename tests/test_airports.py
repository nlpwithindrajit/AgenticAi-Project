"""City -> IATA resolution for the SerpAPI flight path.

SerpAPI rejects a city name outright (HTTP 400), so a wrong answer here is not
a degraded search — it is no search at all, or worse, a confident itinerary to
the wrong airport.
"""

from __future__ import annotations

import pytest

from app.tools.airports import CITY_TO_IATA, METRO_TO_PRIMARY, resolve_iata
from app.tools.errors import FlightSearchError


def test_iata_code_passes_through_untouched() -> None:
    assert resolve_iata("BOM") == "BOM"


def test_lowercase_code_is_upcased() -> None:
    assert resolve_iata("bom") == "BOM"


def test_city_name_resolves() -> None:
    assert resolve_iata("Mumbai") == "BOM"
    assert resolve_iata("mumbai") == "BOM"


def test_multi_airport_cities_resolve_to_a_real_airport() -> None:
    """Verified against the live API: a `LON` search returns zero results
    where `LHR` returns twelve. Metro codes fail silently, so they are never
    what we send."""
    assert resolve_iata("London") == "LHR"
    assert resolve_iata("New York") == "JFK"
    assert resolve_iata("Tokyo") == "HND"
    assert resolve_iata("Paris") == "CDG"


def test_metro_code_passed_in_directly_is_rewritten() -> None:
    """A caller who types "LON" would otherwise get a silent empty result."""
    assert resolve_iata("LON") == "LHR"
    assert resolve_iata("nyc") == "JFK"
    assert resolve_iata("Tokyo (TYO)") == "HND"


def test_no_metro_code_survives_into_the_city_table() -> None:
    leaked = {
        city: code for city, code in CITY_TO_IATA.items() if code in METRO_TO_PRIMARY
    }
    assert not leaked, f"these would search an empty result set: {leaked}"


def test_country_qualifier_is_ignored() -> None:
    assert resolve_iata("Paris, France") == "CDG"


def test_accents_are_stripped() -> None:
    assert resolve_iata("Zürich") == "ZRH"


def test_airport_noise_words_are_ignored() -> None:
    assert resolve_iata("Dubai International Airport") == "DXB"


def test_explicit_code_in_parentheses_wins_over_the_table() -> None:
    """A caller naming the airport has overridden the city default on purpose."""
    assert resolve_iata("Tokyo (HND)") == "HND"


def test_knowledge_graph_id_passes_through() -> None:
    assert resolve_iata("/m/04jpl") == "/m/04jpl"


def test_multi_word_city_resolves() -> None:
    assert resolve_iata("Kuala Lumpur") == "KUL"
    assert resolve_iata("Ho Chi Minh City") == "SGN"


def test_trailing_qualifier_falls_back_to_the_leading_words() -> None:
    assert resolve_iata("London UK") == "LHR"


def test_unknown_place_raises_rather_than_guessing() -> None:
    with pytest.raises(FlightSearchError, match="no airport code known"):
        resolve_iata("Atlantis")


def test_error_tells_the_caller_what_to_pass_instead() -> None:
    with pytest.raises(FlightSearchError, match="IATA"):
        resolve_iata("Some Village")


def test_empty_place_raises() -> None:
    with pytest.raises(FlightSearchError, match="without a place name"):
        resolve_iata("   ")


def test_kyoto_maps_to_its_serving_airport() -> None:
    """Kyoto has no airport of its own; Kansai serves it."""
    assert resolve_iata("Kyoto") == "KIX"


def test_table_values_are_all_well_formed_codes() -> None:
    bad = {
        city: code
        for city, code in CITY_TO_IATA.items()
        if not (len(code) == 3 and code.isalpha() and code.isupper())
    }
    assert not bad, f"malformed IATA codes in the table: {bad}"


def test_table_keys_are_already_normalised() -> None:
    """Keys are looked up post-normalisation, so an unnormalised key is dead."""
    bad = [city for city in CITY_TO_IATA if city != city.lower().strip()]
    assert not bad, f"table keys must be lowercase and stripped: {bad}"
