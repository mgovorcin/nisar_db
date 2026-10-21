"""Unit tests for the QA collector's CMR orbit-type lookup (no network)."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GUNW = [
    f"NISAR_L2_PR_GUNW_030_155_D_084_031_4000_SH_20260916T231125_20260916T231159"
    f"_20260928T231125_20260928T231159_P05023_N_F_J_00{i}"
    for i in range(4)
]


def _load() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "collect_granule_qa", SCRIPTS / "collect_granule_qa.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _orbits(names: list[str]) -> dict[str, dict[str, str]]:
    return {n: {"ORBIT_TYPE": "POE" if i % 2 else "MOE"} for i, n in enumerate(names)}


def _write(path: Path, flags: dict) -> None:
    with gzip.open(path, "wt") as fh:
        json.dump(flags, fh)


def _read(path: Path) -> dict:
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


def test_orbit_types_include_granules_umm_json_drops(fake_cmr) -> None:
    qa = _load()
    cmr = fake_cmr(
        GUNW, page_dropped={"G0001-ASF"}, hidden={"G0002-ASF"}, attributes=_orbits(GUNW)
    )

    orbits = qa.cmr_orbit_types(GUNW, batch=2)

    assert orbits == {n: v["ORBIT_TYPE"] for n, v in _orbits(GUNW).items()}
    assert cmr.concept_lookups() == ["G0002-ASF"]


def test_backfill_fills_only_cached_granules_without_orbit(
    fake_cmr, tmp_path: Path
) -> None:
    qa = _load()
    fake_cmr(GUNW, hidden={"G0001-ASF"}, attributes=_orbits(GUNW))
    flags = tmp_path / "flags.json.gz"
    # GUNW[1] lost its orbit type to a UMM-JSON drop; GUNW[3] is not cached.
    _write(
        flags,
        {
            GUNW[0]: {"j": 1, "o": "MOE"},
            GUNW[1]: {"j": 0},
            GUNW[2]: {"j": 1, "o": "MOE"},
        },
    )

    filled = qa.backfill_orbit_types(flags, GUNW)

    assert filled == 1
    assert _read(flags) == {
        GUNW[0]: {"j": 1, "o": "MOE"},
        GUNW[1]: {"j": 0, "o": "POE"},
        GUNW[2]: {"j": 1, "o": "MOE"},
    }


def test_backfill_makes_no_request_when_nothing_is_missing(
    fake_cmr, tmp_path: Path
) -> None:
    qa = _load()
    cmr = fake_cmr(GUNW, attributes=_orbits(GUNW))
    flags = tmp_path / "flags.json.gz"
    _write(flags, {GUNW[0]: {"o": "MOE"}})

    assert qa.backfill_orbit_types(flags, GUNW) == 0
    assert cmr.calls == []
