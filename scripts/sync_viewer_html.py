#!/usr/bin/env python
"""Refresh a built viewer's markup, styles and app code in place.

``generate_scope_viewer.py`` needs the frame GeoPackage and a GSLC catalog, which
are not in the repository, so a checked-in viewer cannot simply be rebuilt after
a UI change. This script swaps the generated parts of an existing HTML file --
``APP_CSS``, ``BODY_HTML``, ``APP_JS`` and the GPS site collection -- for the
current ones, re-derives the frames' CalVal flag and rollout options from the
current site and rollout lists, optionally attaches blackout dates and a GUNW
catalog and,
given a granule flag cache, attaches the per-granule flags. The vendored MapLibre
bundle and the rest of the embedded frame data are left untouched.

Examples
--------
Update the copies tracked in the repository::

    python scripts/sync_viewer_html.py \\
        scripts/opera_nisar_db_viewer.html docs/assets/opera_nisar_db_viewer.html

"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd

import generate_scope_viewer as gen

#: Marks the end of the vendored MapLibre stylesheet and the start of ours.
_STYLE_SPLIT = "</style>\n<style>"


def _replace_app_css(html: str) -> str:
    head, sep, rest = html.partition(_STYLE_SPLIT)
    if not sep:
        raise ValueError("no second <style> block: not a generated viewer")
    _, close, tail = rest.partition("</style>")
    return f"{head}{sep}{gen.APP_CSS}{close}{tail}"


def _replace_body(html: str) -> str:
    head, sep, rest = html.partition("</head>\n")
    if not sep:
        raise ValueError("no </head>: not a generated viewer")
    _, script, tail = rest.partition("\n<script>")
    return f"{head}{sep}{gen.BODY_HTML}{script}{tail}"


def _replace_app_js(html: str) -> str:
    marker = "<script>"
    start = html.rindex(marker) + len(marker)
    end = html.index("</script>", start)
    return f"{html[:start]}{gen.APP_JS}{html[end:]}"


def _upsert_const(html: str, name: str, value: dict) -> str:
    payload = json.dumps(value, separators=(",", ":"))
    if f"const {name}" in html:
        # The payload is one line of JSON, with or without a trailing newline.
        return re.sub(
            rf"const {name} = [^\n]*;",
            lambda _: f"const {name} = {payload};",
            html,
            count=1,
        )
    # Append to the data script, which is the one holding META.
    anchor = re.search(r"(const META = .*?;)", html, flags=re.S)
    if anchor is None:
        raise ValueError("no META block: not a generated viewer")
    return html.replace(
        anchor.group(1), f"{anchor.group(1)}\nconst {name} = {payload};", 1
    )


def _refresh_frame_data(
    html: str,
    calval_sites: gpd.GeoDataFrame,
    granule_flags: dict[str, dict] | None,
    rollout: Path,
    blackout: dict[str, list] | None,
    gunw: pd.DataFrame | None,
    granule_qa: dict[str, dict] | None = None,
) -> str:
    opener = "const FRAME_DATA = "
    start = html.index(opener) + len(opener)
    end = html.index(";\nconst META", start)
    frame_data = json.loads(html[start:end])
    frames = gpd.GeoDataFrame.from_features(frame_data["features"], crs=4326)
    flags = gen.flag_calval_frames(frames, calval_sites)
    options, frame_rollout, frame_regions = gen.rollout_by_frame(frames, rollout)
    gunw_stats = (
        {
            key: gen.summarize_gunw(grp)
            for key, grp in gunw.groupby(["track", "frame", "direction"])
        }
        if gunw is not None
        else {}
    )
    for feature, flag, opts, names in zip(
        frame_data["features"], flags, frame_rollout, frame_regions, strict=True
    ):
        feature["properties"]["isCalVal"] = bool(flag)
        feature["properties"]["rollout"] = opts
        feature["properties"]["rollout_regions"] = names
        # OPERA processes only the science modes; a page built before that rule
        # may label a frame with another mode, which is no consistent mode.
        if feature["properties"].get("cons_mode") not in gen.STANDARD_MODES:
            feature["properties"]["cons_mode"] = "none"
            feature["properties"]["cons_cov"] = "none"
        if blackout is not None:
            props = feature["properties"]
            bo = gen.blackout_summary(blackout.get(str(props["frame_idx"]), []))
            props["has_blackout"] = bo["n_windows"] > 0
            props["blackout_months"] = bo["months"]
            props["blackout_label"] = bo["label"]
            props["blackout_start_month"] = bo["start_month"]
            props["blackout_end_month"] = bo["end_month"]
            props["blackout_windows"] = bo["n_windows"]
            props["blackout_ranges"] = bo["ranges"]
        if gunw is not None:
            props = feature["properties"]
            key = (props["track"], props["frame"], props["passDirection"][0])
            g = gunw_stats.get(key)
            props["gunw_count"] = g["gunw_count"] if g else 0
            props["gunw_pairs"] = g["gunw_pairs"] if g else 0
            props["gunw_modes"] = g["gunw_modes"] if g else []
            props["gunw_pols"] = g["gunw_pols"] if g else []
            props["gunw_dt_min"] = g["gunw_dt_min"] if g else 0
            props["gunw_dt_max"] = g["gunw_dt_max"] if g else 0
            props["gunw_ifgs"] = g["gunw_ifgs"] if g else []
    n_flagged = None
    if granule_flags is not None:
        n_flagged = gen.attach_granule_flags(frame_data, granule_flags)
        print(f"  flags for {n_flagged} granules / interferograms")
    n_qa = None
    if granule_qa is not None:
        n_qa = gen.attach_granule_qa(frame_data, granule_qa)
        print(f"  QA metrics for {n_qa} granules / interferograms")
    payload = json.dumps(frame_data, separators=(",", ":"))
    html = f"{html[:start]}{payload}{html[end:]}"
    overview = gen.rollout_overview(frames, rollout, options, frame_rollout)
    html = _upsert_const(html, "ROLLOUT_DATA", overview)
    # META is one line of JSON; the viewer only offers the flag and rollout views
    # when it says the page carries them.
    match = re.search(r"const META = ([^\n]*);", html)
    if match is None:
        raise ValueError("no META block: not a generated viewer")
    meta = json.loads(match.group(1))
    meta["rollout_options"] = options
    meta["rollout_source"] = rollout.name
    if blackout is not None:
        meta["has_blackout"] = True
    if gunw is not None:
        meta["has_gunw"] = True
        meta["n_gunw"] = sum(
            f["properties"]["gunw_count"] for f in frame_data["features"]
        )
    if n_flagged is not None:
        meta["has_flags"] = n_flagged > 0
    if n_qa is not None:
        meta["has_qa"] = n_qa > 0
    return html.replace(
        match.group(0), f"const META = {json.dumps(meta, separators=(',', ':'))};", 1
    )


def sync(
    path: Path,
    gps_sites: dict,
    calval_sites: gpd.GeoDataFrame,
    granule_flags: dict[str, dict] | None = None,
    rollout: Path = gen.ROLLOUT_REGIONS,
    blackout: dict[str, list] | None = None,
    gunw: pd.DataFrame | None = None,
    granule_qa: dict[str, dict] | None = None,
) -> None:
    """Rewrite ``path`` with the current generated blocks."""
    html = path.read_text()
    html = _replace_app_css(html)
    html = _replace_body(html)
    html = _replace_app_js(html)
    html = _upsert_const(html, "UNR_GPS_DATA", gps_sites)
    html = _upsert_const(html, "VOLCANO_DATA", gen.load_volcanoes())
    html = _refresh_frame_data(
        html, calval_sites, granule_flags, rollout, blackout, gunw, granule_qa
    )
    path.write_text(html)
    print(f"synced {path} ({path.stat().st_size / 1e6:.1f} MB)")


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("html", type=Path, nargs="+", help="Built viewer(s) to update.")
    parser.add_argument(
        "--gps-source",
        default=gen.NGL_STATION_MAP,
        help="UNR/NGL station map URL, a local copy of that page, or a GeoJSON "
        "of sites to embed.",
    )
    parser.add_argument(
        "--no-gps", action="store_true", help="Embed an empty GPS collection."
    )
    parser.add_argument(
        "--calval-sites",
        type=Path,
        default=gen.CALVAL_SITES,
        help="GeoJSON of CalVal site polygons the frames are flagged against.",
    )
    parser.add_argument(
        "--granule-flags",
        type=Path,
        default=None,
        help="Per-granule flag cache (from collect_granule_flags.py) to attach.",
    )
    parser.add_argument(
        "--granule-qa",
        type=Path,
        default=None,
        help="Per-granule QA cache (from collect_granule_qa.py) to attach.",
    )
    parser.add_argument(
        "--rollout",
        type=Path,
        default=gen.ROLLOUT_REGIONS,
        help="Rollout regions GeoJSON, or a {option: [frame_idx, ...]} JSON.",
    )
    parser.add_argument(
        "--blackout-json",
        type=Path,
        default=None,
        help="Per-frame blackout-dates JSON (from 'nisar-db create-blackout-dates') "
        "to attach; without it the page keeps whatever blackout data it has.",
    )
    parser.add_argument(
        "--gunw-catalog",
        type=Path,
        default=None,
        help="GUNW catalog (gunw_interferograms.json[.gz]) to attach; adds the "
        "GSLC / GUNW switch to a page built without one.",
    )
    args = parser.parse_args(argv)

    gps_sites = gen.load_gps_sites(None if args.no_gps else args.gps_source)
    calval_sites = gpd.read_file(args.calval_sites)
    granule_flags = (
        gen.load_granule_flags(args.granule_flags) if args.granule_flags else None
    )
    blackout = (
        gen.load_period_json(args.blackout_json, "blackout_dates", "data")
        if args.blackout_json
        else None
    )
    gunw = gen.load_gunw_catalog(args.gunw_catalog) if args.gunw_catalog else None
    granule_qa = gen.load_granule_flags(args.granule_qa) if args.granule_qa else None
    for path in args.html:
        sync(
            path,
            gps_sites,
            calval_sites,
            granule_flags,
            args.rollout,
            blackout,
            gunw,
            granule_qa,
        )


if __name__ == "__main__":
    main()
