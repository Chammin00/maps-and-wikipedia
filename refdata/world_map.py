"""Offline Natural Earth vector GeoPackage & raster map query toolkit."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

import shapely
from shapely.geometry import Point, box, mapping

from .config import get_data_root, get_natural_earth_gpkg_path, get_raster_paths

_ENV_SIZES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}

COUNTRY_LAYER = "ne_10m_admin_0_countries"
ADMIN1_LAYER = "ne_10m_admin_1_states_provinces"
TIMEZONE_LAYER = "ne_10m_time_zones"


def _gpkg_blob_to_shapely(blob: bytes | memoryview | None) -> shapely.Geometry | None:
    """Decode a GeoPackage StandardBinary geometry blob into a Shapely geometry."""
    if blob is None:
        return None
    raw = bytes(blob)
    if len(raw) < 8:
        return None
    if raw[:2] != b"GP":
        raise ValueError("Invalid GeoPackage geometry header (missing 'GP' magic)")

    flags = raw[3]
    is_empty = bool((flags >> 4) & 0x01)
    if is_empty:
        return None

    env_indicator = (flags >> 1) & 0x07
    if env_indicator not in _ENV_SIZES:
        raise ValueError(f"Unsupported GeoPackage envelope indicator: {env_indicator}")

    wkb_offset = 8 + _ENV_SIZES[env_indicator]
    if wkb_offset >= len(raw):
        return None
    return shapely.from_wkb(raw[wkb_offset:])


class WorldMap:
    """Read-only spatial query interface over Natural Earth vector GeoPackage & rasters."""

    def __init__(
        self,
        gpkg_path: Path | str | None = None,
        *,
        data_root: Path | str | None = None,
    ) -> None:
        self.data_root = Path(data_root) if data_root else get_data_root()
        self.gpkg_path = (
            Path(gpkg_path) if gpkg_path else get_natural_earth_gpkg_path(self.data_root)
        )
        if not self.gpkg_path.exists():
            raise FileNotFoundError(
                f"Natural Earth GeoPackage not found: {self.gpkg_path}"
            )
        uri = f"file:{self.gpkg_path.resolve().as_posix()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._layer_meta_cache: dict[str, dict[str, Any]] | None = None

    def close(self) -> None:
        if getattr(self, "conn", None) is not None:
            self.conn.close()
            self.conn = None  # type: ignore[assignment]

    def __enter__(self) -> "WorldMap":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _load_layer_meta(self) -> dict[str, dict[str, Any]]:
        if self._layer_meta_cache is not None:
            return self._layer_meta_cache

        rows = self.conn.execute(
            """
            SELECT
                c.table_name,
                c.data_type,
                c.identifier,
                c.description,
                c.min_x,
                c.min_y,
                c.max_x,
                c.max_y,
                g.column_name AS geom_column,
                g.geometry_type_name,
                g.srs_id
            FROM gpkg_contents c
            LEFT JOIN gpkg_geometry_columns g ON g.table_name = c.table_name
            ORDER BY c.table_name
            """
        ).fetchall()

        meta: dict[str, dict[str, Any]] = {}
        for row in rows:
            tname = row["table_name"]
            geom_col = row["geom_column"]
            rtree_name = f"rtree_{tname}_{geom_col}" if geom_col else None
            has_rtree = False
            if rtree_name:
                has_rtree = bool(
                    self.conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = ?", (rtree_name,)
                    ).fetchone()
                )
            d = dict(row)
            d["rtree_table"] = rtree_name if has_rtree else None
            meta[tname] = d

        self._layer_meta_cache = meta
        return meta

    def list_layers(self) -> list[dict[str, Any]]:
        """Return metadata for all layers inside the Natural Earth GeoPackage."""
        return list(self._load_layer_meta().values())

    def _require_layer(self, layer_name: str) -> dict[str, Any]:
        meta = self._load_layer_meta()
        if layer_name not in meta:
            raise ValueError(f"Unknown GeoPackage layer: {layer_name}")
        return meta[layer_name]

    def _query_point_in_layer(
        self, layer_name: str, lat: float, lon: float
    ) -> list[dict[str, Any]]:
        """Return feature attribute dicts in layer_name covering (lat, lon)."""
        layer = self._require_layer(layer_name)
        geom_col = layer["geom_column"]
        if not geom_col:
            return []
        rtree = layer["rtree_table"]

        if rtree:
            sql = f"""
                SELECT t.*
                FROM "{layer_name}" t
                JOIN "{rtree}" rt ON t.rowid = rt.id
                WHERE rt.minx <= ? AND rt.maxx >= ?
                  AND rt.miny <= ? AND rt.maxy >= ?
            """
            rows = self.conn.execute(sql, (lon, lon, lat, lat)).fetchall()
        else:
            sql = f'SELECT * FROM "{layer_name}"'
            rows = self.conn.execute(sql).fetchall()

        pt = Point(lon, lat)
        matches: list[dict[str, Any]] = []
        for row in rows:
            geom = _gpkg_blob_to_shapely(row[geom_col])
            if geom is not None and geom.covers(pt):
                attrs = {k: row[k] for k in row.keys() if k != geom_col}
                matches.append(attrs)
        return matches

    def point_in_country(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Identify the Natural Earth 1:10m country polygon covering (lat, lon)."""
        matches = self._query_point_in_layer(COUNTRY_LAYER, lat, lon)
        if not matches:
            return None
        row = matches[0]
        return {
            "name": row.get("NAME"),
            "name_long": row.get("NAME_LONG"),
            "admin": row.get("ADMIN"),
            "formal_en": row.get("FORMAL_EN"),
            "iso_a2": row.get("ISO_A2"),
            "iso_a2_eh": row.get("ISO_A2_EH"),
            "iso_a3": row.get("ISO_A3"),
            "iso_a3_eh": row.get("ISO_A3_EH"),
            "iso_n3": row.get("ISO_N3"),
            "continent": row.get("CONTINENT"),
            "region_un": row.get("REGION_UN"),
            "subregion": row.get("SUBREGION"),
            "sovereignt": row.get("SOVEREIGNT"),
            "pop_est": row.get("POP_EST"),
            "wikidataid": row.get("WIKIDATAID"),
        }

    def point_in_admin1(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Identify the Natural Earth 1:10m state/province polygon covering (lat, lon)."""
        matches = self._query_point_in_layer(ADMIN1_LAYER, lat, lon)
        if not matches:
            return None
        row = matches[0]
        return {
            "name": row.get("name"),
            "name_en": row.get("name_en"),
            "type": row.get("type"),
            "type_en": row.get("type_en"),
            "iso_3166_2": row.get("iso_3166_2"),
            "iso_a2": row.get("iso_a2"),
            "adm1_code": row.get("adm1_code"),
            "postal": row.get("postal"),
            "region": row.get("region"),
            "admin": row.get("admin"),
            "adm0_a3": row.get("adm0_a3"),
            "gn_id": row.get("gn_id"),
            "gn_a1_code": row.get("gn_a1_code"),
            "wikidataid": row.get("wikidataid"),
        }

    def point_in_timezone(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Identify the Natural Earth 1:10m time zone polygon covering (lat, lon)."""
        matches = self._query_point_in_layer(TIMEZONE_LAYER, lat, lon)
        if not matches:
            return None
        row = matches[0]
        return {
            "name": row.get("name"),
            "zone": row.get("zone"),
            "utc_format": row.get("utc_format"),
            "time_zone": row.get("time_zone"),
            "tz_name1st": row.get("tz_name1st"),
            "places": row.get("places"),
            "dst_places": row.get("dst_places"),
        }

    def locate(self, lat: float, lon: float) -> dict[str, Any]:
        """Return country, admin1 state/province, and timezone for (lat, lon)."""
        return {
            "lat": lat,
            "lon": lon,
            "country": self.point_in_country(lat, lon),
            "admin1": self.point_in_admin1(lat, lon),
            "timezone": self.point_in_timezone(lat, lon),
        }

    def export_geojson(
        self,
        layer_name: str,
        *,
        where: str | None = None,
        params: tuple[Any, ...] = (),
        bbox: tuple[float, float, float, float] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Export features from a GeoPackage layer as a GeoJSON FeatureCollection.

        bbox is (min_lon, min_lat, max_lon, max_lat) in WGS84 degrees.
        """
        layer = self._require_layer(layer_name)
        geom_col = layer["geom_column"]
        rtree = layer["rtree_table"]

        joins = ""
        clauses: list[str] = []
        query_params: list[Any] = []

        bbox_geom = None
        if bbox is not None:
            min_lon, min_lat, max_lon, max_lat = bbox
            bbox_geom = box(min_lon, min_lat, max_lon, max_lat)
            if rtree:
                joins = f' JOIN "{rtree}" rt ON t.rowid = rt.id'
                clauses.append(
                    "rt.maxx >= ? AND rt.minx <= ? AND rt.maxy >= ? AND rt.miny <= ?"
                )
                query_params.extend([min_lon, max_lon, min_lat, max_lat])

        if where:
            clauses.append(f"({where})")
            query_params.extend(params)

        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit_sql = f" LIMIT {int(limit)}" if (limit is not None and (bbox_geom is None or rtree)) else ""

        sql = f'SELECT t.* FROM "{layer_name}" t{joins}{where_sql}{limit_sql}'
        rows = self.conn.execute(sql, query_params).fetchall()

        features: list[dict[str, Any]] = []
        for row in rows:
            geom = _gpkg_blob_to_shapely(row[geom_col]) if geom_col else None
            if bbox_geom is not None and not rtree:
                if geom is None or not geom.intersects(bbox_geom):
                    continue
            props = {k: row[k] for k in row.keys() if k != geom_col}
            features.append(
                {
                    "type": "Feature",
                    "id": props.get("fid"),
                    "geometry": mapping(geom) if geom is not None else None,
                    "properties": props,
                }
            )
            if limit is not None and len(features) >= limit:
                break

        return {
            "type": "FeatureCollection",
            "name": layer_name,
            "features": features,
        }

    def list_rasters(self) -> list[dict[str, Any]]:
        """List available Natural Earth GeoTIFF basemaps on disk."""
        return get_raster_paths(self.data_root)
