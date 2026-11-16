"""Checks that every registered source (and the 3 AI steps) has a row in
docs/DATA_SOURCES.md, so the published list can't silently fall behind the
registry."""

from pathlib import Path

from app.services.site_check import registry

DOC = Path(__file__).resolve().parents[2] / "docs" / "DATA_SOURCES.md"
AI_STEPS = ["historical_aerials", "entitlement_reader", "narratives"]


def test_every_registered_source_and_ai_step_is_documented():
    text = DOC.read_text()
    missing = [
        source_id for source_id in sorted(registry.all_source_ids()) + AI_STEPS
        if source_id not in text
    ]
    assert not missing, f"Missing from docs/DATA_SOURCES.md: {missing}"
