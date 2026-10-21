"""Shared fixtures for the nisar_db test suite.

Fixtures here provide deterministic, network-free inputs: valid GSLC/GUNW
granule names, a synthetic S3-derived product list, and a small in-memory
catalog DataFrame. Tests must stay order-independent (pytest-randomly is
active) and warning-clean (``filterwarnings = ["error"]``).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import requests

from nisar_db.search.models import NISARProduct

# A valid 18-field GSLC granule name and 20-field GUNW granule name. Field
# layout matches nisar_db.filenames.{GSLCFilename,GUNWFilename}.from_path.
GSLC_NAME = (
    "NISAR_L_GSLC_L2_005_128_A_00129_4005_HH_ODC"
    "_20240601T000000_20240601T000030_P01234_001_F_NA_v1.0"
)
GUNW_NAME = (
    "NISAR_L_GUNW_L2_005_128_A_00129_006_4005_HH"
    "_20240601T000000_20240601T000030_20240613T000000_20240613T000030"
    "_P01234_001_F_NA_v1.0"
)


@pytest.fixture
def gslc_name() -> str:
    """Return a valid GSLC granule name (no extension)."""
    return GSLC_NAME


@pytest.fixture
def gunw_name() -> str:
    """Return a valid GUNW granule name (no extension)."""
    return GUNW_NAME


@pytest.fixture
def s3_products() -> list[NISARProduct]:
    """Two GSLC products parsed from synthetic S3 keys (distinct crids)."""
    key_old = f"products/L2_L_GSLC/{GSLC_NAME}.h5"
    key_new = key_old.replace("_P01234_", "_P09999_")
    return [
        NISARProduct.from_s3_key(
            "my-bucket",
            key_old,
            size=2_000_000_000,
            last_modified=datetime(2024, 6, 2, 12, 0, tzinfo=timezone.utc),
        ),
        NISARProduct.from_s3_key(
            "my-bucket",
            key_new,
            size=3_000_000_000,
            last_modified=datetime(2024, 9, 30, 12, 0, tzinfo=timezone.utc),
        ),
    ]


@pytest.fixture
def consistent_catalog_df() -> pd.DataFrame:
    """Synthetic GSLC catalog exercising the mode/coverage selection rules.

    Frame (128, 129): standard mode 4005 dominates (2xF, 1xP) plus a 2005 F and
    a same-date duplicate -> keeps the two 4005 F dates. Frame (200, 300): only
    the non-standard mode 7700 (1xF, 2xP) -> dropped, unless
    ``keep_nonstandard_modes`` is set, which falls back to 7700 P (two dates).
    """
    rows = [
        # (track, frame, mode, coverage, sensing_time)
        (128, 129, "4005", "F", "2024-06-01T00:00:00"),
        (128, 129, "4005", "F", "2024-06-01T00:05:00"),  # dup date -> dropped
        (128, 129, "4005", "F", "2024-06-13T00:00:00"),
        (128, 129, "4005", "P", "2024-06-25T00:00:00"),  # loses coverage vote
        (128, 129, "2005", "F", "2024-07-01T00:00:00"),  # loses mode vote
        (200, 300, "7700", "F", "2024-06-02T00:00:00"),  # non-standard, minority
        (200, 300, "7700", "P", "2024-06-14T00:00:00"),
        (200, 300, "7700", "P", "2024-06-26T00:00:00"),
    ]
    df = pd.DataFrame(
        rows, columns=["track", "frame", "mode", "coverage", "sensing_time"]
    )
    df["sensing_date"] = pd.to_datetime(df["sensing_time"]).dt.strftime("%Y-%m-%d")
    return df


# ---------------------------------------------------------------------------
# REST API: a tiny viewer page and a fake job launcher
# ---------------------------------------------------------------------------
GSLC_22 = (
    "NISAR_L2_PR_GSLC_022_034_A_019_4005_DHDH_A"
    "_20260604T000000_20260604T000030_P05023_N_F_J_001"
)
GSLC_23 = (
    "NISAR_L2_PR_GSLC_023_034_A_019_2005_QPDH_A"
    "_20260616T000000_20260616T000030_P05012_N_F_J_001"
)
GUNW_22_23 = (
    "NISAR_L2_PR_GUNW_022_034_A_019_023_4000_SH_20260604T000000_20260604T000030"
    "_20260616T000000_20260616T000030_P05023_N_F_J_001"
)


def _square(lon: float, lat: float) -> dict:
    ring = [[lon, lat], [lon + 1, lat], [lon + 1, lat + 1], [lon, lat + 1], [lon, lat]]
    return {"type": "MultiPolygon", "coordinates": [[ring]]}


FRAMES = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": _square(-120, 35),
            "properties": {
                "id": "34_19",
                "frame_idx": 5826,
                "track": 34,
                "frame": 19,
                "passDirection": "Ascending",
                "isCalVal": True,
                "hasLand": True,
                "gslc_count": 2,
                "n_modes": 2,
                "cons_mode": "4005",
                "cons_cov": "F",
                "rollout": ["P1"],
                "has_blackout": True,
                "blackout_label": "Jan-Feb",
                "blackout_months": 2.0,
                "blackout_ranges": ["2026-01-01 -> 2026-02-28"],
                "granules": [
                    {
                        "gid": GSLC_22,
                        "date": "2026-06-04",
                        "mode": "4005",
                        "cov": "F",
                        "pol": "DHDH",
                        "cycle": 22,
                    },
                    {
                        "gid": GSLC_23,
                        "date": "2026-06-16",
                        "mode": "2005",
                        "cov": "F",
                        "pol": "QPDH",
                        "cycle": 23,
                    },
                ],
                # Older pages stored the pairs as a JSON string.
                "gunw_ifgs": json.dumps(
                    [
                        {
                            "gid": GUNW_22_23,
                            "ref": "2026-06-04",
                            "sec": "2026-06-16",
                            "dt": 12,
                            "mode": "4000",
                            "pol": "SH",
                        }
                    ]
                ),
            },
        },
        {
            "type": "Feature",
            "geometry": _square(150, -40),
            "properties": {
                "id": "1_61",
                "frame_idx": 60,
                "track": 1,
                "frame": 61,
                "passDirection": "Descending",
                "isCalVal": False,
                "hasLand": False,
                "gslc_count": 0,
                "n_modes": 0,
                "cons_mode": "none",
                "cons_cov": "none",
                "rollout": [],
                "has_blackout": False,
                "granules": [],
                "gunw_ifgs": [],
            },
        },
    ],
}

META = {
    "generated_at": "2026-10-09T00:00:00+00:00",
    "n_frames_with_gslc": 1,
    "n_granules": 2,
    "n_gunw": 1,
    "has_blackout": True,
    "rollout_options": ["P1"],
}


def write_page(path: Path, frames: dict = FRAMES, meta: dict = META) -> Path:
    """Write a minimal viewer page carrying ``META`` and ``FRAME_DATA``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<html><script>\n"
        f"const META = {json.dumps(meta)};\n"
        f"const FRAME_DATA = {json.dumps(frames)};\n"
        "</script></html>\n"
    )
    return path


