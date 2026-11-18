"""The one proof command for a state or county file (docs/EXTENDING.md).

    scripts/check_region.sh <state> [<county>]

Needs no running app. In order, it:
  1. checks the files load (every field the right name and type);
  2. runs the tests that keep Settings in step with the files;
  3. for each county: checks each map layer and web page in its file answers
     at the county's sample address, then runs a real lookup on that address
     in a throwaway database, prints each result card's status, and judges
     the county's level (Full/Partial) from what the lookup actually found.

It ends with PASS or FAIL, and one plain line per problem. It uses no keys
from your own key store, so it never spends AI money.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import requests

BACKEND_DIR = Path(__file__).resolve().parents[2]
_TIMEOUT = 25
_HEADERS = {"User-Agent": "PropRisk check_region (open-source contributor check)"}
_FOUND_KINDS = {"full_purpose_city", "limited_purpose", "etj", "unincorporated_county"}
# plats_intel statuses that mean the county's subdivision layer was really queried.
_SUBDIVISION_ANSWERED = {
    "recorded_plat_found", "platted_subdivision_match",
    "subdivision_match_not_recorded", "no_intersecting_recorded_subdivision",
}


class Report:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def ok(self, line: str) -> None:
        print(f"  ok      {line}")

    def warn(self, line: str) -> None:
        print(f"  warning {line}")

    def fail(self, line: str) -> None:
        print(f"  FAIL    {line}")
        self.problems.append(line)


# ── 2. Live check ──────────────────────────────────────────────────────────


def _geocode(address: str) -> tuple[float, float] | None:
    """The sample address's point, from the US Census geocoder (free, no key)."""
    try:
        resp = requests.get(
            "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
            params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        matches = resp.json()["result"]["addressMatches"]
    except (requests.RequestException, ValueError, KeyError):
        return None
    if not matches:
        return None
    point = matches[0]["coordinates"]
    return float(point["y"]), float(point["x"])


def _query_layer(url: str, lat: float, lng: float, where: str | None = None) -> tuple[str, str]:
    """('answered' | 'empty' | 'error', detail) for one point query."""
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "where": where or "1=1",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        return "error", str(exc)[:160]
    except ValueError:
        return "error", "the answer was not map data (JSON)"
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        return "error", f"map service error: {err.get('message') if isinstance(err, dict) else err}"
    features = body.get("features") if isinstance(body, dict) else None
    if features is None:
        return "error", "the answer has no features list"
    return ("answered", f"{len(features)} found") if features else ("empty", "nothing at this point")


def _check_layer_list(
    report: Report, label: str, layers: list[dict[str, Any]], lat: float, lng: float,
) -> None:
    """A list of layers tried in order: one answering is enough."""
    answers = []
    for layer in layers:
        status, detail = _query_layer(layer["url"], lat, lng, layer.get("where"))
        answers.append((status, detail, layer["url"]))
    if any(status != "error" for status, _, _ in answers):
        for status, detail, url in answers:
            if status == "error":
                report.warn(f"{label}: one layer failed ({detail}): {url}")
            else:
                report.ok(f"{label}: {'answered' if status == 'answered' else 'answered, empty here'} ({detail})")
    else:
        for _, detail, url in answers:
            report.fail(f"{label}: layer failed ({detail}): {url}")


def _check_page(report: Report, label: str, url: str | None) -> None:
    if not url:
        return
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        report.fail(f"{label}: page does not open ({str(exc)[:120]}): {url}")
        return
    if resp.status_code < 400:
        report.ok(f"{label}: page opens")
    elif resp.status_code in (401, 403, 429):
        report.warn(
            f"{label}: the site refuses automated checks (HTTP {resp.status_code}); "
            f"open it in a browser to confirm: {url}"
        )
    else:
        report.fail(f"{label}: page answers HTTP {resp.status_code}: {url}")


def live_check(report: Report, county: dict[str, Any], lat: float, lng: float) -> None:
    jurisdiction = county.get("jurisdiction")
    if jurisdiction:
        _check_page(report, "jurisdiction source page", jurisdiction.get("source_url"))
        for key in ("city_limits", "etj", "limited_purpose"):
            if jurisdiction.get(key):
                _check_layer_list(report, f"jurisdiction {key}", jurisdiction[key], lat, lng)
    parcels = county.get("parcels")
    if parcels:
        _check_layer_list(report, "parcels", [{"url": parcels["query_url"]}], lat, lng)
        _check_page(report, "parcels source page", parcels.get("source_url"))
    _check_page(report, "appraisal district search page", county.get("appraisal_search_url"))
    for district in county.get("districts") or []:
        _check_layer_list(
            report, f"district layer '{district['label']}'",
            [{"url": url} for url in district["urls"]], lat, lng,
        )
    if county.get("fire_stations"):
        _check_layer_list(report, "fire stations", [county["fire_stations"]], lat, lng)
    clerk = county.get("clerk")
    if clerk:
        _check_page(report, "county clerk portal", clerk["url"])
        _check_page(report, "county clerk plat portal", clerk.get("plat_url"))
        _check_page(report, "county clerk help page", clerk.get("fallback_url"))
    subdivisions = county.get("subdivisions")
    if subdivisions:
        _check_layer_list(report, "subdivisions", [{"url": subdivisions["query_url"]}], lat, lng)


# ── 3. Real lookup ─────────────────────────────────────────────────────────


def _postgres_env() -> dict[str, str]:
    env = dict(os.environ)
    extra = ["/opt/homebrew/bin", "/usr/local/bin"]
    extra += sorted(str(p / "bin") for p in Path("/opt/homebrew/opt").glob("postgresql@*"))
    env["PATH"] = ":".join(extra) + ":" + env.get("PATH", "")
    env["LC_ALL"] = env.get("LC_ALL") or "en_US.UTF-8"
    return env


def _start_throwaway_postgres() -> str:
    """Same recipe as the test suite (tests/conftest.py): a private cluster on
    a free port, removed when this command exits."""
    env = _postgres_env()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    pg_dir = tempfile.mkdtemp(prefix="proprisk_check_pg_")
    sock_dir = tempfile.mkdtemp(prefix="proprisk_check_sock_")

    def _stop() -> None:
        if (Path(pg_dir) / "postmaster.pid").exists():
            # "immediate": a throwaway database needs no shutdown checkpoint,
            # which on a busy disk can outlast pg_ctl's wait and leave the
            # server running after its folder is removed.
            subprocess.run(["pg_ctl", "-D", pg_dir, "stop", "-m", "immediate"],
                           check=False, capture_output=True, env=env)
        shutil.rmtree(pg_dir, ignore_errors=True)
        shutil.rmtree(sock_dir, ignore_errors=True)

    # Registered before anything starts, so a failed start is cleaned up too.
    atexit.register(_stop)

    def run(args: list[str]) -> None:
        subprocess.run(args, check=True, capture_output=True, text=True, env=env)

    run(["initdb", "-D", pg_dir, "-U", "proprisk", "--auth=trust"])
    run(["pg_ctl", "-D", pg_dir, "-l", os.path.join(pg_dir, "postgres.log"),
         "-o", f"-p {port} -k {sock_dir} -c listen_addresses=127.0.0.1", "start", "-w"])
    run(["createdb", "-h", "127.0.0.1", "-p", str(port), "-U", "proprisk", "proprisk_check"])
    return f"postgresql+psycopg://proprisk@127.0.0.1:{port}/proprisk_check"


_DB_READY = False


def _prepare_database() -> None:
    """Throwaway database, no keys from your own key store (so no AI spend),
    and the fixed office/owner rows every lookup belongs to."""
    global _DB_READY
    if _DB_READY:
        return
    os.environ["DATABASE_URL"] = _start_throwaway_postgres()
    scratch = Path(tempfile.mkdtemp(prefix="proprisk_check_data_"))
    atexit.register(shutil.rmtree, scratch, ignore_errors=True)

    from app.config.settings import settings

    settings.secrets_db_path = str(scratch / "secrets.sqlite3")
    settings.error_sink_file = str(scratch / "errors.jsonl")
    settings.historical_aerials_enabled = False
    settings.entitlement_read_enabled = False

    from sqlalchemy import text

    import app.models  # noqa: F401  (registers every table)
    from app.core.local_identity import LOCAL_TENANT_ID, LOCAL_USER_ID
    from app.db import Base, engine

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO tenants (id, name, monthly_budget_cents, created_at) "
            "VALUES (:id, 'Local', 0, now())"
        ), {"id": LOCAL_TENANT_ID})
        conn.execute(text(
            "INSERT INTO users (id, tenant_id, display_name, role, created_at) "
            "VALUES (:id, :tenant, 'Local owner', 'owner', now())"
        ), {"id": LOCAL_USER_ID, "tenant": LOCAL_TENANT_ID})
    _DB_READY = True


