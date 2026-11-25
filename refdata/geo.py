"""Offline GeoNames spatial and full-text GPS reference database."""

from __future__ import annotations

import math
from pathlib import Path
import re
import sqlite3
from typing import Any

from .config import get_geonames_db_path

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in kilometers between two WGS84 points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute initial compass bearing in degrees [0, 360) from point 1 to point 2."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _sanitize_fts_query(query: str) -> str:
    """Convert raw user text into a safe FTS5 prefix-AND query."""
    tokens = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    if not tokens:
        return ""
    return " AND ".join(f'"{tok}"*' for tok in tokens)


def _format_place_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "geoname_id": row["geoname_id"],
        "name": row["name"],
        "asciiname": row["asciiname"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "feature_class": row["feature_class"],
        "feature_code": row["feature_code"],
        "feature_name": row["feature_name"],
        "country_code": row["country_code"],
        "country_name": row["country_name"],
        "admin1_code": row["admin1_code"],
        "admin1_name": row["admin1_name"],
        "admin2_code": row["admin2_code"],
        "admin2_name": row["admin2_name"],
        "population": row["population"],
        "elevation": row["elevation"],
        "dem": row["dem"],
        "timezone": row["timezone"],
        "modification_date": row["modification_date"],
    }


_BASE_PLACE_SELECT = """
    SELECT
        p.geoname_id,
        p.name,
        p.asciiname,
        p.latitude,
        p.longitude,
        p.feature_class,
        p.feature_code,
        fc.name AS feature_name,
        p.country_code,
        c.name AS country_name,
        p.admin1_code,
        a1.name AS admin1_name,
        p.admin2_code,
        a2.name AS admin2_name,
        p.population,
        p.elevation,
        p.dem,
        p.timezone,
        p.modification_date
    FROM places p
    LEFT JOIN countries c ON c.iso = p.country_code
    LEFT JOIN admin1 a1 ON a1.code = (p.country_code || '.' || p.admin1_code)
    LEFT JOIN admin2 a2 ON a2.code = (p.country_code || '.' || p.admin1_code || '.' || p.admin2_code)
    LEFT JOIN feature_codes fc ON fc.code = (p.feature_class || '.' || p.feature_code)
"""


