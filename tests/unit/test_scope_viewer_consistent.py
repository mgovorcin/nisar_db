"""Unit tests for the frame viewer's consistent-mode label."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pandas as pd

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_scope_viewer.py"


def _viewer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_scope_viewer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _granules(*modes: str) -> pd.DataFrame:
    return pd.DataFrame({"mode": list(modes), "coverage": ["F"] * len(modes)})


def test_consistent_mode_is_only_ever_a_science_mode() -> None:
    viewer = _viewer()

    assert viewer.common_mode_coverage(_granules("0505", "0505", "2005")) == (
        "2005",
        "F",
    )
    assert viewer.common_mode_coverage(_granules("0505", "0005")) == ("none", "none")


def test_summarize_frame_stores_the_cycle_as_a_number() -> None:
    # The CMR catalog keeps the cycle as zero-padded text; the viewer's cycle
    # filter compares numbers.
    group = pd.DataFrame(
        {
            "granule_id": ["a", "b"],
            "date": ["2026-06-14", "2026-06-26"],
            "start_datetime": ["2026-06-14T06:31", "2026-06-26T06:31"],
            "mode": ["4005", "4005"],
            "coverage": ["F", "F"],
            "polarization": ["DHDH", "DHDH"],
            "cycle": ["023", "024"],
        }
    )

    granules = _viewer().summarize_frame(group)["granules"]

    assert [g["cycle"] for g in granules] == [23, 24]
