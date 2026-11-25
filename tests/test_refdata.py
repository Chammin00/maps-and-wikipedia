"""End-to-end verification tests for GeoNames, Natural Earth, Wikipedia, and ReferenceHub."""

from __future__ import annotations

import json
from pathlib import Path
import sys

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from refdata import GeoDatabase, ReferenceHub, WikipediaArchive, WorldMap  # noqa: E402


def test_geo_search_austin() -> None:
    with GeoDatabase() as geo:
        results = geo.search_places("Austin", country_code="US", feature_class="P", limit=5)
        assert results, "Expected GeoNames search results for Austin in US"
        top = results[0]
        assert top["name"] == "Austin"
        assert top["country_code"] == "US"
        assert top["admin1_code"] == "TX"
        assert 30.0 <= top["latitude"] <= 30.6
        assert -98.1 <= top["longitude"] <= -97.4


def test_geo_reverse_geocode_austin_and_tokyo() -> None:
    with GeoDatabase() as geo:
        austin_hits = geo.reverse_geocode(
            30.2672, -97.7431, radius_km=15.0, feature_class="P", min_population=100_000, limit=5
        )
        assert austin_hits
        assert any(h["name"] == "Austin" and h["distance_km"] < 5.0 for h in austin_hits)

        tokyo_hits = geo.reverse_geocode(
            35.6762, 139.6503, radius_km=25.0, feature_class="P", min_population=500_000, limit=5
        )
        assert tokyo_hits
        assert any("Tokyo" in h["name"] or "Tokyo" in h["asciiname"] for h in tokyo_hits)


def test_geo_bbox_and_country() -> None:
    with GeoDatabase() as geo:
        us = geo.get_country("US")
        assert us is not None
        assert us["iso3"] == "USA"
        assert "United States" in us["name"]

        us_by_iso3 = geo.get_country("usa")
        assert us_by_iso3 is not None
        assert us_by_iso3["iso"] == "US"

        bbox_places = geo.places_in_bbox(
            30.1, 30.5, -98.0, -97.5, feature_class="P", min_population=10_000, limit=10
        )
        assert bbox_places
        assert any(p["name"] == "Austin" for p in bbox_places)


def test_world_map_layers_and_rasters() -> None:
    with WorldMap() as wm:
        layers = wm.list_layers()
        assert len(layers) > 50
        layer_names = {lyr["table_name"] for lyr in layers}
        assert "ne_10m_admin_0_countries" in layer_names
        assert "ne_10m_admin_1_states_provinces" in layer_names
        assert "ne_10m_time_zones" in layer_names

        rasters = wm.list_rasters()
        assert len(rasters) >= 3
        assert all(Path(r["path"]).exists() and r["size_bytes"] > 0 for r in rasters)


def test_world_map_locate_austin_and_paris() -> None:
    with WorldMap() as wm:
        austin = wm.locate(30.2672, -97.7431)
        assert austin["country"] is not None
        assert austin["country"]["iso_a2"] == "US"
        assert "United States" in austin["country"]["name"]
        assert austin["admin1"] is not None
        assert austin["admin1"]["name"] == "Texas"
        assert austin["timezone"] is not None
        assert austin["timezone"]["tz_name1st"] == "America/Chicago" or austin["timezone"]["zone"] == -6.0

        paris = wm.locate(48.8566, 2.3522)
        assert paris["country"] is not None
        assert paris["country"]["iso_a3_eh"] == "FRA" or paris["country"]["admin"] == "France"
        assert paris["admin1"] is not None
        assert paris["admin1"]["name"] == "Paris"


def test_world_map_export_geojson() -> None:
    with WorldMap() as wm:
        fc = wm.export_geojson("ne_10m_admin_0_countries", limit=3)
        assert fc["type"] == "FeatureCollection"
        assert len(fc["features"]) == 3
        for feat in fc["features"]:
            assert feat["type"] == "Feature"
            assert feat["geometry"] is not None
            assert "NAME" in feat["properties"]


def test_wiki_stats_search_suggest_and_article() -> None:
    with WikipediaArchive() as wiki:
        st = wiki.stats()
        assert st["all_entry_count"] > 6_000_000
        assert st["article_count"] > 5_000_000
        assert st["has_fulltext_index"] is True
        assert st["has_title_index"] is True

        suggestions = wiki.suggest("Global Positioning", limit=5)
        assert suggestions
        assert any("Global Positioning" in s["title"] for s in suggestions)

        hits = wiki.search("Global Positioning System", limit=5)
        assert hits

        gps_art = wiki.get_article("Global Positioning System")
        assert gps_art is not None
        assert gps_art["title"] == "Global Positioning System"
        assert len(gps_art["summary"]) > 40
        assert len(gps_art["text"]) > 500

        # Redirect resolution test
        redirect_art = wiki.get_article("USA")
        assert redirect_art is not None
        assert "United States" in redirect_art["title"]


def test_hub_describe_location_end_to_end() -> None:
    with ReferenceHub() as hub:
        info = hub.describe_location(30.2672, -97.7431)
        assert info["map_region"]["country"]["iso_a2"] == "US"
        assert info["map_region"]["admin1"]["name"] == "Texas"
        assert info["nearest_populated_places"]
        assert info["nearest_populated_places"][0]["name"] == "Austin"
        assert info["wikipedia"] is not None
        assert "Austin" in info["wikipedia"]["title"]
        assert len(info["wikipedia"]["summary"]) > 40


def test_manifest_files_exist() -> None:
    manifest_path = WORKSPACE_ROOT / "manifest.json"
    assert manifest_path.exists(), "manifest.json must exist"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    datasets = manifest["datasets"]

    wiki = datasets["wikipedia"]
    assert wiki["status"] == "ready"
    assert wiki["sha256_verified"] is True
    assert Path(wiki["path"]).exists()

    geo = datasets["geonames"]
    assert geo["status"] == "ready"
    assert Path(geo["sqlite_path"]).exists()
    assert geo["table_counts"]["places"] > 12_000_000

    ne = datasets["natural_earth"]
    assert ne["vector_geopackage"]["status"] == "ready"
    assert Path(ne["vector_geopackage"]["path"]).exists()
    assert ne["rasters"]["status"] == "ready"
    assert ne["rasters"]["count"] >= 3
    for r in ne["rasters"]["files"]:
        assert Path(r["path"]).exists()
