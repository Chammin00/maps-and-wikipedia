#!/usr/bin/env python3
"""Compile GeoNames dumps into an indexed SQLite database (R-Tree + FTS5)."""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import sqlite3
import sys
import time
import zipfile


def parse_int(val: str, default: int | None = None) -> int | None:
    s = val.strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return default


def parse_float(val: str, default: float | None = None) -> float | None:
    s = val.strip()
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS places_fts;
        DROP TABLE IF EXISTS places_rtree;
        DROP TABLE IF EXISTS places;
        DROP TABLE IF EXISTS feature_codes;
        DROP TABLE IF EXISTS timezones;
        DROP TABLE IF EXISTS admin2;
        DROP TABLE IF EXISTS admin1;
        DROP TABLE IF EXISTS countries;

        CREATE TABLE countries (
            iso TEXT PRIMARY KEY,
            iso3 TEXT,
            iso_numeric TEXT,
            fips TEXT,
            name TEXT,
            capital TEXT,
            area_sqkm REAL,
            population INTEGER,
            continent TEXT,
            tld TEXT,
            currency_code TEXT,
            currency_name TEXT,
            phone TEXT,
            postal_format TEXT,
            postal_regex TEXT,
            languages TEXT,
            geoname_id INTEGER,
            neighbours TEXT
        );

        CREATE TABLE admin1 (
            code TEXT PRIMARY KEY,
            name TEXT,
            asciiname TEXT,
            geoname_id INTEGER
        );

        CREATE TABLE admin2 (
            code TEXT PRIMARY KEY,
            name TEXT,
            asciiname TEXT,
            geoname_id INTEGER
        );

        CREATE TABLE timezones (
            country_code TEXT,
            timezone_id TEXT PRIMARY KEY,
            gmt_offset REAL,
            dst_offset REAL,
            raw_offset REAL
        );

        CREATE TABLE feature_codes (
            code TEXT PRIMARY KEY,
            name TEXT,
            description TEXT
        );

        CREATE TABLE places (
            geoname_id INTEGER PRIMARY KEY,
            name TEXT,
            asciiname TEXT,
            alternatenames TEXT,
            latitude REAL,
            longitude REAL,
            feature_class TEXT,
            feature_code TEXT,
            country_code TEXT,
            cc2 TEXT,
            admin1_code TEXT,
            admin2_code TEXT,
            admin3_code TEXT,
            admin4_code TEXT,
            population INTEGER,
            elevation INTEGER,
            dem INTEGER,
            timezone TEXT,
            modification_date TEXT
        );
        """
    )


def load_countries(conn: sqlite3.Connection, path: Path) -> int:
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\r\n").split("\t")
            while len(parts) < 18:
                parts.append("")
            rows.append(
                (
                    parts[0],
                    parts[1],
                    parts[2],
                    parts[3],
                    parts[4],
                    parts[5],
                    parse_float(parts[6]),
                    parse_int(parts[7], 0),
                    parts[8],
                    parts[9],
                    parts[10],
                    parts[11],
                    parts[12],
                    parts[13],
                    parts[14],
                    parts[15],
                    parse_int(parts[16]),
                    parts[17],
                )
            )
    conn.executemany(
        """
        INSERT OR REPLACE INTO countries VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        rows,
    )
    return len(rows)