@pytest.fixture
def viewer_page(tmp_path: Path) -> Path:
    """A published-style viewer page with two frames."""
    return write_page(tmp_path / "viewer.html")


class FakeProc:
    """A finished-on-demand process: ``wait`` blocks until ``release``."""

    def __init__(
        self, cwd: Path, outputs: tuple[str, ...], rc: int, block: bool
    ) -> None:
        self.cwd, self.outputs, self.rc = cwd, outputs, rc
        self.released = threading.Event()
        self.terminated = False
        if not block:
            self.released.set()

    def wait(self) -> int:
        self.released.wait(10)
        for name in self.outputs:
            (self.cwd / name).parent.mkdir(parents=True, exist_ok=True)
            (self.cwd / name).write_text("out\n")
        return -15 if self.terminated else self.rc

    def terminate(self) -> None:
        self.terminated = True
        self.released.set()


class FakeLauncher:
    """Records each command line and hands back a ``FakeProc``."""

    def __init__(
        self,
        outputs: tuple[str, ...] = (),
        rc: int = 0,
        block: bool = False,
        log: str = "",
    ) -> None:
        self.outputs, self.rc, self.block, self.log = outputs, rc, block, log
        self.calls: list[list[str]] = []
        self.procs: list[FakeProc] = []
        self.started = threading.Semaphore(0)

    def __call__(self, argv: list[str], cwd: Path, log: Path) -> FakeProc:
        self.calls.append(argv)
        log.write_text(self.log)
        proc = FakeProc(cwd, self.outputs, self.rc, self.block)
        self.procs.append(proc)
        self.started.release()
        return proc


def wait_for(predicate, timeout: float = 5.0) -> bool:
    """Wait for a background job to reach a state, without a bare sleep."""
    tick = threading.Event()
    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.start()
    try:
        while not predicate():
            if deadline.is_set():
                return False
            tick.wait(0.01)
        return True
    finally:
        timer.cancel()


@pytest.fixture
def make_launcher():
    """The ``FakeLauncher`` class, to build one per test."""
    return FakeLauncher


@pytest.fixture
def waiter():
    """``wait_for(predicate, timeout)``."""
    return wait_for


@pytest.fixture
def page_writer():
    """``write_page(path, frames, meta)``."""
    return write_page


# ---------------------------------------------------------------------------
# A CMR stand-in that drops records from UMM-JSON the way the real one does
# ---------------------------------------------------------------------------

