#!/usr/bin/env python3
"""Inspect F:\\ReferenceData and write manifest.json into the workspace hub."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from refdata.config import (
    DEFAULT_ZIM_NAME,
    get_geonames_db_path,
    get_natural_earth_gpkg_path,
    get_raster_paths,
    get_wikipedia_zim_path,
)


def compute_sha256(path: Path) -> str:
    with open(path, "rb") as f:
        if hasattr(hashlib, "file_digest"):
            return hashlib.file_digest(f, "sha256").hexdigest()
        h = hashlib.sha256()
        while True:
            buf = f.read(8 * 1024 * 1024)
            if not buf:
                break
            h.update(buf)
        return h.hexdigest()


def inspect_wikipedia(data_root: Path, *, rehash: bool = False) -> dict:
    wiki_dir = data_root / "wikipedia"
    zim_path = get_wikipedia_zim_path(data_root)
    sha_file = wiki_dir / f"{zim_path.name}.sha256"
    if not sha_file.exists():
        sha_file = wiki_dir / f"{DEFAULT_ZIM_NAME}.sha256"

    expected_sha256 = None
    if sha_file.exists():
        expected_sha256 = sha_file.read_text(encoding="utf-8").strip().split()[0].lower()

    info: dict = {
        "source_url": f"https://download.kiwix.org/zim/wikipedia/{zim_path.name}",
        "path": str(zim_path),
        "exists": zim_path.exists(),
        "expected_sha256": expected_sha256,
    }

    if zim_path.exists():
        info["size_bytes"] = zim_path.stat().st_size
        if rehash:
            actual = compute_sha256(zim_path)
            info["sha256"] = actual
            info["sha256_verified"] = (
                actual == expected_sha256 if expected_sha256 else None
            )
        else:
            part_exists = (wiki_dir / f"{zim_path.name}.part").exists()
            info["sha256"] = expected_sha256
            info["sha256_verified"] = bool(expected_sha256 and not part_exists)

        try:
            from libzim.reader import Archive

            arc = Archive(str(zim_path))
            info["article_count"] = arc.article_count
            info["all_entry_count"] = arc.all_entry_count
            info["media_count"] = arc.media_count
            info["has_fulltext_index"] = arc.has_fulltext_index
            info["has_title_index"] = arc.has_title_index
            info["status"] = "ready"
        except Exception as exc:
            info["status"] = f"error: {exc}"
    else:
        part = wiki_dir / f"{zim_path.name}.part"
        info["status"] = "downloading" if part.exists() else "missing"
        if part.exists():
            info["partial_size_bytes"] = part.stat().st_size

    return info


def inspect_geonames(data_root: Path) -> dict:
    geo_dir = data_root / "geonames"
    db_path = get_geonames_db_path(data_root)
    raw_files = {}
    if geo_dir.exists():
        for p in sorted(geo_dir.iterdir()):
            if p.is_file() and not p.name.endswith(".part"):
                raw_files[p.name] = {
                    "path": str(p),
                    "size_bytes": p.stat().st_size,
                }

    info: dict = {
        "source_url": "https://download.geonames.org/export/dump/",
        "raw_dir": str(geo_dir),
        "raw_files": raw_files,
        "sqlite_path": str(db_path),
        "sqlite_exists": db_path.exists(),
    }
    if db_path.exists():
        info["sqlite_size_bytes"] = db_path.stat().st_size
        conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            info["table_counts"] = {
                "places": conn.execute("SELECT COUNT(*) FROM places").fetchone()[0],
                "places_rtree": conn.execute("SELECT COUNT(*) FROM places_rtree").fetchone()[0],
                "countries": conn.execute("SELECT COUNT(*) FROM countries").fetchone()[0],
                "admin1": conn.execute("SELECT COUNT(*) FROM admin1").fetchone()[0],
                "admin2": conn.execute("SELECT COUNT(*) FROM admin2").fetchone()[0],
                "timezones": conn.execute("SELECT COUNT(*) FROM timezones").fetchone()[0],
                "feature_codes": conn.execute("SELECT COUNT(*) FROM feature_codes").fetchone()[0],
            }
            info["status"] = "ready"
        finally:
            conn.close()
    else:
        info["status"] = "raw_only" if raw_files else "missing"
    return info


def inspect_natural_earth(data_root: Path) -> dict:
    gpkg_path = get_natural_earth_gpkg_path(data_root)
    rasters = get_raster_paths(data_root)

    vec_info: dict = {
        "source_url": "https://naciscdn.org/naturalearth/packages/natural_earth_vector.gpkg.zip",
        "path": str(gpkg_path),
        "exists": gpkg_path.exists(),
    }
    if gpkg_path.exists():
        vec_info["size_bytes"] = gpkg_path.stat().st_size
        conn = sqlite3.connect(f"file:{gpkg_path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            layers = conn.execute(
                "SELECT table_name, data_type FROM gpkg_contents ORDER BY table_name"
            ).fetchall()
            vec_info["layer_count"] = len(layers)
            vec_info["layers"] = [{"table_name": r[0], "data_type": r[1]} for r in layers]
            vec_info["status"] = "ready"
        finally:
            conn.close()
    else:
        vec_info["status"] = "missing"

    return {
        "vector_geopackage": vec_info,
        "rasters": {
            "root": str(data_root / "natural_earth" / "raster"),
            "count": len(rasters),
            "files": rasters,
            "status": "ready" if rasters else "missing",
        },
    }


def write_manifest(data_root: Path, output_path: Path, *, rehash: bool = False) -> Path:
    manifest = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_root": str(data_root),
        "workspace_root": str(WORKSPACE_ROOT),
        "datasets": {
            "wikipedia": inspect_wikipedia(data_root, rehash=rehash),
            "geonames": inspect_geonames(data_root),
            "natural_earth": inspect_natural_earth(data_root),
        },
    }
    tmp = output_path.with_name(output_path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, output_path)
    print(f"[manifest] Wrote {output_path}")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect F:\\ReferenceData and generate workspace manifest.json."
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(r"F:\ReferenceData"),
        help="ReferenceData root directory (default: F:\\ReferenceData)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=WORKSPACE_ROOT / "manifest.json",
        help="Manifest output path",
    )
    parser.add_argument(
        "--rehash",
        action="store_true",
        help="Recompute full SHA-256 hash of the ~49 GiB Wikipedia ZIM file",
    )
    args = parser.parse_args()
    write_manifest(args.data_root, args.output, rehash=args.rehash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