def real_lookup(address: str) -> tuple[dict[str, Any], list[tuple[str, str]], str]:
    """(adapter results, [(card, status)], run status) for a fresh lookup."""
    _prepare_database()
    from app.config.settings import settings
    from app.core import job_queue
    from app.core.local_identity import LOCAL_TENANT_ID, LOCAL_USER_ID
    from app.db import SessionLocal
    from app.services.site_check import cards, orchestrator, service

    db = SessionLocal()
    try:
        run, _job, _ = service.create_run(
            db, tenant_id=LOCAL_TENANT_ID, requested_by=LOCAL_USER_ID,
            address=address, mode="fresh",
        )
        job = job_queue.claim_next_job(db, worker_id="check_region")
        run = orchestrator.run_collect(
            db, run_id=run.id, job_id=job.id, lease_token=job.lease_token,
        )
        built = cards.build_cards(run, settings)
        card_statuses = [
            (card["card_key"], card["status"])
            for group in built.get("groups", [])
            for card in group.get("cards", [])
        ]
        return dict(run.adapter_results or {}), card_statuses, run.status
    finally:
        db.close()


def _facts(results: dict[str, Any], source_id: str) -> dict[str, Any]:
    result = results.get(source_id) or {}
    return result.get("normalized_facts") or {}


