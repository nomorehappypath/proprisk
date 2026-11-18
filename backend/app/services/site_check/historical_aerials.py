"""Historical aerials — "what this land was before" (site_check.md §19).

Two halves, deliberately separated:

1. **Deterministic era ladder** — ported field-for-field from the pinned
   the reference app `public_intel/historical_aerials.py`. For a geocoded property it
   builds one TxGIO/TNRIS ImageServer chip URL per configured era (default
   1981 → 2022), validates each with one server-side liveness GET, and frames
   every era with the SAME envelope as the modern aerial chip (the sizing
   helpers are imported from `adapters/public_aerial_imagery.py` — single
   source of truth) so the filmstrip is comparable frame to frame. A dead era
   never raises: it comes back `status="unavailable"` with its error, and the
   advisory names the missing years WITHOUT implying that a missing photo
   means the land was unused (TOP/NHAP coverage genuinely varies by area).

2. **Prior-use read (AI)** — rebuilt on OpenAI vision through the brick-10
   gateway (`site_check_vision_model`), replacing the reference app's Anthropic call.
   Every resolved era chip goes to the model with its year label in ONE
   multimodal call; the model returns strict JSON: per-era observations,
   change events between eras, environmental-risk signals, and a plain
   "what this land appears to have been" summary. Signals are PROBABILISTIC
   observations from imagery, never asserted history — the honesty contract
   lives in the system prompt AND in the card wording.

Degradation ladder — identical in spirit to `narratives.py` §11.3, sharing
its exact plain-English messages: no key / budget cap / invalid key /
provider error / invalid JSON each return the era IMAGES and DATES with an
honest note. The run NEVER fails because of anything in this module.

Caching contract (§9.3): ONLY the deterministic chip timeline is written to
the global `site_check_source_cache`, keyed by anchor + parcel + era set +
chip size, with the long `historical_aerials_cache_ttl_days` TTL. The AI
reading is deliberately NOT cached there — that table is documented and
test-asserted as PUBLIC FACTS ONLY, no narratives, and an AI-written
prior-use read is a narrative. Documented divergence from the pinned module
(which cached chips+read together): each tenant's read is metered to that
tenant, and no tenant's model output is served to another.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import time
from typing import Any

import requests
from openai import OpenAI

from app.models import Tenant
from app.services import gateway
from app.services.site_check import source_cache
from app.services.site_check.adapters import public_aerial_imagery as _imagery
from app.services.site_check.adapters._base import http_session
from app.services.site_check.identity import PropertyIdentity
from app.services.site_check.narratives import (
    APP_SLUG,
    MSG_BUDGET_CAP,
    MSG_INVALID_KEY,
    MSG_NO_KEY,
    MSG_UNAVAILABLE,
)

log = logging.getLogger("propdesk.site_check.historical_aerials")

SOURCE_ID = "historical_aerials"

# Bumped when the SHAPE of the cached deterministic timeline changes, so old
# rows are ignored rather than misread (§9.3 producer_version).
PRODUCER_VERSION = 1

# Part of the verified endpoint shape, not tunable config: jpgpng lets the
# ImageServer answer jpg for opaque chips and png where the out-of-coverage
# collar needs transparency.
_IMAGE_FORMAT = "jpgpng"

_ATTRIBUTION = "Historical aerial photos: Texas Geographic Information Office (TxGIO)"

_NO_LOCATION_ADVISORY = (
    "We could not place this property on the map, so there are no older "
    "photos of it to show. Check the address and run the lookup again."
)

# The one sentence that must ride every rendering of this feature.
HONESTY_NOTE = (
    "These are observations from old photographs — a screening aid, not a "
    "verified history of the site. A Phase I environmental report is the "
    "binding record of what this land was used for."
)

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")
_MAX_FIELD_CHARS = 900


# ── Config ─────────────────────────────────────────────────────────────────


class _Config:
    """Resolved, env-overridable config. Read with getattr defaults so test
    SimpleNamespace stand-ins still work (same pattern as
    public_aerial_imagery._Config)."""

    def __init__(self, settings_obj: Any) -> None:
        def g(name: str, default: Any) -> Any:
            return getattr(settings_obj, name, default)

        self.base_url: str = str(
            g(
                "historical_aerials_base_url",
                "https://imagery.geographic.texas.gov/server/rest/services",
            )
        ).rstrip("/")
        eras = g("historical_aerials_eras", None) or {}
        self.eras: dict[str, str] = {str(k): str(v) for k, v in dict(eras).items()}
        self.chip_size_px: int = int(g("historical_aerials_chip_size_px", 512))
        self.timeout_s: int = int(g("historical_aerials_timeout_seconds", 25))
        self.ttl_days: int = int(g("historical_aerials_cache_ttl_days", 3650))
        self.vision_model: str = str(g("site_check_vision_model", "gpt-5.6-terra"))
        # Envelope sizing deliberately shares the modern aerial chip's radius
        # knobs so every era frames the property identically.
        self.default_radius_m: int = int(
            g("public_aerial_imagery_default_radius_m", 250)
        )
        self.min_radius_m: int = int(g("public_aerial_imagery_min_radius_m", 120))
        self.max_radius_m: int = int(g("public_aerial_imagery_max_radius_m", 800))


# ── Deterministic era ladder (ported) ──────────────────────────────────────


def _year_of(key: Any) -> int:
    """Numeric year for sorting and output. A malformed settings key sorts
    first as 0 rather than crashing the fetch (degrade, never raise)."""
    try:
        return int(str(key).strip())
    except (TypeError, ValueError):
        return 0


def _build_url(env: dict[str, Any], service: str, cfg: _Config) -> str:
    """Deterministic TxGIO exportImage URL — same inputs → same URL, exactly
    like public_aerial_imagery._build_url. The chips are static history, so
    determinism is what makes the per-parcel timeline cacheable."""
    from urllib.parse import urlencode

    bbox = f"{env['xmin']},{env['ymin']},{env['xmax']},{env['ymax']}"
    params = {
        "bbox": bbox,
        "bboxSR": "4326",
        "imageSR": "3857",
        "size": f"{cfg.chip_size_px},{cfg.chip_size_px}",
        "format": _IMAGE_FORMAT,
        "f": "image",
    }
    return f"{cfg.base_url}/{service}/ImageServer/exportImage?{urlencode(params)}"


def _validate_image_url(
    sess: requests.Session, url: str, cfg: _Config,
) -> tuple[bool, str | None]:
    """One server-side GET to confirm the endpoint serves a real image
    (ArcGIS can answer 200 with a JSON error envelope, which would otherwise
    reach the browser as a broken <img>). 3 attempts, 1s/2s/4s backoff on
    429/5xx. Streams headers only — the bytes are not stored here.

    Replicated from public_aerial_imagery._validate_image_url, kept local so
    the error text names TxGIO rather than USGS.
    """
    last_err: str | None = None
    for attempt in range(3):
        resp = None
        try:
            resp = sess.get(url, timeout=cfg.timeout_s, stream=True)
            status = resp.status_code
            if status in (429, 500, 502, 503, 504):
                last_err = f"TxGIO transient HTTP {status}"
                raise requests.HTTPError(last_err, response=resp)
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/"):
                return False, (
                    f"TxGIO returned non-image content-type {content_type!r}"
                )
            return True, None
        except (
            requests.ConnectionError, requests.Timeout, requests.HTTPError,
        ) as exc:
            last_err = str(exc)
            if attempt == 2:
                return False, last_err
            time.sleep(1 * (2 ** attempt))
        finally:
            if resp is not None:
                resp.close()
    return False, last_err or "TxGIO chip validation failed"


def _years_phrase(years: list[int]) -> str:
    """'1981, 1996 and 2004' — a plain year list for reader-facing copy."""
    strs = [str(y) for y in years]
    if not strs:
        return ""
    if len(strs) == 1:
        return strs[0]
    return ", ".join(strs[:-1]) + " and " + strs[-1]


def _advisory(eras: list[dict[str, Any]]) -> str:
    """Plain English about what the timeline holds. Never implies that a
    missing year means nothing was on the land then — coverage for the older
    photo programs varies by area."""
    available = [e["year"] for e in eras if e["status"] == "resolved"]
    missing = [e["year"] for e in eras if e["status"] == "unavailable"]
    if not eras:
        return (
            "No photo years are set up, so there is no timeline to show."
        )
    if not available:
        return (
            "None of the older photos could be loaded for this location "
            "right now. That does not mean the land was unused in those "
            "years — the photo archive may be temporarily unavailable. Try "
            "again later."
        )
    if missing:
        return (
            f"Older photos are available for {_years_phrase(available)}. No "
            f"photo could be loaded for {_years_phrase(missing)}, so the "
            "timeline has gaps — a missing year does not mean nothing was on "
            "the land then; coverage of the older photo programs varies by "
            "area."
        )
    return (
        f"Older photos are available for all {len(available)} years on the "
        f"timeline ({_years_phrase(available)})."
    )


def fetch_era_chips(
    identity: PropertyIdentity,
    settings_obj: Any,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch one validated aerial-chip URL per configured era.

    Returns a plain dict (this is a service module, not a registered
    adapter — no AdapterResult):

        {
          "eras": [{"year": int, "service": str, "image_url": str | None,
                    "status": "resolved" | "unavailable",
                    "error": str | None}, ...],   # sorted by year
          "anchor": "parcel_geometry" | "centroid" | None,
          "bbox": {"xmin","ymin","xmax","ymax","sr"} | None,
          "resolved_count": int,
          "advisory": str,
          "attribution": str,
        }

    Per-era failure degrades — one dead era never raises. Missing lat/lng
    returns an honest empty timeline.
    """
    cfg = _Config(settings_obj)

    if identity.lat is None or identity.lng is None:
        return {
            "eras": [],
            "anchor": None,
            "bbox": None,
            "resolved_count": 0,
            "advisory": _NO_LOCATION_ADVISORY,
            "attribution": _ATTRIBUTION,
        }

    sess = http_session(session)
    # Parcel-bbox/centroid sizing imported from the modern aerial adapter so
    # both features frame the property identically.
    env = _imagery._envelope(identity, cfg)

    eras: list[dict[str, Any]] = []
    for year_key, service in sorted(cfg.eras.items(), key=lambda kv: _year_of(kv[0])):
        url = _build_url(env, service, cfg)
        ok, err = _validate_image_url(sess, url, cfg)
        eras.append({
            "year": _year_of(year_key),
            "service": service,
            "image_url": url if ok else None,
            "status": "resolved" if ok else "unavailable",
            "error": None if ok else (err or "chip validation failed"),
        })

    return {
        "eras": eras,
        "anchor": env["anchor"],
        "bbox": {
            "xmin": env["xmin"], "ymin": env["ymin"],
            "xmax": env["xmax"], "ymax": env["ymax"], "sr": 4326,
        },
        "resolved_count": sum(1 for e in eras if e["status"] == "resolved"),
        "advisory": _advisory(eras),
        "attribution": _ATTRIBUTION,
    }


