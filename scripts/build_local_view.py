#!/usr/bin/env python
"""Build a frame-viewer page for any part of the world, on demand.

The published viewer covers the OPERA North America frames and is rebuilt
weekly. This module rebuilds it locally for one of three scopes, straight from
CMR:

* ``na`` -- the OPERA North America frames (the published page's scope);
* ``globe`` -- every frame of the NISAR TrackFrame database (~30,000);
* ``bbox`` -- the frames intersecting a ``west, south, east, north`` box.

Frames keep the ``frame_idx`` OPERA uses (the TrackFrame database's row index),
so a North American frame has the same number in every scope. GSLC and GUNW
granules come from one CMR search each (the whole archive, or the box); blackout
dates, granule flags and QA metrics are attached from the repo's caches where
they cover a frame or granule; on request, the flags and QA metrics of granules
the caches miss are collected first (``collect_granule_flags.py`` /
``collect_granule_qa.py``, Earthdata login in ``~/.netrc``) into local caches
beside the page, so the repo's own caches are left untouched. The consistent
mode is computed from the catalog.

``scripts/qa_browse_server.py`` runs it when the viewer's search button asks;
it can also be run by hand::

    python scripts/build_local_view.py --scope bbox --bbox -125,32,-114,42 \\
        --output view.html

"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import box

import generate_scope_viewer as gen

REPO = Path(__file__).resolve().parents[1]
BLACKOUT_JSON = REPO / "catalog" / "opera-nisar-disp-blackout-dates.json"
GRANULE_FLAGS = REPO / "catalog" / "granule_flags.json.gz"
GRANULE_QA = REPO / "catalog" / "granule_qa.json.gz"
SCOPES = ("na", "globe", "bbox")
#: Prefix of the last output line, which carries the built page's summary.
META_LINE = "@@meta "
SCOPE_LABELS = {"na": "OPERA North America", "globe": "Globe", "bbox": "Screen view"}
# Coordinates to ~10 m: plenty for a frame outline, and it keeps a 30,000-frame
# page from carrying full float precision.
GRID = 1e-4


def select_frames(
    trackframe_gpkg: Path, scope: str, bbox: tuple[float, ...] | None = None
) -> gpd.GeoDataFrame:
    """Return the frames of ``scope`` with the columns the viewer reads.

    Parameters
    ----------
    trackframe_gpkg : Path
        The global NISAR TrackFrame GeoPackage.
    scope : str
        ``na``, ``globe`` or ``bbox``.
    bbox : tuple of float, optional
        ``west, south, east, north`` for the ``bbox`` scope.

    Returns
    -------
    geopandas.GeoDataFrame
        Frames in lon/lat with ``frame_idx`` (the database's row index) and
        ``direction``.

    """
    from nisar_db.geodb import filter_frames_to_na

    gdf = gpd.read_file(trackframe_gpkg)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    gdf["frame_idx"] = gdf.index
    if scope == "na":
        gdf = filter_frames_to_na(gdf)
    elif scope == "bbox":
        if bbox is None:
            raise ValueError("the bbox scope needs a bbox")
        gdf = gdf[gdf.intersects(box(*bbox))]
    elif scope != "globe":
        raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    gdf = gdf.copy()
    gdf["direction"] = gdf["passDirection"].str[0]
    gdf["geometry"] = shapely.set_precision(gdf.geometry.values, GRID)
    return gdf


def search_gslc(bbox: tuple[float, ...] | None, workdir: Path) -> pd.DataFrame:
    """Search CMR for GSLC granules and parse them into the viewer's catalog."""
    from nisar_db.gslc_catalog import parse_gslc_list, write_catalog_csv
    from nisar_db.search.cmr import search_nisar_products
    from nisar_db.search.frames import products_to_dataframe

    products = search_nisar_products(bbox=bbox, product_type="GSLC", max_results=0)
    found = products_to_dataframe(products)
    listing = workdir / "gslc_search.csv"
    found[["name", "url"]].to_csv(listing, index=False)
    parsed, _failed = parse_gslc_list(listing)
    catalog_csv = workdir / "gslc_catalog.csv"
    write_catalog_csv(parsed, catalog_csv)
    return gen.load_gslc_catalog_csv(catalog_csv)


def gunw_rows(names: list[str]) -> pd.DataFrame:
    """Turn GUNW granule names into the viewer's GUNW catalog rows.

    The fields are those ``load_gunw_catalog`` reads from the GUNW catalog JSON:
    track 5, direction 6, frame 7, mode 9, polarization 10, reference start 11,
    secondary start 13 and coverage 17.

    Examples
    --------
    >>> name = ("NISAR_L2_PR_GUNW_030_155_D_084_031_4000_SH_20260916T231125_"
    ...         "20260916T231159_20260928T231125_20260928T231159_P05023_N_F_J_001")
    >>> gunw_rows([name]).iloc[0][["track", "ref", "sec", "mode", "coverage"]].tolist()
    [155, '2026-09-16', '2026-09-28', '4000', 'F']

    """
    rows = []
    for name in names:
        parts = str(name).split("_")
        if len(parts) < 18 or parts[3] != "GUNW":
            continue
        rows.append(
            {
                "track": int(parts[5]),
                "frame": int(parts[7]),
                "direction": parts[6],
                "ref": gen._iso_date(parts[11][:8]),
                "sec": gen._iso_date(parts[13][:8]),
                "mode": parts[9],
                "coverage": parts[17],
                "polarization": parts[10],
                "granule_id": name,
            }
        )
    return pd.DataFrame(rows, columns=gen._GUNW_COLUMNS).drop_duplicates("granule_id")


def search_gunw(bbox: tuple[float, ...] | None) -> pd.DataFrame:
    """Search CMR for GUNW granules and parse them into the viewer's catalog."""
    from nisar_db.search.cmr import search_nisar_products

    products = search_nisar_products(bbox=bbox, product_type="GUNW", max_results=0)
    return gunw_rows([p.name for p in products])


def merged_cache(
    repo_cache: Path,
    local_cache: Path | None,
    ids: list[str],
    collector: Callable[..., None] | None = None,
    label: str = "",
    progress: Callable[[str], None] = print,
    workers: int = 16,
) -> dict[str, dict]:
    """Read a per-granule cache, the repo's merged with a local one.

    Parameters
    ----------
    repo_cache : Path
        The cache kept in the repository (flags or QA metrics).
    local_cache : Path or None
        A cache beside the built pages that collection writes to.
    ids : list of str
        The granules of this view.
    collector : callable, optional
        ``collect(ids, output, workers, progress=...)`` from
        ``collect_granule_flags`` or ``collect_granule_qa``. When given, the
        granules of ``ids`` neither cache holds are read into ``local_cache``
        first.
    label : str
        What is collected, for the progress messages.
    progress : callable
        Called with a short message as collection advances.
    workers : int
        Worker processes reading at once.

    Returns
    -------
    dict
        Granule id to its flags or QA metrics.

    """
    cache = gen.load_granule_flags(repo_cache) if repo_cache.exists() else {}
    if local_cache is not None and local_cache.exists():
        cache.update(gen.load_granule_flags(local_cache))
    if collector is None:
        return cache
    if local_cache is None:
        raise ValueError("collecting needs a local cache to write to")
    missing = [g for g in dict.fromkeys(ids) if g not in cache]
    if not missing:
        progress(f"All {label} already cached")
        return cache
    progress(f"Collecting {label} for {len(missing):,} granules")
    collector(
        missing,
        local_cache,
        workers,
        progress=lambda m: progress(f"{label}: {m.strip()}"),
    )
    cache.update(gen.load_granule_flags(local_cache))
    return cache


def build_view(
    scope: str,
    output: Path,
    trackframe_gpkg: Path,
    bbox: tuple[float, ...] | None = None,
    progress: Callable[[str], None] = print,
    collect_flags: bool = False,
    collect_qa: bool = False,
    cache_dir: Path | None = None,
) -> dict:
    """Search CMR, build the frame data for ``scope`` and write the page.

    Parameters
    ----------
    scope : str
        ``na``, ``globe`` or ``bbox``.
    output : Path
        HTML file to write.
    trackframe_gpkg : Path
        The global NISAR TrackFrame GeoPackage.
    bbox : tuple of float, optional
        ``west, south, east, north`` for the ``bbox`` scope.
    progress : callable
        Called with a short message as each step starts.
    collect_flags, collect_qa : bool
        Read the granule flags / QA metrics the caches miss before building
        (needs ``~/.netrc`` and ``cache_dir``).
    cache_dir : Path, optional
        Where the local flag and QA caches live.

    Returns
    -------
    dict
        The page's ``META``.

    """
    progress("Selecting frames")
    gdf = select_frames(trackframe_gpkg, scope, bbox)
    gdf["isCalVal"] = gen.flag_calval_frames(gdf, gpd.read_file(gen.CALVAL_SITES))
    rollout_options, gdf["rollout"], gdf["rollout_regions"] = gen.rollout_by_frame(
        gdf, gen.ROLLOUT_REGIONS
    )
    rollout_regions = gen.rollout_overview(
        gdf, gen.ROLLOUT_REGIONS, rollout_options, list(gdf["rollout"])
    )
    search_box = tuple(bbox) if scope == "bbox" and bbox is not None else None

    with tempfile.TemporaryDirectory() as tmp:
        progress(f"Searching CMR for GSLC granules ({len(gdf)} frames)")
        catalog = search_gslc(search_box, Path(tmp))
    progress(f"Searching CMR for GUNW granules ({len(catalog)} GSLC found)")
    gunw = search_gunw(search_box)

    ids = list(catalog["granule_id"]) + list(gunw["granule_id"])
    local = Path(cache_dir) if cache_dir is not None else None
    flags_collector: Callable[..., None] | None = None
    qa_collector: Callable[..., None] | None = None
    if collect_flags:
        from collect_granule_flags import collect

        flags_collector = collect
    if collect_qa:
        from collect_granule_qa import collect as collect_qa_metrics

        qa_collector = collect_qa_metrics
    flags = merged_cache(
        GRANULE_FLAGS,
        local / "granule_flags.json.gz" if local else None,
        ids,
        flags_collector,
        "flags",
        progress,
    )
    qa = merged_cache(
        GRANULE_QA,
        local / "granule_qa.json.gz" if local else None,
        ids,
        qa_collector,
        "QA metrics",
        progress,
    )
    progress(f"Building {len(gdf)} frames ({len(gunw)} GUNW found)")
    blackout = (
        gen.load_period_json(BLACKOUT_JSON, "blackout_dates", "data")
        if BLACKOUT_JSON.exists()
        else None
    )
    frame_data = gen.build_frame_data(gdf, catalog, None, blackout, None, gunw=gunw)
    n_flagged = gen.attach_granule_flags(frame_data, flags) if flags else 0
    n_qa = gen.attach_granule_qa(frame_data, qa) if qa else 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_with = sum(1 for f in frame_data["features"] if f["properties"]["gslc_count"])
    meta = {
        "title": "OPERA NISAR-DB Viewer",
        "generated_at": now,
        "catalog_queried_at": now,
        "catalog_source": "CMR (local search)",
        "catalog_kind": "cmr",
        "n_frames": len(frame_data["features"]),
        "n_frames_with_gslc": n_with,
        "n_granules": sum(
            len(f["properties"]["granules"]) for f in frame_data["features"]
        ),
        "n_catalog_rows": int(len(catalog)),
        "consistent_source": "computed",
        "has_blackout": blackout is not None,
        "has_reference": False,
        "has_gunw": True,
        "has_flags": n_flagged > 0,
        "has_qa": n_qa > 0,
        "rollout_options": rollout_options,
        "rollout_source": gen.ROLLOUT_REGIONS.name,
        "n_gunw": sum(
            f["properties"].get("gunw_count", 0) for f in frame_data["features"]
        ),
        "view_scope": scope,
        "view_label": SCOPE_LABELS[scope],
        "view_bbox": list(bbox) if scope == "bbox" and bbox is not None else None,
    }

    progress("Writing the page")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(gen.render_html(frame_data, meta, None, rollout_regions))
    progress(
        f"Done: {meta['n_frames']} frames, {n_with} with GSLC, "
        f"{meta['n_gunw']} GUNW ({output.stat().st_size / 1e6:.1f} MB)"
    )
    return meta


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    from nisar_db.geodb import get_trackframe_db

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scope", choices=SCOPES, default="na")
    parser.add_argument("--bbox", help="west,south,east,north for --scope bbox")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--trackframe-gpkg",
        type=Path,
        default=None,
        help="TrackFrame GeoPackage; downloaded next to --output when omitted.",
    )
    parser.add_argument(
        "--collect-flags",
        action="store_true",
        help="Read the granule flags the caches miss (needs --cache-dir, ~/.netrc).",
    )
    parser.add_argument(
        "--collect-qa",
        action="store_true",
        help="Read the QA metrics the caches miss (needs --cache-dir, ~/.netrc).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Local flag / QA caches, merged with the repository's.",
    )
    args = parser.parse_args(argv)
    bbox = tuple(float(v) for v in args.bbox.split(",")) if args.bbox else None

    def say(msg: str) -> None:
        # One line per step: the viewer helper reads them as progress.
        print(msg, flush=True)

    if args.trackframe_gpkg is None:
        say("Fetching the NISAR frame database")
    gpkg = args.trackframe_gpkg or get_trackframe_db(output_dir=args.output.parent)
    meta = build_view(
        args.scope,
        args.output,
        gpkg,
        bbox,
        progress=say,
        collect_flags=args.collect_flags,
        collect_qa=args.collect_qa,
        cache_dir=args.cache_dir,
    )
    say(META_LINE + json.dumps({"n_frames": meta["n_frames"]}))


if __name__ == "__main__":
    main()
