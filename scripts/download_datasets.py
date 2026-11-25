#!/usr/bin/env python3
"""Resumable downloader for Wikipedia ZIM, GeoNames, and Natural Earth datasets."""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import os
from pathlib import Path
import random
import shutil
import signal
import sqlite3
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

import requests

KIWIX_ZIM_NAME = "wikipedia_en_all_nopic_2026-06.zim"
KIWIX_META4_URL = f"https://download.kiwix.org/zim/wikipedia/{KIWIX_ZIM_NAME}.meta4"
KIWIX_SHA256_URL = f"https://download.kiwix.org/zim/wikipedia/{KIWIX_ZIM_NAME}.sha256"

# Benchmarked US/fast mirrors first with relative selection weights.
PREFERRED_MIRROR_WEIGHTS: list[tuple[str, int]] = [
    ("ftpmirror.your.org", 6),
    ("ny.mirror.driftle.ss", 4),
    ("ftp.nluug.nl", 2),
    ("dumps.wikimedia.org", 2),
    ("wi.mirror.driftle.ss", 2),
]

GEONAMES_BASE = "https://download.geonames.org/export/dump/"
GEONAMES_FILES = [
    "allCountries.zip",
    "cities500.zip",
    "countryInfo.txt",
    "admin1CodesASCII.txt",
    "admin2Codes.txt",
    "timeZones.txt",
    "featureCodes_en.txt",
    "readme.txt",
]

NATURAL_EARTH_BASE = "https://naciscdn.org/naturalearth/"
NATURAL_EARTH_VECTOR = {
    "url": f"{NATURAL_EARTH_BASE}packages/natural_earth_vector.gpkg.zip",
    "zip_name": "natural_earth_vector.gpkg.zip",
    "target_gpkg": "natural_earth_vector.gpkg",
}
NATURAL_EARTH_RASTERS = [
    ("10m/raster/NE1_HR_LC_SR_W_DR.zip", "NE1_HR_LC_SR_W_DR"),
    ("10m/raster/HYP_HR_SR_OB_DR.zip", "HYP_HR_SR_OB_DR"),
    ("50m/raster/NE1_50M_SR_W.zip", "NE1_50M_SR_W"),
]

USER_AGENT = "ReferenceDataDownloader/1.0 (Python requests)"


def format_bytes(num_bytes: int | float) -> str:
    """Format byte counts in human-readable binary units."""
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TiB"