# ── Prior-use read (OpenAI vision through the gateway) ─────────────────────

# Risk buckets the model may tag a signal with. Closed set so the card can
# word each one itself instead of rendering free model text as a label.
RISK_CATEGORIES = (
    "fuel_or_chemical",
    "industrial_or_yard",
    "pit_or_quarry",
    "landfill_or_dumping",
    "agricultural",
    "other",
)

_SYSTEM_PROMPT = (
    "You are reading a time series of aerial photographs of one Texas "
    "property for a small real-estate investor doing early due diligence. "
    "You observe only what is visible in the images. You NEVER state a past "
    "use as fact — every conclusion is an observation ('appears', "
    "'suggests', 'consistent with'). Small rural operations matter: storage "
    "yards, fuel or chemical drums, junkyards, gravel or borrow pits, burn "
    "or dump areas, and old filling stations are exactly what a Phase I "
    "environmental review looks for. Absence of visible structures does NOT "
    "mean the land was unused, and you must never say the site is clean. "
    "Write plain English a non-engineer understands — no photo-interpretation "
    "jargon. Return JSON matching the requested schema only."
)


def _user_instruction(address: str, years: list[int]) -> str:
    return (
        f"These are aerial photographs of the property at {address}, oldest "
        f"first. Each photo is labelled with the year it was taken "
        f"({_years_phrase(years)}). Compare them and describe what this land "
        "appears to have been.\n"
        "For each year, say what you can see and what it looks like the land "
        "was being used for. Between consecutive photos, note anything that "
        "changed. Flag anything that would matter to an environmental "
        "review — old fuel or filling stations, industrial or storage yards, "
        "gravel or borrow pits, dumping or landfill areas, heavy "
        "agricultural operations — and only when it is actually visible in "
        "the photos. Outbuildings, sheds, and cleared or hard-surfaced yards "
        "that persist across several years DO count, even when you cannot "
        "tell what they were for: unidentified long-standing structures on "
        "rural land are precisely what a historical review is asked to run "
        "down, so list them and say plainly that their use is not visible. "
        "If the land is genuinely open farmland or pasture the whole way "
        "through, with no structures at all, say exactly that and return an "
        "empty prior_use_signals list.\n"
        "The summary is two or three plain sentences answering one question: "
        "what was this land before?"
    )


