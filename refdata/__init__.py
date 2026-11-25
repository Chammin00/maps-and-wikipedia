"""Reusable offline reference toolkit for Wikipedia, World Maps, and GPS lookup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import (
    get_data_root,
    get_geonames_db_path,
    get_natural_earth_gpkg_path,
    get_wikipedia_zim_path,
)
from .geo import GeoDatabase, haversine_km, initial_bearing_deg
from .wikipedia import WikipediaArchive
from .world_map import WorldMap

__all__ = [
    "GeoDatabase",
    "ReferenceHub",
    "WikipediaArchive",
    "WorldMap",
    "get_data_root",
    "haversine_km",
    "initial_bearing_deg",
]


class ReferenceHub:
    """Unified lazy-loaded facade over GeoNames, Natural Earth, and Wikipedia."""

    def __init__(self, data_root: Path | str | None = None) -> None:
        self.data_root = Path(data_root) if data_root else get_data_root()
        self._geo: GeoDatabase | None = None
        self._world_map: WorldMap | None = None
        self._wiki: WikipediaArchive | None = None

    @property
    def geo(self) -> GeoDatabase:
        if self._geo is None:
            self._geo = GeoDatabase(get_geonames_db_path(self.data_root))
        return self._geo

    @property
    def world_map(self) -> WorldMap:
        if self._world_map is None:
            self._world_map = WorldMap(
                get_natural_earth_gpkg_path(self.data_root),
                data_root=self.data_root,
            )
        return self._world_map

    @property
    def wiki(self) -> WikipediaArchive:
        if self._wiki is None:
            self._wiki = WikipediaArchive(get_wikipedia_zim_path(self.data_root))
        return self._wiki

    def close(self) -> None:
        if self._geo is not None:
            self._geo.close()
            self._geo = None
        if self._world_map is not None:
            self._world_map.close()
            self._world_map = None
        if self._wiki is not None:
            self._wiki.close()
            self._wiki = None

    def __enter__(self) -> "ReferenceHub":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def describe_location(
        self,
        lat: float,
        lon: float,
        *,
        radius_km: float = 25.0,
        include_wiki: bool = True,
    ) -> dict[str, Any]:
        """Combine Natural Earth polygons, GeoNames GPS lookup, and Wikipedia summary."""
        map_region = self.world_map.locate(lat, lon)
        nearest_pop = self.geo.reverse_geocode(
            lat, lon, radius_km=radius_km, feature_class="P", limit=5
        )
        if not nearest_pop:
            nearest_pop = self.geo.reverse_geocode(
                lat, lon, radius_km=max(radius_km, 150.0), feature_class="P", limit=3
            )
        nearest_features = self.geo.reverse_geocode(
            lat, lon, radius_km=radius_km, limit=5
        )

        wiki_info: dict[str, Any] | None = None
        if include_wiki:
            zim_path = get_wikipedia_zim_path(self.data_root)
            if zim_path.exists():
                candidates: list[str] = []
                if nearest_pop:
                    top = nearest_pop[0]
                    name = top.get("name") or top.get("asciiname")
                    admin1 = top.get("admin1_name")
                    country_name = top.get("country_name")
                    if name and admin1:
                        candidates.append(f"{name}, {admin1}")
                    if name and country_name:
                        candidates.append(f"{name}, {country_name}")
                    if name:
                        candidates.append(name)
                country_obj = map_region.get("country")
                if country_obj and country_obj.get("name"):
                    candidates.append(country_obj["name"])

                for title in candidates:
                    art = self.wiki.get_article(title)
                    if art:
                        wiki_info = {
                            "title": art["title"],
                            "path": art["path"],
                            "summary": art["summary"],
                            "coordinates": art["coordinates"],
                        }
                        break

        return {
            "lat": lat,
            "lon": lon,
            "map_region": map_region,
            "nearest_populated_places": nearest_pop,
            "nearest_features": nearest_features,
            "wikipedia": wiki_info,
        }
