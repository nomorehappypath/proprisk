"""One-shot §13.2 fixture recorder — run manually, NEVER from pytest.

Usage (from backend/, live network + dev .env keys required):

    uv run python tests/record_site_check_fixtures.py [source_id ...]

For every ACTIVE registry source this drives the real adapter once
against the live providers with the Austin identity (1100 Congress Ave),
capturing every raw HTTP exchange through a recording session. It then
immediately replays the adapter OFFLINE twice through the same
`ReplaySession` the pytest suite uses, auto-detects volatile result
paths (clock-derived values), and writes
`tests/fixtures/site_check/<source_id>/fixture.json` containing the raw
exchanges + the expected AdapterResult dump.

Waves mirror the orchestrator (`_source_waves`), including identity
mutations between waves and `_build_adapter_config` upstream wiring, so
wave-2/3 adapters record against realistic upstream results. Per-wave
result snapshots land in `tests/fixtures/site_check/_shared/` for
config reconstruction at replay time.

Secrets: query values named token/key/... are redacted symmetrically at
capture and match time (see site_check_fixture_replay.SECRET_PARAM_NAMES);
after writing, this script greps every fixture for the live ArcGIS /
Census key values and aborts if either leaked.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from site_check_fixture_replay import (
    FIXTURE_ROOT,
    ReplaySession,
    exchange_from_exception,
    exchange_from_response,
    freeze_adapter_clocks,
    write_gzip_json,
)

from app.config.settings import get_settings
from app.services.site_check import registry
from app.services.site_check.adapters._base import http_session
from app.services.site_check.identity import PropertyIdentity
from app.services.site_check.orchestrator import (
    _adapter_result_payload,
    _apply_identity_mutations,
    _build_adapter_config,
    _load_adapter_fetch,
    _source_waves,
)

ADDRESS = "1100 Congress Ave, Austin, TX 78701"
LAT, LNG = 30.276513, -97.739758
COUNTY = "travis"

# Sources whose responses are validated by headers only — bodies are
# not stored (public_aerial_imagery streams headers and closes).
DROP_IMAGE_BODIES = {"public_aerial_imagery"}


class _Recorder:
    """Wraps a live session (and its no-transport-retry sibling) to
    append every exchange to the current sink."""

    def __init__(self):
        self.sess = http_session()
        self.sink: list[dict] = []
        self.drop_image_bodies = False
        self._wrap(self.sess)
        sibling = getattr(self.sess, "no_transport_retry", None)
        if sibling is not None:
            self._wrap(sibling)

    def _wrap(self, sess_obj: requests.Session) -> None:
        orig = sess_obj.request

        def recording_request(method, url, **kwargs):
            try:
                resp = orig(method, url, **kwargs)
            except requests.RequestException as exc:
                self.sink.append(
                    exchange_from_exception(method, url, kwargs, exc)
                )
                raise
            content_type = (resp.headers.get("Content-Type") or "").lower()
            drop = (
                self.drop_image_bodies and content_type.startswith("image/")
            )
            self.sink.append(
                exchange_from_response(
                    method, url, kwargs, resp, drop_body=drop,
                )
            )
            return resp

        sess_obj.request = recording_request  # type: ignore[method-assign]


def _diff_paths(a, b, prefix="") -> list[str]:
    """Dotted paths where two JSON-ish structures differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        paths = []
        for key in set(a) | set(b):
            sub = f"{prefix}.{key}" if prefix else str(key)
            if key not in a or key not in b:
                paths.append(sub)
            else:
                paths.extend(_diff_paths(a[key], b[key], sub))
        return paths
    if isinstance(a, list) and isinstance(b, list):
        # any element-level drift → ignore the whole list path (the
        # prune helper deletes at the containing key)
        if len(a) != len(b) or any(
            _diff_paths(ia, ib, prefix) for ia, ib in zip(a, b)
        ):
            return [prefix]
        return []
    if a != b:
        return [prefix]
    return []


def _settings_overrides(source_id: str) -> dict:
    settings = get_settings()
    if source_id == "plats_intel":
        return {
            "public_plat_discovery_enabled": settings.public_plat_discovery_enabled,
            "public_plat_search_provider": settings.public_plat_search_provider,
            "public_plat_searxng_url": settings.public_plat_searxng_url,
        }
    return {}