def _response_schema() -> dict[str, Any]:
    """Strict JSON schema (§11.2 pattern): the COMPLETE allowlist. Every
    property is required and additionalProperties is false, as OpenAI strict
    mode demands; optional values are expressed as nullable types."""
    return {
        "name": "historical_aerials_prior_use",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "era_observations", "change_events", "prior_use_signals",
                "environmental_review_relevant", "summary",
            ],
            "properties": {
                "era_observations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "year", "what_is_visible", "apparent_use",
                            "confidence",
                        ],
                        "properties": {
                            "year": {"type": "integer"},
                            "what_is_visible": {"type": "string"},
                            "apparent_use": {"type": "string"},
                            "confidence": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                        },
                    },
                },
                "change_events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["period", "what_changed"],
                        "properties": {
                            "period": {"type": "string"},
                            "what_changed": {"type": "string"},
                        },
                    },
                },
                "prior_use_signals": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "signal", "risk_category", "first_seen",
                            "last_seen", "why_it_matters",
                        ],
                        "properties": {
                            "signal": {"type": "string"},
                            "risk_category": {
                                "type": "string",
                                "enum": list(RISK_CATEGORIES),
                            },
                            "first_seen": {"type": ["integer", "null"]},
                            "last_seen": {"type": ["integer", "null"]},
                            "why_it_matters": {"type": "string"},
                        },
                    },
                },
                "environmental_review_relevant": {"type": "boolean"},
                "summary": {"type": "string"},
            },
        },
    }


