"""CMR (NASA Common Metadata Repository) search backends.

Two entry points:

- :func:`search_nisar_products` returns typed :class:`NISARProduct` objects from
  the UMM-G endpoint (used by the ``search`` CLI and downstream tooling).
  CMR leaves some granules out of UMM-JSON results, so it goes through
  :func:`fetch_umm_granules`, which recovers them.
- :func:`search_nisar_granules` returns a flat granule DataFrame from the
  echo10 ``granules.json`` endpoint (used by the catalog builders, which parse
  the granule *name* and CMR link ``rel`` values themselves).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import requests

from ..filenames import NISARCollection
from .models import NISARProduct, ProductType, UrlType

__all__ = [
    "CMRSearchReport",
    "fetch_cmr_pages",
    "fetch_umm_by_granule_ur",
    "fetch_umm_granules",
    "recover_umm_items",
    "search_nisar_granules",
    "search_nisar_products",
]

logger = logging.getLogger(__name__)

_UMM_SEARCH_URL = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
_JSON_SEARCH_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
_CONCEPT_URL = "https://cmr.earthdata.nasa.gov/search/concepts/{}"
# Concept ids per POST when re-asking UMM-JSON for granules a search left out.
_RECOVER_BATCH = 100


def _extract_items(response_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the granule list from a CMR response body.

    Handles both response shapes: UMM-JSON (``items``) and the echo10 atom-JSON
    used by ``granules.json`` (``feed.entry``).
    """
    if "items" in response_json:
        return response_json["items"]
    return response_json.get("feed", {}).get("entry", [])


def _extract_total_hits(response: requests.Response) -> int:
    """Return the total granule count for a CMR search, or 0 if not reported.

    ``CMR-Hits`` is set on every CMR search response and is the only place the
    total appears for the atom-JSON (``granules.json``) format, whose body
    carries no hit count -- reading the header rather than the body is what
    keeps a granule search from stopping short of the full archive.
    """
    return int(response.headers.get("CMR-Hits", 0))


def _fetch_page(
    url: str, params: Dict[str, Any], page_num: int
) -> List[Dict[str, Any]]:
    """Fetch a single page of CMR results."""
    response = requests.get(url, params={**params, "page_num": page_num}, timeout=30)
    response.raise_for_status()
    return _extract_items(response.json())


def fetch_cmr_pages(
    url: str,
    params: Dict[str, Any],
    max_workers: int = 1,
    rate_limit_delay: float = 0.5,
) -> List[Dict[str, Any]]:
    """Fetch all pages from CMR search API.

    Parameters
    ----------
    url : str
        CMR API URL.
    params : Dict[str, Any]
        Query parameters.
    max_workers : int
        Maximum number of concurrent workers for fetching pages.
        Default is 1 to avoid overwhelming the CMR API.
    rate_limit_delay : float
        Delay in seconds between page requests to respect rate limits.
        Default is 0.5 seconds.

    Returns
    -------
    List[Dict[str, Any]]
        List of items from all pages.

    Raises
    ------
    requests.HTTPError
        If any page request fails. A dropped page silently truncates the
        archive, so page errors are never swallowed.

    """
    page_size = params.get("page_size", 10)

    logger.debug("Fetching page 1 to determine total pages")
    response = requests.get(url, params={**params, "page_num": 1}, timeout=30)
    response.raise_for_status()

    first_page_items = _extract_items(response.json())
    total_hits = _extract_total_hits(response)
    if not total_hits:
        # No count reported: the first short page is the only end-of-results signal.
        total_hits = len(first_page_items)

    total_pages = (total_hits + page_size - 1) // page_size
    logger.debug(f"Total pages: {total_pages} (from {total_hits} hits)")

    # A short page is not the end of the results while CMR-Hits says there are
    # more: UMM-JSON pages come back short when CMR leaves records out of them.
    if total_pages <= 1:
        _warn_if_short(len(first_page_items), total_hits)
        return first_page_items

    all_items = first_page_items.copy()
    remaining = range(2, total_pages + 1)

    # Never use more than 4 workers, and no more than there are pages left.
    max_workers = min(max_workers, len(remaining), 4)

    # For small numbers of pages, sequential is simpler and less risky
    if total_pages <= 3 or max_workers <= 1:
        for page_num in remaining:
            logger.debug(f"Fetching page {page_num}/{total_pages} sequentially")
            if rate_limit_delay > 0:
                time.sleep(rate_limit_delay)

            all_items.extend(_fetch_page(url, params, page_num))
        _warn_if_short(len(all_items), total_hits)
        return all_items

    def fetch_page(page_num: int) -> List[Dict[str, Any]]:
        """Fetch one page, staggering the start to spread out requests."""
        logger.debug(f"Fetching page {page_num}/{total_pages} in parallel")
        if rate_limit_delay > 0:
            time.sleep(rate_limit_delay * ((page_num - 2) % max_workers))
        return _fetch_page(url, params, page_num)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_page, page_num) for page_num in remaining]
        for future in as_completed(futures):
            all_items.extend(future.result())

    _warn_if_short(len(all_items), total_hits)
    return all_items


