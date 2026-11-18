"""Build a state's census block-group centroid file for PropRisk.

    uv --directory backend run python ../scripts/build_census_centroids.py 08

Reads the Census Bureau's public 2020 block groups (TIGERweb, the geography
the ACS 5-year figures use) and writes
backend/app/services/site_check/bg_centroids/bg_centroids_<fips>.json in the
same format as the Texas file: {"<12-digit GEOID>": [lat, lon], ...}.
Then name that file as `census_centroids:` in the state's state.yaml.
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

LAYER = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "Tracts_Blocks/MapServer/11/query"
)
OUT_DIR = (
    Path(__file__).resolve().parents[1]
    / "backend/app/services/site_check/bg_centroids"
)
PAGE = 1000


def main(fips: str) -> None:
    if len(fips) != 2 or not fips.isdigit():
        sys.exit("Give the two-digit census state code, for example 08 for Colorado.")
    centroids: dict[str, list[float]] = {}
    offset = 0
    while True:
        query = urllib.parse.urlencode({
            "where": f"STATE='{fips}'",
            "outFields": "GEOID,INTPTLAT,INTPTLON",
            "returnGeometry": "false",
            "orderByFields": "GEOID",
            "resultOffset": offset,
            "resultRecordCount": PAGE,
            "f": "json",
        })
        with urllib.request.urlopen(f"{LAYER}?{query}", timeout=120) as resp:
            data = json.load(resp)
        if "error" in data:
            sys.exit(f"Census service error: {data['error']}")
        features = data.get("features") or []
        for feature in features:
            a = feature["attributes"]
            centroids[a["GEOID"]] = [float(a["INTPTLAT"]), float(a["INTPTLON"])]
        if len(features) < PAGE:
            break
        offset += PAGE
    if not centroids:
        sys.exit(f"No block groups found for state {fips}.")
    out = OUT_DIR / f"bg_centroids_{fips}.json"
    out.write_text(json.dumps(centroids, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(centroids)} block groups to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