def _clean_text(value: Any, cap: int = _MAX_FIELD_CHARS) -> str:
    """Length-cap + neutralize markup/injection (narratives._clean_text).
    Raw model output never passes through unsanitized."""
    if value is None:
        return ""
    text = _TAG_RE.sub(" ", str(value))
    return _WS_RE.sub(" ", text).strip()[:cap]


def _clean_year(value: Any) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1800 <= year <= 2200 else None


def _sanitize_reading(parsed: Any, years: list[int]) -> dict[str, Any]:
    """Keep ONLY the allowlisted shape — everything else is stripped, exactly
    like narratives._sanitize."""
    parsed = parsed if isinstance(parsed, dict) else {}
    observations = [
        {
            "year": _clean_year(row.get("year")),
            "what_is_visible": _clean_text(row.get("what_is_visible")),
            "apparent_use": _clean_text(row.get("apparent_use")),
            "confidence": (
                str(row.get("confidence")).strip().lower()
                if str(row.get("confidence")).strip().lower()
                in ("low", "medium", "high") else "low"
            ),
        }
        for row in parsed.get("era_observations") or []
        if isinstance(row, dict)
    ]
    changes = [
        {
            "period": _clean_text(row.get("period")),
            "what_changed": _clean_text(row.get("what_changed")),
        }
        for row in parsed.get("change_events") or []
        if isinstance(row, dict)
    ]
    signals = [
        {
            "signal": _clean_text(row.get("signal")),
            "risk_category": (
                str(row.get("risk_category")).strip().lower()
                if str(row.get("risk_category")).strip().lower()
                in RISK_CATEGORIES else "other"
            ),
            "first_seen": _clean_year(row.get("first_seen")),
            "last_seen": _clean_year(row.get("last_seen")),
            "why_it_matters": _clean_text(row.get("why_it_matters")),
        }
        for row in parsed.get("prior_use_signals") or []
        if isinstance(row, dict)
    ]
    return {
        "era_observations": [row for row in observations if row["what_is_visible"]],
        "change_events": [row for row in changes if row["what_changed"]],
        "prior_use_signals": [row for row in signals if row["signal"]],
        "environmental_review_relevant": bool(
            parsed.get("environmental_review_relevant")
        ),
        "summary": _clean_text(parsed.get("summary")),
        "years_compared": years,
    }


