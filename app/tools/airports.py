"""City and airport names -> IATA codes, without calling a provider.

SerpAPI's Google Flights engine rejects anything that is not an uppercase
three-letter code or a Google knowledge-graph id — `departure_id="Mumbai"`
comes back as HTTP 400, not as a best-effort match. The Amadeus path never
needed this because Amadeus has a reference-data endpoint for it; the SerpAPI
path has no equivalent, so the resolution happens here.

A static table rather than a lookup service, deliberately:

* one extra network round trip per search doubles both the latency and the
  number of things that can fail, for a lookup whose answer never changes;
* the set of place names a traveller actually types is small and stable.

Multi-airport cities resolve to one specific airport, **not** the metro code.
This is counter-intuitive and was verified against the live API: a `LON` search
returns "Google Flights hasn't returned any results for this query" with HTTP
200, while the same search on `LHR` returns twelve itineraries. Metro codes
therefore fail *silently* — they look like a route nobody flies rather than
like a bad parameter, which is the worst possible failure mode. `resolve_iata`
rewrites any metro code it is handed, including one a caller passes directly.

An unknown name raises rather than guessing. A wrong airport produces a
plausible-looking itinerary to the wrong continent, which is far worse than an
error message telling the caller to pass the code.
"""

from __future__ import annotations

import re
import unicodedata

from app.tools.errors import FlightSearchError

# City/metro codes Google Flights accepts as input but answers with nothing.
# Mapped to the city's main long-haul airport, which is the one a traveller
# naming the city is overwhelmingly likely to want.
METRO_TO_PRIMARY: dict[str, str] = {
    "LON": "LHR",
    "NYC": "JFK",
    "TYO": "HND",
    "PAR": "CDG",
    "OSA": "KIX",
    "SEL": "ICN",
    "BJS": "PEK",
    "MOW": "SVO",
    "MIL": "MXP",
    "ROM": "FCO",
    "WAS": "IAD",
    "CHI": "ORD",
    "BUE": "EZE",
    "SAO": "GRU",
    "RIO": "GIG",
    "YTO": "YYZ",
    "YMQ": "YUL",
    "STO": "ARN",
    "REK": "KEF",
}