def format_duration(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    if seconds < 0 or seconds == float("inf"):
        return "--:--:--"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def download_small_file(url: str, dest: Path, timeout: int = 30) -> Path:
    """Download a small metadata or text file cleanly via a .part temp file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        stream=True,
        timeout=timeout,
    )
    resp.raise_for_status()
    with open(part, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)
    os.replace(part, dest)
    return dest


def download_resumable_file(url: str, dest: Path, label: str | None = None) -> Path:
    """Download a file with HTTP Range resume support and Content-Length validation."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    name = label or dest.name
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    expected_size: int | None = None
    accept_ranges = False
    try:
        head = session.head(url, allow_redirects=True, timeout=(15, 30))
        if head.ok:
            cl = head.headers.get("Content-Length")
            if cl and cl.isdigit():
                expected_size = int(cl)
            accept_ranges = "bytes" in head.headers.get("Accept-Ranges", "").lower()
    except requests.RequestException:
        pass

    if dest.exists():
        actual = dest.stat().st_size
        if expected_size is not None and actual == expected_size:
            print(f"[skip] {name} already exists ({format_bytes(actual)})")
            return dest
        if expected_size is None and actual > 0:
            print(f"[skip] {name} already exists ({format_bytes(actual)})")
            return dest

    part = dest.with_name(dest.name + ".part")
    existing = part.stat().st_size if part.exists() else 0

    if expected_size is not None and existing == expected_size:
        os.replace(part, dest)
        print(f"[done] {name} recovered from complete .part ({format_bytes(expected_size)})")
        return dest

    headers: dict[str, str] = {}
    mode = "wb"
    if existing > 0 and accept_ranges and (expected_size is None or existing < expected_size):
        headers["Range"] = f"bytes={existing}-"
        mode = "ab"
        print(f"[resume] {name} from {format_bytes(existing)}")
    else:
        existing = 0
        print(f"[start] {name} -> {dest}")

    with session.get(url, headers=headers, stream=True, timeout=(15, 60)) as resp:
        if resp.status_code == 200 and mode == "ab":
            # Server ignored Range; restart cleanly from 0.
            existing = 0
            mode = "wb"
        elif resp.status_code not in (200, 206):
            resp.raise_for_status()

        if expected_size is None:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                expected_size = int(cl) + (existing if resp.status_code == 206 else 0)

        downloaded = existing
        start_time = time.monotonic()
        last_report = start_time

        with open(part, mode) as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if now - last_report >= 10.0:
                    elapsed = max(now - start_time, 0.001)
                    speed = (downloaded - existing) / elapsed
                    pct = (
                        f"{(downloaded / expected_size) * 100:.1f}%"
                        if expected_size
                        else "?"
                    )
                    total_str = format_bytes(expected_size) if expected_size else "?"
                    print(
                        f"  [{name}] {format_bytes(downloaded)} / {total_str} "
                        f"({pct}) @ {format_bytes(speed)}/s"
                    )
                    last_report = now

    actual_part = part.stat().st_size
    if expected_size is not None and actual_part != expected_size:
        raise RuntimeError(
            f"Incomplete download for {name}: got {actual_part} bytes, expected {expected_size}"
        )

    os.replace(part, dest)
    print(f"[done] {name} ({format_bytes(actual_part)})")
    return dest


def parse_meta4(meta4_path: Path) -> dict:
    """Parse Kiwix .meta4 Metalink XML for size, hashes, pieces, and mirror URLs."""
    tree = ET.parse(meta4_path)
    root = tree.getroot()
    ns = {"m": "urn:ietf:params:xml:ns:metalink"}
    file_el = root.find("m:file", ns)
    if file_el is None:
        raise ValueError(f"No <file> element in {meta4_path}")

    size_el = file_el.find("m:size", ns)
    if size_el is None or not size_el.text:
        raise ValueError(f"Missing <size> in {meta4_path}")
    total_size = int(size_el.text.strip())

    file_hashes: dict[str, str] = {}
    for h_el in file_el.findall("m:hash", ns):
        htype = h_el.attrib.get("type", "").lower()
        if h_el.text:
            file_hashes[htype] = h_el.text.strip().lower()

    pieces_el = file_el.find("m:pieces", ns)
    if pieces_el is None:
        raise ValueError(f"Missing <pieces> in {meta4_path}")
    piece_length = int(pieces_el.attrib["length"])
    piece_hash_type = pieces_el.attrib.get("type", "sha-1").lower()
    if piece_hash_type not in ("sha-1", "sha1"):
        raise ValueError(f"Unsupported piece hash type: {piece_hash_type}")

    piece_hashes = [
        h.text.strip().lower() for h in pieces_el.findall("m:hash", ns) if h.text
    ]

    urls = [
        u.text.strip()
        for u in file_el.findall("m:url", ns)
        if u.text and u.text.strip().startswith(("https://", "http://"))
    ]
    if not urls:
        raise ValueError(f"No HTTP/HTTPS mirror URLs found in {meta4_path}")

    return {
        "name": file_el.attrib.get("name", KIWIX_ZIM_NAME),
        "size": total_size,
        "hashes": file_hashes,
        "piece_length": piece_length,
        "piece_hashes": piece_hashes,
        "urls": urls,
    }


def order_and_weight_mirrors(urls: list[str]) -> list[tuple[str, int]]:
    """Prioritize benchmarked fast HTTPS mirrors and assign selection weights."""
    weight_by_host = dict(PREFERRED_MIRROR_WEIGHTS)
    rank_by_host = {host: idx for idx, (host, _) in enumerate(PREFERRED_MIRROR_WEIGHTS)}

    https_urls = [u for u in urls if u.startswith("https://")]
    candidate_urls = https_urls if https_urls else urls

    scored: list[tuple[int, str, int]] = []
    seen: set[str] = set()
    for url in candidate_urls:
        if url in seen:
            continue
        seen.add(url)
        host = urllib.parse.urlparse(url).netloc.lower()
        rank = rank_by_host.get(host, 100)
        weight = weight_by_host.get(host, 1)
        scored.append((rank, url, weight))

    scored.sort(key=lambda item: item[0])
    return [(url, weight) for _, url, weight in scored]


class MirrorPool:
    """Thread-safe mirror pool with per-mirror concurrency caps and failure cooldowns."""

    def __init__(self, mirror_weights: list[tuple[str, int]], max_per_mirror: int = 3) -> None:
        self.mirrors = [url for url, _ in mirror_weights]
        self.weights = {url: weight for url, weight in mirror_weights}
        self.semaphores = {url: threading.Semaphore(max_per_mirror) for url in self.mirrors}
        self.cooldown_until = {url: 0.0 for url in self.mirrors}
        self.lock = threading.Lock()
        self._tls = threading.local()

    def session(self) -> requests.Session:
        sess = getattr(self._tls, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": USER_AGENT})
            adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
            self._tls.session = sess
        return sess

    def pick_mirror(self, exclude: set[str] | None = None) -> str:
        now = time.monotonic()
        with self.lock:
            available = [
                m
                for m in self.mirrors
                if now >= self.cooldown_until[m] and (not exclude or m not in exclude)
            ]
            if not available:
                available = [m for m in self.mirrors if now >= self.cooldown_until[m]]
            if not available:
                # All mirrors cooling down; pick the one nearest expiry.
                return min(self.mirrors, key=lambda m: self.cooldown_until[m])
            weights = [self.weights[m] for m in available]
            return random.choices(available, weights=weights, k=1)[0]

    def penalize(self, url: str, seconds: float = 30.0) -> None:
        with self.lock:
            self.cooldown_until[url] = max(
                self.cooldown_until[url], time.monotonic() + seconds
            )


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file using streaming/file_digest."""
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


def download_wikipedia(data_root: Path, workers: int = 8) -> Path:
    """Download and SHA-256 verify wikipedia_en_all_nopic_2026-06.zim via Metalink pieces."""
    wiki_dir = data_root / "wikipedia"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    meta4_path = wiki_dir / f"{KIWIX_ZIM_NAME}.meta4"
    sha256_path = wiki_dir / f"{KIWIX_ZIM_NAME}.sha256"
    zim_path = wiki_dir / KIWIX_ZIM_NAME
    part_path = wiki_dir / f"{KIWIX_ZIM_NAME}.part"
    bitmap_path = wiki_dir / f"{KIWIX_ZIM_NAME}.bitmap"

    print(f"[wikipedia] Fetching Metalink & SHA-256 metadata into {wiki_dir}...")
    download_small_file(KIWIX_META4_URL, meta4_path)
    download_small_file(KIWIX_SHA256_URL, sha256_path)

    meta = parse_meta4(meta4_path)
    total_size: int = meta["size"]
    piece_len: int = meta["piece_length"]
    piece_hashes: list[str] = meta["piece_hashes"]
    num_pieces = len(piece_hashes)

    sha256_file_text = sha256_path.read_text(encoding="utf-8").strip()
    expected_sha256 = sha256_file_text.split()[0].lower()
    meta_sha256 = meta["hashes"].get("sha-256", "")
    if meta_sha256 and expected_sha256 != meta_sha256:
        raise RuntimeError(
            f"SHA-256 mismatch between .sha256 ({expected_sha256}) and .meta4 ({meta_sha256})"
        )

    if zim_path.exists():
        actual_size = zim_path.stat().st_size
        if actual_size == total_size:
            print(f"[wikipedia] Final ZIM exists ({format_bytes(actual_size)}); verifying SHA-256...")
            actual_sha = compute_sha256(zim_path)
            if actual_sha == expected_sha256:
                print(f"[wikipedia] Verified OK: {zim_path} (sha256={actual_sha})")
                return zim_path
            print(
                f"[wikipedia] Existing ZIM SHA-256 mismatch ({actual_sha}); re-downloading."
            )
        else:
            print(
                f"[wikipedia] Existing ZIM size mismatch ({actual_size} != {total_size}); re-downloading."
            )

    # Load or initialize piece completion bitmap.
    if bitmap_path.exists() and part_path.exists():
        raw_bm = bitmap_path.read_bytes()
        if len(raw_bm) == num_pieces:
            bitmap = bytearray(raw_bm)
        else:
            bitmap = bytearray(num_pieces)
    else:
        bitmap = bytearray(num_pieces)

    part_size = part_path.stat().st_size if part_path.exists() else 0
    invalidated = 0
    for idx in range(num_pieces):
        if bitmap[idx]:
            end_offset = min((idx + 1) * piece_len, total_size)
            if end_offset > part_size:
                bitmap[idx] = 0
                invalidated += 1
    if invalidated:
        print(f"[wikipedia] Reset {invalidated} bitmap pieces past .part EOF ({part_size} bytes)")

    pending_indices = deque(idx for idx in range(num_pieces) if bitmap[idx] == 0)
    already_done = num_pieces - len(pending_indices)
    print(
        f"[wikipedia] {KIWIX_ZIM_NAME}: {format_bytes(total_size)}, "
        f"{num_pieces} pieces ({format_bytes(piece_len)}/piece), "
        f"resuming {already_done}/{num_pieces} completed"
    )

    mirror_weights = order_and_weight_mirrors(meta["urls"])
    print("[wikipedia] Mirror priority:")
    for m_url, m_w in mirror_weights:
        print(f"  weight={m_w} {m_url}")

    pool = MirrorPool(mirror_weights, max_per_mirror=3)
    stop_event = threading.Event()
    write_lock = threading.Lock()
    queue_lock = threading.Lock()

    state = {
        "completed_pieces": already_done,
        "session_bytes": 0,
        "total_written_since_flush": 0,
    }

    def flush_bitmap() -> None:
        tmp_bm = bitmap_path.with_name(bitmap_path.name + ".tmp")
        with open(tmp_bm, "wb") as bf:
            bf.write(bitmap)
        os.replace(tmp_bm, bitmap_path)

    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)

    def handle_signal(signum, _frame):
        print(f"\n[wikipedia] Signal {signum} received; stopping workers cleanly...")
        stop_event.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

    file_mode = "r+b" if part_path.exists() else "w+b"
    part_file = open(part_path, file_mode)

    def fetch_piece(piece_idx: int) -> None:
        start = piece_idx * piece_len
        end = min(start + piece_len, total_size) - 1
        expected_len = end - start + 1
        expected_sha1 = piece_hashes[piece_idx]
        recent_failures: set[str] = set()

        for attempt in range(1, 13):
            if stop_event.is_set():
                return
            mirror = pool.pick_mirror(exclude=recent_failures)
            sem = pool.semaphores[mirror]
            with sem:
                if stop_event.is_set():
                    return
                try:
                    resp = pool.session().get(
                        mirror,
                        headers={"Range": f"bytes={start}-{end}"},
                        timeout=(15, 90),
                    )
                    if resp.status_code != 206:
                        raise RuntimeError(f"HTTP {resp.status_code} (expected 206)")
                    chunk = resp.content
                    if len(chunk) != expected_len:
                        raise RuntimeError(
                            f"Short piece {len(chunk)} != {expected_len} bytes"
                        )
                    actual_sha1 = hashlib.sha1(chunk).hexdigest()
                    if actual_sha1 != expected_sha1:
                        raise RuntimeError(
                            f"SHA-1 mismatch {actual_sha1} != {expected_sha1}"
                        )
                except Exception as exc:
                    pool.penalize(mirror, seconds=30.0)
                    recent_failures.add(mirror)
                    if len(recent_failures) >= len(pool.mirrors):
                        recent_failures.clear()
                    if attempt == 12:
                        raise RuntimeError(
                            f"Piece {piece_idx} failed after 12 attempts (last: {mirror}: {exc})"
                        ) from exc
                    time.sleep(min(1.0 * attempt, 5.0))
                    continue

            with write_lock:
                part_file.seek(start)
                part_file.write(chunk)
                bitmap[piece_idx] = 1
                state["completed_pieces"] += 1
                state["session_bytes"] += len(chunk)
                state["total_written_since_flush"] += 1
                if state["total_written_since_flush"] >= 25:
                    part_file.flush()
                    flush_bitmap()
                    state["total_written_since_flush"] = 0
            return

    def worker_loop() -> None:
        while not stop_event.is_set():
            with queue_lock:
                if not pending_indices:
                    return
                piece_idx = pending_indices.popleft()
            fetch_piece(piece_idx)

    start_ts = time.monotonic()

    def reporter_loop() -> None:
        while not stop_event.wait(15.0):
            with write_lock:
                done_pieces = state["completed_pieces"]
                sess_bytes = state["session_bytes"]
            elapsed = max(time.monotonic() - start_ts, 0.001)
            speed_bps = sess_bytes / elapsed
            done_bytes = min(done_pieces * piece_len, total_size)
            remaining_bytes = max(total_size - done_bytes, 0)
            eta = remaining_bytes / speed_bps if speed_bps > 1024 else float("inf")
            pct = (done_pieces / num_pieces) * 100.0
            print(
                f"[wikipedia] {format_bytes(done_bytes)} / {format_bytes(total_size)} "
                f"({pct:.2f}%) | pieces {done_pieces}/{num_pieces} | "
                f"{speed_bps / (1024 * 1024):.2f} MB/s | ETA {format_duration(eta)}"
            )

    reporter = threading.Thread(target=reporter_loop, daemon=True)
    reporter.start()

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker_loop) for _ in range(workers)]
            for fut in as_completed(futures):
                exc = fut.exception()
                if exc is not None:
                    stop_event.set()
                    raise exc
    finally:
        stop_event.set()
        reporter.join(timeout=2.0)
        with write_lock:
            part_file.flush()
            part_file.close()
            flush_bitmap()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, prev_sigint)
            signal.signal(signal.SIGTERM, prev_sigterm)

    if any(b == 0 for b in bitmap):
        remaining = sum(1 for b in bitmap if b == 0)
        raise RuntimeError(
            f"Wikipedia download interrupted with {remaining} pieces remaining; rerun to resume."
        )

    actual_part_size = part_path.stat().st_size
    if actual_part_size != total_size:
        raise RuntimeError(
            f"Part file size mismatch: {actual_part_size} != {total_size}"
        )

    print(f"[wikipedia] All {num_pieces} pieces verified. Computing full-file SHA-256...")
    full_sha256 = compute_sha256(part_path)
    if full_sha256 != expected_sha256:
        raise RuntimeError(
            f"Full-file SHA-256 verification FAILED: got {full_sha256}, expected {expected_sha256}"
        )

    os.replace(part_path, zim_path)
    if bitmap_path.exists():
        bitmap_path.unlink()
    print(f"[wikipedia] SUCCESS! Verified SHA-256={full_sha256} -> {zim_path}")
    return zim_path


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> list[Path]:
    """Safely extract zip contents into dest_dir without Zip Slip traversal."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    extracted: list[Path] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            rel = Path(member.filename)
            target = (dest_resolved / rel).resolve()
            if not str(target).startswith(str(dest_resolved) + os.sep) and target != dest_resolved:
                raise RuntimeError(f"Unsafe zip member path: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            extracted.append(target)
    return extracted


def validate_gpkg(gpkg_path: Path) -> int:
    """Validate that a GeoPackage opens in SQLite and has gpkg_contents entries."""
    if not gpkg_path.exists() or gpkg_path.stat().st_size == 0:
        raise RuntimeError(f"Missing or empty GeoPackage: {gpkg_path}")
    conn = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
    try:
        count = conn.execute("SELECT COUNT(*) FROM gpkg_contents").fetchone()[0]
    finally:
        conn.close()
    if count <= 0:
        raise RuntimeError(f"GeoPackage {gpkg_path} has no gpkg_contents layers")
    return int(count)


def download_maps(data_root: Path) -> None:
    """Download GeoNames dumps and Natural Earth vector/raster datasets."""
    geonames_dir = data_root / "geonames"
    geonames_dir.mkdir(parents=True, exist_ok=True)
    for fname in GEONAMES_FILES:
        download_resumable_file(f"{GEONAMES_BASE}{fname}", geonames_dir / fname, label=f"geonames/{fname}")

    # Natural Earth Vector GeoPackage
    ne_vector_dir = data_root / "natural_earth" / "vector"
    ne_vector_dir.mkdir(parents=True, exist_ok=True)
    gpkg_target = ne_vector_dir / NATURAL_EARTH_VECTOR["target_gpkg"]

    gpkg_ready = False
    if gpkg_target.exists():
        try:
            layers = validate_gpkg(gpkg_target)
            print(f"[skip] {gpkg_target} already valid ({layers} layers)")
            gpkg_ready = True
        except Exception as exc:
            print(f"[warn] Existing {gpkg_target} invalid ({exc}); re-downloading.")

    if not gpkg_ready:
        vec_zip = ne_vector_dir / NATURAL_EARTH_VECTOR["zip_name"]
        download_resumable_file(
            NATURAL_EARTH_VECTOR["url"],
            vec_zip,
            label="natural_earth/vector_gpkg.zip",
        )
        print(f"[extract] Extracting GeoPackage from {vec_zip}...")
        with zipfile.ZipFile(vec_zip, "r") as zf:
            gpkg_members = [m for m in zf.infolist() if not m.is_dir() and m.filename.lower().endswith(".gpkg")]
            if not gpkg_members:
                raise RuntimeError(f"No .gpkg file found inside {vec_zip}")
            member = gpkg_members[0]
            tmp_gpkg = gpkg_target.with_name(gpkg_target.name + ".tmp")
            with zf.open(member, "r") as src, open(tmp_gpkg, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
        os.replace(tmp_gpkg, gpkg_target)
        layers = validate_gpkg(gpkg_target)
        print(f"[done] Extracted & verified {gpkg_target} ({layers} layers)")
        vec_zip.unlink()

    # Natural Earth Rasters
    ne_raster_root = data_root / "natural_earth" / "raster"
    ne_raster_root.mkdir(parents=True, exist_ok=True)
    for rel_url, folder_name in NATURAL_EARTH_RASTERS:
        raster_dir = ne_raster_root / folder_name
        existing_tifs = list(raster_dir.rglob("*.tif")) if raster_dir.exists() else []
        if existing_tifs and all(p.stat().st_size > 0 for p in existing_tifs):
            print(f"[skip] raster/{folder_name} already extracted ({len(existing_tifs)} .tif)")
            continue

        zip_path = ne_raster_root / Path(rel_url).name
        download_resumable_file(
            f"{NATURAL_EARTH_BASE}{rel_url}",
            zip_path,
            label=f"natural_earth/{zip_path.name}",
        )
        print(f"[extract] Extracting {zip_path.name} -> {raster_dir}...")
        # Extract into a staging directory to normalize optional single-root wrapper folder.
        stage_dir = ne_raster_root / f"._stage_{folder_name}"
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        safe_extract_zip(zip_path, stage_dir)

        top_entries = list(stage_dir.iterdir())
        if len(top_entries) == 1 and top_entries[0].is_dir():
            source_root = top_entries[0]
        else:
            source_root = stage_dir

        if raster_dir.exists():
            shutil.rmtree(raster_dir)
        shutil.move(str(source_root), str(raster_dir))
        if stage_dir.exists():
            shutil.rmtree(stage_dir)

        tifs = list(raster_dir.rglob("*.tif"))
        if not tifs or any(p.stat().st_size == 0 for p in tifs):
            raise RuntimeError(f"No valid .tif files found after extracting {zip_path}")
        print(f"[done] Verified {len(tifs)} GeoTIFF(s) in {raster_dir}")
        zip_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download offline Wikipedia, GeoNames, and Natural Earth datasets."
    )
    parser.add_argument("--wikipedia", action="store_true", help="Download full English Wikipedia ZIM (nopic)")
    parser.add_argument("--maps", action="store_true", help="Download GeoNames & Natural Earth maps")
    parser.add_argument("--all", action="store_true", help="Download both Wikipedia and maps datasets")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent piece workers for Wikipedia ZIM")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(r"F:\ReferenceData"),
        help="Destination root directory (default: F:\\ReferenceData)",
    )
    args = parser.parse_args()

    if not (args.wikipedia or args.maps or args.all):
        parser.error("Specify --wikipedia, --maps, or --all")

    args.data_root.mkdir(parents=True, exist_ok=True)

    if args.maps or args.all:
        download_maps(args.data_root)

    if args.wikipedia or args.all:
        download_wikipedia(args.data_root, workers=args.workers)

    return 0


if __name__ == "__main__":
    sys.exit(main())
