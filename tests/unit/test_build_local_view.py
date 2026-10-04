"""Unit tests for building a viewer page for another scope on demand."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import geopandas as gpd
import pytest
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GUNW = (
    "NISAR_L2_PR_GUNW_030_155_D_084_031_4000_SH_20260916T231125_20260916T231159"
    "_20260928T231125_20260928T231159_P05023_N_F_J_001"
)


def _load(name: str) -> ModuleType:
    # The builder imports the viewer generator the way it does from scripts/.
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trackframe(tmp_path: Path) -> Path:
    # Three frames: two in California, one over Africa.
    gdf = gpd.GeoDataFrame(
        {
            "track": [1, 2, 3],
            "frame": [10, 11, 12],
            "passDirection": ["Ascending", "Descending", "Ascending"],
            "isCalVal": [False] * 3,
            "isSNWG": [False] * 3,
            "isDNC": [False] * 3,
        },
        geometry=[
            box(-122.5, 36.0, -121.5, 37.0),
            box(-120.5, 38.0, -119.5, 39.0),
            box(10.0, 20.0, 11.0, 21.0),
        ],
        crs=4326,
    )
    path = tmp_path / "tf.gpkg"
    gdf.to_file(path, driver="GPKG")
    return path


def test_select_frames_keeps_the_trackframe_index(tmp_path: Path) -> None:
    builder = _load("build_local_view")
    gpkg = _trackframe(tmp_path)

    globe = builder.select_frames(gpkg, "globe")
    boxed = builder.select_frames(gpkg, "bbox", (-123.0, 35.0, -121.0, 37.5))

    assert list(globe["frame_idx"]) == [0, 1, 2]
    assert list(globe["direction"]) == ["A", "D", "A"]
    # The frame number OPERA uses is the database row, whatever the scope.
    assert list(boxed["frame_idx"]) == [0]
    with pytest.raises(ValueError):
        builder.select_frames(gpkg, "bbox")


def test_gunw_rows_parse_the_granule_names() -> None:
    rows = _load("build_local_view").gunw_rows([GUNW, GUNW, "not a gunw"])

    assert len(rows) == 1
    row = rows.iloc[0]
    assert (row["track"], row["frame"], row["direction"]) == (155, 84, "D")
    assert (row["ref"], row["sec"]) == ("2026-09-16", "2026-09-28")
    assert (row["mode"], row["polarization"], row["coverage"]) == ("4000", "SH", "F")


def test_build_requests_are_checked(tmp_path: Path) -> None:
    helper = _load("qa_browse_server")
    builds = helper.ViewBuilds(tmp_path)

    assert builds.status() == {"state": "idle"}
    with pytest.raises(ValueError):
        builds.start("moon", None)
    with pytest.raises(ValueError):
        builds.start("bbox", [10, 0, 5, 1])  # west east of east
    # Only ids the helper writes are served.
    assert builds.page("../secrets") is None


def test_merged_cache_collects_only_the_missing_granules(tmp_path: Path) -> None:
    import gzip
    import json

    builder = _load("build_local_view")
    repo = tmp_path / "repo.json.gz"
    with gzip.open(repo, "wt") as fh:
        json.dump({"A": {"r": 1}}, fh)
    local = tmp_path / "local.json.gz"
    asked: list[list[str]] = []

    def collector(ids, output, _workers, **_kwargs):
        asked.append(list(ids))
        with gzip.open(output, "wt") as fh:
            json.dump({g: {"r": 0} for g in ids}, fh)

    merged = builder.merged_cache(
        repo, local, ["A", "B", "B", "C"], collector, "flags", progress=lambda _: None
    )

    assert asked == [["B", "C"]]
    assert merged == {"A": {"r": 1}, "B": {"r": 0}, "C": {"r": 0}}
    # Without a collector the caches are only read.
    assert builder.merged_cache(repo, local, ["Z"]) == merged


def _fake_build(lines: list[str], returncode: int) -> contextlib.nullcontext:
    """Stand in for ``subprocess.Popen`` of build_local_view.py."""
    return contextlib.nullcontext(
        SimpleNamespace(
            stdout=iter(line + "\n" for line in lines), returncode=returncode
        )
    )


def test_build_runs_as_a_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load("qa_browse_server")
    builds = helper.ViewBuilds(tmp_path)
    seen: list[list[str]] = []
    lines = [
        "Selecting frames",
        "Building 3 frames",
        helper.META_LINE + '{"n_frames": 3}',
    ]

    def popen(cmd: list[str], **_kwargs: object) -> contextlib.nullcontext:
        seen.append(cmd)
        return _fake_build(lines, 0)

    monkeypatch.setattr(helper.subprocess, "Popen", popen)
    job = {
        "id": "bbox-1",
        "scope": "bbox",
        "bbox": [1, 2, 3, 4],
        "collect_flags": True,
        "collect_qa": False,
    }
    builds._job = dict(job, state="running")
    builds._run(job)

    status = builds.status()
    assert status["state"] == "done" and status["n_frames"] == 3
    assert status["view"] == "/view/bbox-1"
    assert "--collect-flags" in seen[0] and "--collect-qa" not in seen[0]
    assert "--bbox=1,2,3,4" in seen[0]


def test_a_killed_build_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load("qa_browse_server")
    builds = helper.ViewBuilds(tmp_path)
    monkeypatch.setattr(
        helper.subprocess,
        "Popen",
        lambda _cmd, **_kw: _fake_build(["flags: 200/900 read"], -9),
    )
    job = {
        "id": "globe-1",
        "scope": "globe",
        "bbox": None,
        "collect_flags": False,
        "collect_qa": False,
    }
    builds._job = dict(job, state="running")
    builds._run(job)

    status = builds.status()
    assert status["state"] == "error"
    assert "killed (signal 9)" in status["step"]