CITY_TO_IATA: dict[str, str] = {
    # --- India ---------------------------------------------------------
    "mumbai": "BOM",
    "bombay": "BOM",
    "delhi": "DEL",
    "new delhi": "DEL",
    "bengaluru": "BLR",
    "bangalore": "BLR",
    "hyderabad": "HYD",
    "chennai": "MAA",
    "madras": "MAA",
    "kolkata": "CCU",
    "calcutta": "CCU",
    "pune": "PNQ",
    "ahmedabad": "AMD",
    "goa": "GOI",
    "kochi": "COK",
    "cochin": "COK",
    "jaipur": "JAI",
    "lucknow": "LKO",
    "srinagar": "SXR",
    "varanasi": "VNS",
    "amritsar": "ATQ",
    "guwahati": "GAU",
    "thiruvananthapuram": "TRV",
    "trivandrum": "TRV",
    "udaipur": "UDR",
    "leh": "IXL",
    "port blair": "IXZ",
    # --- Asia-Pacific --------------------------------------------------
    "tokyo": "HND",
    "osaka": "KIX",
    "kyoto": "KIX",  # Kyoto has no airport; Kansai serves it.
    "nagoya": "NGO",
    "sapporo": "CTS",
    "fukuoka": "FUK",
    "okinawa": "OKA",
    "seoul": "ICN",
    "busan": "PUS",
    "beijing": "PEK",
    "shanghai": "PVG",
    "guangzhou": "CAN",
    "shenzhen": "SZX",
    "chengdu": "CTU",
    "hong kong": "HKG",
    "macau": "MFM",
    "taipei": "TPE",
    "singapore": "SIN",
    "bangkok": "BKK",
    "phuket": "HKT",
    "chiang mai": "CNX",
    "krabi": "KBV",
    "kuala lumpur": "KUL",
    "penang": "PEN",
    "jakarta": "CGK",
    "bali": "DPS",
    "denpasar": "DPS",
    "surabaya": "SUB",
    "manila": "MNL",
    "cebu": "CEB",
    "hanoi": "HAN",
    "ho chi minh city": "SGN",
    "saigon": "SGN",
    "da nang": "DAD",
    "phnom penh": "PNH",
    "siem reap": "SAI",
    "vientiane": "VTE",
    "yangon": "RGN",
    "kathmandu": "KTM",
    "colombo": "CMB",
    "male": "MLE",
    "maldives": "MLE",
    "dhaka": "DAC",
    "thimphu": "PBH",
    "paro": "PBH",
    "karachi": "KHI",
    "lahore": "LHE",
    "islamabad": "ISB",
    "sydney": "SYD",
    "melbourne": "MEL",
    "brisbane": "BNE",
    "perth": "PER",
    "adelaide": "ADL",
    "cairns": "CNS",
    "gold coast": "OOL",
    "canberra": "CBR",
    "hobart": "HBA",
    "auckland": "AKL",
    "wellington": "WLG",
    "christchurch": "CHC",
    "queenstown": "ZQN",
    "nadi": "NAN",
    "fiji": "NAN",
    # --- Middle East ---------------------------------------------------
    "dubai": "DXB",
    "abu dhabi": "AUH",
    "sharjah": "SHJ",
    "doha": "DOH",
    "muscat": "MCT",
    "kuwait": "KWI",
    "kuwait city": "KWI",
    "manama": "BAH",
    "bahrain": "BAH",
    "riyadh": "RUH",
    "jeddah": "JED",
    "dammam": "DMM",
    "tel aviv": "TLV",
    "jerusalem": "TLV",
    "amman": "AMM",
    "beirut": "BEY",
    "istanbul": "IST",
    "ankara": "ESB",
    "antalya": "AYT",
    "baku": "GYD",
    "tbilisi": "TBS",
    "yerevan": "EVN",
    "tehran": "IKA",
    # --- Europe --------------------------------------------------------
    "london": "LHR",
    "manchester": "MAN",
    "birmingham": "BHX",
    "edinburgh": "EDI",
    "glasgow": "GLA",
    "dublin": "DUB",
    "paris": "CDG",
    "nice": "NCE",
    "lyon": "LYS",
    "marseille": "MRS",
    "toulouse": "TLS",
    "bordeaux": "BOD",
    "amsterdam": "AMS",
    "brussels": "BRU",
    "luxembourg": "LUX",
    "berlin": "BER",
    "munich": "MUC",
    "frankfurt": "FRA",
    "hamburg": "HAM",
    "dusseldorf": "DUS",
    "cologne": "CGN",
    "stuttgart": "STR",
    "zurich": "ZRH",
    "geneva": "GVA",
    "basel": "BSL",
    "vienna": "VIE",
    "salzburg": "SZG",
    "prague": "PRG",
    "budapest": "BUD",
    "warsaw": "WAW",
    "krakow": "KRK",
    "bratislava": "BTS",
    "ljubljana": "LJU",
    "zagreb": "ZAG",
    "split": "SPU",
    "dubrovnik": "DBV",
    "belgrade": "BEG",
    "bucharest": "OTP",
    "sofia": "SOF",
    "athens": "ATH",
    "santorini": "JTR",
    "mykonos": "JMK",
    "thessaloniki": "SKG",
    "rome": "FCO",
    "milan": "MXP",
    "venice": "VCE",
    "florence": "FLR",
    "naples": "NAP",
    "pisa": "PSA",
    "bologna": "BLQ",
    "turin": "TRN",
    "catania": "CTA",
    "palermo": "PMO",
    "madrid": "MAD",
    "barcelona": "BCN",
    "seville": "SVQ",
    "valencia": "VLC",
    "malaga": "AGP",
    "bilbao": "BIO",
    "palma": "PMI",
    "mallorca": "PMI",
    "ibiza": "IBZ",
    "lisbon": "LIS",
    "porto": "OPO",
    "faro": "FAO",
    "copenhagen": "CPH",
    "stockholm": "ARN",
    "oslo": "OSL",
    "bergen": "BGO",
    "helsinki": "HEL",
    "reykjavik": "KEF",
    "riga": "RIX",
    "vilnius": "VNO",
    "tallinn": "TLL",
    "moscow": "SVO",
    "saint petersburg": "LED",
    "st petersburg": "LED",
    "kyiv": "KBP",
    "kiev": "KBP",
    "malta": "MLA",
    "valletta": "MLA",
    "nicosia": "LCA",
    "larnaca": "LCA",
    # --- Africa --------------------------------------------------------
    "cairo": "CAI",
    "hurghada": "HRG",
    "sharm el sheikh": "SSH",
    "casablanca": "CMN",
    "marrakech": "RAK",
    "tunis": "TUN",
    "algiers": "ALG",
    "lagos": "LOS",
    "abuja": "ABV",
    "accra": "ACC",
    "dakar": "DSS",
    "nairobi": "NBO",
    "mombasa": "MBA",
    "kigali": "KGL",
    "kampala": "EBB",
    "dar es salaam": "DAR",
    "zanzibar": "ZNZ",
    "addis ababa": "ADD",
    "johannesburg": "JNB",
    "cape town": "CPT",
    "durban": "DUR",
    "victoria falls": "VFA",
    "windhoek": "WDH",
    "mauritius": "MRU",
    "port louis": "MRU",
    "seychelles": "SEZ",
    "victoria": "SEZ",
    "antananarivo": "TNR",
    # --- North America -------------------------------------------------
    "new york": "JFK",
    "new york city": "JFK",
    "nyc": "JFK",
    "los angeles": "LAX",
    "san francisco": "SFO",
    "san jose": "SJC",
    "chicago": "ORD",
    "washington": "IAD",
    "washington dc": "IAD",
    "boston": "BOS",
    "philadelphia": "PHL",
    "atlanta": "ATL",
    "miami": "MIA",
    "orlando": "MCO",
    "tampa": "TPA",
    "dallas": "DFW",
    "houston": "IAH",
    "austin": "AUS",
    "denver": "DEN",
    "phoenix": "PHX",
    "las vegas": "LAS",
    "seattle": "SEA",
    "portland": "PDX",
    "san diego": "SAN",
    "detroit": "DTW",
    "minneapolis": "MSP",
    "salt lake city": "SLC",
    "nashville": "BNA",
    "new orleans": "MSY",
    "charlotte": "CLT",
    "honolulu": "HNL",
    "anchorage": "ANC",
    "toronto": "YYZ",
    "montreal": "YUL",
    "vancouver": "YVR",
    "calgary": "YYC",
    "ottawa": "YOW",
    "quebec city": "YQB",
    "mexico city": "MEX",
    "cancun": "CUN",
    "guadalajara": "GDL",
    "monterrey": "MTY",
    "los cabos": "SJD",
    "puerto vallarta": "PVR",
    "havana": "HAV",
    "san juan": "SJU",
    "kingston": "KIN",
    "montego bay": "MBJ",
    "nassau": "NAS",
    "punta cana": "PUJ",
    "santo domingo": "SDQ",
    "panama city": "PTY",
    "san jose costa rica": "SJO",
    "guatemala city": "GUA",
    # --- South America -------------------------------------------------
    "sao paulo": "GRU",
    "rio de janeiro": "GIG",
    "brasilia": "BSB",
    "buenos aires": "EZE",
    "santiago": "SCL",
    "lima": "LIM",
    "cusco": "CUZ",
    "bogota": "BOG",
    "cartagena": "CTG",
    "medellin": "MDE",
    "quito": "UIO",
    "guayaquil": "GYE",
    "la paz": "LPB",
    "montevideo": "MVD",
    "asuncion": "ASU",
    "caracas": "CCS",
}