def _requires_keys(source_id: str) -> list[str]:
    return {
        "arcgis_geocode": ["arcgis"],
        "census_acs": ["census"],
    }.get(source_id, [])


def _record_source(
    recorder: _Recorder,
    source_id: str,
    identity: PropertyIdentity,
    stage: str,
    stage_results: dict,
) -> tuple[dict | None, Exception | None]:
    """Record one adapter live, verify offline replay, write fixture.
    Returns (result_payload_for_snapshot, live_exception)."""
    print(f"── recording {source_id} (stage={stage}) ...", flush=True)
    recorder.sink = []
    recorder.drop_image_bodies = source_id in DROP_IMAGE_BODIES
    config = _build_adapter_config(source_id, stage_results, project_context={})
    fetch = _load_adapter_fetch(source_id)
    live_exc: Exception | None = None
    live_result = None
    started = datetime.now(UTC)
    try:
        live_result = fetch(identity, session=recorder.sess, config=config)
    except Exception as exc:  # recorded failure is still a fixture
        live_exc = exc
        print(f"   live fetch raised {type(exc).__name__}: {exc}")
    exchanges = list(recorder.sink)
    print(f"   captured {len(exchanges)} exchanges")

    # Offline replay x2 with frozen clocks → expected dump + volatility.
    restore = freeze_adapter_clocks(started)
    try:
        dumps = []
        replay_exc: Exception | None = None
        for _ in range(2):
            try:
                replayed = fetch(
                    identity,
                    session=ReplaySession(list(exchanges)),
                    config=config,
                )
                dumps.append(replayed.model_dump(mode="json"))
            except Exception as exc:
                replay_exc = exc
                break
    finally:
        restore()

    fixture_dir = FIXTURE_ROOT / source_id
    fixture_dir.mkdir(parents=True, exist_ok=True)

    if replay_exc is not None or not dumps:
        print(f"   !! offline replay failed: {replay_exc!r} — fixture NOT written")
        return (
            {"source_id": source_id, "normalized_facts": {},
             "error": str(live_exc or replay_exc)},
            live_exc,
        )

    ignore_paths = sorted(set(_diff_paths(dumps[0], dumps[1])))
    if ignore_paths:
        print(f"   volatile paths ignored: {ignore_paths}")
    notes = []
    if live_result is not None:
        for field in (
            "source_access_status", "data_confidence_status",
            "coverage_status",
        ):
            live_v = getattr(live_result, field).value
            replay_v = dumps[0].get(field)
            if live_v != replay_v:
                notes.append(
                    f"replay {field}={replay_v!r} != live {live_v!r}"
                )
    elif live_exc is not None:
        notes.append(
            f"live fetch raised {type(live_exc).__name__}; fixture replays "
            "the recorded failure exchanges"
        )
    if notes:
        print(f"   NOTE: {notes}")

    fixture = {
        "source_id": source_id,
        "recorded_at": started.isoformat(),
        "strategy": "live-recorded raw payloads (Austin identity)",
        "identity": json.loads(identity.model_dump_json()),
        "stage": stage,
        "settings_overrides": _settings_overrides(source_id),
        "requires_keys": _requires_keys(source_id),
        "registry_result_schema_version": registry.result_schema_version(
            source_id
        ),
        "ignore_paths": ignore_paths,
        "notes": notes,
        "exchanges_file": "exchanges.json.gz",
        "expected": dumps[0],
    }
    write_gzip_json(fixture_dir / "exchanges.json.gz", exchanges)
    path = fixture_dir / "fixture.json"
    with path.open("w") as fh:
        json.dump(fixture, fh, indent=1)
    size = (
        path.stat().st_size
        + (fixture_dir / "exchanges.json.gz").stat().st_size
    )
    print(f"   wrote {path} ({size / 1024:.0f} KiB incl. exchanges.json.gz)")

    if live_result is not None:
        return _adapter_result_payload(live_result), live_exc
    return (
        {"source_id": source_id, "normalized_facts": {},
         "error": str(live_exc)},
        live_exc,
    )


