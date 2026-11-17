"""Where PropRisk works. One function, `get_coverage()`, that returns the
table the Settings → "Where PropRisk works" screen shows and
`GET /api/settings/coverage` serves. The county rows and the statewide sources
come from the county and state files in app/regions/; the city lists are read
from the adapters' own city lists (see each one below).
`tests/test_coverage_matches_adapters.py` checks this table against those files
and adapters so the screen cannot silently drift from the code.
"""

from app.regions import loader as regions

# One row per county file (app/regions/tx/counties/*.yaml): Full counties
# first, then Partial, each group in alphabetical order. A county is listed
# here only if it has a file.
COUNTIES = [
    {"county": c["name"], "level": c["level"], "detail": c["summary"]}
    for c in sorted(
        regions.counties("TX").values(),
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
    "Kyle", "Manor", "Pflugerville", "Round Rock",
]

# Cities with future land use, from future_land_use.py's city list.
FUTURE_LAND_USE_CITIES = [
    "Austin", "Buda", "Kyle", "Round Rock", "Pflugerville", "Hutto", "Georgetown", "Cedar Park",
]

# Sources that cover the whole state, from the state file (app/regions/tx/state.yaml).
STATEWIDE = list(regions.state("TX")["statewide_summary"])

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
        "state": "Texas",
        "counties": COUNTIES,
        "cities": {
            "zoning": ZONING_CITIES,
            "standards": STANDARDS_CITIES,
            "future_land_use": FUTURE_LAND_USE_CITIES,
        },
        "statewide": STATEWIDE,
        "nationwide": NATIONWIDE,
    }