# Common suffixes people append that carry no routing information.
_NOISE = re.compile(
    r"\b(international|intl|airport|city centre|city center|downtown)\b"
)
_PARENTHESISED_CODE = re.compile(r"\(([A-Za-z]{3})\)")


def _normalise(place: str) -> str:
    """Casefold, strip accents and punctuation: "Zürich, CH" -> "zurich"."""
    decomposed = unicodedata.normalize("NFKD", place)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    # Everything after a comma is a qualifier ("Paris, France"), not the name.
    head = ascii_only.split(",")[0]
    lowered = _NOISE.sub(" ", head.lower())
    cleaned = re.sub(r"[^a-z ]+", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def _usable(code: str) -> str:
    """Swap a metro code for a real airport Google will actually answer on."""
    return METRO_TO_PRIMARY.get(code, code)


def resolve_iata(place: str) -> str:
    """"Mumbai" -> "BOM". Raises `FlightSearchError` when the name is unknown.

    Accepts, in order: an IATA code as-is, a Google knowledge-graph id
    (`/m/...`), a code in parentheses ("Tokyo (HND)"), then the city table.
    Metro codes are rewritten at every one of those exits, because Google
    answers them with an empty result set rather than an error.
    """
    raw = (place or "").strip()
    if not raw:
        raise FlightSearchError("cannot search flights without a place name")

    # A knowledge-graph id is what Google itself uses; pass it straight on.
    if raw.startswith("/m/") or raw.startswith("/g/"):
        return raw

    if len(raw) == 3 and raw.isalpha():
        return _usable(raw.upper())

    # "Tokyo (HND)" — an explicit code always beats the table.
    parenthesised = _PARENTHESISED_CODE.search(raw)
    if parenthesised:
        return _usable(parenthesised.group(1).upper())

    key = _normalise(raw)
    if key in CITY_TO_IATA:
        return _usable(CITY_TO_IATA[key])

    # "greater london", "london uk" — try the leading words too.
    words = key.split()
    for size in range(len(words) - 1, 0, -1):
        prefix = " ".join(words[:size])
        if prefix in CITY_TO_IATA:
            return _usable(CITY_TO_IATA[prefix])

    raise FlightSearchError(
        f"no airport code known for {place!r} — pass the three-letter IATA "
        f"code instead (for example 'BOM' for Mumbai)"
    )


__all__ = ["CITY_TO_IATA", "METRO_TO_PRIMARY", "resolve_iata"]
