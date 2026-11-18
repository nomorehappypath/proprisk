"""The facts each card reads, per slot (spec 2.6).

A state's or county's own source for a slot must return at least the
REQUIRED fields of that slot (the ones the card, the map or a later source
cannot do without), plus the STANDARD fields every own source carries.
An empty list is an answer ("none found"); a missing field is not.
The check command and the source tests use these lists.
"""

from __future__ import annotations

STANDARD: tuple[str, ...] = ("provider_name", "source_url", "data_as_of")

REQUIRED: dict[str, tuple[str, ...]] = {
    "txdot_aadt": ("traffic_counts",),
    "txdot_aadt_history": ("stations",),
    "txdot_roadway": ("roadways",),
    "txdot_projects": ("projects",),
    "txdot_bridges": ("items", "nearest_distance_mi"),
    "transit_intel": ("bus_stops_count", "rail_stations_count"),
    "austin_zoning": ("zoning_type", "base_zone"),
    "zoning_standards": ("district",),
    "future_land_use": ("designations", "plan_name"),
    "entitlement_pipeline": ("zoning_cases", "city_entitlement_records"),
    "historic_intel": ("is_in_local_historic_district",),
    "twdb_ccn": ("service_territories",),
    "schools_intel": ("school_district",),
    "tax_intel": ("levies",),
    "state_environmental_records": ("pst_records",),
}

# Sources that answer one part of another slot's result.
EMBEDDED: dict[str, str] = {
    "state_environmental_records": "environmental_screening",
}
PART_NAME: dict[str, str] = {
    "state_environmental_records": "state_records",
}