def judge_level(
    report: Report, key: str, county: dict[str, Any], results: dict[str, Any],
) -> None:
    """The four questions in the spec, answered from the lookup's own facts."""
    from app.services.site_check import county_clerk_portals

    jurisdiction = _facts(results, "jurisdiction_resolver")
    jurisdiction_ok = (
        "jurisdiction" in county
        and jurisdiction.get("jurisdiction_kind") in _FOUND_KINDS
        and jurisdiction.get("source") == county["jurisdiction"]["source"]
    )
    parcel_ok = False
    for source_id in ("tcad_parcel", "wcad_parcel"):
        result = results.get(source_id) or {}
        facts = result.get("normalized_facts") or {}
        candidates = facts.get("candidates")
        if (
            result.get("coverage_status") == "covered"
            and facts.get("parcel_id")
            and not (isinstance(candidates, list) and len(candidates) > 1)
        ):
            parcel_ok = True
    district_labels = {d["label"] for d in county.get("districts") or []}
    district_result = results.get("district_overlays") or {}
    district_facts = district_result.get("normalized_facts") or {}
    layer_errors = district_facts.get("layer_errors")
    districts_ok = (
        bool(district_labels)
        and district_result.get("coverage_status") in ("covered", "partial")
        and not district_result.get("errors")
        and isinstance(layer_errors, list)  # the district source really ran
        and not [e for e in layer_errors if e.get("layer") in district_labels]
    )
    steps = county_clerk_portals.clerk_lookup_steps(key, "deed")
    clerk_ok = bool(steps and steps.get("url") and steps.get("instructions"))

    answers = {
        "Jurisdiction (city, ETJ or confirmed unincorporated, from the county's own layers)": jurisdiction_ok,
        "Parcel (exactly one parcel found for the point)": parcel_ok,
        "Districts (every district layer answered)": districts_ok,
        "Clerk (portal and steps present)": clerk_ok,
    }
    for question, passed in answers.items():
        (report.ok if passed else report.warn)(f"level test: {question}: {'yes' if passed else 'no'}")

    level = county["level"]
    if level == "Full" and not all(answers.values()):
        missing = [q.split(" (")[0] for q, passed in answers.items() if not passed]
        report.fail(
            f"the file says Full, but the lookup did not answer: {', '.join(missing)}. "
            "Fix the file, or set level: Partial"
        )
    elif level == "Partial" and not any(answers.values()):
        fire = (results.get("fire_district") or {}).get("coverage_status") == "covered"
        subdivision_status = _facts(results, "plats_intel").get(f"{key}_subdivision_gis_status")
        subdivisions = subdivision_status in _SUBDIVISION_ANSWERED
        if not (fire or subdivisions):
            report.fail("the file says Partial, but no answer came from the county's own file")
    elif level == "Partial" and all(answers.values()):
        report.ok("every Full test passed: this county can be set to level: Full")