def _download_chip(
    sess: requests.Session, url: str, timeout_s: int,
) -> bytes | None:
    """The chip bytes for the vision call. None on any failure — one
    undownloadable era just drops out of the comparison."""
    try:
        resp = sess.get(url, timeout=timeout_s)
        resp.raise_for_status()
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if not content_type.startswith("image/"):
            return None
        return resp.content
    except requests.RequestException:
        return None


def _estimated_image_tokens(size_px: int) -> int:
    """OpenAI's tiled vision accounting: the image is scaled to fit 2048px,
    then billed 170 tokens per 512px tile plus an 85-token base. Estimating
    from the tiling (not from the base64 byte count, which overstates by
    ~50x) keeps the gateway reservation proportionate to the real spend."""
    edge = max(1, min(int(size_px), 2048))
    tiles = math.ceil(edge / 512) ** 2
    return 85 + 170 * tiles


def _make_client(api_key: str) -> Any:
    """Isolated (house pattern, extraction.py / narratives.py) so tests can
    monkeypatch."""
    return OpenAI(api_key=api_key)


def _call_model(
    client: Any, *, model: str, content: list[dict[str, Any]],
    max_output_tokens: int,
) -> tuple[str, int, int]:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        max_completion_tokens=max_output_tokens,
        response_format={"type": "json_schema", "json_schema": _response_schema()},
    )
    return (
        resp.choices[0].message.content or "",
        resp.usage.prompt_tokens,
        resp.usage.completion_tokens,
    )


def _release_quietly(session: Any, usage: Any) -> None:
    """Terminal `released` on the reservation; never raises. No branch may
    leak a pending row, and no branch may fail the run."""
    try:
        gateway.release(session, usage)
    except Exception:
        log.exception(
            "historical aerials release failed usage_id=%s",
            getattr(usage, "id", None),
        )
        try:
            session.rollback()
        except Exception:
            log.exception("historical aerials rollback after failed release")


def _read_meta(
    *, status: str, model: str,
    error_kind: str | None = None, message: str | None = None,
    ai_call_id: Any = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"status": status, "model": model}
    if error_kind is not None:
        meta["error_kind"] = error_kind
    if message is not None:
        meta["message"] = message
    if ai_call_id is not None:
        meta["ai_call_id"] = ai_call_id
    return meta


