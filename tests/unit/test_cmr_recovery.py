"""Unit tests for recovering granules CMR leaves out of UMM-JSON (no network).

CMR counts some granules in ``CMR-Hits`` but drops them from
``granules.umm_json`` results, while atom-JSON lists them all. In October 2026
that hid 6,024 of 89,817 NISAR GUNWs from the viewer's searches.
"""

from __future__ import annotations

import logging

import pytest
import requests

from nisar_db.search import cmr
from nisar_db.search.cmr import (
    _JSON_SEARCH_URL,
    _UMM_SEARCH_URL,
    CMRSearchReport,
    fetch_cmr_pages,
    fetch_umm_by_granule_ur,
    fetch_umm_granules,
    recover_umm_items,
    search_nisar_products,
)

NAMES = [f"NISAR_L2_PR_GSLC_{i:03d}" for i in range(7)]


def _names(items: list[dict]) -> list[str]:
    return [item["umm"]["GranuleUR"] for item in items]


def test_fetch_umm_granules_recovers_every_dropped_granule(fake_cmr) -> None:
    cmr = fake_cmr(NAMES, page_dropped={"G0001-ASF"}, hidden={"G0004-ASF"})

    items = fetch_umm_granules({"page_size": 3}, max_workers=1)

    assert sorted(_names(items)) == NAMES
    # Only the granule UMM-JSON never returns costs a concepts-endpoint request.
    assert cmr.concept_lookups() == ["G0004-ASF"]


def test_fetch_umm_granules_replaces_records_served_under_the_wrong_id(
    fake_cmr,
) -> None:
    """UMM-JSON served another granule's record under ~3,500 GUNW concept
    ids, so the viewer lost them as duplicate names. Regression: 2026-10."""
    cmr = fake_cmr(NAMES, mislabelled={"G0002-ASF": "G0005-ASF"})

    items = fetch_umm_granules({"page_size": 3}, max_workers=1)

    assert sorted(_names(items)) == NAMES
    by_id = {item["meta"]["concept-id"]: item["umm"]["GranuleUR"] for item in items}
    assert by_id["G0002-ASF"] == NAMES[2]
    # Asking UMM-JSON by id serves the same wrong record, so it is not trusted.
    assert cmr.concept_lookups() == ["G0002-ASF"]


def test_fetch_umm_granules_skips_recovery_when_nothing_is_missing(fake_cmr) -> None:
    cmr = fake_cmr(NAMES)

    items = fetch_umm_granules({"page_size": 3}, max_workers=1)

    assert sorted(_names(items)) == NAMES
    assert all(method == "GET" for method, _ in cmr.calls)


def test_short_page_does_not_end_the_walk(fake_cmr) -> None:
    """A UMM-JSON page shortened by drops used to end the walk early, losing
    every later page. Regression: CMR UMM-JSON drops, 2026-10."""
    fake_cmr(NAMES, page_dropped={"G0000-ASF"})

    items = fetch_cmr_pages(_UMM_SEARCH_URL, {"page_size": 3}, max_workers=1)

    assert _names(items) == NAMES[1:]


def test_page_walk_warns_when_short_of_cmr_hits(
    fake_cmr, caplog: pytest.LogCaptureFixture
) -> None:
    fake_cmr(NAMES, hidden={"G0002-ASF", "G0005-ASF"})

    with caplog.at_level(logging.WARNING):
        fetch_cmr_pages(_UMM_SEARCH_URL, {"page_size": 3}, max_workers=1)

    assert "Fetched only 5 of the 7" in caplog.text


def test_atom_walk_lists_every_granule(fake_cmr) -> None:
    fake_cmr(NAMES, hidden={"G0002-ASF"})

    entries = fetch_cmr_pages(_JSON_SEARCH_URL, {"page_size": 3}, max_workers=1)

    assert [e["title"] for e in entries] == NAMES


def test_recover_umm_items_keeps_order_and_skips_deleted(
    fake_cmr, caplog: pytest.LogCaptureFixture
) -> None:
    fake_cmr(
        NAMES,
        page_dropped={"G0003-ASF"},
        hidden={"G0001-ASF", "G0005-ASF"},
        deleted={"G0005-ASF"},
    )

    with caplog.at_level(logging.WARNING):
        items = recover_umm_items(["G0005-ASF", "G0003-ASF", "G0001-ASF"])

    assert _names(items) == [NAMES[3], NAMES[1]]
    assert "1 of 3 granules are no longer in CMR" in caplog.text