class GeoDatabase:
    """Read-only interface to the offline GeoNames SQLite database."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else get_geonames_db_path()
        if not self.db_path.exists():
            raise FileNotFoundError(f"GeoNames SQLite DB not found: {self.db_path}")
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if getattr(self, "conn", None) is not None:
            self.conn.close()
            self.conn = None  # type: ignore[assignment]

    def __enter__(self) -> "GeoDatabase":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def search_places(
        self,
        query: str,
        *,
        country_code: str | None = None,
        feature_class: str | None = None,
        min_population: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Full-text search places by name/asciiname ranked by exact match and population."""
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []

        where_clauses = ["places_fts MATCH ?"]
        params: list[Any] = [fts_query]

        if country_code:
            where_clauses.append("p.country_code = ?")
            params.append(country_code.upper())
        if feature_class:
            where_clauses.append("p.feature_class = ?")
            params.append(feature_class.upper())
        if min_population > 0:
            where_clauses.append("p.population >= ?")
            params.append(int(min_population))

        q_lower = query.strip().lower()
        sql = f"""
            SELECT
                p.geoname_id,
                p.name,
                p.asciiname,
                p.latitude,
                p.longitude,
                p.feature_class,
                p.feature_code,
                fc.name AS feature_name,
                p.country_code,
                c.name AS country_name,
                p.admin1_code,
                a1.name AS admin1_name,
                p.admin2_code,
                a2.name AS admin2_name,
                p.population,
                p.elevation,
                p.dem,
                p.timezone,
                p.modification_date
            FROM places_fts
            JOIN places p ON p.geoname_id = places_fts.rowid
            LEFT JOIN countries c ON c.iso = p.country_code
            LEFT JOIN admin1 a1 ON a1.code = (p.country_code || '.' || p.admin1_code)
            LEFT JOIN admin2 a2 ON a2.code = (p.country_code || '.' || p.admin1_code || '.' || p.admin2_code)
            LEFT JOIN feature_codes fc ON fc.code = (p.feature_class || '.' || p.feature_code)
            WHERE {' AND '.join(where_clauses)}
            ORDER BY
                CASE WHEN lower(p.name) = ? OR lower(p.asciiname) = ? THEN 0 ELSE 1 END,
                COALESCE(p.population, 0) DESC,
                bm25(places_fts)
            LIMIT ?
        """
        params.extend([q_lower, q_lower, int(limit)])
        rows = self.conn.execute(sql, params).fetchall()
        return [_format_place_row(r) for r in rows]

    def reverse_geocode(
        self,
        lat: float,
        lon: float,
        *,
        radius_km: float = 50.0,
        feature_class: str | None = None,
        min_population: int = 0,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Find nearest GeoNames features within radius_km of (lat, lon)."""
        lat_delta = radius_km / 111.045
        min_lat = max(-90.0, lat - lat_delta)
        max_lat = min(90.0, lat + lat_delta)

        cos_lat = max(math.cos(math.radians(min(abs(lat), 89.9))), 0.01)
        lon_delta = min(180.0, radius_km / (111.320 * cos_lat))

        raw_min_lon = lon - lon_delta
        raw_max_lon = lon + lon_delta
        if raw_min_lon < -180.0:
            lon_boxes = [(raw_min_lon + 360.0, 180.0), (-180.0, raw_max_lon)]
        elif raw_max_lon > 180.0:
            lon_boxes = [(raw_min_lon, 180.0), (-180.0, raw_max_lon - 360.0)]
        else:
            lon_boxes = [(raw_min_lon, raw_max_lon)]

        extra_where: list[str] = []
        extra_params: list[Any] = []
        if feature_class:
            extra_where.append("p.feature_class = ?")
            extra_params.append(feature_class.upper())
        if min_population > 0:
            extra_where.append("p.population >= ?")
            extra_params.append(int(min_population))

        extra_sql = (" AND " + " AND ".join(extra_where)) if extra_where else ""
        candidates_by_id: dict[int, dict[str, Any]] = {}

        for box_min_lon, box_max_lon in lon_boxes:
            sql = f"""
                SELECT
                    p.geoname_id,
                    p.name,
                    p.asciiname,
                    p.latitude,
                    p.longitude,
                    p.feature_class,
                    p.feature_code,
                    fc.name AS feature_name,
                    p.country_code,
                    c.name AS country_name,
                    p.admin1_code,
                    a1.name AS admin1_name,
                    p.admin2_code,
                    a2.name AS admin2_name,
                    p.population,
                    p.elevation,
                    p.dem,
                    p.timezone,
                    p.modification_date
                FROM places_rtree rt
                JOIN places p ON p.geoname_id = rt.id
                LEFT JOIN countries c ON c.iso = p.country_code
                LEFT JOIN admin1 a1 ON a1.code = (p.country_code || '.' || p.admin1_code)
                LEFT JOIN admin2 a2 ON a2.code = (p.country_code || '.' || p.admin1_code || '.' || p.admin2_code)
                LEFT JOIN feature_codes fc ON fc.code = (p.feature_class || '.' || p.feature_code)
                WHERE rt.min_lat >= ? AND rt.max_lat <= ?
                  AND rt.min_lon >= ? AND rt.max_lon <= ?
                  {extra_sql}
            """
            params = [min_lat, max_lat, box_min_lon, box_max_lon, *extra_params]
            for row in self.conn.execute(sql, params):
                gid = row["geoname_id"]
                if gid in candidates_by_id:
                    continue
                dist = haversine_km(lat, lon, row["latitude"], row["longitude"])
                if dist <= radius_km:
                    item = _format_place_row(row)
                    item["distance_km"] = round(dist, 4)
                    item["bearing_deg"] = round(
                        initial_bearing_deg(lat, lon, row["latitude"], row["longitude"]), 2
                    )
                    candidates_by_id[gid] = item

        results = sorted(
            candidates_by_id.values(),
            key=lambda x: (x["distance_km"], -(x["population"] or 0)),
        )
        return results[:limit]

    def places_in_bbox(
        self,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        *,
        feature_class: str | None = None,
        min_population: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query places inside a geographic bounding box using the R-Tree index."""
        where = [
            "rt.min_lat >= ? AND rt.max_lat <= ?",
            "rt.min_lon >= ? AND rt.max_lon <= ?",
        ]
        params: list[Any] = [min_lat, max_lat, min_lon, max_lon]
        if feature_class:
            where.append("p.feature_class = ?")
            params.append(feature_class.upper())
        if min_population > 0:
            where.append("p.population >= ?")
            params.append(int(min_population))

        sql = f"""
            SELECT
                p.geoname_id,
                p.name,
                p.asciiname,
                p.latitude,
                p.longitude,
                p.feature_class,
                p.feature_code,
                fc.name AS feature_name,
                p.country_code,
                c.name AS country_name,
                p.admin1_code,
                a1.name AS admin1_name,
                p.admin2_code,
                a2.name AS admin2_name,
                p.population,
                p.elevation,
                p.dem,
                p.timezone,
                p.modification_date
            FROM places_rtree rt
            JOIN places p ON p.geoname_id = rt.id
            LEFT JOIN countries c ON c.iso = p.country_code
            LEFT JOIN admin1 a1 ON a1.code = (p.country_code || '.' || p.admin1_code)
            LEFT JOIN admin2 a2 ON a2.code = (p.country_code || '.' || p.admin1_code || '.' || p.admin2_code)
            LEFT JOIN feature_codes fc ON fc.code = (p.feature_class || '.' || p.feature_code)
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(p.population, 0) DESC
            LIMIT ?
        """
        params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [_format_place_row(r) for r in rows]

    def get_place(self, geoname_id: int) -> dict[str, Any] | None:
        """Fetch a single place by its GeoNames ID."""
        row = self.conn.execute(
            f"{_BASE_PLACE_SELECT} WHERE p.geoname_id = ?", (int(geoname_id),)
        ).fetchone()
        return _format_place_row(row) if row else None

    def get_country(self, code: str) -> dict[str, Any] | None:
        """Fetch country metadata by ISO-2 or ISO-3 code."""
        norm = code.strip().upper()
        row = self.conn.execute(
            "SELECT * FROM countries WHERE upper(iso) = ? OR upper(iso3) = ?",
            (norm, norm),
        ).fetchone()
        return dict(row) if row else None

    def list_countries(self) -> list[dict[str, Any]]:
        """List all countries ordered by name."""
        rows = self.conn.execute("SELECT * FROM countries ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        """Return database file size and table counts."""
        places_count = self.conn.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        rtree_count = self.conn.execute("SELECT COUNT(*) FROM places_rtree").fetchone()[0]
        countries_count = self.conn.execute("SELECT COUNT(*) FROM countries").fetchone()[0]
        admin1_count = self.conn.execute("SELECT COUNT(*) FROM admin1").fetchone()[0]
        admin2_count = self.conn.execute("SELECT COUNT(*) FROM admin2").fetchone()[0]
        tz_count = self.conn.execute("SELECT COUNT(*) FROM timezones").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "size_bytes": self.db_path.stat().st_size,
            "places": places_count,
            "places_rtree": rtree_count,
            "countries": countries_count,
            "admin1": admin1_count,
            "admin2": admin2_count,
            "timezones": tz_count,
        }
