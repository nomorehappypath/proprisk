"""The region files (app/regions/) load, and a wrong file stops startup with a
message that names the file and the field."""

from pathlib import Path

import pytest

from app.regions import loader

STATE = 'code: TX\nname: Texas\nfips: "48"\nsearch_box: [-106.65, 25.84, -93.51, 36.50]\n'
COUNTY = "name: Test\nlevel: Full\nsummary: s\nsample_address: 1 Main St\n"


def _region(tmp_path: Path, county_text: str) -> Path:
    counties = tmp_path / "tx" / "counties"
    counties.mkdir(parents=True)
    (tmp_path / "tx" / "state.yaml").write_text(STATE)
    (counties / "test.yaml").write_text(county_text)
    return tmp_path


def test_shipped_texas_files_load():
    texas = loader.load_regions()["TX"]
    assert texas["state"]["name"] == "Texas"
    assert set(texas["counties"]) == {
        "travis", "williamson", "hays", "bastrop", "caldwell", "burnet", "blanco",
    }


def test_left_out_keys_stay_absent(tmp_path):
    root = _region(tmp_path, COUNTY + "fire_stations:\n  url: https://example.test/query\n")
    county = loader.load_regions.__wrapped__(root)["TX"]["counties"]["test"]
    assert county["fire_stations"] == {"url": "https://example.test/query"}
    assert "jurisdiction" not in county


def test_wrong_field_names_file_and_field(tmp_path):
    root = _region(tmp_path, COUNTY.replace("level: Full", "level: Mostly"))
    with pytest.raises(loader.RegionFileError) as err:
        loader.load_regions.__wrapped__(root)
    assert "test.yaml" in str(err.value) and "level" in str(err.value)


def test_misspelled_key_is_refused(tmp_path):
    root = _region(tmp_path, COUNTY + "fire_station:\n  url: https://example.test/query\n")
    with pytest.raises(loader.RegionFileError) as err:
        loader.load_regions.__wrapped__(root)
    assert "fire_station" in str(err.value)


def test_state_code_must_match_folder(tmp_path):
    root = _region(tmp_path, COUNTY)
    (root / "tx" / "state.yaml").write_text(STATE.replace("code: TX", "code: CO"))
    with pytest.raises(loader.RegionFileError) as err:
        loader.load_regions.__wrapped__(root)
    assert "state.yaml" in str(err.value) and "folder" in str(err.value)


def test_key_written_twice_is_refused(tmp_path):
    text = COUNTY + "districts:\n  - label: A\n    kind: esd\n    urls: [https://example.test/q]\ndistricts: []\n"
    root = _region(tmp_path, text)
    with pytest.raises(loader.RegionFileError) as err:
        loader.load_regions.__wrapped__(root)
    assert "test.yaml" in str(err.value) and "districts" in str(err.value)


def test_empty_section_is_refused(tmp_path):
    root = _region(tmp_path, COUNTY + "fire_stations:\n")
    with pytest.raises(loader.RegionFileError) as err:
        loader.load_regions.__wrapped__(root)
    assert "test.yaml" in str(err.value) and "fire_stations" in str(err.value)


def test_blank_templates_load(tmp_path):
    """The files a contributor copies are valid as they stand."""
    template = loader.REGIONS_DIR / "_template"
    state_dir = tmp_path / "xx"
    (state_dir / "counties").mkdir(parents=True)
    (state_dir / "state.yaml").write_text((template / "state.yaml").read_text())
    (state_dir / "counties" / "example.yaml").write_text((template / "county.yaml").read_text())
    loaded = loader.load_regions.__wrapped__(tmp_path)
    assert loaded["XX"]["counties"]["example"]["name"] == "Example"