def read_prior_use(
    session: Any,
    run: Any,
    chips: dict[str, Any],
    settings_obj: Any,
    *,
    sess: requests.Session | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(reading, meta) for ONE metered vision call over the resolved era
    chips. Every failure branch returns (None, meta) with a plain-English
    message — the era images still render, and the run never fails.

    The degradation ladder mirrors narratives.py §11.3 exactly, down to the
    shared message constants: no key → budget cap → invalid key → provider
    error → invalid output.
    """
    cfg = _Config(settings_obj)
    model = cfg.vision_model
    resolved = [
        era for era in chips.get("eras") or []
        if era.get("status") == "resolved" and era.get("image_url")
    ]
    if len(resolved) < 2:
        # One frame cannot show change over time — an honest skip, not a
        # failure. (A zero-era timeline lands here too.)
        return None, _read_meta(
            status="skipped_insufficient_eras", model=model,
            message=(
                "Not enough older photos were available to read a timeline. "
                "That does not mean the land has no history — try the lookup "
                "again later."
            ),
        )

    tenant = session.get(Tenant, run.tenant_id)
    try:
        api_key, _source = gateway.resolve_key(tenant)
    except gateway.NoTenantKey:
        return None, _read_meta(
            status="fallback_no_key", model=model,
            error_kind="no_key", message=MSG_NO_KEY,
        )

    http = http_session(sess)
    content: list[dict[str, Any]] = []
    years: list[int] = []
    for era in resolved:
        raw = _download_chip(http, era["image_url"], cfg.timeout_s)
        if raw is None:
            continue
        content.append({
            "type": "text", "text": f"Aerial photograph, year {era['year']}:",
        })
        content.append({
            "type": "image_url",
            "image_url": {
                "url": (
                    "data:image/jpeg;base64,"
                    + base64.b64encode(raw).decode("ascii")
                ),
                "detail": "high",
            },
        })
        years.append(int(era["year"]))

    if len(years) < 2:
        return None, _read_meta(
            status="skipped_insufficient_eras", model=model,
            message=(
                "The older photos could not be downloaded for reading this "
                "time. The photos above are still the archive's; try the "
                "lookup again later for the written read."
            ),
        )

    instruction = _user_instruction(getattr(run, "address", "") or "", years)
    content.append({"type": "text", "text": instruction})

    price = settings_obj.openai_model_prices[model]
    max_output_tokens = int(price.max_output_tokens)
    # Conservative estimate from the ACTUAL request: prompt bytes/3 plus the
    # tiled image accounting. The gateway pads by 1.25 on top.
    text_bytes = len(_SYSTEM_PROMPT.encode()) + len(instruction.encode())
    estimated_input_tokens = (
        text_bytes // 3 + 1 + len(years) * _estimated_image_tokens(cfg.chip_size_px)
    )

    client = _make_client(api_key)
    try:
        usage = gateway.reserve(
            session,
            tenant_id=run.tenant_id,
            user_id=getattr(run, "requested_by", None),
            app_slug=APP_SLUG,
            model=model,
            estimated_input_tokens=estimated_input_tokens,
        )
    except gateway.BudgetExceeded:
        return None, _read_meta(
            status="fallback_budget_cap", model=model,
            error_kind="budget_cap", message=MSG_BUDGET_CAP,
        )
    except gateway.NoTenantKey:
        return None, _read_meta(
            status="fallback_no_key", model=model,
            error_kind="no_key", message=MSG_NO_KEY,
        )
    usage_id = usage.id  # capture before any rollback can expire it

    try:
        text, prompt_tokens, completion_tokens = _call_model(
            client, model=model, content=content,
            max_output_tokens=max_output_tokens,
        )
    except Exception as exc:
        # The call itself failed — nothing billable happened: release the
        # reservation (terminal, bills nothing) and degrade.
        _release_quietly(session, usage)
        status_code = getattr(exc, "status_code", None)
        log.warning(
            "historical aerials vision call failed run_id=%s: %s",
            getattr(run, "id", None), str(exc)[:300],
        )
        if status_code in (401, 403):
            return None, _read_meta(
                status="fallback_invalid_key", model=model,
                error_kind="auth", message=MSG_INVALID_KEY,
            )
        return None, _read_meta(
            status="fallback_provider_error", model=model,
            error_kind="provider", message=MSG_UNAVAILABLE,
        )

    # The model answered — the tokens are real, so this attempt is metered no
    # matter how the payload validates (§11.2).
    try:
        gateway.settle(session, usage, prompt_tokens, completion_tokens)
    except Exception:
        log.exception(
            "historical aerials settlement failed run_id=%s usage_id=%s",
            getattr(run, "id", None), usage_id,
        )
        try:
            gateway.mark_settle_failed(
                session, usage, prompt_tokens, completion_tokens,
            )
        except Exception:
            log.exception(
                "historical aerials mark_settle_failed failed usage_id=%s",
                usage_id,
            )

    try:
        parsed = json.loads(text) if text.strip() else None
        if not isinstance(parsed, dict):
            raise TypeError("model response was not a JSON object")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        log.warning(
            "historical aerials schema-invalid run_id=%s: %s",
            getattr(run, "id", None), str(exc)[:300],
        )
        return None, _read_meta(
            status="fallback_invalid_output", model=model,
            error_kind="invalid_output", message=MSG_UNAVAILABLE,
            ai_call_id=usage_id,
        )

    reading = _sanitize_reading(parsed, years)
    if not reading["summary"] and not reading["era_observations"]:
        # Schema-valid but empty of substance — treat as invalid output
        # rather than rendering a blank read.
        return None, _read_meta(
            status="fallback_invalid_output", model=model,
            error_kind="invalid_output", message=MSG_UNAVAILABLE,
            ai_call_id=usage_id,
        )
    return reading, _read_meta(
        status="ok", model=model, ai_call_id=usage_id,
    )


# ── Derived builder (the §3.2 build_historical_aerials call site) ──────────


def _scope_key(run: Any, cfg: _Config) -> str:
    """Cache scope: the anchor, the parcel, the era ladder and the chip size.
    Changing any of them produces a different timeline, so each gets its own
    row. The read model is deliberately NOT part of the key — only the
    deterministic chips are cached."""
    return (
        f"state={(getattr(run, 'state', None) or 'TX').upper()}"
        f"|{round(float(run.lat), 5)},{round(float(run.lng), 5)}"
        f"|parcel={getattr(run, 'parcel_id', None) or '-'}"
        f"|eras={','.join(sorted(cfg.eras))}"
        f"|px={cfg.chip_size_px}"
    )


def _identity_for(run: Any) -> PropertyIdentity:
    return PropertyIdentity(
        address=getattr(run, "address", "") or "",
        lat=getattr(run, "lat", None),
        lng=getattr(run, "lng", None),
        county=getattr(run, "county", None) or None,
        state=getattr(run, "state", None) or "TX",
        parcel_id=getattr(run, "parcel_id", None) or None,
    )


def _cached_chips(
    session: Any, run: Any, cfg: _Config, settings_obj: Any,
    *, sess: requests.Session | None = None,
) -> tuple[dict[str, Any], str]:
    """(timeline, cache_state) via the §9.3 read-through. Only a timeline
    with at least one resolved era is cacheable — a fully failed pass must
    retry on the next refresh rather than freeze for a decade."""
    identity = _identity_for(run)
    if run.lat is None or run.lng is None:
        return fetch_era_chips(identity, settings_obj, session=sess), "skipped"

    def _fetch() -> dict[str, Any]:
        timeline = fetch_era_chips(identity, settings_obj, session=sess)
        if timeline.get("resolved_count", 0) < 1:
            # Not cacheable: an empty facts payload is refused by the cache
            # helper anyway; returning it explicitly documents the intent.
            return {"normalized_facts": {}}
        return {"normalized_facts": {"timeline": timeline}}

    try:
        outcome = source_cache.get_or_fetch(
            session,
            source_id=SOURCE_ID,
            scope_key=_scope_key(run, cfg),
            producer_version=PRODUCER_VERSION,
            fetch=_fetch,
            ttl_days=cfg.ttl_days,
            force_refresh=str(getattr(run, "mode", "") or "") == "fresh",
        )
    except Exception:
        # The cache layer must never break the refresh — fall back to a
        # direct live fetch.
        log.warning(
            "historical aerials cache read failed run_id=%s — fetching live",
            getattr(run, "id", None), exc_info=True,
        )
        return fetch_era_chips(identity, settings_obj, session=sess), "error"

    facts = (outcome.get("payload") or {}).get("normalized_facts") or {}
    timeline = facts.get("timeline")
    if not isinstance(timeline, dict):
        # Cache stored an empty/unusable payload — the fetcher already ran
        # live inside get_or_fetch, so just report the honest empty timeline.
        return fetch_era_chips(identity, settings_obj, session=sess), "miss"
    if outcome.get("stale"):
        return timeline, "stale"
    return timeline, "hit" if outcome.get("from_cache") else "miss"


def build_historical_aerials(
    session: Any, run: Any, settings_obj: Any,
    *, sess: requests.Session | None = None,
) -> dict[str, Any]:
    """Derived builder (mirrors build_narratives). ALWAYS returns a result
    dict; failures are non-fatal — the report must complete without this
    card.

    Returns `{"historical_aerials": payload|None,
              "historical_aerials_meta": {...}}` for `run.derived`.
    """
    model = str(getattr(settings_obj, "site_check_vision_model", "") or "")
    if not getattr(settings_obj, "historical_aerials_enabled", True):
        return {
            "historical_aerials": None,
            "historical_aerials_meta": {"status": "disabled", "model": model},
        }
    try:
        return _build(session, run, settings_obj, sess=sess)
    except Exception:
        # Absolute boundary — an unexpected bug degrades, never fails the run.
        log.exception(
            "historical aerials unexpected failure run_id=%s",
            getattr(run, "id", None),
        )
        return {
            "historical_aerials": None,
            "historical_aerials_meta": _read_meta(
                status="fallback_provider_error", model=model,
                error_kind="provider",
                message=(
                    "The older photos of this land could not be loaded this "
                    "time. Try the lookup again later."
                ),
            ),
        }


def _build(
    session: Any, run: Any, settings_obj: Any,
    *, sess: requests.Session | None = None,
) -> dict[str, Any]:
    cfg = _Config(settings_obj)
    if not cfg.eras:
        return {
            "historical_aerials": None,
            "historical_aerials_meta": {
                "status": "disabled", "model": cfg.vision_model,
            },
        }

    try:
        timeline, cache_state = _cached_chips(
            session, run, cfg, settings_obj, sess=sess,
        )
    except Exception as exc:
        log.warning(
            "historical aerials chip fetch failed run_id=%s: %s",
            getattr(run, "id", None), exc,
        )
        return {
            "historical_aerials": None,
            "historical_aerials_meta": _read_meta(
                status="chips_failed", model=cfg.vision_model,
                error_kind="provider",
                message=(
                    "The archive of older photos could not be reached this "
                    "time. That says nothing about this land's history — try "
                    "the lookup again later."
                ),
            ),
        }

    payload: dict[str, Any] = {
        "eras": timeline.get("eras") or [],
        "anchor": timeline.get("anchor"),
        "bbox": timeline.get("bbox"),
        "resolved_count": int(timeline.get("resolved_count") or 0),
        "advisory": timeline.get("advisory"),
        "attribution": timeline.get("attribution") or _ATTRIBUTION,
        "reading": None,
    }
    reading, meta = read_prior_use(session, run, timeline, settings_obj, sess=sess)
    payload["reading"] = reading
    meta = {**meta, "cache": cache_state}
    return {"historical_aerials": payload, "historical_aerials_meta": meta}


__all__ = [
    "HONESTY_NOTE",
    "PRODUCER_VERSION",
    "RISK_CATEGORIES",
    "SOURCE_ID",
    "build_historical_aerials",
    "fetch_era_chips",
    "read_prior_use",
]
