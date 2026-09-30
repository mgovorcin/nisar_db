"""Unit tests for the frame viewer's CalVal frame selection."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import geopandas as gpd
from shapely.geometry import box

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_scope_viewer.py"


def _viewer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_scope_viewer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frames() -> gpd.GeoDataFrame:
    # (track, frame, pass, lon0, lat0, lon1, lat1) around a site at 0-2 E, 0-2 N
    rows = [
        (1, 10, "Ascending", 0.0, -1.0, 2.0, 1.0),  # lower half of the site
        (1, 11, "Ascending", 0.0, 1.0, 2.0, 3.0),  # upper half
        (1, 12, "Ascending", 0.0, 1.9, 2.0, 4.0),  # clips the top edge
        (2, 10, "Ascending", 1.5, 0.0, 3.5, 2.0),  # neighbouring track, a quarter
        (2, 11, "Ascending", 1.9, 0.0, 3.9, 2.0),  # neighbouring track, a sliver
        (3, 50, "Descending", -0.5, 0.0, 1.5, 2.0),
        (4, 50, "Descending", 5.0, 5.0, 7.0, 7.0),  # nowhere near
    ]
    return gpd.GeoDataFrame(
        {
            "track": [r[0] for r in rows],
            "frame": [r[1] for r in rows],
            "passDirection": [r[2] for r in rows],
        },
        geometry=[box(*r[3:]) for r in rows],
        crs=4326,
    )


def test_calval_frames_cover_a_site_in_both_directions() -> None:
    frames = _frames()
    sites = gpd.GeoDataFrame(geometry=[box(0.0, 0.0, 2.0, 2.0)], crs=4326)

    flagged = frames[_viewer().flag_calval_frames(frames, sites)]

    assert sorted(zip(flagged.track, flagged.frame, strict=True)) == [
        (1, 10),
        (1, 11),
        (2, 10),
        (3, 50),
    ]


def test_shipped_calval_sites_are_the_disp_s1_frames_and_mexico_city() -> None:
    sites = gpd.read_file(_viewer().CALVAL_SITES)

    assert sites[sites.frame_id.isna()].site.tolist() == ["Mexico City"]
    assert sorted(sites.frame_id.dropna().astype(int)) == [
        8622,
        8882,
        9156,
        11115,
        11116,
        12640,
        18903,
        28486,
        33039,
        33065,
        36542,
        42779,
    ]
