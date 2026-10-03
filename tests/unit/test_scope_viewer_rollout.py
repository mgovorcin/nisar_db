"""Unit tests for tagging frames with rollout options."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import geopandas as gpd
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _frames() -> gpd.GeoDataFrame:
    # Unit squares along the equator at x = 0, 1, 2, 3.
    return gpd.GeoDataFrame(
        {"frame_idx": [10, 11, 12, 13]},
        geometry=[box(x, 0.0, x + 1.0, 1.0) for x in range(4)],
        crs=4326,
    )


def test_geojson_rollout_is_matched_by_overlap(tmp_path: Path) -> None:
    regions = gpd.GeoDataFrame(
        {
            "rollout": ["P0", "P1", "P1"],
            "region_name": ["West", "Middle", "East"],
        },
        # P0 covers frame 10 and a sliver of 11; P1 covers half of 11 and all of 12.
        geometry=[
            box(-0.5, 0.0, 1.1, 1.0),
            box(1.5, 0.0, 2.5, 1.0),
            box(2.5, 0.0, 3.05, 1.0),
        ],
        crs=4326,
    )
    path = tmp_path / "rollout.geojson"
    regions.to_file(path, driver="GeoJSON")

    options, rollout, names = _load("generate_scope_viewer").rollout_by_frame(
        _frames(), path
    )

    assert options == ["P0", "P1"]
    assert rollout == [["P0"], ["P1"], ["P1"], []]
    assert names == [["West"], ["Middle"], ["East", "Middle"], []]


def test_frame_list_rollout_is_taken_as_is(tmp_path: Path) -> None:
    path = tmp_path / "region_db.json"
    path.write_text(json.dumps({"phase_b": [12, 10], "phase_a": [10]}))

    options, rollout, names = _load("generate_scope_viewer").rollout_by_frame(
        _frames(), path
    )

    assert options == ["phase_b", "phase_a"]
    assert rollout == [["phase_b", "phase_a"], [], ["phase_b"], []]
    assert names == [[], [], [], []]


def test_shipped_rollout_splits_priority_three_into_3a_and_3b() -> None:
    regions = gpd.read_file(_load("generate_scope_viewer").ROLLOUT_REGIONS)

    assert regions.rollout.value_counts().to_dict() == {
        "P4": 843,
        "P3a": 223,
        "P1": 203,
        "P3b": 86,
        "P0": 36,
        "P2": 35,
    }


def test_build_rollout_regions_labels_3a_and_3b() -> None:
    priority = gpd.GeoDataFrame(
        {
            "frame_id": [1, 2, 3],
            "priority": [0.0, 3.0, 3.0],
            "region_name": ["A", "B", "C"],
            "orbit_pass": ["ASCENDING"] * 3,
        },
        geometry=[box(0, 0, 1, 1)] * 3,
        crs=4326,
    )
    r3a = gpd.GeoDataFrame({"frame_id": [2]}, geometry=[box(0, 0, 1, 1)], crs=4326)
    r3b = gpd.GeoDataFrame({"frame_id": [3]}, geometry=[box(0, 0, 1, 1)], crs=4326)

    out = _load("make_rollout_regions").build_rollout_regions(priority, r3a, r3b)

    assert dict(zip(out.frame_id, out.rollout, strict=True)) == {
        1: "P0",
        2: "P3a",
        3: "P3b",
    }