def load_admin_table(conn: sqlite3.Connection, path: Path, table: str) -> int:
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            parts = line.rstrip("\r\n").split("\t")
            while len(parts) < 4:
                parts.append("")
            rows.append((parts[0], parts[1], parts[2], parse_int(parts[3])))
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def load_timezones(conn: sqlite3.Connection, path: Path) -> int:
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f):
            if idx == 0 and line.lower().startswith("countrycode"):
                continue
            if not line.strip():
                continue
            parts = line.rstrip("\r\n").split("\t")
            while len(parts) < 5:
                parts.append("")
            rows.append(
                (
                    parts[0],
                    parts[1],
                    parse_float(parts[2]),
                    parse_float(parts[3]),
                    parse_float(parts[4]),
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO timezones VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def load_feature_codes(conn: sqlite3.Connection, path: Path) -> int:
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            parts = line.rstrip("\r\n").split("\t")
            while len(parts) < 3:
                parts.append("")
            rows.append((parts[0], parts[1], parts[2]))
    conn.executemany(
        "INSERT OR REPLACE INTO feature_codes VALUES (?, ?, ?)",
        rows,
    )
    return len(rows)


def load_places_from_zip(
    conn: sqlite3.Connection, zip_path: Path, batch_size: int = 50_000
) -> int:
    total = 0
    skipped = 0
    batch: list[tuple] = []
    t0 = time.monotonic()

    insert_sql = """
        INSERT INTO places VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """

    with zipfile.ZipFile(zip_path, "r") as zf:
        txt_members = [m for m in zf.namelist() if m.lower().endswith(".txt")]
        if not txt_members:
            raise RuntimeError(f"No .txt member found inside {zip_path}")
        member_name = "allCountries.txt" if "allCountries.txt" in txt_members else txt_members[0]
        with zf.open(member_name, "r") as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
            for line in stream:
                if not line:
                    continue
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) != 19:
                    skipped += 1
                    continue
                gid = parse_int(parts[0])
                lat = parse_float(parts[4])
                lon = parse_float(parts[5])
                if gid is None:
                    skipped += 1
                    continue
                batch.append(
                    (
                        gid,
                        parts[1],
                        parts[2],
                        parts[3],
                        lat,
                        lon,
                        parts[6],
                        parts[7],
                        parts[8],
                        parts[9],
                        parts[10],
                        parts[11],
                        parts[12],
                        parts[13],
                        parse_int(parts[14], 0),
                        parse_int(parts[15]),
                        parse_int(parts[16]),
                        parts[17],
                        parts[18],
                    )
                )
                if len(batch) >= batch_size:
                    conn.executemany(insert_sql, batch)
                    total += len(batch)
                    batch.clear()
                    if total % 1_000_000 == 0:
                        elapsed = time.monotonic() - t0
                        print(f"  [places] {total:,} rows loaded ({elapsed:.1f}s)...")

            if batch:
                conn.executemany(insert_sql, batch)
                total += len(batch)
                batch.clear()

    if skipped:
        print(f"  [places] Skipped {skipped} malformed line(s)")
    print(f"  [places] Finished loading {total:,} rows in {time.monotonic() - t0:.1f}s")
    return total


def build_indexes(conn: sqlite3.Connection) -> None:
    t0 = time.monotonic()
    print("[index] Creating R-Tree spatial index (places_rtree)...")
    conn.execute(
        "CREATE VIRTUAL TABLE places_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon);"
    )
    conn.execute(
        """
        INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon)
        SELECT geoname_id, latitude, latitude, longitude, longitude
        FROM places
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL;
        """
    )
    print(f"[index] R-Tree built in {time.monotonic() - t0:.1f}s")

    t1 = time.monotonic()
    print("[index] Creating FTS5 full-text index (places_fts)...")
    conn.execute(
        """
        CREATE VIRTUAL TABLE places_fts USING fts5(
            name,
            asciiname,
            content='places',
            content_rowid='geoname_id',
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    conn.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild');")
    print(f"[index] FTS5 built in {time.monotonic() - t1:.1f}s")

    t2 = time.monotonic()
    print("[index] Creating B-Tree indexes...")
    conn.execute(
        "CREATE INDEX idx_places_country_pop ON places(country_code, population DESC);"
    )
    conn.execute(
        "CREATE INDEX idx_places_feature_pop ON places(feature_class, feature_code, population DESC);"
    )
    conn.execute(
        "CREATE INDEX idx_places_asciiname ON places(asciiname COLLATE NOCASE);"
    )
    print(f"[index] B-Tree indexes built in {time.monotonic() - t2:.1f}s")


def build_database(data_root: Path) -> Path:
    geonames_dir = data_root / "geonames"
    db_dir = data_root / "databases"
    db_dir.mkdir(parents=True, exist_ok=True)

    final_db = db_dir / "geonames.sqlite"
    tmp_db = db_dir / "geonames.sqlite.tmp"
    if tmp_db.exists():
        tmp_db.unlink()

    required = [
        geonames_dir / "allCountries.zip",
        geonames_dir / "countryInfo.txt",
        geonames_dir / "admin1CodesASCII.txt",
        geonames_dir / "admin2Codes.txt",
        geonames_dir / "timeZones.txt",
        geonames_dir / "featureCodes_en.txt",
    ]
    for req in required:
        if not req.exists():
            raise FileNotFoundError(f"Missing required GeoNames file: {req}")

    t_start = time.monotonic()
    print(f"[build] Building {tmp_db} ...")
    conn = sqlite3.connect(str(tmp_db))
    try:
        conn.execute("PRAGMA journal_mode=OFF;")
        conn.execute("PRAGMA synchronous=OFF;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA cache_size=-512000;")

        create_schema(conn)

        c_cnt = load_countries(conn, geonames_dir / "countryInfo.txt")
        a1_cnt = load_admin_table(conn, geonames_dir / "admin1CodesASCII.txt", "admin1")
        a2_cnt = load_admin_table(conn, geonames_dir / "admin2Codes.txt", "admin2")
        tz_cnt = load_timezones(conn, geonames_dir / "timeZones.txt")
        fc_cnt = load_feature_codes(conn, geonames_dir / "featureCodes_en.txt")
        print(
            f"[build] Metadata loaded: countries={c_cnt}, admin1={a1_cnt}, "
            f"admin2={a2_cnt}, timezones={tz_cnt}, feature_codes={fc_cnt}"
        )

        places_cnt = load_places_from_zip(conn, geonames_dir / "allCountries.zip")
        conn.commit()

        build_indexes(conn)
        conn.commit()

        print("[build] Running ANALYZE and setting safe journal mode...")
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("ANALYZE;")
        conn.commit()

        rtree_cnt = conn.execute("SELECT COUNT(*) FROM places_rtree").fetchone()[0]
    finally:
        conn.close()

    os.replace(tmp_db, final_db)
    size_gib = final_db.stat().st_size / (1024**3)
    print(
        f"[build] SUCCESS: {final_db} ({size_gib:.2f} GiB) | "
        f"places={places_cnt:,} | places_rtree={rtree_cnt:,} | "
        f"elapsed={time.monotonic() - t_start:.1f}s"
    )
    return final_db


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build geonames.sqlite from downloaded GeoNames datasets."
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(r"F:\ReferenceData"),
        help="Root directory containing geonames/ and databases/ (default: F:\\ReferenceData)",
    )
    args = parser.parse_args()
    build_database(args.data_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
