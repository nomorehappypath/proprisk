"""Where PropRisk works. One function, `get_coverage()`, that returns the
table the Settings → "Where PropRisk works" screen shows and
`GET /api/settings/coverage` serves. The county, city and source lists here
are read from the adapters' own county/city lists (see each adapter listed
below); `tests/test_coverage_matches_adapters.py` checks this table against
those same adapters so the screen cannot silently drift from the code.
"""

# Counties with jurisdiction/parcel/district coverage, from
# jurisdiction_resolver.py, tcad_parcel.py, wcad_parcel.py,
# district_overlays.py, fire_district.py and plats_intel.py's county list.
COUNTIES = [
    {
        "county": "Travis",
        "level": "Full",
        "detail": (
            "City limits and ETJ, parcel layers, districts (MUD, ESD, PID, "
            "TIRZ, school), fire stations, plats and clerk portal, historic "
            "districts (Austin), transit (Capital Metro), city zoning, "
            "future land use and planning cases for its cities"
        ),
    },
    {
        "county": "Williamson",
        "level": "Full",
        "detail": (
            "City limits and ETJ, parcel lookup (WCAD), districts, fire (via "
            "Round Rock), plats and clerk portal, transit, zoning for its "
            "cities"
        ),
    },
    {
        "county": "Hays",
        "level": "Full",
        "detail": (
            "City limits and ETJ, parcel layer, districts, plats and clerk "
            "portal, zoning for Buda, Kyle, Dripping Springs, San Marcos"
        ),
    },
    {
        "county": "Bastrop",
        "level": "Full",
        "detail": "City limits and ETJ, parcel layer, districts, fire, plats and clerk portal",
    },
    {
        "county": "Caldwell",
        "level": "Partial",
        "detail": "Plat records and clerk portal, water/sewer service area",
    },
    {
        "county": "Burnet",
        "level": "Partial",
        "detail": "County clerk portal and city planning-portal links",
    },
    {
        "county": "Blanco",
        "level": "Partial",
        "detail": "Parcel layer, county clerk portal link",
    },
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

# Sources that cover the whole state, from their own adapter modules.
STATEWIDE = [
    "TxDOT traffic, roads, bridges and projects",
    "TCEQ and TWDB water and sewer service areas",
    "TEA schools",
    "TCEQ environmental records",
    "Texas historical aerial photos",
    "Texas parcel layer (TxGIO)",
    "HB 1378 district debt reports",
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
