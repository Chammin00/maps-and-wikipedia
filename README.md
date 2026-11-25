# Offline Wikipedia, World Maps & GPS Reference Hub

Centralized offline knowledge base, global vector/raster maps, and GPS place index stored on `F:\ReferenceData\` with a lightweight Python toolkit and manifest in this workspace.

## Storage Architecture

- **Large Archives & Databases (`F:\ReferenceData\`):**
  - `F:\ReferenceData\wikipedia\wikipedia_en_all_nopic_2026-06.zim` — Full English Wikipedia (no images, all ~7M+ articles, Kiwix ZIM with full-text and title indexes)
  - `F:\ReferenceData\databases\geonames.sqlite` — Compiled GeoNames global database (12M+ places) with SQLite **R-Tree** spatial index and **FTS5** full-text search
  - `F:\ReferenceData\geonames\` — Raw GeoNames dump files (`allCountries.zip`, `cities500.zip`, `countryInfo.txt`, `admin1CodesASCII.txt`, `admin2Codes.txt`, `timeZones.txt`, `featureCodes_en.txt`)
  - `F:\ReferenceData\natural_earth\vector\natural_earth_vector.gpkg` — Natural Earth complete vector GeoPackage (1:10m, 1:50m, 1:110m countries, states/provinces, time zones, coastlines, populated places)
  - `F:\ReferenceData\natural_earth\raster\` — Natural Earth GeoTIFF basemaps (`NE1_HR_LC_SR_W_DR`, `HYP_HR_SR_OB_DR`, `NE1_50M_SR_W`)
- **Workspace Hub (`g:\My Drive\Antigravity\Projects\Maps and Wikepedia\`):**
  - `manifest.json` — Machine-readable inventory, file paths, sizes, hashes, and counts
  - `refdata/` — Reusable Python package (`GeoDatabase`, `WorldMap`, `WikipediaArchive`, `ReferenceHub`)
  - `scripts/` — Downloader, GeoNames SQLite builder, and manifest generator
  - `tests/` — Pytest verification suite

> Set `REFERENCE_DATA_DIR` in your environment to override `F:\ReferenceData` if you ever move the data directory to another drive.

## Quick CLI Usage

Run from this directory (or add this directory to `PYTHONPATH`):

```powershell
# Check status of all datasets
python -m refdata status

# Search GeoNames places by name
python -m refdata place "Austin" --country US --feature-class P

# Reverse geocode GPS coordinates (nearest places within radius)
python -m refdata reverse 30.2672 -97.7431 --radius-km 25 --feature-class P

# Locate GPS coordinates in Natural Earth vector polygons (country, state/province, timezone)
python -m refdata map-point 30.2672 -97.7431

# List Natural Earth GeoPackage layers and GeoTIFF rasters
python -m refdata map-layers

# Full-text search / autocomplete / read offline Wikipedia
python -m refdata wiki-search "Global Positioning System"
python -m refdata wiki-suggest "Global Positioning"
python -m refdata wiki-get "Global Positioning System"

# Unified GPS lookup (Natural Earth + GeoNames + Wikipedia summary)
python -m refdata describe-gps 30.2672 -97.7431
```

## Python Usage in Future Projects

```python
import sys
sys.path.insert(0, r"g:\My Drive\Antigravity\Projects\Maps and Wikepedia")

from refdata import ReferenceHub

with ReferenceHub() as hub:
    # 1. Unified GPS context (Natural Earth polygons + GeoNames + Wikipedia)
    info = hub.describe_location(30.2672, -97.7431)
    print(info["map_region"]["country"]["name"])
    print(info["nearest_populated_places"][0])
    print(info["wikipedia"]["summary"])

    # 2. Direct GeoNames queries
    cities = hub.geo.search_places("Tokyo", feature_class="P", limit=5)
    nearby = hub.geo.reverse_geocode(35.6762, 139.6503, radius_km=15.0)

    # 3. Direct Natural Earth GeoJSON export
    geojson = hub.world_map.export_geojson(
        "ne_10m_admin_0_countries",
        where="ISO_A2 = ?",
        params=("US",),
    )

    # 4. Direct offline Wikipedia lookup
    article = hub.wiki.get_article("Global Positioning System")
    hits = hub.wiki.search("Great Circle Distance", limit=5)
```

## Maintenance Scripts

```powershell
# Resume or verify downloads
python scripts\download_datasets.py --maps
python scripts\download_datasets.py --wikipedia

# Rebuild GeoNames SQLite database from raw dumps
python scripts\build_geonames_db.py

# Refresh manifest.json (add --rehash to recompute full 49 GiB ZIM SHA-256)
python scripts\update_manifest.py

# Run verification tests
pytest -v tests\test_refdata.py
```
