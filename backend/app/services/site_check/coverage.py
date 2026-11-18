"""Where PropRisk works. One function, `get_coverage()`, that returns the
table the Settings → "Where PropRisk works" screen shows and
`GET /api/settings/coverage` serves. The county rows and the statewide sources
come from the county and state files in app/regions/; the city lists are read
from the adapters' own city lists (see each one below).
`tests/test_coverage_matches_adapters.py` checks this table against those files
and adapters so the screen cannot silently drift from the code.
"""

from app.regions import loader as regions

# The states with a folder in app/regions/, Texas first, then by name.
STATES = sorted(
    (regions.state(code)["name"] for code in regions.load_regions()),
    key=lambda name: (name != "Texas", name),
)

# One row per county file (app/regions/<state>/counties/*.yaml), grouped by
# state; within a state, Full counties first, then Partial, each group in
# alphabetical order. A county is listed here only if it has a file.
COUNTIES = [
    {"state": regions.state(code)["name"], "county": c["name"],
     "level": c["level"], "detail": c["summary"]}
    for code in sorted(
        regions.load_regions(),
        key=lambda code: STATES.index(regions.state(code)["name"]),
    )
    for c in sorted(
        regions.counties(code).values(),
        key=lambda c: (c["level"] != "Full", c["name"]),
    )
]

# Cities with zoning data, from austin_zoning.py's and entitlement_pipeline.py's city lists.
ZONING_CITIES = [
    "Austin", "Pflugerville", "Cedar Park", "Georgetown", "Hutto", "Round Rock",
    "Buda", "Kyle", "Dripping Springs", "San Marcos", "Leander", "Taylor", "Elgin",
]

# Cities with curated written zoning standards, from zoning_standards.py's curated YAML files.
STANDARDS_CITIES = [
    "Austin", "Buda", "Cedar Park", "Dripping Springs", "Georgetown", "Hutto",
    "Kyle", "Manor", "Pflugerville", "Round Rock", "Denver",
]

# Cities with future land use, from future_land_use.py's city list.
FUTURE_LAND_USE_CITIES = [
    "Austin", "Buda", "Kyle", "Round Rock", "Pflugerville", "Hutto", "Georgetown", "Cedar Park",
]


def _own_source_places(slot: str) -> list[str]:
    """Counties that connect this card with their own source (spec 2.6),
    named as the city and county they are ("Denver")."""
    from app.regions import sources

    return [
        county["name"]
        for code in regions.load_regions()
        for key, county in regions.counties(code).items()
        if slot in sources.modules_for(code, key)
    ]


# What Settings shows: the built-in Texas cities plus every county that
# connects the card with its own source.
ZONING_PLACES = ZONING_CITIES + _own_source_places("austin_zoning")
FUTURE_LAND_USE_PLACES = FUTURE_LAND_USE_CITIES + _own_source_places("future_land_use")

# Sources that cover a whole state, from each state file
# (app/regions/<state>/state.yaml), named with their state.
STATEWIDE = [
    f"{state['name']}: {item}"
    for code in sorted(
        regions.load_regions(),
        key=lambda code: STATES.index(regions.state(code)["name"]),
    )
    for state in [regions.state(code)]
    for item in state["statewide_summary"]
]

# Sources that cover the whole country, from their own adapter modules.
NATIONWIDE = [
    "FEMA flood zones",
    "USGS elevation and streams",
    "NRCS soils",
    "National Wetlands Inventory",
    "EPA ECHO facilities",
    "HIFLD power lines and substations",
    "OpenStreetMap places and infrastructure",
    "Modern aerial imagery",
    "FCC broadband link",
]


def get_coverage() -> dict:
    return {
        "state": ", ".join(STATES),
        "counties": COUNTIES,
        "cities": {
            "zoning": ZONING_PLACES,
            "standards": STANDARDS_CITIES,
            "future_land_use": FUTURE_LAND_USE_PLACES,
        },
        "statewide": STATEWIDE,
        "nationwide": NATIONWIDE,
    }
