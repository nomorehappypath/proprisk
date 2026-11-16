"""
core/zoning_standards/schema.py

Pydantic models for the curated zoning rules. The schema is
the contract between curation (writing YAML) and consumption
(the engine + adapter).

Design principles:
  - Every numeric standard has explicit units. No implicit
    feet/meters/percent confusion.
  - Every rule has a `code_ref` citing the section it came
    from. The memo renders these so a broker can verify.
  - Every rule has a `notes` field for the human-readable
    nuance ("by right", "requires CUP", "subject to overlay X").
  - Conditional triggers are first-class (overlays, abutting
    use restrictions, NCCD modifiers).
  - Versioning: each city YAML records `last_verified` and
    the UDC version string so the audit trail can say
    "rules from Title 25 as of 2026-04-15."

Adding a city: drop a YAML file under `curated/{city}.yaml`.
Engine auto-discovers files at load time.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Atomic value types ──────────────────────────────────────────


class CitedValue(BaseModel):
    """A numeric or string standard with the code section it
    came from. The whole point of curation is that every
    answer is auditable back to a specific UDC clause."""

    value: float | int | str
    unit: str | None = Field(
        default=None,
        description=(
            "Unit string: 'feet', 'percent', 'units_per_acre', "
            "'square_feet', 'spaces_per_unit', etc. None when "
            "the value is categorical (e.g., 'corner_lot_only')."
        ),
    )
    code_ref: str = Field(
        description=(
            "Source code section, e.g. '25-2-492'. Empty string "
            "is invalid — every value must cite its source."
        ),
        min_length=1,
    )
    notes: str | None = Field(
        default=None,
        description="Human-readable nuance, exceptions, conditions.",
    )


# ── Use entries ────────────────────────────────────────────────


class UseEntry(BaseModel):
    """A use type permitted/conditional/prohibited in a district."""

    use_id: str = Field(
        description=(
            "Canonical use identifier matching the cross-jurisdiction "
            "vocabulary in `_USE_VOCABULARY`. Lets a broker query "
            "'multifamily_residential' and have it match Austin's "
            "'multifamily' AND Hutto's 'apartment_complex'."
        ),
    )
    code_ref: str = Field(
        description="UDC section that lists this use as permitted/CUP/etc.",
        min_length=1,
    )
    notes: str | None = Field(default=None)


# ── Parking ──────────────────────────────────────────────────────


class ParkingRule(BaseModel):
    """Parking ratio for a specific use type. Texas codes vary
    on whether ratios are per-unit, per-bedroom, per-1000-sf,
    etc. — the unit string MUST match the source."""

    use_id: str
    rate: float
    unit: str = Field(
        description=(
            "'spaces_per_unit', 'spaces_per_bedroom', "
            "'spaces_per_1000_sf', 'spaces_per_seat', etc."
        ),
    )
    code_ref: str = Field(min_length=1)
    notes: str | None = Field(default=None)


# ── Overlay interactions ─────────────────────────────────────────


class OverlayInteraction(BaseModel):
    """How an overlay district modifies the base district's
    rules. Overlays are common in Austin (NCCD, NP, McMansion)
    and central to whether a parcel actually buildable as the
    base district suggests."""

    overlay_id: str = Field(
        description=(
            "Overlay identifier ('NCCD', 'NP', 'CO', "
            "'WaterfrontOverlay', etc.)."
        ),
    )
    effect: str = Field(
        description=(
            "Plain-language description of how the overlay "
            "modifies the base district. Memo renders this "
            "verbatim."
        ),
    )
    code_ref: str = Field(min_length=1)


# ── District definition ──────────────────────────────────────────


class DistrictRules(BaseModel):
    """Complete rule set for one zoning district in one city."""

    full_name: str = Field(
        description="Human-readable district name.",
    )
    code_section: str = Field(
        description=(
            "Top-level UDC section that defines the district "
            "(e.g., Austin '25-2-65' for SF-3)."
        ),
        min_length=1,
    )
    purpose: str | None = Field(
        default=None,
        description=(
            "Plain-language purpose statement from the UDC. "
            "Helps the memo render context."
        ),
    )

    permitted_uses: list[UseEntry] = Field(default_factory=list)
    conditional_uses: list[UseEntry] = Field(
        default_factory=list,
        description=(
            "Uses that require a Conditional Use Permit (CUP), "
            "Special Use Permit (SUP), or similar discretionary "
            "approval."
        ),
    )
    prohibited_uses: list[UseEntry] = Field(default_factory=list)

    dimensional_standards: dict[str, CitedValue] = Field(
        default_factory=dict,
        description=(
            "Standardized dimensional keys (see "
            "`_DIMENSIONAL_KEYS`). Each value is a CitedValue "
            "so the memo can render '60 ft per §25-2-492'."
        ),
    )

    parking: list[ParkingRule] = Field(default_factory=list)
    overlay_interactions: list[OverlayInteraction] = Field(
        default_factory=list,
    )

    notes: str | None = Field(
        default=None,
        description="District-level free-text notes.",
    )


# ── City file ──────────────────────────────────────────────────


class CityZoningStandards(BaseModel):
    """One city's complete zoning rules. Loaded from
    `core/zoning_standards/curated/{city}.yaml`."""

    city: str = Field(min_length=1, description="Lowercase city name.")
    state: str = Field(default="TX", min_length=2, max_length=2)
    udc_version: str = Field(
        description=(
            "Title or version label, e.g. 'Title 25 - Land "
            "Development Code (Austin, current through "
            "Ord. 20240314-093)'."
        ),
        min_length=1,
    )
    udc_source_url: str = Field(
        description="Public URL to the UDC text/PDF.",
        min_length=1,
    )
    last_verified: str = Field(
        description="ISO date, e.g. '2026-05-02'.",
        min_length=10,
        max_length=10,
    )
    districts: dict[str, DistrictRules] = Field(
        description=(
            "District key (e.g. 'MF-4', 'CS-MU') → DistrictRules. "
            "Keys are case-sensitive and must match the city's "
            "zoning_type strings as published by its GIS layer."
        ),
    )


# ── Vocabularies (cross-jurisdiction normalization) ─────────────


# Standardized dimensional keys. New entries land here as new
# districts/cities surface them. Keys MUST be stable —
# downstream queries reference them.
_DIMENSIONAL_KEYS: tuple[str, ...] = (
    "max_density",            # units per acre
    "max_far",                # floor area ratio
    "max_height",             # feet
    "max_stories",            # count
    "min_lot_size",           # square feet
    "min_lot_width",          # feet
    "min_lot_depth",          # feet
    "front_setback",          # feet
    "side_setback",           # feet
    "rear_setback",           # feet
    "side_setback_corner",    # feet
    "max_impervious_cover",   # percent
    "max_building_coverage",  # percent
    "min_open_space",         # percent
    "max_units_per_lot",      # count
)


# Canonical use vocabulary. New use IDs land here as cities
# surface them. The point is cross-jurisdiction match — a
# broker querying 'multifamily_residential' should match
# Austin's permitted_uses entry under MF-4 even though
# Austin's UDC calls it 'multifamily'.
_USE_VOCABULARY: tuple[str, ...] = (
    # Residential
    "single_family_detached",
    "single_family_attached",
    "duplex",
    "triplex",
    "fourplex",
    "townhouse",
    "condominium",
    "multifamily_residential",
    "manufactured_housing",
    "mobile_home",
    "accessory_dwelling_unit",
    "group_residential",
    "senior_living",
    "assisted_living",
    "nursing_home",
    "extended_stay",
    # Commercial
    "general_retail",
    "convenience_store",
    "grocery",
    "restaurant",
    "bar_or_lounge",
    "auto_sales",
    "auto_service",
    "gas_station",
    "car_wash",
    "drive_through",
    "office_general",
    "office_medical",
    "bank_or_financial",
    "personal_services",
    "hotel",
    "motel",
    "bed_and_breakfast",
    # Industrial
    "warehouse",
    "light_industrial",
    "heavy_industrial",
    "manufacturing",
    "outdoor_storage",
    "self_storage",
    "research_and_development",
    # Civic / institutional
    "school_public",
    "school_private",
    "church",
    "library",
    "hospital",
    "clinic",
    "childcare",
    "park",
    "community_center",
    # Mixed / other
    "mixed_use",
    "live_work",
    "agricultural",
    "ag_exempt",
    "telecom_tower",
    "data_center",
    "solar_farm",
    "ev_charging",
    "event_venue",
    "gym_or_fitness",
    "cannabis_retail",
)


__all__ = [
    "CitedValue",
    "CityZoningStandards",
    "DistrictRules",
    "OverlayInteraction",
    "ParkingRule",
    "UseEntry",
]


# ── Smoke test ────────────────────────────────────────────────


if __name__ == "__main__":
    rules = DistrictRules(
        full_name="Multifamily Residence Moderate-High Density",
        code_section="25-2-67",
        purpose="Moderate-to-high density multifamily housing.",
        permitted_uses=[
            UseEntry(
                use_id="multifamily_residential",
                code_ref="25-2-491",
                notes="By right.",
            ),
        ],
        dimensional_standards={
            "max_height": CitedValue(
                value=60, unit="feet", code_ref="25-2-492",
            ),
            "max_impervious_cover": CitedValue(
                value=65, unit="percent", code_ref="25-2-492",
            ),
        },
        parking=[
            ParkingRule(
                use_id="multifamily_residential",
                rate=1.5,
                unit="spaces_per_bedroom",
                code_ref="25-6-471",
            ),
        ],
    )

    city = CityZoningStandards(
        city="austin",
        udc_version="Title 25 — Land Development Code",
        udc_source_url="https://library.municode.com/tx/austin",
        last_verified="2026-05-02",
        districts={"MF-4": rules},
    )
    assert city.districts["MF-4"].dimensional_standards["max_height"].value == 60
    print("zoning_standards.schema: smoke OK")
