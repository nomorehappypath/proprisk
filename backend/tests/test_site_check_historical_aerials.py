"""Historical-aerials contract tests (site_check.md §19).

Three layers, all on the house Postgres conftest:
  1. the deterministic era ladder — parsing, ordering, URL construction,
     per-era unavailability, the honest no-location timeline;
  2. the §9.3 read-through cache — a second build makes NO provider call,
     and a fully failed pass is never frozen into the cache;
  3. the OpenAI vision read — every branch of the §11.3 degradation ladder
     (no key / budget cap / rejected key / provider error / invalid output),
     each of which must still hand back the era images, plus the card
     content the report renders from all of it.
"""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest
import requests
from sqlalchemy import select

from app.config.settings import settings
from app.core import secret_store
from app.models import AiUsage, SiteCheckSourceCache
from app.services import gateway
from app.services.site_check import cards
from app.services.site_check import historical_aerials as ha
from app.services.site_check.identity import PropertyIdentity

MODEL = settings.site_check_vision_model


@pytest.fixture(autouse=True)
def _tenant_key():
    # The gateway resolves the OpenAI key from the one shared secret store
    # (no per-tenant key any more) — seed a dummy value so these tests
    # exercise the "has a key" path by default.
    secret_store.set_secret("openai", "sk-test-tenant-key")
    yield
CHIP = b"\xff\xd8\xff" + b"0" * 4096  # a plausible little JPEG body

ERAS = {
    "1996": "TOP/TOP96_BW_CIR_1m",
    "1981": "NHAP/NHAP81_CIR_75cm",   # deliberately out of order in config
    "2022": "NAIP/NAIP22_NCCIR_60cm",
}


