"""The county check's verdicts need real evidence from the lookup, and its
throwaway database is cleaned up even when it fails to start."""

import subprocess

import pytest

from app.cli import check_region

COUNTY = {
    "name": "Test", "level": "Partial", "summary": "s", "sample_address": "1 Main St",
    "districts": [{"label": "Test ESDs", "kind": "esd", "urls": ["https://example.test/q"]}],
    "subdivisions": {"source_label": "Test subdivisions", "layer_url": "https://example.test/s",
                     "query_url": "https://example.test/s/query"},
}


def _answers(results, county=COUNTY):
    report = check_region.Report()
    check_region.judge_level(report, "test", county, results)
    return report


def test_districts_fail_when_the_district_source_did_not_run():
    report = _answers({})
    assert report.problems  # Partial with no evidence at all fails


def test_districts_fail_when_the_district_source_errored():
    results = {"district_overlays": {"coverage_status": "covered", "errors": ["down"],
                                     "normalized_facts": {"layer_errors": []}}}
    report = _answers(results, {**COUNTY, "level": "Full"})
    assert any("Districts" in line for line in report.problems)


def test_districts_pass_only_when_every_layer_answered():
    ok = {"district_overlays": {"coverage_status": "covered", "errors": None,
                                "normalized_facts": {"layer_errors": []}}}
    assert not _answers(ok).problems


def test_a_subdivision_section_alone_does_not_earn_partial():
    results = {"plats_intel": {"normalized_facts": {"test_subdivision_gis_status": "no_parcel_polygon"}}}
    assert _answers(results).problems


def test_a_queried_subdivision_layer_earns_partial():
    results = {"plats_intel": {"normalized_facts": {
        "test_subdivision_gis_status": "no_intersecting_recorded_subdivision"}}}
    assert not _answers(results).problems


def test_failed_database_start_is_still_cleaned_up(monkeypatch):
    registered = []
    monkeypatch.setattr(check_region.atexit, "register", lambda fn, *a, **k: registered.append(fn))

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(check_region.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        check_region._start_throwaway_postgres()
    assert len(registered) == 1
    monkeypatch.undo()
    registered[0]()  # removes the temporary folders without error
