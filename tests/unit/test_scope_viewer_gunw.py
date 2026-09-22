"""Unit tests for the frame viewer's GUNW catalog loading and summary."""

from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_scope_viewer.py"
GID = (
    "NISAR_L2_PR_GUNW_024_001_A_004_025_2000_{pol}_20260626T060402_20260626T060426"
    "_20260708T060401_20260708T060425_P05023_N_{cov}_J_001"
)


def _viewer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_scope_viewer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog() -> dict:
    def ifg(ref: str, sec: str, pols: list[str], cov: str = "F") -> dict:
        return {
            "track": "001",
            "frame": "004",
            "pass_direction": "A",
            "ref_date": ref,
            "sec_date": sec,
            "polarizations": {p: {"id": GID.format(pol=p, cov=cov)} for p in pols},
        }

    return {
        "interferograms": [
            ifg("20260626", "20260708", ["HH", "HV"]),
            ifg("20260626", "20260720", ["HH"], cov="P"),
        ],
        "generated_at": "2026-09-21T00:00:00+00:00",
    }


@pytest.mark.parametrize("name", ["gunw.json", "gunw.json.gz"])
def test_load_gunw_catalog_reads_plain_and_gzipped(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    payload = json.dumps(_catalog()).encode()
    path.write_bytes(gzip.compress(payload) if name.endswith(".gz") else payload)

    df = _viewer().load_gunw_catalog(path)

    assert len(df) == 3  # one row per pair and polarization
    first = df.iloc[0]
    assert (first.track, first.frame, first.direction) == (1, 4, "A")
    assert (first.ref, first.sec, first["mode"]) == ("2026-06-26", "2026-07-08", "2000")
    assert sorted(df["coverage"]) == ["F", "F", "P"]


def test_summarize_gunw_counts_pairs_and_baselines(tmp_path: Path) -> None:
    viewer = _viewer()
    path = tmp_path / "gunw.json"
    path.write_text(json.dumps(_catalog()))

    s = viewer.summarize_gunw(viewer.load_gunw_catalog(path))

    assert s["gunw_count"] == 3
    assert s["gunw_pairs"] == 2  # HH and HV of one pair count once
    assert (s["gunw_dt_min"], s["gunw_dt_max"]) == (12, 24)
    assert s["gunw_pols"] == ["HH", "HV"]
    assert s["gunw_ifgs"][0] == {
        "ref": "2026-06-26",
        "sec": "2026-07-08",
        "dt": 12,
        "mode": "2000",
        "cov": "F",
        "pol": "HH",
        "gid": GID.format(pol="HH", cov="F"),
    }
