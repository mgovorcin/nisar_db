#!/usr/bin/env python
"""Write the DISP-S1 rollout regions the frame viewer maps onto NISAR frames.

The OPERA DISP-S1 North America rollout is defined on Sentinel-1 frames in
``opera-adt/burst_db``: a priority 0-4 per frame, with priority 3 split into the
3a and 3b deliveries. NISAR has no rollout list of its own yet, so the viewer
tags each NISAR frame with the S1 rollout options it overlaps
(``generate_scope_viewer.rollout_by_frame``). This script flattens the three
burst_db files into the one GeoJSON the viewer reads, one polygon per S1 frame
carrying its ``rollout`` option (``P0`` ... ``P4``, ``P3a`` / ``P3b``) and
``region_name``.

Examples
--------
Refresh the shipped copy from burst_db's main branch::

    python scripts/make_rollout_regions.py

"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd

BURST_DB_DATA = (
    "https://raw.githubusercontent.com/opera-adt/burst_db/main/src/burst_db/data"
)
PRIORITY_FILE = f"{BURST_DB_DATA}/NApriorityrollout_framebased_v8_13Mar2025.geojson"
REGION_3A_FILE = f"{BURST_DB_DATA}/region3a_v1_23Jul2025.json"
REGION_3B_FILE = f"{BURST_DB_DATA}/region3b_v1_23Jul2025.json"
OUTPUT = Path(__file__).resolve().parent / "disp_s1_rollout_regions.geojson"


def build_rollout_regions(
    priority: gpd.GeoDataFrame, region_3a: gpd.GeoDataFrame, region_3b: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Label every S1 frame with its rollout option.

    Parameters
    ----------
    priority : geopandas.GeoDataFrame
        The priority rollout, with ``frame_id``, ``priority`` and ``region_name``.
    region_3a, region_3b : geopandas.GeoDataFrame
        The two deliveries priority 3 is split into, keyed by ``frame_id``.

    Returns
    -------
    geopandas.GeoDataFrame
        ``frame_id``, ``rollout``, ``region_name`` and ``orbit_pass`` per frame.

    Raises
    ------
    ValueError
        If a priority-3 frame is in neither or both of the 3a / 3b lists.

    """
    in_3a = priority.frame_id.isin(set(region_3a.frame_id))
    in_3b = priority.frame_id.isin(set(region_3b.frame_id))
    is_p3 = priority.priority == 3
    if (is_p3 & (in_3a == in_3b)).any():
        raise ValueError("priority-3 frames must be in exactly one of 3a / 3b")
    rollout = "P" + priority.priority.astype(int).astype(str)
    rollout[is_p3 & in_3a] = "P3a"
    rollout[is_p3 & in_3b] = "P3b"
    out = gpd.GeoDataFrame(
        {
            "frame_id": priority.frame_id.astype(int),
            "rollout": rollout,
            "region_name": priority.region_name,
            "orbit_pass": priority.orbit_pass,
        },
        geometry=priority.geometry,
        crs=4326,
    )
    return out.sort_values(["rollout", "frame_id"]).reset_index(drop=True)


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--priority", default=PRIORITY_FILE)
    parser.add_argument("--region-3a", default=REGION_3A_FILE)
    parser.add_argument("--region-3b", default=REGION_3B_FILE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)

    regions = build_rollout_regions(
        gpd.read_file(args.priority),
        gpd.read_file(args.region_3a),
        gpd.read_file(args.region_3b),
    )
    regions.to_file(args.output, driver="GeoJSON", COORDINATE_PRECISION=4)
    print(
        f"Wrote {args.output}: {regions.rollout.value_counts().sort_index().to_dict()}"
    )


if __name__ == "__main__":
    main()
