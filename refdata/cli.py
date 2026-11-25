"""Command-line interface for the offline Wikipedia, Map, and GPS reference hub."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from . import ReferenceHub
from .config import (
    get_data_root,
    get_geonames_db_path,
    get_natural_earth_gpkg_path,
    get_raster_paths,
    get_wikipedia_zim_path,
)


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is not None:
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def cmd_status(hub: ReferenceHub, as_json: bool) -> int:
    root = hub.data_root
    geo_path = get_geonames_db_path(root)
    gpkg_path = get_natural_earth_gpkg_path(root)
    zim_path = get_wikipedia_zim_path(root)
    rasters = get_raster_paths(root)

    status: dict[str, Any] = {
        "data_root": str(root),
        "geonames_db": {"path": str(geo_path), "exists": geo_path.exists()},
        "natural_earth_gpkg": {"path": str(gpkg_path), "exists": gpkg_path.exists()},
        "natural_earth_rasters": {"count": len(rasters), "files": rasters},
        "wikipedia_zim": {"path": str(zim_path), "exists": zim_path.exists()},
    }

    if geo_path.exists():
        status["geonames_db"].update(hub.geo.stats())
    if gpkg_path.exists():
        status["natural_earth_gpkg"]["layers"] = len(hub.world_map.list_layers())
        status["natural_earth_gpkg"]["size_bytes"] = gpkg_path.stat().st_size
    if zim_path.exists():
        status["wikipedia_zim"].update(hub.wiki.stats())

    if as_json:
        _print_json(status)
    else:
        print(f"Data Root: {status['data_root']}")
        if geo_path.exists():
            gs = status["geonames_db"]
            print(
                f"GeoNames SQLite: READY ({gs['places']:,} places, "
                f"{gs['countries']} countries, {gs['size_bytes'] / (1024**3):.2f} GiB)"
            )
        else:
            print(f"GeoNames SQLite: MISSING ({geo_path})")
        if gpkg_path.exists():
            ns = status["natural_earth_gpkg"]
            print(
                f"Natural Earth GPKG: READY ({ns['layers']} layers, "
                f"{ns['size_bytes'] / (1024**2):.1f} MiB)"
            )
        else:
            print(f"Natural Earth GPKG: MISSING ({gpkg_path})")
        print(f"Natural Earth Rasters: {len(rasters)} GeoTIFF(s)")
        if zim_path.exists():
            ws = status["wikipedia_zim"]
            print(
                f"Wikipedia ZIM: READY ({ws['article_count']:,} articles, "
                f"{ws['all_entry_count']:,} entries, {ws['size_bytes'] / (1024**3):.2f} GiB)"
            )
        else:
            part = zim_path.with_name(zim_path.name + ".part")
            state = "DOWNLOADING (.part present)" if part.exists() else "MISSING"
            print(f"Wikipedia ZIM: {state} ({zim_path})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m refdata",
        description="Query offline Wikipedia, Natural Earth world maps, and GeoNames GPS data.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Override ReferenceData root directory (default: env/manifest/F:\\ReferenceData)",
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show dataset availability and summary counts")

    p_place = sub.add_parser("place", help="Search GeoNames places by name")
    p_place.add_argument("query", help="Place name query")
    p_place.add_argument("--country", default=None, help="Filter by ISO-2 country code")
    p_place.add_argument("--feature-class", default=None, help="Filter by feature class (e.g. P)")
    p_place.add_argument("--min-pop", type=int, default=0, help="Minimum population")
    p_place.add_argument("--limit", type=int, default=10, help="Max results")

    p_rev = sub.add_parser("reverse", help="Reverse geocode GPS coordinates via GeoNames")
    p_rev.add_argument("lat", type=float, help="Latitude (-90..90)")
    p_rev.add_argument("lon", type=float, help="Longitude (-180..180)")
    p_rev.add_argument("--radius-km", type=float, default=50.0, help="Search radius in km")
    p_rev.add_argument("--feature-class", default=None, help="Filter by feature class (e.g. P)")
    p_rev.add_argument("--min-pop", type=int, default=0, help="Minimum population")
    p_rev.add_argument("--limit", type=int, default=10, help="Max results")

    p_map_pt = sub.add_parser("map-point", help="Locate GPS point in Natural Earth map layers")
    p_map_pt.add_argument("lat", type=float, help="Latitude (-90..90)")
    p_map_pt.add_argument("lon", type=float, help="Longitude (-180..180)")

    sub.add_parser("map-layers", help="List available Natural Earth vector layers and rasters")

    p_ws = sub.add_parser("wiki-search", help="Full-text search offline Wikipedia")
    p_ws.add_argument("query", help="Search query")
    p_ws.add_argument("--limit", type=int, default=10, help="Max results")

    p_wg = sub.add_parser("wiki-suggest", help="Autocomplete Wikipedia article titles")
    p_wg.add_argument("prefix", help="Title prefix")
    p_wg.add_argument("--limit", type=int, default=10, help="Max results")

    p_wa = sub.add_parser("wiki-get", help="Fetch a Wikipedia article summary/text")
    p_wa.add_argument("title", help="Article title or ZIM path")
    p_wa.add_argument("--full", action="store_true", help="Print full plain-text article")

    p_desc = sub.add_parser(
        "describe-gps",
        help="Combined Natural Earth + GeoNames + Wikipedia lookup for GPS coordinates",
    )
    p_desc.add_argument("lat", type=float, help="Latitude (-90..90)")
    p_desc.add_argument("lon", type=float, help="Longitude (-180..180)")
    p_desc.add_argument("--radius-km", type=float, default=25.0, help="Search radius in km")
    p_desc.add_argument("--no-wiki", action="store_true", help="Skip Wikipedia lookup")

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    data_root = args.data_root or get_data_root()

    with ReferenceHub(data_root) as hub:
        if args.command == "status":
            return cmd_status(hub, args.json)

        if args.command == "place":
            rows = hub.geo.search_places(
                args.query,
                country_code=args.country,
                feature_class=args.feature_class,
                min_population=args.min_pop,
                limit=args.limit,
            )
            if args.json:
                _print_json(rows)
            else:
                for r in rows:
                    print(
                        f"[{r['geoname_id']}] {r['name']} ({r['country_code']}"
                        f"{', ' + r['admin1_name'] if r['admin1_name'] else ''}) "
                        f"lat={r['latitude']:.5f}, lon={r['longitude']:.5f} "
                        f"pop={r['population'] or 0:,} [{r['feature_class']}.{r['feature_code']}]"
                    )
            return 0

        if args.command == "reverse":
            rows = hub.geo.reverse_geocode(
                args.lat,
                args.lon,
                radius_km=args.radius_km,
                feature_class=args.feature_class,
                min_population=args.min_pop,
                limit=args.limit,
            )
            if args.json:
                _print_json(rows)
            else:
                for r in rows:
                    print(
                        f"{r['distance_km']:6.2f} km ({r['bearing_deg']:5.1f} deg) | "
                        f"{r['name']} ({r['country_code']}"
                        f"{', ' + r['admin1_name'] if r['admin1_name'] else ''}) "
                        f"pop={r['population'] or 0:,} [{r['feature_class']}.{r['feature_code']}]"
                    )
            return 0

        if args.command == "map-point":
            res = hub.world_map.locate(args.lat, args.lon)
            _print_json(res)
            return 0

        if args.command == "map-layers":
            layers = hub.world_map.list_layers()
            rasters = hub.world_map.list_rasters()
            if args.json:
                _print_json({"layers": layers, "rasters": rasters})
            else:
                print(f"Vector Layers ({len(layers)}):")
                for lyr in layers:
                    print(f"  - {lyr['table_name']} ({lyr.get('geometry_type_name', lyr['data_type'])})")
                print(f"\nRasters ({len(rasters)}):")
                for rst in rasters:
                    print(f"  - {rst['dataset']}/{rst['name']} ({rst['size_bytes'] / (1024**2):.1f} MiB)")
            return 0

        if args.command == "wiki-search":
            rows = hub.wiki.search(args.query, limit=args.limit)
            if args.json:
                _print_json(rows)
            else:
                for r in rows:
                    print(f"- {r['title']} [{r['path']}]")
            return 0

        if args.command == "wiki-suggest":
            rows = hub.wiki.suggest(args.prefix, limit=args.limit)
            if args.json:
                _print_json(rows)
            else:
                for r in rows:
                    print(f"- {r['title']} [{r['path']}]")
            return 0

        if args.command == "wiki-get":
            art = hub.wiki.get_article(args.title)
            if art is None:
                print(f"Article not found: {args.title}", file=sys.stderr)
                return 1
            if args.json:
                out = art if args.full else {k: v for k, v in art.items() if k != "text"}
                _print_json(out)
            else:
                print(f"Title: {art['title']} ({art['path']})")
                if art["coordinates"]:
                    print(
                        f"GPS: {art['coordinates']['lat']}, {art['coordinates']['lon']}"
                    )
                print("\nSummary:")
                print(art["summary"])
                if args.full:
                    print("\nFull Text:")
                    print(art["text"])
            return 0

        if args.command == "describe-gps":
            res = hub.describe_location(
                args.lat,
                args.lon,
                radius_km=args.radius_km,
                include_wiki=not args.no_wiki,
            )
            _print_json(res)
            return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
