"""Configuration and dataset path resolution for the offline ReferenceData hub."""

from __future__ import annotations

import json
import os
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = Path(r"F:\ReferenceData")
MANIFEST_PATH = WORKSPACE_ROOT / "manifest.json"
DEFAULT_ZIM_NAME = "wikipedia_en_all_nopic_2026-06.zim"


def get_data_root() -> Path:
    """Resolve the root directory containing downloaded reference datasets."""
    env_dir = os.environ.get("REFERENCE_DATA_DIR")
    if env_dir:
        return Path(env_dir)

    if MANIFEST_PATH.exists():
        try:
            data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            manifest_root = data.get("data_root")
            if manifest_root:
                return Path(manifest_root)
        except Exception:
            pass

    return DEFAULT_DATA_ROOT


def get_geonames_db_path(data_root: Path | None = None) -> Path:
    """Return path to the compiled GeoNames SQLite database."""
    root = data_root or get_data_root()
    return root / "databases" / "geonames.sqlite"


def get_natural_earth_gpkg_path(data_root: Path | None = None) -> Path:
    """Return path to the Natural Earth vector GeoPackage."""
    root = data_root or get_data_root()
    return root / "natural_earth" / "vector" / "natural_earth_vector.gpkg"


def get_wikipedia_zim_path(data_root: Path | None = None) -> Path:
    """Return path to the English Wikipedia ZIM archive (latest nopic match if present)."""
    root = data_root or get_data_root()
    wiki_dir = root / "wikipedia"
    preferred = wiki_dir / DEFAULT_ZIM_NAME
    if preferred.exists():
        return preferred

    candidates = sorted(wiki_dir.glob("wikipedia_en_all_nopic_*.zim"))
    if candidates:
        return candidates[-1]

    any_zim = sorted(wiki_dir.glob("*.zim"))
    if any_zim:
        return any_zim[-1]

    return preferred


def get_raster_paths(data_root: Path | None = None) -> list[dict[str, str | int]]:
    """Discover extracted Natural Earth GeoTIFF raster files."""
    root = data_root or get_data_root()
    raster_dir = root / "natural_earth" / "raster"
    if not raster_dir.exists():
        return []

    results: list[dict[str, str | int]] = []
    for tif in sorted(raster_dir.rglob("*.tif")):
        try:
            size_bytes = tif.stat().st_size
        except OSError:
            size_bytes = 0
        results.append(
            {
                "dataset": tif.parent.name,
                "name": tif.name,
                "path": str(tif),
                "size_bytes": size_bytes,
            }
        )
    return results