# ── Main ───────────────────────────────────────────────────────────────────


def check_county(report: Report, state: str, key: str, county: dict[str, Any]) -> None:
    print(f"\n{county['name']} County, {state}  ({county['level']})")
    print(f"  sample address: {county['sample_address']}")
    point = _geocode(county["sample_address"])
    if point is None:
        report.fail(f"the sample address was not found by the US Census geocoder: {county['sample_address']}")
        return
    live_check(report, county, *point)

    results, card_statuses, run_status = real_lookup(county["sample_address"])
    if run_status != "completed":
        report.fail(f"the real lookup ended as '{run_status}', not completed")
        return
    looked_up_state = _facts(results, "arcgis_geocode").get("state") or ""
    if looked_up_state != state:
        report.fail(
            f"the lookup placed the sample address in state '{looked_up_state or 'unknown'}', "
            f"not {state}: put the state in the sample address"
        )
    looked_up_county = _facts(results, "arcgis_geocode").get("county") or ""
    if looked_up_county.lower().removesuffix(" county") != key:
        report.fail(
            f"the lookup placed the sample address in '{looked_up_county or 'no county'}', "
            f"not {county['name']} County: use an address inside the county"
        )
    print("  result cards: " + ", ".join(f"{card}={status}" for card, status in card_statuses))
    # The county's key in the lookup tables: "comal" in Texas, "co:denver" elsewhere.
    from app.regions import loader

    judge_level(report, loader.county_key(state, key), county, results)


def run_settings_tests(report: Report) -> None:
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_coverage_matches_adapters.py", "tests/test_regions_loader.py"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, check=False,
    )
    summary = (proc.stdout.strip().splitlines() or ["no output"])[-1]
    if proc.returncode == 0:
        report.ok(f"Settings tests: {summary}")
    else:
        report.fail(f"Settings tests failed: {summary}")


def main(argv: list[str]) -> int:
    # The lookup's own progress logs are noise here; real problems are
    # reported as FAIL or warning lines.
    logging.disable(logging.WARNING)
    # A stop signal exits normally, so the throwaway database is removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if not argv or len(argv) > 2:
        print("usage: scripts/check_region.sh <state> [<county>]   e.g.  tx comal")
        return 2
    state = argv[0].upper()
    county_key = argv[1].lower().replace("_", " ").strip() if len(argv) == 2 else None
    report = Report()

    from app.regions import loader

    print("File check")
    try:
        regions = loader.load_regions()
    except loader.RegionFileError as exc:
        report.fail(str(exc))
        print("\nFAIL")
        return 1
    if state not in regions:
        report.fail(f"no folder app/regions/{state.lower()}/ with a state.yaml")
        print("\nFAIL")
        return 1
    counties = regions[state]["counties"]
    if county_key and county_key not in counties:
        file_name = county_key.replace(" ", "_")
        report.fail(f"no file app/regions/{state.lower()}/counties/{file_name}.yaml")
        print("\nFAIL")
        return 1
    report.ok(f"{state.lower()}/state.yaml and {len(counties)} county file(s) load")
    # Before any lookup: a lookup leaves many threads running, and starting
    # the test run from such a process is unsafe on macOS.
    run_settings_tests(report)

    selected = {county_key: counties[county_key]} if county_key else counties
    for key, county in selected.items():
        check_county(report, state, key, county)

    if report.problems:
        print(f"\nFAIL ({len(report.problems)} problem(s))")
        for line in report.problems:
            print(f"  - {line}")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