def make_settings(**overrides):
    """A settings stand-in — the module reads everything through getattr
    defaults, so a SimpleNamespace is a faithful substitute."""
    base = {
        "historical_aerials_enabled": True,
        "historical_aerials_base_url": (
            "https://imagery.geographic.texas.gov/server/rest/services"
        ),
        "historical_aerials_eras": dict(ERAS),
        "historical_aerials_chip_size_px": 512,
        "historical_aerials_timeout_seconds": 5,
        "historical_aerials_cache_ttl_days": 3650,
        "site_check_vision_model": MODEL,
        "openai_model_prices": settings.openai_model_prices,
        "public_aerial_imagery_default_radius_m": 250,
        "public_aerial_imagery_min_radius_m": 120,
        "public_aerial_imagery_max_radius_m": 800,
        "site_check_freshness_days": 180,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


IDENTITY = PropertyIdentity(
    address="County Road 130 & Chandler Road, Hutto TX 78634",
    lat=30.590318, lng=-97.552200, county="williamson", parcel_id="R482369",
)


# ── Fake HTTP ──────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status=200, content_type="image/jpeg", body=CHIP):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.content = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def close(self):
        pass


class FakeSession:
    """Records every URL and answers per-service. `dead` names services that
    fail every attempt (the unavailable-era path)."""

    def __init__(self, dead=(), content_type="image/jpeg"):
        self.dead = tuple(dead)
        self.content_type = content_type
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if any(service in url for service in self.dead):
            return FakeResponse(status=503)
        return FakeResponse(content_type=self.content_type)


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """The ladder's 1s/2s/4s retry backoff must not slow the suite."""
    monkeypatch.setattr(ha.time, "sleep", lambda _seconds: None)


# ── 1. Deterministic era ladder ────────────────────────────────────────────


def test_era_ladder_is_year_sorted_and_urls_are_deterministic():
    sess = FakeSession()
    out = ha.fetch_era_chips(IDENTITY, make_settings(), session=sess)

    assert [era["year"] for era in out["eras"]] == [1981, 1996, 2022]
    assert out["resolved_count"] == 3
    assert all(era["status"] == "resolved" for era in out["eras"])
    assert all(era["error"] is None for era in out["eras"])

    for era in out["eras"]:
        assert era["image_url"].startswith(
            "https://imagery.geographic.texas.gov/server/rest/services/"
        )
        assert f"/{era['service']}/ImageServer/exportImage?" in era["image_url"]
        assert "size=512%2C512" in era["image_url"]
        assert "bboxSR=4326" in era["image_url"]
        assert "f=image" in era["image_url"]

    # Same inputs → byte-identical URLs (what makes the timeline cacheable).
    again = ha.fetch_era_chips(IDENTITY, make_settings(), session=FakeSession())
    assert [e["image_url"] for e in again["eras"]] == [
        e["image_url"] for e in out["eras"]
    ]


def test_era_ladder_frames_every_year_with_the_same_bbox():
    """The filmstrip is only readable if each frame shows the same ground."""
    out = ha.fetch_era_chips(IDENTITY, make_settings(), session=FakeSession())
    boxes = {
        url.split("bbox=")[1].split("&")[0]
        for url in [era["image_url"] for era in out["eras"]]
    }
    assert len(boxes) == 1
    assert out["anchor"] == "centroid"
    assert out["bbox"]["sr"] == 4326


def test_one_dead_era_degrades_and_never_raises():
    sess = FakeSession(dead=("TOP96",))
    out = ha.fetch_era_chips(IDENTITY, make_settings(), session=sess)

    by_year = {era["year"]: era for era in out["eras"]}
    assert by_year[1996]["status"] == "unavailable"
    assert by_year[1996]["image_url"] is None
    assert "503" in by_year[1996]["error"]
    assert by_year[1981]["status"] == "resolved"
    assert out["resolved_count"] == 2
    # Three attempts on the dead service, one on each healthy one.
    assert len(sess.urls) == 5

    advisory = out["advisory"]
    assert "1981 and 2022" in advisory
    assert "1996" in advisory
    # The honesty rule: a gap must never imply the land was empty that year.
    assert "does not mean nothing was on the land" in advisory


def test_non_image_content_type_is_treated_as_unavailable():
    """ArcGIS answers 200 with a JSON error envelope — that is not a photo."""
    out = ha.fetch_era_chips(
        IDENTITY, make_settings(), session=FakeSession(content_type="text/html"),
    )
    assert out["resolved_count"] == 0
    assert all(era["status"] == "unavailable" for era in out["eras"])
    assert "None of the older photos could be loaded" in out["advisory"]


def test_no_coordinates_returns_an_honest_empty_timeline():
    out = ha.fetch_era_chips(
        PropertyIdentity(address="Somewhere"), make_settings(),
        session=FakeSession(),
    )
    assert out["eras"] == []
    assert out["anchor"] is None and out["bbox"] is None
    assert out["resolved_count"] == 0
    assert "could not place this property" in out["advisory"]


def test_malformed_era_year_sorts_first_instead_of_crashing():
    out = ha.fetch_era_chips(
        IDENTITY,
        make_settings(historical_aerials_eras={
            "nineteen-eighty-one": "NHAP/NHAP81_CIR_75cm",
            "2022": "NAIP/NAIP22_NCCIR_60cm",
        }),
        session=FakeSession(),
    )
    assert [era["year"] for era in out["eras"]] == [0, 2022]


# ── Shared fixtures for the DB-backed layers ───────────────────────────────


@pytest.fixture
def tenant_user(db, make_tenant, make_user):
    tenant = make_tenant("Aerials Co", budget_cents=100_000)
    user = make_user(tenant, "aerials@example.com")
    return tenant, user


def make_run(tenant, user, **overrides):
    fields = {
        "id": "run-ha-1", "tenant_id": tenant.id, "requested_by": user.id,
        "address": IDENTITY.address, "county": "williamson",
        "parcel_id": "R482369", "lat": IDENTITY.lat, "lng": IDENTITY.lng,
        "mode": "check", "status": "completed", "finished_at": None,
        "derived": {},
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def valid_reading():
    return json.dumps({
        "era_observations": [
            {"year": 1981, "what_is_visible": "Open cultivated fields with a "
                                              "farm track along the north edge.",
             "apparent_use": "Cropland", "confidence": "medium"},
            {"year": 2022, "what_is_visible": "A fenced yard with several "
                                              "storage buildings and drums.",
             "apparent_use": "Small storage yard", "confidence": "high"},
        ],
        "change_events": [
            {"period": "1981-2022",
             "what_changed": "Cropland gave way to a fenced yard."},
        ],
        "prior_use_signals": [
            {"signal": "A fenced yard with drums and storage buildings",
             "risk_category": "fuel_or_chemical",
             "first_seen": 1996, "last_seen": 2022,
             "why_it_matters": "Drum storage is a common source of soil "
                               "contamination."},
        ],
        "environmental_review_relevant": True,
        "summary": "This land was open cropland into the 1990s, then a fenced "
                   "storage yard appears. The yard is still there in 2022.",
    })


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        content, prompt_tokens, completion_tokens = outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens),
        )


