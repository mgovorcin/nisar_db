"""The API's read model: datasets from viewer pages, and frame queries."""

from __future__ import annotations

import os
from datetime import date

import pytest

from nisar_db.api import store


def _ds(viewer_page):
    return store.FrameStore(viewer_page, None).get("published")


@pytest.mark.parametrize("key", ["5826", 5826, "34_19", "T34_F19", "t34_f19"])
def test_frame_lookup_by_id_or_track_frame(viewer_page, key):
    assert _ds(viewer_page).frame(key)["properties"]["frame_idx"] == 5826


def test_pairs_stored_as_strings_are_parsed(viewer_page):
    pairs = _ds(viewer_page).frame("34_19")["properties"]["gunw_ifgs"]
    assert isinstance(pairs, list) and pairs[0]["dt"] == 12


def test_store_lists_published_and_rebuilt_views(tmp_path, viewer_page, page_writer):
    views = tmp_path / "views"
    page_writer(views / "globe-20261009T193409.html")
    page_writer(views / "notes.html")  # not a rebuild: ignored
    assert list(store.FrameStore(viewer_page, views).paths()) == [
        "published",
        "globe-20261009T193409",
    ]


def test_unknown_dataset_raises(viewer_page):
    with pytest.raises(KeyError):
        store.FrameStore(viewer_page, None).get("na-20990101T000000")


def test_dataset_is_reparsed_when_its_page_changes(viewer_page, page_writer):
    st = store.FrameStore(viewer_page, None)
    assert st.get().meta["n_gunw"] == 1
    page_writer(
        viewer_page,
        meta={**store._embedded(viewer_page.read_text(), "META"), "n_gunw": 7},
    )
    os.utime(viewer_page, (1, 1))
    assert st.get().meta["n_gunw"] == 7


@pytest.mark.parametrize(
    ("text", "expected"), [("", None), ("12", {12}), ("3-1,7", {1, 2, 3, 7})]
)
def test_int_set(text, expected):
    assert store.int_set(text) == expected


def _ids(ds, **kw):
    return [
        f["properties"]["frame_idx"] for f in store.select(ds, store.FrameQuery(**kw))
    ]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({}, [5826, 60]),
        ({"track": "30-40"}, [5826]),
        ({"direction": "D"}, [60]),
        ({"bbox": (140.0, -50.0, 160.0, -30.0)}, [60]),
        ({"calval": True}, [5826]),
        ({"land": False}, [60]),
        ({"rollout": ("none",)}, [60]),
        ({"consistent_mode": "4005"}, [5826]),
        ({"cycle": "23"}, [5826]),
        ({"cycle": "30"}, []),
        ({"has_data": False}, [60]),
    ],
)
def test_frame_filters(viewer_page, query, expected):
    assert _ids(_ds(viewer_page), **query) == expected


def test_entry_filters_on_granules(viewer_page):
    p = _ds(viewer_page).frame("34_19")["properties"]

    def gids(**kw):
        return [g["gid"][17:20] for g in store.FrameQuery(**kw).entries(p)]

    assert gids(cycle="22") == ["022"]
    assert gids(modes=("2005",)) == ["023"]
    assert gids(crids=("P05023",)) == ["022"]
    assert gids(start=date(2026, 6, 10)) == ["023"]
    assert gids(end=date(2026, 6, 10)) == ["022"]


def test_a_gunw_pair_matches_either_cycle(viewer_page):
    p = _ds(viewer_page).frame("34_19")["properties"]
    for c in ("22", "23"):
        assert len(store.FrameQuery(product="gunw", cycle=c).entries(p)) == 1
    assert store.FrameQuery(product="gunw", cycle="24").entries(p) == []


def test_summary_leaves_out_the_granule_lists(viewer_page):
    f = _ds(viewer_page).frame("34_19")
    out = store.frame_summary(f, store.FrameQuery(cycle="22"))
    assert "granules" not in out and "gunw_ifgs" not in out and out["n_selected"] == 1


def test_cycles_with_spans_and_frame_counts(viewer_page):
    ds = _ds(viewer_page)
    assert store.cycles(ds) == [
        {"cycle": 22, "start": "2026-06-04", "end": "2026-06-04", "n_frames": 1},
        {"cycle": 23, "start": "2026-06-16", "end": "2026-06-16", "n_frames": 1},
    ]
    assert [c["cycle"] for c in store.cycles(ds, "gunw")] == [22, 23]


def test_consistent_and_rollout_summaries(viewer_page):
    ds = _ds(viewer_page)
    s = store.consistent_summary(ds.features)
    assert s == {
        "n_frames": 2,
        "with_gslc": 1,
        "full_frame": 1,
        "partial": 0,
        "multi_mode": 1,
        "frames_per_mode": {"4005": 1, "none": 1},
    }
    assert store.rollout_summary(ds, ds.features[:1]) == [
        {"option": "P1", "shown": 1, "total": 1},
        {"option": "none", "shown": 0, "total": 1},
    ]


def test_blackout_month_shares(viewer_page):
    b = store.blackout(_ds(viewer_page).frame("34_19"))
    assert b["label"] == "Jan-Feb" and b["month_share"]["Jan"] == 1.0
    assert b["month_share"]["Feb"] == 1.0 and b["month_share"]["Mar"] == 0.0


def test_header_reads_only_the_meta(tmp_path, page_writer):
    """Listing datasets must not parse a 100 MB page's frames."""
    page = page_writer(
        tmp_path / "v.html", meta={"n_frames": 30448, "view_scope": "globe"}
    )
    page.write_text(
        page.read_text().replace("const FRAME_DATA = {", "const FRAME_DATA = {broken")
    )
    head = store.FrameStore(page, None).header("published")
    assert head["n_frames"] == 30448 and head["scope"] == "globe"
