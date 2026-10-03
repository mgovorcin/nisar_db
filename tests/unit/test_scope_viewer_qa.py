"""Unit tests for collecting and attaching per-granule QA metrics in the viewer."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import h5py
import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GSLC = (
    "NISAR_L2_PR_GSLC_031_155_D_084_4005_DHDH_A_20260928T231125_20260928T231159"
    "_P05023_N_F_J_001"
)
GUNW = (
    "NISAR_L2_PR_GUNW_030_155_D_084_031_4000_SH_20260916T231125_20260916T231159"
    "_20260928T231125_20260928T231159_P05023_N_F_J_001"
)


def _load(name: str) -> ModuleType:
    # The QA collector imports its HTTP helpers from the flag collector, the way
    # it does when run from scripts/.
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _histogram(group: h5py.Group, edges: np.ndarray, density: np.ndarray) -> None:
    group["histogramBins"] = edges
    group["histogramDensity"] = density


def test_hist_stats_reads_the_median_inside_its_bin() -> None:
    qa = _load("collect_granule_qa")
    edges = np.linspace(0.0, 1.0, 11)
    density = np.zeros(10)
    density[[2, 7]] = [1.0, 3.0]  # a quarter of the weight at 0.25, the rest at 0.75

    stats = qa.hist_stats(edges, density)

    assert stats["mean"] == pytest.approx(0.625)
    assert stats["median"] == pytest.approx(0.7 + 0.1 / 3)
    assert stats["std"] == pytest.approx(np.sqrt(0.25 * 0.375**2 + 0.75 * 0.125**2))
    assert qa.hist_stats(edges, np.zeros(10)) is None


def test_gunw_metrics_scale_coverage_to_the_data_area(tmp_path: Path) -> None:
    qa = _load("collect_granule_qa")
    path = tmp_path / "qa.h5"
    with h5py.File(path, "w") as h5:
        grp = h5.create_group(
            "science/LSAR/QA/data/frequencyA/unwrappedInterferogram/HH"
        )
        grp["unwrappedPhase/percentNan"] = 60.0
        cc = grp.create_group("connectedComponents")
        cc["numValidConnectedComponents"] = 3
        cc["percentPixelsWithNonZeroCC"] = 30.0
        cc["percentPixelsInLargestCC"] = 20.0
        edges = np.linspace(0.0, 1.0, 5)
        _histogram(
            grp.create_group("coherenceMagnitude"), edges, np.array([0, 1, 1, 0.0])
        )
        # The QA software's own mean of this layer reads zero for some granules;
        # the collector must not use it.
        grp["coherenceMagnitude/mean_value"] = 0.0
        iono = np.linspace(-10.0, 10.0, 5)
        _histogram(
            grp.create_group("ionospherePhaseScreen"), iono, np.array([0, 0, 1, 0.0])
        )
        _histogram(
            grp.create_group("ionospherePhaseScreenUncertainty"),
            np.linspace(0.0, 4.0, 5),
            np.array([0, 1, 0, 0.0]),
        )
        out = qa.gunw_metrics(h5)

    assert out["n"] == 3
    assert out["v"] == 75.0  # 30 % of the raster over a 40 % data area
    assert out["l"] == 50.0
    assert out["cm"] == pytest.approx(0.5)
    assert out["ca"] == pytest.approx(0.5)
    assert out["im"] == pytest.approx(2.5)
    assert out["is"] == pytest.approx(0.0)
    assert out["iu"] == pytest.approx(1.5)


def test_gslc_metrics_take_the_worst_rfi_likelihood(tmp_path: Path) -> None:
    qa = _load("collect_granule_qa")
    with h5py.File(tmp_path / "qa.h5", "w") as h5:
        h5["science/LSAR/RFI/data/frequencyA/HH/rfiLikelihood"] = 0.2
        h5["science/LSAR/RFI/data/frequencyA/HV/rfiLikelihood"] = np.nan
        h5["science/LSAR/RFI/data/frequencyB/VV/rfiLikelihood"] = 1.25
        assert qa.gslc_metrics(h5) == {"rl": 1.25}
    with h5py.File(tmp_path / "empty.h5", "w") as h5:
        assert qa.gslc_metrics(h5) == {}


def test_qa_url_points_next_to_the_product() -> None:
    url = _load("collect_granule_qa").qa_url(GUNW)
    assert url.endswith(f"NISAR_L2_GUNW_PROVISIONAL_V1/{GUNW}/{GUNW}_QA_STATS.h5")


def test_attach_granule_qa_skips_withdrawn_granules() -> None:
    data = {
        "features": [
            {
                "properties": {
                    "granules": [{"gid": GSLC}],
                    "gunw_ifgs": [{"gid": GUNW}],
                }
            }
        ]
    }

    n = _load("generate_scope_viewer").attach_granule_qa(
        data, {GSLC: {}, GUNW: {"cm": 0.4, "n": 1}}
    )

    props = data["features"][0]["properties"]
    assert n == 1
    assert "qa" not in props["granules"][0]
    assert props["gunw_ifgs"][0]["qa"] == {"cm": 0.4, "n": 1}