UMM_CONTENT_TYPE = "application/vnd.nasa.cmr.umm+json;version=1.6.6; charset=utf-8"


class FakeCMRResponse:
    """The parts of ``requests.Response`` the CMR helpers read."""

    def __init__(
        self,
        body: dict | None = None,
        *,
        hits: int | None = None,
        status_code: int = 200,
        content_type: str = UMM_CONTENT_TYPE,
    ) -> None:
        self._body = body or {}
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        if hits is not None:
            self.headers["CMR-Hits"] = str(hits)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} from fake CMR")


class FakeCMR:
    """A granule archive served as CMR serves it in October 2026.

    ``page_dropped`` granules are missing from paged UMM-JSON results but come
    back when UMM-JSON is asked for them by concept id; ``hidden`` granules
    never come back from UMM-JSON. ``mislabelled`` maps a concept id to the
    one whose record UMM-JSON serves in its place. Atom-JSON (``granules.json``)
    lists every granule under its own name, and the concepts endpoint serves
    every granule not ``deleted``.
    ``calls`` records ``(method, url)`` of every request.
    """

    def __init__(
        self,
        names: list[str],
        *,
        page_dropped: Iterable[str] = (),
        hidden: Iterable[str] = (),
        deleted: Iterable[str] = (),
        mislabelled: dict[str, str] | None = None,
        native_content_type: str = UMM_CONTENT_TYPE,
        attributes: dict[str, dict[str, str]] | None = None,
        failures: Iterable[Exception | int] = (),
    ) -> None:
        # Replayed before any real answer: an exception is raised, a status
        # code is returned as an empty response.
        self.failures = list(failures)
        self.ids = {f"G{i:04d}-ASF": name for i, name in enumerate(names)}
        self.page_dropped = set(page_dropped)
        self.hidden = set(hidden)
        self.deleted = set(deleted)
        self.mislabelled = mislabelled or {}
        self.native_content_type = native_content_type
        self.attributes = attributes or {}
        self.calls: list[tuple[str, str]] = []

    def concept_id(self, name: str) -> str:
        return next(cid for cid, n in self.ids.items() if n == name)

    def umm(self, cid: str) -> dict:
        attrs = self.attributes.get(self.ids[cid], {})
        return {
            "GranuleUR": self.ids[cid],
            "AdditionalAttributes": [
                {"Name": k, "Values": [v]} for k, v in attrs.items()
            ],
        }

    def _body(self, url: str, cids: list[str]) -> dict:
        if "umm_json" in url:
            return {
                "hits": len(cids),
                "items": [
                    {
                        "meta": {"concept-id": c},
                        "umm": self.umm(self.mislabelled.get(c, c)),
                    }
                    for c in cids
                    if c not in self.hidden
                ],
            }
        return {"feed": {"entry": [{"id": c, "title": self.ids[c]} for c in cids]}}

    def get(self, url: str, params: dict | None = None, timeout: float = 0):
        assert timeout > 0, "every CMR request needs a timeout"
        self.calls.append(("GET", url))
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return FakeCMRResponse(status_code=failure)
        if "/concepts/" in url:
            cid = url.rsplit("/", 1)[1]
            if cid not in self.ids or cid in self.deleted:
                return FakeCMRResponse(status_code=404)
            return FakeCMRResponse(self.umm(cid), content_type=self.native_content_type)
        params = params or {}
        size, page = params["page_size"], params["page_num"]
        cids = list(self.ids)
        rows = cids[(page - 1) * size : page * size]
        if "umm_json" in url:
            rows = [c for c in rows if c not in self.page_dropped]
        return FakeCMRResponse(self._body(url, rows), hits=len(cids))

    def post(self, url: str, data: dict, timeout: float = 0):
        assert timeout > 0, "every CMR request needs a timeout"
        self.calls.append(("POST", url))
        if "concept_id[]" in data:
            cids = [c for c in data["concept_id[]"] if c in self.ids]
        else:
            wanted = set(data["granule_ur[]"])
            cids = [c for c, n in self.ids.items() if n in wanted]
            if "umm_json" in url:
                cids = [c for c in cids if c not in self.page_dropped]
        return FakeCMRResponse(self._body(url, cids), hits=len(cids))

    def concept_lookups(self) -> list[str]:
        return [u.rsplit("/", 1)[1] for m, u in self.calls if "/concepts/" in u]


@pytest.fixture
def fake_cmr(monkeypatch: pytest.MonkeyPatch):
    """Build a ``FakeCMR`` and route ``nisar_db.search.cmr``'s HTTP through it."""
    from nisar_db.search import cmr

    def install(names: list[str], **kwargs) -> FakeCMR:
        fake = FakeCMR(names, **kwargs)
        monkeypatch.setattr(cmr, "requests", fake)
        return fake

    return install