def refresh_expected(only: set[str] | None = None) -> None:
    """OFFLINE maintenance mode (`--refresh-expected`): re-derive every
    fixture's expected AdapterResult dump from the CURRENT adapter code
    over the stored raw exchanges. No network. Use after intentional
    adapter behavior/wording changes so the §13.2 floor tracks the new
    contract without re-recording providers."""
    from site_check_fixture_replay import load_fixture, replay_fetch

    for path in sorted(FIXTURE_ROOT.glob("*/fixture.json")):
        source_id = path.parent.name
        if only and source_id not in only:
            continue
        fixture = load_fixture(source_id)
        restore = freeze_adapter_clocks(
            datetime.fromisoformat(fixture["recorded_at"])
        )
        try:
            dumps = [
                replay_fetch(source_id, fixture).model_dump(mode="json")
                for _ in range(2)
            ]
        finally:
            restore()
        fixture["ignore_paths"] = sorted(set(_diff_paths(dumps[0], dumps[1])))
        fixture["expected"] = dumps[0]
        fixture["registry_result_schema_version"] = (
            registry.result_schema_version(source_id)
        )
        exchanges = fixture.pop("exchanges")
        write_gzip_json(path.parent / "exchanges.json.gz", exchanges)
        fixture["exchanges_file"] = "exchanges.json.gz"
        with path.open("w") as fh:
            json.dump(fixture, fh, indent=1)
        print(f"refreshed expected for {source_id}")


def main(only: set[str] | None = None) -> None:
    recorder = _Recorder()
    results: dict[str, dict] = {}
    shared_dir = FIXTURE_ROOT / "_shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    if only:
        # single-source re-record: seed upstream results from the last
        # full recording so wave-2/3 configs stay realistic
        prior = shared_dir / "results_wave2.json.gz"
        if prior.exists():
            import gzip

            with gzip.open(prior, "rt", encoding="utf-8") as fh:
                results.update(json.load(fh))

    def snapshot(stage: str) -> None:
        if only:
            return  # partial re-record must not clobber stage snapshots
        write_gzip_json(shared_dir / f"results_{stage}.json.gz", results)

    def record(source_id: str, identity: PropertyIdentity, stage: str):
        if only and source_id not in only:
            # still need the snapshot chain — reuse prior snapshot entry
            return
        payload, _exc = _record_source(
            recorder, source_id, identity, stage, results,
        )
        if payload is not None:
            results[source_id] = payload

    active = registry.active_source_ids()
    wave1, post_parcel, wave2, wave3 = _source_waves(active)

    # Wave 0: geocode gets the bare address (its real job); the
    # jurisdiction resolver gets the anchored identity.
    geocode_identity = PropertyIdentity(address=ADDRESS, county=COUNTY)
    record("arcgis_geocode", geocode_identity, "initial")

    identity = PropertyIdentity(
        address=ADDRESS, lat=LAT, lng=LNG, county=COUNTY,
    )
    record("jurisdiction_resolver", identity, "initial")

    for sid in wave1:
        record(sid, identity, "initial")
    identity = _apply_identity_mutations(
        identity, completed_sources_this_wave=wave1, results=results,
    )
    snapshot("wave1")

    for sid in post_parcel:
        record(sid, identity, "wave1")
    identity = _apply_identity_mutations(
        identity, completed_sources_this_wave=post_parcel, results=results,
    )
    snapshot("post_parcel")

    for sid in wave2:
        record(sid, identity, "post_parcel")
    identity = _apply_identity_mutations(
        identity, completed_sources_this_wave=wave2, results=results,
    )
    snapshot("wave2")

    for sid in wave3:
        record(sid, identity, "wave2")

    _assert_no_secret_leak()
    print("done.")


def _assert_no_secret_leak() -> None:
    from app.core import secret_store

    secrets = [
        s.get_secret_value()
        for s in (secret_store.get_secret("arcgis"), secret_store.get_secret("census"))
        if s is not None and s.get_secret_value()
    ]
    if not secrets:
        return
    import gzip

    for path in (
        list(FIXTURE_ROOT.rglob("*.json"))
        + list(FIXTURE_ROOT.rglob("*.json.gz"))
    ):
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                blob = fh.read()
        else:
            blob = path.read_text()
        for value in secrets:
            if value in blob:
                raise SystemExit(
                    f"SECRET LEAKED into {path} — fix redaction, delete "
                    "the fixture, re-record."
                )
    print("secret-leak scan: clean")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--refresh-expected" in args:
        args.remove("--refresh-expected")
        refresh_expected(set(args) or None)
    else:
        main(set(args) or None)