def patch_client(monkeypatch, outcomes):
    fake = FakeCompletions(outcomes)
    monkeypatch.setattr(
        ha, "_make_client",
        lambda api_key: SimpleNamespace(
            chat=SimpleNamespace(completions=fake)),
    )
    return fake


def usage_rows(db):
    db.expire_all()
    return db.execute(select(AiUsage).order_by(AiUsage.id)).scalars().all()


def assert_no_pending_leak(db):
    """§11.3 invariant: every branch settles or releases its reservation."""
    assert all(row.status != "pending" for row in usage_rows(db))


def http_error(status, kind):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return kind("simulated", response=httpx.Response(status, request=request),
                body=None)


def build(db, run, monkeypatch, *, sess=None, settings_obj=None):
    return ha.build_historical_aerials(
        db, run, settings_obj or make_settings(),
        sess=sess if sess is not None else FakeSession(),
    )


# ── 2. Cache reuse (§9.3 read-through) ─────────────────────────────────────


def test_resolved_timeline_is_cached_and_reused_without_provider_calls(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user
    run = make_run(tenant, user)
    patch_client(monkeypatch, [(valid_reading(), 6000, 900)] * 2)

    first_sess = FakeSession()
    first = build(db, run, monkeypatch, sess=first_sess)
    db.commit()
    assert first["historical_aerials_meta"]["cache"] == "miss"
    assert first["historical_aerials"]["resolved_count"] == 3
    # 3 liveness GETs + 3 chip downloads for the vision read.
    assert len(first_sess.urls) == 6

    row = db.execute(
        select(SiteCheckSourceCache).where(
            SiteCheckSourceCache.source_id == "historical_aerials")
    ).scalars().one()
    assert row.payload["normalized_facts"]["timeline"]["resolved_count"] == 3
    # A 10-year reuse window, deliberately longer than the 180-day rule: a
    # 1981 photograph cannot go out of date.
    assert (row.expires_at - row.fetched_at).days > 3000

    second_sess = FakeSession()
    second = build(db, make_run(tenant, user, id="run-ha-2"), monkeypatch,
                   sess=second_sess)
    assert second["historical_aerials_meta"]["cache"] == "hit"
    assert second["historical_aerials"]["eras"] == first["historical_aerials"]["eras"]
    # Only the 3 chip downloads for the fresh AI read — zero liveness GETs.
    assert len(second_sess.urls) == 3


def test_fresh_mode_bypasses_the_cache(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    patch_client(monkeypatch, [(valid_reading(), 6000, 900)] * 2)
    build(db, make_run(tenant, user), monkeypatch)
    db.commit()

    sess = FakeSession()
    out = build(db, make_run(tenant, user, id="run-ha-3", mode="fresh"),
                monkeypatch, sess=sess)
    assert out["historical_aerials_meta"]["cache"] == "miss"
    assert len(sess.urls) == 6  # re-validated live


def test_a_fully_failed_timeline_is_never_cached(db, tenant_user, monkeypatch):
    """Freezing a dead pass for ten years would hide the land's history for
    a decade — a failed fetch must retry on the next lookup."""
    tenant, user = tenant_user
    out = build(db, make_run(tenant, user), monkeypatch,
                sess=FakeSession(dead=("NHAP", "TOP96", "NAIP22")))
    db.commit()
    assert out["historical_aerials"]["resolved_count"] == 0
    assert db.execute(
        select(SiteCheckSourceCache).where(
            SiteCheckSourceCache.source_id == "historical_aerials")
    ).scalars().first() is None


def test_cache_layer_failure_falls_back_to_a_live_fetch(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user
    patch_client(monkeypatch, [(valid_reading(), 6000, 900)])

    def _boom(*args, **kwargs):
        raise RuntimeError("cache table exploded")

    monkeypatch.setattr(ha.source_cache, "get_or_fetch", _boom)
    out = build(db, make_run(tenant, user), monkeypatch)
    assert out["historical_aerials"]["resolved_count"] == 3
    assert out["historical_aerials_meta"]["cache"] == "error"


# ── 3. The AI read + its degradation ladder ────────────────────────────────


def test_ok_read_is_metered_sanitized_and_attached(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [(valid_reading(), 6000, 900)])
    out = build(db, make_run(tenant, user), monkeypatch)

    meta = out["historical_aerials_meta"]
    assert meta["status"] == "ok"
    assert meta["model"] == MODEL
    reading = out["historical_aerials"]["reading"]
    assert reading["years_compared"] == [1981, 1996, 2022]
    assert reading["environmental_review_relevant"] is True
    assert reading["prior_use_signals"][0]["risk_category"] == "fuel_or_chemical"

    # ONE call, carrying one labelled image per era plus the instruction.
    assert fake.calls == 1
    content = fake.kwargs[0]["messages"][1]["content"]
    images = [part for part in content if part["type"] == "image_url"]
    assert len(images) == 3
    assert all(
        part["image_url"]["url"].startswith("data:image/jpeg;base64,")
        for part in images
    )
    assert "year 1981" in content[0]["text"]

    # Its own settled ai_usage row (§8.6 metering).
    rows = usage_rows(db)
    assert len(rows) == 1
    assert rows[0].status == "settled"
    assert rows[0].model == MODEL
    assert (rows[0].prompt_tokens, rows[0].completion_tokens) == (6000, 900)
    assert_no_pending_leak(db)


def test_reservation_is_estimated_from_image_tiles_not_base64_bytes():
    """A 512px chip is ONE vision tile (~255 tokens). Estimating from the
    base64 payload would over-reserve by ~50x and trip the budget cap."""
    assert ha._estimated_image_tokens(512) == 255
    assert ha._estimated_image_tokens(1024) == 85 + 170 * 4


def _ladder_case(db, run, monkeypatch, outcome):
    patch_client(monkeypatch, [outcome])
    return build(db, run, monkeypatch)


def test_no_key_still_renders_the_photos(db, make_tenant, make_user, monkeypatch):
    tenant = make_tenant("Keyless Co", budget_cents=100_000)
    user = make_user(tenant, "keyless@example.com")
    monkeypatch.setattr(
        ha.gateway, "resolve_key",
        lambda t: (_ for _ in ()).throw(gateway.NoTenantKey("no key")),
    )
    out = build(db, make_run(tenant, user), monkeypatch)
    meta = out["historical_aerials_meta"]
    assert meta["status"] == "fallback_no_key"
    assert meta["message"] == "Add your OpenAI key in Settings to get written summaries."
    assert out["historical_aerials"]["resolved_count"] == 3
    assert out["historical_aerials"]["reading"] is None
    assert usage_rows(db) == []


def test_budget_cap_still_renders_the_photos(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    monkeypatch.setattr(
        ha.gateway, "reserve",
        lambda *a, **k: (_ for _ in ()).throw(gateway.BudgetExceeded("cap")),
    )
    out = build(db, make_run(tenant, user), monkeypatch)
    meta = out["historical_aerials_meta"]
    assert meta["status"] == "fallback_budget_cap"
    assert meta["message"] == "This month's AI budget is used up. It resets on the 1st."
    assert out["historical_aerials"]["resolved_count"] == 3
    assert_no_pending_leak(db)


def test_rejected_key_releases_the_reservation(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    out = _ladder_case(
        db, make_run(tenant, user), monkeypatch,
        http_error(401, openai.AuthenticationError),
    )
    meta = out["historical_aerials_meta"]
    assert meta["status"] == "fallback_invalid_key"
    assert meta["message"] == "Your OpenAI key was rejected — check it in Settings."
    assert out["historical_aerials"]["resolved_count"] == 3
    rows = usage_rows(db)
    assert [row.status for row in rows] == ["released"]
    assert_no_pending_leak(db)


def test_provider_error_releases_the_reservation(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    out = _ladder_case(
        db, make_run(tenant, user), monkeypatch,
        http_error(503, openai.InternalServerError),
    )
    meta = out["historical_aerials_meta"]
    assert meta["status"] == "fallback_provider_error"
    assert meta["message"] == (
        "Summaries are unavailable right now — the facts below are complete."
    )
    assert out["historical_aerials"]["resolved_count"] == 3
    assert [row.status for row in usage_rows(db)] == ["released"]


def test_invalid_json_is_still_metered_and_degrades(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    out = _ladder_case(
        db, make_run(tenant, user), monkeypatch, ("not json at all", 6000, 40),
    )
    meta = out["historical_aerials_meta"]
    assert meta["status"] == "fallback_invalid_output"
    assert out["historical_aerials"]["reading"] is None
    # The model answered — those tokens are real and stay billed (§11.2).
    rows = usage_rows(db)
    assert [row.status for row in rows] == ["settled"]
    assert rows[0].prompt_tokens == 6000
    assert_no_pending_leak(db)


def test_schema_valid_but_empty_read_is_treated_as_invalid(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user
    empty = json.dumps({
        "era_observations": [], "change_events": [], "prior_use_signals": [],
        "environmental_review_relevant": False, "summary": "   ",
    })
    out = _ladder_case(db, make_run(tenant, user), monkeypatch, (empty, 6000, 20))
    assert out["historical_aerials_meta"]["status"] == "fallback_invalid_output"
    assert out["historical_aerials"]["reading"] is None


def test_fewer_than_two_photos_skips_the_call_entirely(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user
    fake = patch_client(monkeypatch, [])
    out = build(db, make_run(tenant, user), monkeypatch,
                sess=FakeSession(dead=("TOP96", "NAIP22")))
    meta = out["historical_aerials_meta"]
    assert meta["status"] == "skipped_insufficient_eras"
    assert fake.calls == 0
    assert usage_rows(db) == []
    assert "does not mean the land has no history" in meta["message"]


def test_injection_in_model_output_is_stripped(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    hostile = json.dumps({
        "era_observations": [{
            "year": 1981,
            "what_is_visible": "<img src=x onerror=alert(1)>Open fields",
            "apparent_use": "Cropland", "confidence": "wildly sure",
        }],
        "change_events": [],
        "prior_use_signals": [{
            "signal": "A yard", "risk_category": "made_up_category",
            "first_seen": "not a year", "last_seen": 2022,
            "why_it_matters": "Because.",
        }],
        "environmental_review_relevant": True,
        "summary": "Fields, then a yard.",
    })
    out = _ladder_case(db, make_run(tenant, user), monkeypatch, (hostile, 6000, 80))
    reading = out["historical_aerials"]["reading"]
    assert "<img" not in reading["era_observations"][0]["what_is_visible"]
    assert reading["era_observations"][0]["confidence"] == "low"
    assert reading["prior_use_signals"][0]["risk_category"] == "other"
    assert reading["prior_use_signals"][0]["first_seen"] is None


def test_disabled_by_settings_makes_no_call_at_all(db, tenant_user, monkeypatch):
    tenant, user = tenant_user
    sess = FakeSession()
    out = ha.build_historical_aerials(
        db, make_run(tenant, user),
        make_settings(historical_aerials_enabled=False), sess=sess,
    )
    assert out == {
        "historical_aerials": None,
        "historical_aerials_meta": {"status": "disabled", "model": MODEL},
    }
    assert sess.urls == []


def test_unexpected_crash_degrades_instead_of_failing_the_run(
    db, tenant_user, monkeypatch,
):
    tenant, user = tenant_user
    monkeypatch.setattr(
        ha, "_build",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = ha.build_historical_aerials(
        db, make_run(tenant, user), make_settings(), sess=FakeSession(),
    )
    assert out["historical_aerials"] is None
    assert out["historical_aerials_meta"]["status"] == "fallback_provider_error"


# ── 4. The card the report renders ─────────────────────────────────────────


def card_for(derived):
    run = SimpleNamespace(
        id="run-1", status="completed", adapter_results={
            "environmental_screening": {
                "source_id": "environmental_screening",
                "coverage_status": "covered",
                "source_access_status": "PROGRAMMATIC_PUBLIC",
                "data_confidence_status": "screened_indicator",
                "normalized_facts": {"total_records": 0, "radius_m": 1600},
                "fetched_at": "2026-08-02T15:00:00Z",
                "refresh_status": "fetched",
            },
        },
        sources_queried=[], sources_failed=[], derived=derived,
        anchor_kind="geocode", anchor_quality="verified",
        finished_at="2026-08-02T15:04:00Z",
    )
    payload = cards.build_cards(run, SETTINGS_STUB)
    group = next(g for g in payload["groups"] if g["key"] == "environment")
    card = next(
        (c for c in group["cards"] if c["card_key"] == "historical_aerials"),
        None,
    )
    return card, group


SETTINGS_STUB = SimpleNamespace(
    site_check_freshness_days=180, site_check_expiring_notice_days=30,
)


def derived_with_reading(**overrides):
    payload = {
        "eras": [
            {"year": 1981, "service": "NHAP/NHAP81_CIR_75cm",
             "image_url": "https://imagery.geographic.texas.gov/a?1981",
             "status": "resolved", "error": None},
            {"year": 1996, "service": "TOP/TOP96_BW_CIR_1m",
             "image_url": None, "status": "unavailable", "error": "HTTP 503"},
            {"year": 2022, "service": "NAIP/NAIP22_NCCIR_60cm",
             "image_url": "https://imagery.geographic.texas.gov/a?2022",
             "status": "resolved", "error": None},
        ],
        "anchor": "centroid", "bbox": None, "resolved_count": 2,
        "advisory": (
            "Older photos are available for 1981 and 2022. No photo could be "
            "loaded for 1996, so the timeline has gaps — a missing year does "
            "not mean nothing was on the land then; coverage of the older "
            "photo programs varies by area."
        ),
        "attribution": (
            "Historical aerial photos: Texas Geographic Information Office (TxGIO)"
        ),
        "reading": json.loads(valid_reading()) | {"years_compared": [1981, 2022]},
    }
    payload.update(overrides)
    return {
        "historical_aerials": payload,
        "historical_aerials_meta": {"status": "ok", "model": MODEL,
                                    "cache": "miss"},
    }


def test_card_renders_the_filmstrip_and_leads_with_the_risk():
    card, group = card_for(derived_with_reading())
    assert card is not None
    assert card["title"] == "Historical Aerials — what was this land before"
    assert card["status"] == "ok"
    # It sits in Environment & history, after the screening card.
    assert [c["card_key"] for c in group["cards"]][:2] == [
        "environmental_screening", "historical_aerials",
    ]

    # The era gallery — resolved years only, oldest first, each labelled.
    assert [img["label"] for img in card["images"]] == ["1981", "2022"]
    assert all(
        img["url"].startswith("https://") and img["caption"] and img["attribution"]
        for img in card["images"]
    )
    assert set(card["images"][0]) == {"url", "label", "caption", "attribution"}

    # A flagged prior use leads the collapsed line, NAMED — never buried.
    assert card["takeaway"].startswith(
        "Worth confirming before you buy: possible fuel or chemical storage —"
    )
    assert "drums" in card["takeaway"]
    assert card["takeaway"].endswith("(1996–2022).")

    titles = [t["title"] for t in card["tables"]]
    assert titles == ["Worth confirming before you buy", "Year by year",
                      "What changed"]
    signal_row = card["tables"][0]["rows"][0]
    assert signal_row[0].startswith("Possible fuel or chemical storage —")
    assert signal_row[1] == "1996–2022"

    labels = [f["label"] for f in card["facts"]]
    assert "What this land was before" in labels
    assert card["facts"][1]["value"] == "2 of 3 years"

    # The gap note and the honesty note both ride along.
    assert any("1996" in a for a in card["advisories"])
    assert card["advisories"][-1].startswith("These are observations from old")
    assert "Phase I environmental report" in card["advisories"][-1]
    assert card["sources"][0]["source_name"] == "Texas historical aerial photos"


def test_card_without_a_read_still_shows_the_photos_and_says_why():
    derived = derived_with_reading(reading=None)
    derived["historical_aerials_meta"] = {
        "status": "fallback_no_key", "model": MODEL, "error_kind": "no_key",
        "message": "Add your OpenAI key in Settings to get written summaries.",
        "cache": "miss",
    }
    card, _ = card_for(derived)
    assert card["status"] == "ok"
    assert len(card["images"]) == 2
    assert card["tables"] == []
    assert any("Add your OpenAI key" in a for a in card["advisories"])
    assert any("still the archive's own" in a for a in card["advisories"])


def test_card_with_no_photos_is_an_honest_issue_card():
    derived = {
        "historical_aerials": {
            "eras": [{"year": 1981, "service": "x", "image_url": None,
                      "status": "unavailable", "error": "HTTP 503"}],
            "resolved_count": 0, "reading": None,
            "advisory": "None of the older photos could be loaded.",
        },
        "historical_aerials_meta": {
            "status": "chips_failed", "model": MODEL,
            "message": "The archive of older photos could not be reached "
                       "this time.",
        },
    }
    card, _ = card_for(derived)
    assert card["status"] == "issue"
    assert card["images"] == []
    assert "could not be reached" in card["takeaway"]
    assert "Phase I environmental report" in card["advisories"][0]


def test_no_card_at_all_for_runs_without_the_block():
    card, group = card_for({})
    assert card is None
    assert [c["card_key"] for c in group["cards"]] == ["environmental_screening"]


def test_no_card_when_the_feature_is_switched_off():
    card, _ = card_for({
        "historical_aerials": None,
        "historical_aerials_meta": {"status": "disabled", "model": MODEL},
    })
    assert card is None


def test_every_card_carries_the_v3_images_key():
    _card, group = card_for(derived_with_reading())
    assert cards.CARDS_SCHEMA_VERSION == 4
    for other in group["cards"]:
        assert isinstance(other["images"], list)


def test_the_worst_signal_leads_not_the_first_one_written():
    """Live Chandler run 2026-08-02: the model listed long-term farming
    BEFORE an industrial-looking roadside yard, and the card led with the
    farming — the least useful thing on the list."""
    derived = derived_with_reading()
    derived["historical_aerials"]["reading"]["prior_use_signals"] = [
        {"signal": "Large cultivated fields around the intersection",
         "risk_category": "agricultural", "first_seen": 1981,
         "last_seen": 2022, "why_it_matters": "Farm chemicals."},
        {"signal": "A building beside a large hard-surfaced yard",
         "risk_category": "industrial_or_yard", "first_seen": 2010,
         "last_seen": 2022, "why_it_matters": "Outdoor storage."},
    ]
    card, _ = card_for(derived)
    assert "possible industrial or storage yard" in card["takeaway"]
    assert "cultivated" not in card["takeaway"]
    # The table is ordered the same way — worst first, all rows kept.
    rows = card["tables"][0]["rows"]
    assert rows[0][0].startswith("Possible industrial or storage yard")
    assert rows[1][0].startswith("Heavy farming use")