def _warn_if_short(fetched: int, total_hits: int) -> None:
    """Warn when a page walk returned fewer records than ``CMR-Hits``.

    Over-fetching is normal -- ASF ingests granules while the pages are being
    walked -- but coming up short means records or pages were dropped.
    """
    if fetched < total_hits:
        logger.warning(
            "Fetched only %d of the %d granules CMR reported.", fetched, total_hits
        )


@dataclass
class CMRSearchReport:
    """What CMR got wrong in one granule search, for reporting upstream.

    Attributes
    ----------
    listed : int
        Granules the atom-JSON listing returned.
    left_out : list of str
        Concept ids UMM-JSON did not return.
    mislabelled : dict
        Concept id -> the other granule's name UMM-JSON served under it.
    duplicate_names : dict
        Granule name -> every concept id CMR lists under it, when more than
        one. The search keeps one record per name; the rest are listed here.
    unrecovered : list of str
        Concept ids no endpoint returned (deleted mid-search).

    """

    listed: int = 0
    left_out: List[str] = field(default_factory=list)
    mislabelled: Dict[str, str] = field(default_factory=dict)
    duplicate_names: Dict[str, List[str]] = field(default_factory=dict)
    unrecovered: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return the report as JSON-ready data, counts first."""
        return {
            "counts": {
                "listed": self.listed,
                "left_out": len(self.left_out),
                "mislabelled": len(self.mislabelled),
                "duplicate_names": len(self.duplicate_names),
                "unrecovered": len(self.unrecovered),
            },
            "left_out": sorted(self.left_out),
            "mislabelled": dict(sorted(self.mislabelled.items())),
            "duplicate_names": dict(sorted(self.duplicate_names.items())),
            "unrecovered": sorted(self.unrecovered),
        }


def _concept_number(concept_id: str) -> int:
    """Return the numeric part of a concept id (``G4245361810-ASF`` -> 4245361810)."""
    digits = concept_id[1:].split("-", 1)[0]
    return int(digits) if digits.isdigit() else -1


def _concept_id(item: Dict[str, Any]) -> str:
    """Return the concept id of a UMM-JSON item or an atom-JSON entry."""
    return item.get("meta", {}).get("concept-id") or item.get("id", "")


def _by_concept_id(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index items by concept id, keeping the first of any page-boundary repeats."""
    out: Dict[str, Dict[str, Any]] = {}
    for item in items:
        out.setdefault(_concept_id(item), item)
    return out


def _fetch_native_umm(concept_id: str) -> Optional[Dict[str, Any]]:
    """Fetch one granule's native UMM-G record, or None if CMR no longer has it.

    The concepts endpoint serves the metadata as ASF ingested it, so it returns
    the records the ``granules.umm_json`` search drops.
    """
    response = requests.get(_CONCEPT_URL.format(concept_id), timeout=60)
    if response.status_code == 404:
        # Deleted between the atom-JSON walk and this lookup.
        return None
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")
    if "umm+json" not in content_type:
        raise ValueError(
            f"{concept_id}: native metadata is {content_type!r}, not UMM-G JSON"
        )
    return {"meta": {"concept-id": concept_id}, "umm": response.json()}


