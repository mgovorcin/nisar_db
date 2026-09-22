"""Behavioural tests for the frame viewer's JavaScript, run under node.

The viewer is a single template string, so each test lifts the functions it
needs out of ``APP_JS`` and runs them on a few fake granules. Skipped where
node is not installed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_scope_viewer.py"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = """
const DAY_MS = 86400000;
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
                "Jul","Aug","Sep","Oct","Nov","Dec"];
const CHART_PALETTE = ["#111111", "#222222"];
const DIR_LABEL = {A: "Ascending", D: "Descending"};
const window = {innerWidth: 900};
let chartPoints = [];
let modeKeyOrder = null;
"""

GRANULES = [
    {
        "date": "2025-11-01",
        "mode": "20",
        "cov": "F",
        "pol": "HH",
        "cycle": 1,
        "dir": "A",
        "gid": "g1",
    },
    {
        "date": "2025-11-01",
        "mode": "20",
        "cov": "F",
        "pol": "HH",
        "cycle": 1,
        "dir": "A",
        "gid": "g2",
    },
    {
        "date": "2026-08-01",
        "mode": "40",
        "cov": "F",
        "pol": "HH",
        "cycle": 9,
        "dir": "D",
        "gid": "g3",
    },
]


def _app_js() -> str:
    spec = importlib.util.spec_from_file_location("generate_scope_viewer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.APP_JS)


def _function(js: str, name: str) -> str:
    match = re.search(rf"\n  function {name}\(.*?\n  \}}\n", js, re.S)
    assert match, f"function {name} not found in APP_JS"
    return match.group(0)


def run_js(names: list[str], body: str, frames: list | None = None) -> Any:
    """Run ``body`` after defining ``names`` from APP_JS; return its printed JSON."""
    js = _app_js()
    consts = "".join(
        m.group(0) + "\n" for m in re.finditer(r"  const DUP_ROW = [^\n]*;", js)
    )
    source = (
        PRELUDE
        + consts
        + f"const FRAME_DATA = {json.dumps({'features': frames or []})};\n"
        + f"const GRANULES = {json.dumps(GRANULES)};\n"
        + "".join(_function(js, n) for n in names)
        + f"console.log(JSON.stringify((() => {{ {body} }})()));\n"
    )
    assert NODE is not None
    out = subprocess.run(
        [NODE, "-e", source], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


CHART = [
    "uniqSorted",
    "asArray",
    "modeKeys",
    "modeColor",
    "timeTicks",
    "modeTimelineSvg",
]
LANES = (
    "const lanes = [...svg.matchAll(/chart-row-label[^>]*>([^<]*)/g)]"
    ".map(m => m[1]);"
)


def test_duplicate_groups_share_date_mode_and_coverage() -> None:
    groups = run_js(
        ["duplicateGroups"],
        "return duplicateGroups(GRANULES).map(gs => gs.map(g => g.gid));",
    )
    assert groups == [["g1", "g2"]]


def test_duplicate_list_names_every_granule_in_a_group() -> None:
    html = run_js(
        ["duplicateGroups", "duplicateRowsHtml"],
        "return duplicateRowsHtml(duplicateGroups(GRANULES));",
    )
    assert "2 granules" in html
    assert "g1" in html and "g2" in html and "g3" not in html


def test_plot_adds_a_duplicates_row_only_when_there_are_duplicates() -> None:
    result = run_js(
        CHART,
        "const svg = modeTimelineSvg(GRANULES); "
        + LANES
        + " const dups = chartPoints.filter(p => p.group)"
        + ".map(p => p.group.map(g => g.gid));"
        + " const svg2 = modeTimelineSvg(GRANULES.slice(2));"
        + " return {lanes, dups, single: svg2.includes('>duplicates<')};",
    )
    assert result["lanes"] == ["20_F", "40_F", "duplicates"]
    assert result["dups"] == [["g1", "g2"]]
    assert result["single"] is False


def test_search_reads_lat_lon_in_either_order() -> None:
    result = run_js(
        ["parseCoords"],
        "return [parseCoords('34.2 -118.2'), parseCoords('-118.2, 34.2'),"
        " parseCoords('Los Angeles')];",
    )
    assert result == [{"lat": 34.2, "lon": -118.2}, {"lat": 34.2, "lon": -118.2}, None]


def test_search_matches_frames_by_id_or_track_and_frame() -> None:
    frames = [
        {"properties": {"frame_idx": 8109, "track": 47, "frame": 14}},
        {"properties": {"frame_idx": 8110, "track": 47, "frame": 15}},
        {"properties": {"frame_idx": 19776, "track": 113, "frame": 65}},
    ]
    result = run_js(
        ["matchFrames"],
        "const ids = q => matchFrames(q).map(f => f.properties.frame_idx);"
        " return [ids('81'), ids('T113_F65'), ids('t47 f15'), ids('Denver')];",
        frames,
    )
    assert result == [[8109, 8110], [19776], [8110], []]
