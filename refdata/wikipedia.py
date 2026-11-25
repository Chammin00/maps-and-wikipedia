"""Offline Wikipedia ZIM archive reader, search, and article extractor."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from bs4 import BeautifulSoup
from libzim.reader import Archive, Entry
from libzim.search import Query, Searcher
from libzim.suggestion import SuggestionSearcher

from .config import get_wikipedia_zim_path


def _parse_geo_coordinates(soup: BeautifulSoup) -> dict[str, float] | None:
    """Extract decimal GPS coordinates from Wikipedia HTML markup if present."""
    geo_el = soup.find(class_="geo")
    if geo_el:
        raw = geo_el.get_text(" ", strip=True)
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*[;,]\s*(-?\d+(?:\.\d+)?)", raw)
        if match:
            lat = float(match.group(1))
            lon = float(match.group(2))
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return {"lat": lat, "lon": lon}

    geo_dec = soup.find(class_="geo-dec")
    if geo_dec:
        raw = geo_dec.get_text(" ", strip=True)
        match = re.search(
            r"(\d+(?:\.\d+)?)\s*°?\s*([NS])[,;\s]+(\d+(?:\.\d+)?)\s*°?\s*([EW])",
            raw,
            re.IGNORECASE,
        )
        if match:
            lat = float(match.group(1)) * (-1.0 if match.group(2).upper() == "S" else 1.0)
            lon = float(match.group(3)) * (-1.0 if match.group(4).upper() == "W" else 1.0)
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return {"lat": lat, "lon": lon}

    return None


def _html_to_text_and_summary(html: str) -> tuple[str, str, dict[str, float] | None]:
    """Parse Wikipedia article HTML into (summary, plain_text, coordinates)."""
    soup = BeautifulSoup(html, "lxml")
    coords = _parse_geo_coordinates(soup)

    for tag in soup.select(
        "script, style, noscript, .mw-editsection, sup.reference, .reflist, "
        ".navbox, .vertical-navbox, .metadata, .sistersitebox, .hatnote"
    ):
        tag.decompose()

    root = soup.body or soup
    paragraphs: list[str] = []
    for p in root.find_all("p"):
        txt = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
        if txt:
            paragraphs.append(txt)

    summary = ""
    for p_txt in paragraphs:
        if len(p_txt) >= 40:
            summary = p_txt
            break
    if not summary and paragraphs:
        summary = paragraphs[0]

    raw_text = root.get_text("\n", strip=True)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw_text.splitlines()]
    clean_text = "\n".join(ln for ln in lines if ln)
    return summary, clean_text, coords


class WikipediaArchive:
    """Read-only search and article lookup for a local Kiwix ZIM archive."""

    def __init__(self, zim_path: Path | str | None = None) -> None:
        self.zim_path = Path(zim_path) if zim_path else get_wikipedia_zim_path()
        if not self.zim_path.exists():
            raise FileNotFoundError(f"Wikipedia ZIM file not found: {self.zim_path}")
        self.archive = Archive(str(self.zim_path))
        self._searcher: Searcher | None = None
        self._suggestion_searcher: SuggestionSearcher | None = None

    def close(self) -> None:
        # libzim Archive cleans up in C++ destructor; drop references cleanly.
        self._searcher = None
        self._suggestion_searcher = None
        self.archive = None  # type: ignore[assignment]

    def __enter__(self) -> "WikipediaArchive":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _get_searcher(self) -> Searcher:
        if self._searcher is None:
            self._searcher = Searcher(self.archive)
        return self._searcher

    def _get_suggestion_searcher(self) -> SuggestionSearcher:
        if self._suggestion_searcher is None:
            self._suggestion_searcher = SuggestionSearcher(self.archive)
        return self._suggestion_searcher

    def _follow_redirect(self, entry: Entry) -> Entry:
        current = entry
        for _ in range(10):
            if not current.is_redirect:
                return current
            current = current.get_redirect_entry()
        return current

    def _resolve_entry(self, title_or_path: str) -> Entry | None:
        candidate = title_or_path.strip()
        if not candidate:
            return None

        underscored = candidate.replace(" ", "_")
        spaced = candidate.replace("_", " ")

        path_candidates = [candidate, underscored, f"A/{underscored}"]
        for path in path_candidates:
            if self.archive.has_entry_by_path(path):
                return self._follow_redirect(self.archive.get_entry_by_path(path))

        title_candidates = [candidate, spaced]
        for title in title_candidates:
            if self.archive.has_entry_by_title(title):
                return self._follow_redirect(self.archive.get_entry_by_title(title))

        # Case-insensitive fallback via title suggestion index.
        if self.archive.has_title_index:
            sugg = self._get_suggestion_searcher().suggest(spaced)
            est = sugg.get_estimated_matches()
            if est > 0:
                target_low = spaced.lower()
                for res_path in sugg.get_results(0, min(10, est)):
                    entry = self.archive.get_entry_by_path(res_path)
                    if entry.title.lower() == target_low:
                        return self._follow_redirect(entry)

        return None

    def search(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        """Full-text search Wikipedia articles and return matching titles and paths."""
        if not query.strip():
            return []
        q = Query().set_query(query.strip())
        search = self._get_searcher().search(q)
        est = search.get_estimated_matches()
        if est <= 0:
            return []

        results: list[dict[str, str]] = []
        for path in search.get_results(0, min(limit, est)):
            try:
                entry = self._follow_redirect(self.archive.get_entry_by_path(path))
                results.append({"title": entry.title, "path": entry.path})
            except Exception:
                results.append({"title": path, "path": path})
        return results

    def suggest(self, prefix: str, limit: int = 10) -> list[dict[str, str]]:
        """Autocomplete Wikipedia article titles matching prefix."""
        if not prefix.strip():
            return []
        sugg = self._get_suggestion_searcher().suggest(prefix.strip())
        est = sugg.get_estimated_matches()
        if est <= 0:
            return []

        results: list[dict[str, str]] = []
        for path in sugg.get_results(0, min(limit, est)):
            try:
                entry = self.archive.get_entry_by_path(path)
                results.append({"title": entry.title, "path": entry.path})
            except Exception:
                results.append({"title": path, "path": path})
        return results

    def _entry_to_article(
        self, entry: Entry, *, include_html: bool = False
    ) -> dict[str, Any]:
        resolved = self._follow_redirect(entry)
        item = resolved.get_item()
        raw_bytes = bytes(item.content)
        mimetype = item.mimetype
        if "html" in mimetype.lower():
            html_str = raw_bytes.decode("utf-8", errors="replace")
            summary, text, coords = _html_to_text_and_summary(html_str)
        else:
            html_str = ""
            text = raw_bytes.decode("utf-8", errors="replace")
            summary = text[:400]
            coords = None

        article: dict[str, Any] = {
            "title": resolved.title,
            "path": resolved.path,
            "mimetype": mimetype,
            "summary": summary,
            "text": text,
            "coordinates": coords,
        }
        if include_html:
            article["html"] = html_str
        return article

    def get_article(
        self, title_or_path: str, *, include_html: bool = False
    ) -> dict[str, Any] | None:
        """Retrieve and parse a Wikipedia article by title or ZIM path."""
        entry = self._resolve_entry(title_or_path)
        if entry is None:
            return None
        return self._entry_to_article(entry, include_html=include_html)

    def random_article(self, *, include_html: bool = False) -> dict[str, Any]:
        """Return a random article from the Wikipedia ZIM archive."""
        entry = self.archive.get_random_entry()
        return self._entry_to_article(entry, include_html=include_html)

    def stats(self) -> dict[str, Any]:
        """Return archive metadata and entry statistics."""
        return {
            "zim_path": str(self.zim_path),
            "size_bytes": self.zim_path.stat().st_size,
            "article_count": self.archive.article_count,
            "all_entry_count": self.archive.all_entry_count,
            "media_count": self.archive.media_count,
            "has_fulltext_index": self.archive.has_fulltext_index,
            "has_title_index": self.archive.has_title_index,
            "has_new_namespace_scheme": self.archive.has_new_namespace_scheme,
        }