def _granule_ur(item: Dict[str, Any]) -> str:
    """Return the granule name a UMM-JSON item carries."""
    return item.get("umm", {}).get("GranuleUR", "")


def recover_umm_items(
    concept_ids: List[str],
    max_workers: int = 4,
    expected: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Fetch the UMM-G records of granules a UMM-JSON search left out.

    CMR drops some records from ``granules.umm_json`` results while still
    counting them in ``CMR-Hits``, and for others it serves another granule's
    record under their concept id. Some come back right when UMM-JSON is asked
    for them by concept id; the rest only come back from the concepts endpoint,
    one granule per request.

    Parameters
    ----------
    concept_ids : list of str
        Granule concept ids, e.g. ``"G4245361810-ASF"``.
    max_workers : int
        Concurrent concepts-endpoint requests (at most 4).
    expected : dict, optional
        Granule name of each concept id, from atom-JSON. A UMM-JSON record
        carrying another name is not trusted and is fetched from the concepts
        endpoint instead.

    Returns
    -------
    list of dict
        Items shaped like UMM-JSON search results (``meta`` and ``umm``), in the
        order of ``concept_ids``. Granules CMR no longer has are left out and
        counted in a warning.

    """
    ids = list(dict.fromkeys(concept_ids))
    found: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(ids), _RECOVER_BATCH):
        batch = ids[start : start + _RECOVER_BATCH]
        response = requests.post(
            _UMM_SEARCH_URL,
            data={"concept_id[]": batch, "page_size": 2 * len(batch)},
            timeout=120,
        )
        response.raise_for_status()
        for cid, item in _by_concept_id(_extract_items(response.json())).items():
            if expected is None or _granule_ur(item) == expected.get(cid):
                found[cid] = item

    rest = [cid for cid in ids if cid not in found]
    if rest:
        logger.info("Fetching %d granules from the CMR concepts endpoint", len(rest))
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 4))) as executor:
            for cid, native in zip(rest, executor.map(_fetch_native_umm, rest)):
                if native is not None:
                    found[cid] = native

    lost = len(ids) - len(found)
    if lost:
        logger.warning("%d of %d granules are no longer in CMR", lost, len(ids))
    return [found[cid] for cid in ids if cid in found]


def fetch_umm_granules(
    params: Dict[str, Any],
    max_workers: int = 4,
    rate_limit_delay: float = 0.0,
    report: Optional[CMRSearchReport] = None,
) -> List[Dict[str, Any]]:
    """Fetch the UMM-G record of every granule a CMR search matches.

    ``granules.umm_json`` leaves out records that ``CMR-Hits`` counts, and
    under some concept ids serves another granule's record (in October 2026,
    about 6,000 and 3,500 of 89,890 NISAR GUNWs), while the atom-JSON
    ``granules.json`` lists every granule under its own name. The atom walk
    therefore supplies the complete set of concept ids and names, and
    :func:`recover_umm_items` fetches every record the UMM-JSON walk missed or
    got wrong.

    Parameters
    ----------
    params : dict
        CMR granule search parameters, as from ``_build_search_params``.
    max_workers : int
        Concurrent CMR page workers.
    rate_limit_delay : float
        Delay in seconds between page requests.
    report : CMRSearchReport, optional
        Filled with what CMR left out, mislabelled or listed twice.

    Returns
    -------
    list of dict
        One UMM-JSON item per granule name. When CMR lists a name under several
        concept ids, the most recently ingested one (highest concept id) is
        kept and the duplicates go to ``report``.

    """
    items, listed = (
        _by_concept_id(
            fetch_cmr_pages(
                url,
                params,
                max_workers=max_workers,
                rate_limit_delay=rate_limit_delay,
            )
        )
        for url in (_UMM_SEARCH_URL, _JSON_SEARCH_URL)
    )
    names = {cid: entry.get("title", "") for cid, entry in listed.items()}
    wrong = [
        cid for cid in names if cid in items and _granule_ur(items[cid]) != names[cid]
    ]
    if report is not None:
        report.listed = len(names)
        report.left_out = [cid for cid in names if cid not in items]
        report.mislabelled = {cid: _granule_ur(items[cid]) for cid in wrong}
    for cid in wrong:
        del items[cid]
    missing = [cid for cid in names if cid not in items]
    if wrong:
        logger.warning(
            "CMR UMM-JSON served another granule's record for %d of %d granules",
            len(wrong),
            len(names),
        )
    if missing:
        logger.warning(
            "CMR UMM-JSON left out or mislabelled %d of %d granules; "
            "recovering them by concept id",
            len(missing),
            len(names),
        )
        items.update(
            _by_concept_id(recover_umm_items(missing, max_workers, expected=names))
        )
    unrecovered = [cid for cid in missing if cid not in items]
    duplicates, unique = _one_per_name(items)
    if duplicates:
        logger.warning(
            "CMR lists %d granule names under more than one concept id; "
            "keeping the most recent of each",
            len(duplicates),
        )
    if report is not None:
        report.unrecovered = unrecovered
        report.duplicate_names = duplicates
    return unique


def _one_per_name(
    items: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, List[str]], List[Dict[str, Any]]]:
    """Keep one item per granule name, the one with the highest concept id.

    Returns the names CMR lists under several concept ids (name -> sorted ids)
    and the kept items.
    """
    by_name: Dict[str, List[str]] = defaultdict(list)
    for cid, item in items.items():
        by_name[_granule_ur(item)].append(cid)
    duplicates = {
        name: sorted(cids, key=_concept_number)
        for name, cids in by_name.items()
        if len(cids) > 1
    }
    kept = [items[max(cids, key=_concept_number)] for cids in by_name.values()]
    return duplicates, kept


def fetch_umm_by_granule_ur(
    short_name: str, granule_urs: List[str], batch: int = 50
) -> List[Dict[str, Any]]:
    """Fetch the UMM-G records of named granules, recovering any CMR drops.

    Names UMM-JSON does not return are looked up in atom-JSON for their concept
    ids and recovered with :func:`recover_umm_items`. Names CMR does not know
    are left out.

    Parameters
    ----------
    short_name : str
        CMR collection short name the granules belong to.
    granule_urs : list of str
        Granule names (``GranuleUR``).
    batch : int
        Names per POST request.

    Returns
    -------
    list of dict
        UMM-JSON items in the order of ``granule_urs``.

    """
    names = list(dict.fromkeys(granule_urs))
    found: Dict[str, Dict[str, Any]] = {}
    missing_ids: List[str] = []
    expected: Dict[str, str] = {}
    for start in range(0, len(names), batch):
        chunk = names[start : start + batch]
        data = {"short_name": short_name, "page_size": 2 * len(chunk)}
        response = requests.post(
            _UMM_SEARCH_URL, data={**data, "granule_ur[]": chunk}, timeout=120
        )
        response.raise_for_status()
        for item in _extract_items(response.json()):
            found[item["umm"]["GranuleUR"]] = item

        absent = [name for name in chunk if name not in found]
        if absent:
            response = requests.post(
                _JSON_SEARCH_URL, data={**data, "granule_ur[]": absent}, timeout=120
            )
            response.raise_for_status()
            for entry in _extract_items(response.json()):
                missing_ids.append(_concept_id(entry))
                expected[_concept_id(entry)] = entry.get("title", "")

    if missing_ids:
        logger.warning(
            "CMR UMM-JSON left out %d of %d named granules; recovering them",
            len(missing_ids),
            len(names),
        )
        for item in recover_umm_items(missing_ids, expected=expected):
            found[_granule_ur(item)] = item
    return [found[name] for name in names if name in found]


def _build_search_params(
    short_name: Union[str, List[str]],
    provider: str,
    *,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    track: Optional[int] = None,
    frame: Optional[int] = None,
    direction: Optional[str] = None,
    cycle: Optional[int] = None,
    polarization: Optional[str] = None,
    start_datetime: Optional[datetime] = None,
    end_datetime: Optional[datetime] = None,
    page_size: int = 2000,
) -> Dict[str, Any]:
    """Assemble CMR query params shared by the product and granule searches."""
    params: Dict[str, Any] = {
        "short_name": short_name,  # str or list; a list is sent as repeated params (OR)
        "provider": provider,
        "page_size": page_size,
        # Paging is not a snapshot: without a stable sort, granules ingested
        # mid-query shift rows across page boundaries and get duplicated or
        # missed. Sorting by start_date appends new ingests past the last page.
        "sort_key": "start_date",
    }

    if bbox is not None:
        west, south, east, north = bbox
        params["bounding_box"] = f"{west},{south},{east},{north}"

    if start_datetime is not None or end_datetime is not None:
        start_str = start_datetime.isoformat() if start_datetime is not None else ""
        end_str = end_datetime.isoformat() if end_datetime is not None else ""
        params["temporal"] = f"{start_str},{end_str}"

    attribute_filters: List[str] = []
    if track is not None:
        attribute_filters.append(f"int,TRACK_NUMBER,{track}")
    if frame is not None:
        attribute_filters.append(f"int,FRAME_NUMBER,{frame}")
    if direction is not None:
        dir_str = "ASCENDING" if direction.upper() == "A" else "DESCENDING"
        attribute_filters.append(f"string,ASCENDING_DESCENDING,{dir_str}")
    if cycle is not None:
        attribute_filters.append(f"int,CYCLE_NUMBER,{cycle}")
    if polarization is not None:
        attribute_filters.append(f"string,POLARIZATION,{polarization.upper()}")

    if attribute_filters:
        params["attribute[]"] = attribute_filters

    return params


def _apply_max_results(items: List[Any], max_results: Optional[int]) -> List[Any]:
    """Truncate ``items`` to ``max_results``, warning when anything is dropped.

    ``None`` or a non-positive cap keeps everything. CMR is paged eagerly, so a
    cap never saves query time -- silently returning a partial archive would
    make a catalog look complete when it is not.
    """
    if max_results is None or max_results <= 0 or len(items) <= max_results:
        return items
    logger.warning(
        "Truncating %d CMR results to max_results=%d; %d granules dropped. "
        "Pass max_results=None (CLI: --max-results 0) for the whole archive.",
        len(items),
        max_results,
        len(items) - max_results,
    )
    return items[:max_results]


def search_nisar_products(
    bbox: Optional[Tuple[float, float, float, float]] = None,
    track: Optional[int] = None,
    frame: Optional[int] = None,
    direction: Optional[str] = None,
    cycle: Optional[int] = None,
    product_type: Union[str, ProductType] = ProductType.GSLC,
    polarization: Optional[str] = None,
    start_datetime: Optional[datetime] = None,
    end_datetime: Optional[datetime] = None,
    url_type: UrlType = UrlType.HTTPS,
    provider: str = "ASF",
    short_name: Optional[Union[str, List[str]]] = None,
    max_results: Optional[int] = None,
    max_workers: int = 4,  # Increased workers to 4 by default
    report: Optional[CMRSearchReport] = None,
) -> List[NISARProduct]:
    """Search for NISAR products in CMR.

    Parameters
    ----------
    bbox : Optional[Tuple[float, float, float, float]]
        Bounding box as (west, south, east, north) in degrees lon/lat.
    track : Optional[int]
        Track/relative orbit number.
    frame : Optional[int]
        Frame number.
    direction : Optional[str]
        Orbit direction: "A" for ascending, "D" for descending.
    cycle : Optional[int]
        Cycle number.
    product_type : Union[str, ProductType]
        Product type: "GSLC" or "GUNW".
    polarization : Optional[str]
        Polarization (e.g., "HH", "VV").
    start_datetime : Optional[datetime]
        Start datetime for temporal filtering.
    end_datetime : Optional[datetime]
        End datetime for temporal filtering.
    url_type : UrlType
        URL type: "https" or "s3".
    provider : str
        Data provider (default: "ASF").
    short_name : Optional[str]
        CMR short name. If None, determined from product_type.
    max_results : Optional[int]
        Cap on the number of returned products. ``None`` (the default) or a
        value <= 0 returns the whole matching archive. The cap is applied
        *after* every page is fetched, so it saves no query time -- it only
        drops granules, in arbitrary CMR order. Truncation is logged as a
        warning.
    max_workers : int
        Maximum number of concurrent workers for fetching pages.
        Default is 2 to avoid overwhelming the CMR API.
    report : CMRSearchReport, optional
        Filled with what CMR left out, mislabelled or listed twice; see
        :func:`fetch_umm_granules`.

    Returns
    -------
    List[NISARProduct]
        List of NISAR products matching the search criteria.

    """
    # Convert product_type to enum if it's a string
    if isinstance(product_type, str):
        product_type = ProductType(product_type.upper())

    # Default to the PROVISIONAL collection for the product type. Pass
    # ``NISARCollection.GSLC_SHORT_NAMES`` to also search BETA -- CMR ORs
    # multiple ``short_name`` values into a single search.
    if short_name is None:
        short_name = (
            NISARCollection.GSLC_PROVISIONAL_V1_SHORT_NAME
            if product_type == ProductType.GSLC
            else NISARCollection.GUNW_PROVISIONAL_V1_SHORT_NAME
        )

    params = _build_search_params(
        short_name,
        provider,
        bbox=bbox,
        track=track,
        frame=frame,
        direction=direction,
        cycle=cycle,
        polarization=polarization,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
    )

    # Fetch all pages with optimized parameters for large result sets
    logger.info(f"Searching for NISAR products with {max_results=}, {max_workers=}")

    # Ensure we respect CMR rate limits while using parallel workers
    rate_limit_delay = 0.1 if max_workers > 2 else 0.0
    items = fetch_umm_granules(
        params,
        max_workers=max_workers,
        rate_limit_delay=rate_limit_delay,
        report=report,
    )

    # Convert to NISARProduct objects
    products = []
    for item in items:
        try:
            product = NISARProduct.from_cmr_item(item, url_type=url_type)

            # Filter by product type if needed (server filtering may not work)
            if product.product_type != product_type:
                continue

            products.append(product)
        except (ValueError, KeyError) as e:
            logger.debug(f"Skipping item due to parsing error: {e}")

    # Sort by track, frame, and date
    products.sort(
        key=lambda p: (p.track or 0, p.frame or 0, p.direction or "", p.start_datetime)
    )

    return _apply_max_results(products, max_results)


def search_nisar_granules(
    short_name: Union[str, List[str]],
    provider: str = "ASF",
    max_results: Optional[int] = None,
    max_workers: int = 4,
    output_format: str = "json",  # noqa: ARG001  # kept for backward compatibility
) -> pd.DataFrame:
    """Search CMR and return a flat granule DataFrame for the catalog builders.

    Unlike :func:`search_nisar_products` (which returns typed
    :class:`NISARProduct` objects), this returns one row per granule with the
    raw fields the catalog pipeline needs: ``granule_id``, ``name`` and the
    CMR ``links`` list (``{"rel": ..., "href": ...}``). The granule *name* is
    parsed by ``GSLCFilename`` / ``GUNWFilename`` and the links by
    ``nisar_db.catalog._common.extract_urls`` downstream.

    Parameters
    ----------
    short_name : str or list of str
        CMR collection short name(s) to search.
    provider : str
        Data provider (default: "ASF").
    max_results : Optional[int]
        Cap on the number of returned rows; ``None`` (the default) or <= 0
        keeps the whole archive. See :func:`search_nisar_products` for why a
        cap costs the same query time as no cap.
    max_workers : int
        Concurrent CMR page workers.
    output_format : str
        Kept for backward compatibility; the echo10 ``granules.json`` endpoint
        is always used because its ``links`` carry the ``rel`` values the
        catalog extractor expects.

    Returns
    -------
    pd.DataFrame
        Columns ``granule_id``, ``name``, ``links``.

    """
    params = _build_search_params(short_name, provider)

    rate_limit_delay = 0.1 if max_workers > 2 else 0.0
    entries = fetch_cmr_pages(
        _JSON_SEARCH_URL,
        params,
        max_workers=max_workers,
        rate_limit_delay=rate_limit_delay,
    )

    rows = []
    for entry in _apply_max_results(entries, max_results):
        rows.append(
            {
                "granule_id": entry.get("id", ""),
                "name": entry.get("title", ""),  # atom "title" is the granule name
                "links": entry.get("links", []),
            }
        )

    df = pd.DataFrame(rows, columns=["granule_id", "name", "links"])
    logger.info(f"Found {len(df)} granules for {short_name}")
    return df