def test_recover_umm_items_rejects_non_umm_native_metadata(fake_cmr) -> None:
    fake_cmr(NAMES, hidden={"G0001-ASF"}, native_content_type="application/echo10+xml")

    with pytest.raises(ValueError, match=r"G0001-ASF: native metadata is .*echo10"):
        recover_umm_items(["G0001-ASF"])


def test_fetch_umm_by_granule_ur_recovers_named_granules(fake_cmr) -> None:
    fake_cmr(NAMES, page_dropped={"G0002-ASF"}, hidden={"G0006-ASF"})
    wanted = [NAMES[6], NAMES[0], "NISAR_L2_PR_GSLC_UNKNOWN", NAMES[2]]

    items = fetch_umm_by_granule_ur("NISAR_L2_GSLC_PROVISIONAL_V1", wanted, batch=2)

    # Names CMR does not know are left out; the rest keep the requested order.
    assert _names(items) == [NAMES[6], NAMES[0], NAMES[2]]


def test_search_nisar_products_includes_granules_umm_json_drops(fake_cmr) -> None:
    fake_cmr(NAMES, hidden={"G0003-ASF"})

    products = search_nisar_products(product_type="GSLC", max_workers=1)

    assert sorted(p.name for p in products) == NAMES


def test_one_record_per_name_and_cmr_duplicates_are_reported(fake_cmr) -> None:
    # CMR lists NAMES[1] under two concept ids (G0001 and G0002).
    names = [NAMES[0], NAMES[1], NAMES[1], NAMES[2]]
    fake_cmr(names, hidden={"G0003-ASF"})
    report = CMRSearchReport()

    items = fetch_umm_granules({"page_size": 3}, max_workers=1, report=report)

    assert sorted(_names(items)) == [NAMES[0], NAMES[1], NAMES[2]]
    kept = {i["umm"]["GranuleUR"]: i["meta"]["concept-id"] for i in items}
    assert kept[NAMES[1]] == "G0002-ASF"  # the most recently ingested
    assert report.duplicate_names == {NAMES[1]: ["G0001-ASF", "G0002-ASF"]}


def test_report_records_what_cmr_got_wrong(fake_cmr) -> None:
    fake_cmr(
        NAMES,
        page_dropped={"G0001-ASF"},
        hidden={"G0004-ASF", "G0006-ASF"},
        deleted={"G0006-ASF"},
        mislabelled={"G0002-ASF": "G0005-ASF"},
    )
    report = CMRSearchReport()

    fetch_umm_granules({"page_size": 3}, max_workers=1, report=report)

    data = report.to_dict()
    assert data["counts"] == {
        "listed": 7,
        "left_out": 3,
        "mislabelled": 1,
        "duplicate_names": 0,
        "unrecovered": 1,
    }
    assert data["left_out"] == ["G0001-ASF", "G0004-ASF", "G0006-ASF"]
    assert data["mislabelled"] == {"G0002-ASF": NAMES[5]}
    assert data["unrecovered"] == ["G0006-ASF"]


@pytest.fixture
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmr, "_RETRY_DELAYS", (0.0, 0.0, 0.0, 0.0))


@pytest.mark.usefixtures("no_retry_wait")
@pytest.mark.parametrize("failure", [requests.exceptions.ReadTimeout("slow"), 503, 429])
def test_transient_cmr_failures_are_retried(fake_cmr, failure: Exception | int) -> None:
    """One slow CMR page failed the weekly viewer build on 2026-10-21.
    Regression: ReadTimeout in "Query CMR for GSLC granules"."""
    fake_cmr(NAMES, failures=[failure, failure])

    items = fetch_umm_granules({"page_size": 3}, max_workers=1)

    assert sorted(_names(items)) == NAMES


@pytest.mark.usefixtures("no_retry_wait")
def test_persistent_timeouts_still_fail(fake_cmr) -> None:
    cmr_fake = fake_cmr(NAMES, failures=[requests.exceptions.ReadTimeout("x")] * 5)

    with pytest.raises(requests.exceptions.ReadTimeout):
        fetch_cmr_pages(_UMM_SEARCH_URL, {"page_size": 3}, max_workers=1)
    assert len(cmr_fake.calls) == 5  # the first try and four retries


@pytest.mark.usefixtures("no_retry_wait")
def test_bad_requests_are_not_retried(fake_cmr) -> None:
    cmr_fake = fake_cmr(NAMES, failures=[400])

    with pytest.raises(requests.HTTPError):
        fetch_cmr_pages(_UMM_SEARCH_URL, {"page_size": 3}, max_workers=1)
    assert len(cmr_fake.calls) == 1
