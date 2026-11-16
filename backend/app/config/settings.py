"""PropRisk settings. Every non-secret tunable lives here, env-overridable.

Never read os.environ at call sites — import `settings` from this module.
Every API key and token lives in the local secret store
(`app/core/secret_store.py`), never here and never in an environment
variable: see SPEC.md chapter 1.6.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# PropRisk/ — this file lives at PropRisk/backend/app/config/settings.py.
# Data paths default to an ABSOLUTE location under it, so they resolve the
# same way regardless of the process's current working directory (the
# launch script always runs from backend/, but a test runner or anything
# else may not).
_PROPRISK_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DATA_DIR = _PROPRISK_ROOT / "data"


class ModelPrice(BaseModel):
    """Per-model OpenAI pricing (spec site_check.md §11.1).

    Rates are US DOLLARS per 1,000 tokens, parsed as Decimal (never float) and
    validated to at most 6 decimal places so `rate × 1_000_000` is an EXACT
    integer — all downstream metering arithmetic is pure-integer microdollars.
    """

    input_usd_per_1k: Decimal
    output_usd_per_1k: Decimal
    max_output_tokens: int = Field(gt=0)

    @field_validator("input_usd_per_1k", "output_usd_per_1k")
    @classmethod
    def _exact_microdollars(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("model rates must be positive")
        micro = value * 1_000_000
        if micro != micro.to_integral_value():
            raise ValueError(
                "model rates must have at most 6 decimal places so the "
                "microdollars-per-1k value is an exact integer"
            )
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "PropRisk"
    # Kept in step with [project]/version in pyproject.toml; stamped into every
    # error_sink event's required "version" field (nmhp-error/1 contract).
    app_version: str = "0.1.0"
    environment: str = "dev"  # dev | prod

    # A private PostgreSQL cluster the launch script owns (SPEC.md §1, §2):
    # 127.0.0.1 only, its own port, its own data directory under PropRisk/data.
    database_url: str = "postgresql+psycopg://proprisk@127.0.0.1:55432/proprisk"
    proprisk_port: int = 8765
    proprisk_data_dir: str = str(_DEFAULT_DATA_DIR)
    secrets_db_path: str = str(_DEFAULT_DATA_DIR / "proprisk_secrets.sqlite3")

    # Central error sink (SPEC.md chapter 8). The forwarding token itself is
    # not here — it lives in the secret store (name "error_sink_token").
    error_sink_file: str = str(_DEFAULT_DATA_DIR / "logs" / "errors.jsonl")
    error_sink_url: str | None = None
    error_sink_rate: int = Field(default=60, ge=1)

    # AI gateway metering + budgets (ledger in integer cents; arithmetic in
    # integer microdollars — spec site_check.md §11).
    # LEGACY/INERT since brick 12: per-model rates live in openai_model_prices
    # below; these two stay declared only so old .env files load without error.
    ai_prompt_cents_per_1k: float = 0.25
    ai_completion_cents_per_1k: float = 1.0
    # Flat per-call reservation ceiling used when a call site has no token
    # estimate (the scanner path — behavior unchanged by brick 12).
    ai_reserve_cents_per_call: int = 5
    monthly_budget_cents_default: int = 2000
    budget_warn_threshold: float = 0.8
    gateway_pending_timeout_minutes: int = 10

    # Per-model OpenAI pricing map (spec §11.1). USD per 1,000 tokens, ≤ 6 dp
    # (validated → exact integer microdollars per 1k). Every configured model
    # name MUST have an entry — validated at startup, fail fast.
    openai_model_prices: dict[str, ModelPrice] = {
        # Legacy scanner pair migrated into the map: 0.25 ¢/1k in, 1.0 ¢/1k out.
        "gpt-4o": ModelPrice(
            input_usd_per_1k=Decimal("0.0025"),
            output_usd_per_1k=Decimal("0.01"),
            max_output_tokens=16_384,
        ),
        # GPT-5.6 family (Admin's guidance): terra default, sol escalation,
        # luna economy. max_output_tokens = the models' permitted maximum —
        # no artificial caps (house rule).
        "gpt-5.6-terra": ModelPrice(
            input_usd_per_1k=Decimal("0.002"),
            output_usd_per_1k=Decimal("0.012"),
            max_output_tokens=128_000,
        ),
        "gpt-5.6-sol": ModelPrice(
            input_usd_per_1k=Decimal("0.005"),
            output_usd_per_1k=Decimal("0.03"),
            max_output_tokens=128_000,
        ),
        "gpt-5.6-luna": ModelPrice(
            input_usd_per_1k=Decimal("0.0002"),
            output_usd_per_1k=Decimal("0.0012"),
            max_output_tokens=128_000,
        ),
    }

    # ── Site Check (brick 12) — PropDesk-new fields (spec §12) ─────────────
    # Admin's 180-day rule: saved public facts count as current this long.
    # Every reuse window, cache TTL, badge threshold, and copy string derives
    # from this field — nothing renders a hard-coded 180 or "6 months".
    site_check_freshness_days: int = Field(default=180, ge=1)
    # Amber "expiring" window before the freshness deadline (validated < freshness).
    site_check_expiring_notice_days: int = Field(default=30, ge=1)
    # §11 narrative model. Sol/Luna are config values, never automatic fallbacks.
    site_check_narrative_model: str = "gpt-5.6-terra"
    # Declared for Phase B readers; unused in Phase A (gpt-5.6-sol is the
    # documented escalation value).
    site_check_vision_model: str = "gpt-5.6-terra"
    site_check_document_model: str = "gpt-5.6-terra"

    # ── Background-job foundation (spec §7) ────────────────────────────────
    job_heartbeat_interval_seconds: int = Field(default=30, ge=1)
    # A running job with no heartbeat for this long is stale (validated > heartbeat).
    job_stale_after_seconds: int = Field(default=120, ge=2)
    job_sweep_interval_seconds: int = Field(default=600, ge=1)
    job_max_attempts: int = Field(default=2, ge=1)
    worker_max_concurrency: int = Field(default=3, ge=1)
    # Per-work-unit Postgres lock_timeout (spec §7.7: a blocked terminal
    # transition raises instead of wedging the worker thread).
    job_lock_timeout_seconds: int = Field(default=30, ge=1)
    # Idle pause between claim attempts when the queue is empty.
    worker_poll_interval_seconds: float = Field(default=1.0, ge=0.05)

    # ── Site Check ported orchestration tuning (keys themselves live in the
    # secret store — see app/core/secret_store.py) ─────────────────────────
    google_geocode_enabled: bool = Field(
        default=True,
        description=(
            "Gate for the Google Geocoding final-fallback tier. False (or no "
            "google_maps_api_key) skips the tier cleanly — the run falls to "
            "the existing geocode_failed path."
        ),
    )
    google_geocode_cost_micro_per_call: int = Field(
        default=5000,
        ge=0,
        description=(
            "Estimated cost per Google Geocoding API REQUEST in integer "
            "microdollars (default 5000 µ$ = $5/1K calls). Charged on the "
            "provider-usage row for every request the google tier actually "
            "sends — including one that returns ZERO_RESULTS or an error, "
            "because Google bills those too; a disabled gate or a missing "
            "key sends nothing and is charged nothing. Env-overridable so a "
            "pricing change never needs a code edit."
        ),
    )

    # ── Site Check ported orchestration (= the reference app) ───────────────────────
    public_intel_parallel_enabled: bool = Field(
        default=True,
        description=(
            "When True, the orchestrator fans out adapters in parallel waves; "
            "False falls back to the legacy sequential loop."
        ),
    )
    public_intel_max_workers: int = Field(
        default=8,
        ge=1,
        le=32,
        description=(
            "Maximum concurrent adapter workers in the parallel fetch "
            "orchestrator (one ThreadPoolExecutor thread each)."
        ),
    )
    public_intel_anchor_parcel_max_km: float = Field(
        default=2.0,
        ge=0.1,
        le=50.0,
        description=(
            "Max distance (km) the geocoded anchor may sit from the supplied "
            "parcel's centroid before the orchestrator re-anchors on the "
            "parcel. Only applies when the anchor is ALSO outside the parcel "
            "polygon. Well above real geocode error, well below a wrong-county "
            "match."
        ),
    )
    public_intel_provider_concurrency_caps: dict[str, int] = Field(
        default_factory=lambda: {"txdot": 2, "default": 4},
        description=(
            "Per-provider concurrency caps for the parallel orchestrator. "
            "Keys match AdapterRegistration.provider_concurrency_group "
            "(falling back to .provider). 'default' applies to any provider "
            "not explicitly listed."
        ),
    )
    public_intel_rate_limit_cooldown_seconds: int = Field(
        default=300,
        ge=0,
        description=(
            "How long a public-data source stays in retry cooldown after a "
            "provider rate limit (HTTP 429), so a manual refresh does not "
            "immediately hit the same rate-limited site again."
        ),
    )
    public_aerial_imagery_ttl_days: int = Field(
        default=100,
        ge=0,
        le=3650,
        description=(
            "Aerial chip cache lifetime in days (capped at "
            "site_check_freshness_days). The orthoimage is static, so a "
            "refresh reuses a prior run's already-validated chip until it is "
            "this many days old. 0 disables reuse (always re-validate)."
        ),
    )
    public_entitlement_gis_enabled: bool = Field(
        default=True,
        description=(
            "When true, the entitlement pipeline queries a county's public GIS "
            "subdivisions layer for nearby platting activity before falling "
            "back to manual agenda pointers."
        ),
    )
    public_entitlement_subdivision_radius_m: int = Field(
        default=3200,
        ge=100,
        le=16000,
        description=(
            "Radius for the county recorded-plat sweep, independent of the "
            "city case-feed radius. Recorded plats are the area's residential "
            "supply pipeline, so this is the 'what am I competing with' "
            "distance (default 2 mi), not the 'what touches my parcel' one."
        ),
    )
    public_entitlement_subdivision_display_limit: int = Field(
        default=12,
        ge=1,
        le=50,
        description=(
            "How many recorded plats (nearest first) the card lists. The full "
            "count and the largest nearby plat are always stated, so raising "
            "this changes the table length, not the honesty of the summary."
        ),
    )
    public_plat_discovery_enabled: bool = Field(
        default=True,
        description=(
            "When true, plats_intel tries free/unauthenticated public web/PDF "
            "discovery for probable recorded-plat document numbers."
        ),
    )
    public_plat_search_provider: Literal["disabled", "bing_html", "searxng", "brave", "bing"] = (
        Field(
            default="bing_html",
            description=(
                "Search backend for public plat discovery. 'bing_html' is a "
                "best-effort no-key web discovery fallback; production can use "
                "SearXNG, Brave, or Bing without changing plats_intel."
            ),
        )
    )
    public_plat_searxng_url: str | None = Field(
        default=None,
        description="Base URL for a self-hosted SearXNG instance.",
    )

    # ── Site Check ported HTTP resilience (= the reference app) ─────────────────────
    public_intel_http_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "Default total attempts per public-provider request in the shared "
            "resilient HTTP helper (read/connect timeouts, connection errors, "
            "HTTP 5xx/429, transient in-JSON ArcGIS errors)."
        ),
    )
    public_intel_http_timeout_seconds: int = Field(
        default=60,
        ge=5,
        le=300,
        description=(
            "Default per-attempt read timeout for public-provider requests. "
            "60s, not 35s: public GIS layers legitimately take 40-60s in "
            "their slow mode."
        ),
    )
    public_intel_http_backoff_base_seconds: float = Field(
        default=1.5,
        ge=0.0,
        le=30.0,
        description=(
            "Base for exponential backoff between attempts: sleep = "
            "base * 2**(attempt-1) plus up to 50% random jitter. 0 disables "
            "the pause (tests)."
        ),
    )
    txdot_arcgis_timeout_seconds: int = Field(
        default=90,
        ge=5,
        le=300,
        description=(
            "Per-attempt read timeout for TxDOT ArcGIS FeatureServer spatial "
            "queries — the AADT layer's server-side scan legitimately takes "
            "50-90s in its slow mode."
        ),
    )
    txdot_arcgis_attempts: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Total attempts per TxDOT ArcGIS spatial query.",
    )
    txdot_arcgis_backoff_base_seconds: float = Field(
        default=1.5,
        ge=0.0,
        le=30.0,
        description=(
            "Base for exponential backoff between TxDOT ArcGIS attempts. "
            "0 disables the pause (tests)."
        ),
    )
    txdot_arcgis_geometry_precision: int = Field(
        default=6,
        ge=0,
        le=10,
        description=(
            "geometryPrecision for TxDOT geometry queries — decimal places of "
            "returned coordinates (6 ≈ 0.11 m)."
        ),
    )
    txdot_arcgis_max_allowable_offset_deg: float = Field(
        default=0.00001,
        ge=0.0,
        le=0.01,
        description=(
            "maxAllowableOffset (degrees) for TxDOT geometry queries — "
            "server-side line generalization (~1.1 m at 0.00001). 0 disables."
        ),
    )

    # ── Site Check ported per-source tuning (= the reference app) ───────────────────
    environmental_screening_broad_radius_m: int = Field(
        default=1609,
        ge=100,
        le=8000,
        description=(
            "Search radius (metres) for the broader TCEQ contamination layers "
            "(Superfund, corrective-action, brownfield, dry-cleaner, landfill, "
            "groundwater MSD). Default ~1 mile."
        ),
    )
    environmental_registry_max_records: int = Field(
        default=25,
        ge=1,
        le=100,
        description=(
            "Max records fetched per TCEQ open-data regulatory-history dataset "
            "in the address-based environmental registry screen."
        ),
    )
    nwi_fallback_coverage_probe_radius_m: int = Field(
        default=5000,
        ge=500,
        le=50000,
        description=(
            "Radius (meters) of the wide returnCountOnly probe that proves a "
            "fallback NWI layer covers the subject area when the "
            "screening-radius query returns zero features."
        ),
    )
    ccn_water_query_urls: list[str] = Field(
        default=[
            ("https://services.twdb.texas.gov/arcgis/rest/services/PWS/"
             "Public_Utility_Commission_CCN_Water/MapServer/0/query"),
            ("https://services6.arcgis.com/N6Lzvtb46cpxThhu/arcgis/rest/"
             "services/Water_CCN_Service_Areas/FeatureServer/210/query"),
        ],
        description=(
            "Water CCN service-area query URLs, tried in order until one "
            "answers. TWDB state-agency layer first; PUCT's official viewer "
            "layer as mirror."
        ),
    )
    ccn_wastewater_query_urls: list[str] = Field(
        default=[
            ("https://services6.arcgis.com/N6Lzvtb46cpxThhu/arcgis/rest/"
             "services/Sewer_CCN_Service_Areas/FeatureServer/230/query"),
        ],
        description=(
            "Sewer CCN service-area query URLs, tried in order. PUCT's "
            "official layer (TWDB's sewer service was removed — 404, "
            "2026-07-22)."
        ),
    )
    ccn_pws_operator_query_url: str = Field(
        default=(
            "https://services.twdb.texas.gov/arcgis/rest/services/PWS/"
            "Public_Water_Service_Areas/FeatureServer/0/query"
        ),
        description="TCEQ operating-provider (PWS) service-area query URL.",
    )
    ccn_query_timeout_seconds: int = Field(
        default=25, ge=5, le=120,
        description=(
            "Read timeout (seconds) for every CCN / PWS / TCEQ district ArcGIS "
            "query in the utility screening."
        ),
    )
    ccn_point_buffer_m: int = Field(
        default=200, ge=0, le=2000,
        description=(
            "Buffer (meters) around the subject point for CCN membership "
            "queries — catches boundary parcels and recent transfers."
        ),
    )
    tceq_water_district_query_urls: list[str] = Field(
        default=[
            ("https://gisweb.tceq.texas.gov/arcgis/rest/services/iwud/"
             "WaterDistricts_PRD/MapServer/6/query"),   # MUD
            ("https://gisweb.tceq.texas.gov/arcgis/rest/services/iwud/"
             "WaterDistricts_PRD/MapServer/11/query"),  # SUD
            ("https://gisweb.tceq.texas.gov/arcgis/rest/services/iwud/"
             "WaterDistricts_PRD/MapServer/13/query"),  # WCID
            ("https://gisweb.tceq.texas.gov/arcgis/rest/services/iwud/"
             "WaterDistricts_PRD/MapServer/2/query"),   # FWSD
        ],
        description=(
            "TCEQ Water Districts layers checked when NO sewer CCN covers the "
            "parcel — MUD/SUD/WCID/FWSD commonly provide sewer inside their "
            "boundary. Each is a point-in-polygon query."
        ),
    )
    sewer_nearest_search_radii_m: list[int] = Field(
        default=[500, 1000, 2000, 5000],
        description=(
            "Escalating radii (meters) for the nearest-sewer-CCN search when "
            "no sewer CCN covers the parcel."
        ),
    )
    drainage_screen_cell_size_m: float = Field(
        default=10.0, ge=5.0, le=30.0,
        description="Requested DEM cell size (meters) on the EPSG:5070 analysis grid.",
    )
    drainage_screen_window_m: int = Field(
        default=1500, ge=500, le=4000,
        description="Initial analysis-window edge length (meters) centered on the anchor.",
    )
    drainage_screen_window_max_m: int = Field(
        default=4000, ge=1000, le=8000,
        description=(
            "Adaptive-expansion cap for the analysis window. If parcel-crossing "
            "flow still originates at the window edge at this size, run-on is "
            "reported as a window-bounded lower bound (partial)."
        ),
    )
    drainage_screen_max_pixels: int = Field(
        default=1_442_401, ge=10_000, le=4_000_000,
        description=(
            "Hard decoded-raster pixel cap (default 1201x1201); larger "
            "responses are rejected before allocation."
        ),
    )
    drainage_screen_max_download_bytes: int = Field(
        default=25_000_000, ge=1_000_000, le=100_000_000,
        description=(
            "Hard streamed byte cap for the GeoTIFF download; exceeding it "
            "aborts to partial."
        ),
    )
    drainage_screen_dem_host: str = Field(
        default="elevation.nationalmap.gov",
        description=(
            "Exact https host allowlist for the 3DEP exportImage href; "
            "redirects are disabled on the download."
        ),
    )
    drainage_screen_dem_nodata_sentinel: float = Field(
        default=-999999.0,
        description="NoData sentinel used when the GeoTIFF declares no nodata tag.",
    )
    drainage_screen_depression_min_depth_m: float = Field(
        default=0.15, ge=0.01, le=5.0,
        description="Minimum fill-to-spill depth (meters) for a reported topographic depression.",
    )
    drainage_screen_depression_min_area_m2: float = Field(
        default=100.0, ge=10.0, le=100_000.0,
        description="Minimum area (square meters) for a reported topographic depression.",
    )
    drainage_screen_detention_max_slope_pct: float = Field(
        default=2.0, ge=0.1, le=10.0,
        description="Maximum cell slope (percent) for detention-candidate cells.",
    )
    drainage_screen_detention_max_accum_cells: int = Field(
        default=50, ge=1, le=10_000,
        description="Maximum flow accumulation (cells) for detention-candidate cells.",
    )
    drainage_screen_detention_min_area_m2: float = Field(
        default=500.0, ge=50.0, le=500_000.0,
        description=(
            "Minimum contiguous area (square meters) for a detention candidate "
            "inside the parcel."
        ),
    )
    drainage_screen_flowpath_min_accum_cells: int = Field(
        default=300, ge=10, le=100_000,
        description=(
            "Flow-accumulation threshold (cells) that marks a screened flow "
            "path (300 cells at 10 m = 3 ha contributing area)."
        ),
    )
    drainage_screen_max_regions_per_kind: int = Field(
        default=25, ge=1, le=200,
        description=(
            "Map-output cap per feature kind (largest first). When hit, the "
            "advisory names the cap — never a silent truncation."
        ),
    )
    drainage_screen_nhd_record_cap: int = Field(
        default=200, ge=10, le=2000,
        description=(
            "resultRecordCount for the NHD flowline cross-check; "
            "exceededTransferLimit or failure demotes the cross-check to partial."
        ),
    )
    drainage_screen_nhd_agree_distance_m: float = Field(
        default=60.0, ge=10.0, le=500.0,
        description=(
            "An NHD flowline vertex within this distance of a computed flow "
            "path counts as agreement."
        ),
    )
    drainage_screen_conus_bounds: str = Field(
        default="24.0,49.5,-125.0,-66.5",
        description=(
            "CONUS gate as 'lat_min,lat_max,lng_min,lng_max'. Anchors outside "
            "this box get the honest 'not yet supported in this region' state."
        ),
    )
    access_facts_frontage_max_ft: float = Field(
        default=175.0, ge=10.0, le=2000.0,
        description=(
            "Roads whose centerline is within this distance (feet) of the "
            "PARCEL BOUNDARY are frontage candidates. Edge-based, not "
            "anchor-based."
        ),
    )
    access_facts_frontage_max_mi: float = Field(
        default=0.03, ge=0.001, le=0.5,
        description=(
            "Anchor-distance fallback (miles) for frontage candidates when no "
            "parcel polygon exists (~158 ft default)."
        ),
    )
    access_facts_connection_buffer_m: float = Field(
        default=60.0, ge=5.0, le=500.0,
        description=(
            "Community-mapped connections within this distance (meters) of the "
            "frontage centerline are stationed for mapped-connection gap "
            "measurements."
        ),
    )
    public_aerial_imagery_export_base_url: str = Field(
        default=(
            "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/export"
        ),
        description=(
            "ArcGIS ImageServer/MapServer exportImage endpoint for the aerial "
            "chip. Default is the USGS National Map imagery service (NAIP + "
            "USGS orthoimagery, public domain, no API key)."
        ),
    )
    public_aerial_imagery_image_size_px: int = Field(
        default=640,
        ge=128,
        le=2048,
        description="Square chip edge length in pixels requested from exportImage.",
    )
    public_aerial_imagery_default_radius_m: int = Field(
        default=250,
        ge=50,
        le=2000,
        description=(
            "Half-span (metres) of the view box when parcel acreage is "
            "unknown — 250 m → a ~500 m square context view."
        ),
    )
    public_aerial_imagery_min_radius_m: int = Field(
        default=120,
        ge=20,
        le=2000,
        description="Lower clamp on the view half-span (metres).",
    )
    public_aerial_imagery_max_radius_m: int = Field(
        default=800,
        ge=100,
        le=5000,
        description="Upper clamp on the view half-span (metres).",
    )
    public_aerial_imagery_timeout_s: int = Field(
        default=20,
        ge=1,
        le=120,
        description=(
            "Per-request HTTP timeout for the server-side liveness GET that "
            "validates the chip URL before handing it to the browser."
        ),
    )
    public_aerial_imagery_image_format: str = Field(
        default="jpg",
        description=(
            "exportImage output format (jpg keeps the chip small; png for "
            "lossless). Passed straight to the ImageServer."
        ),
    )

    # ── Historical aerials — "what this land was before" (spec §14) ────────
    # The TxGIO/TNRIS era ladder plus the OpenAI vision prior-use read. The
    # era chips are decade-stable public imagery, so they cache for years;
    # the AI read is per-run and metered through the gateway.
    historical_aerials_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the historical-aerials enrichment. Off means "
            "the orchestrator skips the era fetch and the vision read "
            "entirely — the run is unaffected either way."
        ),
    )
    historical_aerials_base_url: str = Field(
        default="https://imagery.geographic.texas.gov/server/rest/services",
        description=(
            "Root of the TxGIO (Texas Geographic Information Office) ArcGIS "
            "ImageServer catalogue that hosts the historical orthoimagery "
            "programs. Each era appends '<service>/ImageServer/exportImage'."
        ),
    )
    historical_aerials_eras: dict[str, str] = Field(
        default={
            "1981": "NHAP/NHAP81_CIR_75cm",
            "1996": "TOP/TOP96_BW_CIR_1m",
            "2004": "NAIP/NAIP04_CIR_1m",
            "2010": "NAIP/NAIP10_NCCIR_1m",
            "2016": "NAIP/NAIP16_NCCIR_1m",
            "2022": "NAIP/NAIP22_NCCIR_60cm",
        },
        description=(
            "The era ladder: photo year → TxGIO ImageServer service path. "
            "Adding or removing a year busts the cached timeline for every "
            "property (the era set is part of the cache scope key)."
        ),
    )
    historical_aerials_chip_size_px: int = Field(
        default=512,
        ge=128,
        le=2048,
        description=(
            "Square edge length in pixels of each era chip. Every era uses "
            "the same size so the filmstrip is comparable frame to frame."
        ),
    )
    historical_aerials_timeout_seconds: int = Field(
        default=25,
        ge=1,
        le=120,
        description=(
            "Per-request HTTP timeout for the era-chip liveness GET and for "
            "downloading the chip bytes that feed the vision read."
        ),
    )
    historical_aerials_cache_ttl_days: int = Field(
        default=3650,
        ge=1,
        description=(
            "Reuse window for a resolved era timeline. Historical photos do "
            "not change, so this is effectively permanent (10 years); the "
            "shared cache helper's freshness model requires an expiry. "
            "Deliberately NOT clamped to site_check_freshness_days — that "
            "rule governs facts that can go out of date, and a 1981 "
            "photograph cannot."
        ),
    )

    # ── Entitlement reader — AI-read the city's own planning pages ─────────
    # Cities like Hutto publish their live case list ONLY as human web pages:
    # there is no feed to query, so a web-search-capable model reads the pages
    # and every case it returns is re-fetched server-side and verified before
    # it may render. Metered through the gateway like every other AI call.
    entitlement_read_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the city-planning-page read. Off means the "
            "entitlement card shows the deterministic county plat records "
            "only — the run is unaffected either way."
        ),
    )
    entitlement_read_model: str = Field(
        default="gpt-5.6-terra",
        description=(
            "Model for the city case read. Must have a web-search tool and an "
            "openai_model_prices entry (validated at startup)."
        ),
    )
    entitlement_read_max_searches: int = Field(
        default=10,
        ge=1,
        le=30,
        description=(
            "Ceiling passed to the Responses API as max_tool_calls. The "
            "provider treats it as a budget, not a hard stop (a live probe "
            "returned 9 searches for a cap of 8), so it bounds cost roughly "
            "rather than exactly."
        ),
    )
    entitlement_read_radius_m: int = Field(
        default=3200,
        ge=100,
        le=16000,
        description=(
            "How far around the site the model is told to look for city "
            "cases. Matches the recorded-plat sweep so the two tables on the "
            "card describe the same neighbourhood."
        ),
    )
    entitlement_read_max_cases: int = Field(
        default=12,
        ge=1,
        le=40,
        description="Cap on validated cases rendered from one read.",
    )
    entitlement_read_validation_timeout_s: int = Field(
        default=20,
        ge=1,
        le=120,
        description=(
            "Per-URL timeout for the server-side fetch that VERIFIES a "
            "returned case actually appears on the page it cites. A URL that "
            "times out fails validation and its case is dropped."
        ),
    )
    entitlement_read_validation_max_attempts: int = Field(
        default=2,
        ge=1,
        le=3,
        description=(
            "Hard ceiling on server-side attempts to fetch one cited city "
            "page for entitlement verification. Validation uses the shared "
            "session's retry-free transport so this is the only retry layer."
        ),
    )
    entitlement_read_cache_ttl_days: int = Field(
        default=7,
        ge=1,
        description=(
            "Reuse window for a read. Deliberately SHORT: unlike a recorded "
            "plat or a 1981 photograph, an active case moves through P&Z and "
            "council on a two-to-four-week cadence, so a week-old copy is at "
            "most one meeting cycle behind while still sparing a repeat "
            "web-search spend for the same site inside one diligence week. "
            "'Get fresh data' bypasses it entirely."
        ),
    )

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "Settings":
        if self.site_check_expiring_notice_days >= self.site_check_freshness_days:
            raise ValueError(
                "site_check_expiring_notice_days must be smaller than "
                "site_check_freshness_days"
            )
        if self.job_stale_after_seconds <= self.job_heartbeat_interval_seconds:
            raise ValueError(
                "job_stale_after_seconds must exceed job_heartbeat_interval_seconds"
            )
        # Aerial chip reuse is capped by the freshness rule (spec §12).
        self.public_aerial_imagery_ttl_days = min(self.public_aerial_imagery_ttl_days, self.site_check_freshness_days)
        # Fail fast: every configured model name needs a pricing entry (spec §11.1).
        configured = {
            "site_check_narrative_model": self.site_check_narrative_model,
            "site_check_vision_model": self.site_check_vision_model,
            "site_check_document_model": self.site_check_document_model,
            "entitlement_read_model": self.entitlement_read_model,
        }
        missing = {
            field: model for field, model in configured.items()
            if model not in self.openai_model_prices
        }
        if missing:
            raise ValueError(
                "openai_model_prices is missing an entry for configured model(s): "
                + ", ".join(f"{field}={model!r}" for field, model in sorted(missing.items()))
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
