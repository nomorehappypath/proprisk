"""The 50 US states plus DC: name, census code and map box.

Used to read which state an address is in and to bias the address finder
toward it. A state does not need a PropRisk folder to be listed here, so a
Tennessee address is never searched as a Texas one.
Boxes are (west, south, east, north), from the Census Bureau's TIGERweb
state layer.
"""

from __future__ import annotations

import re

US_STATES: dict[str, tuple[str, str, tuple[float, float, float, float]]] = {
    "AK": ("Alaska", "02", (-179.23, 51.18, 179.86, 71.44)),
    "AL": ("Alabama", "01", (-88.47, 30.14, -84.89, 35.01)),
    "AR": ("Arkansas", "05", (-94.62, 33.0, -89.64, 36.5)),
    "AZ": ("Arizona", "04", (-114.82, 31.33, -109.05, 37.0)),
    "CA": ("California", "06", (-124.48, 32.53, -114.13, 42.01)),
    "CO": ("Colorado", "08", (-109.06, 36.99, -102.04, 41.0)),
    "CT": ("Connecticut", "09", (-73.73, 40.95, -71.79, 42.05)),
    "DC": ("District of Columbia", "11", (-77.12, 38.79, -76.91, 39.0)),
    "DE": ("Delaware", "10", (-75.79, 38.45, -74.98, 39.84)),
    "FL": ("Florida", "12", (-87.63, 24.4, -79.97, 31.0)),
    "GA": ("Georgia", "13", (-85.61, 30.36, -80.78, 35.0)),
    "HI": ("Hawaii", "15", (-178.44, 18.87, -154.76, 28.52)),
    "IA": ("Iowa", "19", (-96.64, 40.38, -90.14, 43.5)),
    "ID": ("Idaho", "16", (-117.24, 41.99, -111.04, 49.0)),
    "IL": ("Illinois", "17", (-91.51, 36.97, -87.02, 42.51)),
    "IN": ("Indiana", "18", (-88.1, 37.77, -84.78, 41.76)),
    "KS": ("Kansas", "20", (-102.05, 36.99, -94.59, 40.0)),
    "KY": ("Kentucky", "21", (-89.57, 36.5, -81.96, 39.15)),
    "LA": ("Louisiana", "22", (-94.04, 28.86, -88.76, 33.02)),
    "MA": ("Massachusetts", "25", (-73.51, 41.19, -69.86, 42.89)),
    "MD": ("Maryland", "24", (-79.49, 37.89, -74.99, 39.72)),
    "ME": ("Maine", "23", (-71.08, 42.92, -66.89, 47.46)),
    "MI": ("Michigan", "26", (-90.42, 41.7, -82.12, 48.31)),
    "MN": ("Minnesota", "27", (-97.24, 43.5, -89.48, 49.38)),
    "MO": ("Missouri", "29", (-95.77, 36.0, -89.1, 40.61)),
    "MS": ("Mississippi", "28", (-91.66, 30.14, -88.1, 35.0)),
    "MT": ("Montana", "30", (-116.05, 44.36, -104.04, 49.0)),
    "NC": ("North Carolina", "37", (-84.32, 33.75, -75.4, 36.59)),
    "ND": ("North Dakota", "38", (-104.05, 45.94, -96.55, 49.0)),
    "NE": ("Nebraska", "31", (-104.05, 40.0, -95.31, 43.0)),
    "NH": ("New Hampshire", "33", (-72.56, 42.7, -70.58, 45.31)),
    "NJ": ("New Jersey", "34", (-75.56, 38.79, -73.89, 41.36)),
    "NM": ("New Mexico", "35", (-109.05, 31.33, -103.0, 37.0)),
    "NV": ("Nevada", "32", (-120.01, 35.0, -114.04, 42.0)),
    "NY": ("New York", "36", (-79.76, 40.48, -71.78, 45.02)),
    "OH": ("Ohio", "39", (-84.82, 38.4, -80.52, 42.33)),
    "OK": ("Oklahoma", "40", (-103.0, 33.62, -94.43, 37.0)),
    "OR": ("Oregon", "41", (-124.7, 41.99, -116.46, 46.3)),
    "PA": ("Pennsylvania", "42", (-80.52, 39.72, -74.69, 42.52)),
    "RI": ("Rhode Island", "44", (-71.91, 41.1, -71.09, 42.02)),
    "SC": ("South Carolina", "45", (-83.35, 32.0, -78.5, 35.22)),
    "SD": ("South Dakota", "46", (-104.06, 42.48, -96.44, 45.95)),
    "TN": ("Tennessee", "47", (-90.31, 34.98, -81.65, 36.68)),
    "TX": ("Texas", "48", (-106.65, 25.84, -93.51, 36.5)),
    "UT": ("Utah", "49", (-114.05, 37.0, -109.04, 42.0)),
    "VA": ("Virginia", "51", (-83.68, 36.54, -75.17, 39.47)),
    "VT": ("Vermont", "50", (-73.44, 42.73, -71.47, 45.02)),
    "WA": ("Washington", "53", (-124.85, 45.54, -116.92, 49.0)),
    "WI": ("Wisconsin", "55", (-92.89, 42.49, -86.25, 47.31)),
    "WV": ("West Virginia", "54", (-82.64, 37.2, -77.72, 40.64)),
    "WY": ("Wyoming", "56", (-111.05, 40.99, -104.05, 45.01)),
}

_BY_NAME = {name.lower(): code for code, (name, _fips, _box) in US_STATES.items()}
_NAME_RE = re.compile(
    r",\s*(" + "|".join(sorted((re.escape(n) for n in _BY_NAME), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
# "Denver, CO 80202" or "Denver, CO" at the end, or "CO 80202" anywhere.
_CODE_RE = re.compile(r"(?:,\s*([A-Za-z]{2})\s*(?:\d{5}(?:-\d{4})?)?\s*$)|(?:\b([A-Z]{2})\s+\d{5}\b)")


def code_for(value: object) -> str | None:
    """A state code from a postal code or a full name, else None."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.upper().startswith("US-"):
        cleaned = cleaned[3:]
    if cleaned.upper() in US_STATES:
        return cleaned.upper()
    return _BY_NAME.get(cleaned.lower())


def state_from_text(address: str) -> str | None:
    """The state the address text names (postal code or full name), else None."""
    text = address or ""
    for match in _CODE_RE.finditer(text):
        code = code_for(match.group(1) or match.group(2))
        if code:
            return code
    match = _NAME_RE.search(text)
    return _BY_NAME[match.group(1).lower()] if match else None


def name_of(code: str) -> str:
    return US_STATES.get(code, (code, "", None))[0]


def settle(named: str | None, provider: str | None) -> tuple[str | None, str]:
    """Settle the lookup's state from the address text and the map provider.

    Returns (state, how). state is None when the two disagree.
    """
    if named and provider:
        return (named, "address_and_map") if named == provider else (None, "conflict")
    if named:
        return named, "address"
    if provider:
        return provider, "map"
    return "TX", "assumed"
