#!/usr/bin/env python
"""Collect per-granule QA metrics from NISAR GSLC / GUNW ``QA_STATS.h5`` files.

Every product ships a small ``<granule>_QA_STATS.h5`` (60-160 kB) next to it,
written by the NISAR QA software. It holds summary statistics and histograms of
the product's layers, so the quality of a granule can be read without opening
the product. The files sit behind the Earthdata login like the products;
credentials are read from ``~/.netrc`` (machine ``urs.earthdata.nasa.gov``).

Results go to a gzipped JSON cache keyed by granule id, the same way
``collect_granule_flags.py`` caches its flags: granules already cached are
skipped, so a rerun only reads new products, and failed reads are retried.

A GUNW maps to these values of its frequency-A co-pol unwrapped group:

* ``cm`` / ``ca`` -- coherence median / mean,
* ``v`` -- valid unwrapped pixels (non-zero connected component), % of the data area,
* ``l`` -- pixels in the largest connected component, % of the data area,
* ``n`` -- number of valid connected components,
* ``im`` / ``imd`` -- ionosphere phase screen mean / median (rad),
* ``is`` -- ionosphere phase screen spread, its standard deviation (rad),
* ``iu`` -- mean ionosphere phase screen uncertainty (rad),

and a GSLC to

* ``rl`` -- RFI likelihood, the largest over its frequencies and polarizations.
  It is documented as lying in [0, 1], but most granules report far larger
  values (up to ~1e31), so it is stored as reported.

A granule withdrawn from the archive is cached as an empty record.

The same file also carries most of the granule flags that
``collect_granule_flags.py`` reads from the multi-gigabyte product: its
``identification`` group holds the joint observation, full frame, mixed mode
and dithering flags. Given a flag cache to fill (``--flags-output``), each
download fills both caches. The two flags the file lacks come from elsewhere:

* ``o`` -- the orbit type, from the granule's CMR record (the CLI looks it up
  for the granules it reads). For a GUNW that is the reference acquisition's
  orbit; ``collect_granule_flags.read_flags`` reads both from the product and
  stores ``REF/SEC`` when they differ, which no small file records;
* ``r`` -- RFI mitigation, which no small file records. It is set per product
  type and composite release (CRID) by the processing configuration, and the
  flags read so far agree within every such group (all GSLC 0, all GUNW 1 at
  P05023). It is inherited from the flags already cached for the group; for a
  group not seen before, it is read from one product with
  ``collect_granule_flags.read_flags`` and inherited by the rest.

Means, medians and spreads are computed from the stored histograms. The QA
software's own ``mean_value`` / ``sample_stddev`` come out near zero for some
granules whose histograms are fine, so they are not used. The data area is the
raster minus its NaN fill (``unwrappedPhase/percentNan``), so ``v`` and ``l``
do not depend on how much of the frame's grid the swath covers. ``qv`` records
the QA software version.

Examples
--------
Collect the QA metrics of every granule in a built viewer::

    python scripts/collect_granule_qa.py \\
        --viewer-html docs/assets/opera_nisar_db_viewer.html \\
        --output catalog/granule_qa.json.gz

and fill the flag cache from the same files::

    python scripts/collect_granule_qa.py \\
        --viewer-html docs/assets/opera_nisar_db_viewer.html \\
        --output catalog/granule_qa.json.gz \\
        --flags-output catalog/granule_flags.json.gz

"""

from __future__ import annotations

import argparse
import io
import netrc
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from collect_granule_flags import (
    COLLECTIONS,
    _flag,
    _text,
    load_cache,
    read_flags,
    save_cache,
    viewer_granule_ids,
)

if TYPE_CHECKING:
    import h5py
    import numpy as np

QA_URL = (
    "https://nisar.asf.earthdatacloud.nasa.gov/NISAR/{collection}/{gid}/"
    "{gid}_QA_STATS.h5"
)
# Co-pol first: the unwrapped group of a dual-pol GUNW carries one of these.
COPOLS = ("HH", "VV")

# The downloads wait on the network, not the CPU, so threads in one process
# do the work; the HDF5 parsing h5py serialises takes milliseconds per file.
DEFAULT_WORKERS = 48
# Throttling and passing server errors are retried with backoff, not counted
# as failed granules.
RETRY = Retry(
    total=5,
    backoff_factor=1.0,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET",),
    raise_on_status=False,
)

_LOCAL = threading.local()
# Earthdata login cookies from the first download, so the other threads skip
# the login round trip.
_COOKIES: requests.cookies.RequestsCookieJar | None = None


def _session() -> requests.Session:
    # requests sessions are not thread-safe: one per thread, all sharing the
    # login cookies once one thread has them.
    session = getattr(_LOCAL, "session", None)
    if session is None:
        auth = netrc.netrc().authenticators("urs.earthdata.nasa.gov")
        if auth is None:
            raise RuntimeError("no urs.earthdata.nasa.gov entry in ~/.netrc")
        session = requests.Session()
        session.auth = (auth[0], auth[2] or "")
        session.mount("https://", HTTPAdapter(max_retries=RETRY))
        if _COOKIES is not None:
            session.cookies.update(_COOKIES)
        _LOCAL.session = session
    return session


def qa_url(gid: str) -> str:
    """Return the HTTPS URL of a granule's ``QA_STATS.h5``."""
    kind = gid.split("_")[3]
    return QA_URL.format(collection=COLLECTIONS[kind], gid=gid)


def hist_stats(bins: np.ndarray, density: np.ndarray) -> dict[str, float] | None:
    """Mean, median and standard deviation of a QA histogram.

    Parameters
    ----------
    bins : numpy.ndarray
        The ``n + 1`` bin edges.
    density : numpy.ndarray
        The ``n`` normalised densities.

    Returns
    -------
    dict or None
        ``mean``, ``median`` and ``std``, or None for an empty histogram.

    Examples
    --------
    >>> import numpy as np
    >>> s = hist_stats(np.array([0.0, 1.0, 2.0, 3.0]), np.array([0.25, 0.5, 0.25]))
    >>> round(s["mean"], 3), round(s["median"], 3), round(s["std"], 3)
    (1.5, 1.5, 0.707)

    """
    import numpy as np

    weights = np.nan_to_num(density) * np.diff(bins)
    total = weights.sum()
    if not total > 0:
        return None
    weights = weights / total
    centres = (bins[1:] + bins[:-1]) / 2
    mean = float((weights * centres).sum())
    std = float(np.sqrt((weights * (centres - mean) ** 2).sum()))
    # Interpolate inside the bin where the cumulative weight crosses one half.
    cdf = np.cumsum(weights)
    i = int(np.searchsorted(cdf, 0.5))
    below = cdf[i - 1] if i else 0.0
    frac = (0.5 - below) / weights[i] if weights[i] > 0 else 0.5
    median = float(bins[i] + frac * (bins[i + 1] - bins[i]))
    return {"mean": mean, "median": median, "std": std}


def _hist(group: h5py.Group) -> dict[str, float] | None:
    return hist_stats(group["histogramBins"][()], group["histogramDensity"][()])


def gunw_metrics(h5: h5py.File) -> dict:
    """Read the GUNW QA metrics described in the module docstring."""
    freq = h5["science/LSAR/QA/data/frequencyA"]
    unw = freq["unwrappedInterferogram"]
    pol = next((p for p in COPOLS if p in unw), sorted(unw)[0])
    grp = unw[pol]
    cc = grp["connectedComponents"]
    data_area = 100.0 - float(grp["unwrappedPhase/percentNan"][()])
    out: dict = {"n": int(cc["numValidConnectedComponents"][()])}
    if data_area > 0:
        out["v"] = round(
            100 * float(cc["percentPixelsWithNonZeroCC"][()]) / data_area, 1
        )
        out["l"] = round(100 * float(cc["percentPixelsInLargestCC"][()]) / data_area, 1)
    coh = _hist(grp["coherenceMagnitude"])
    if coh:
        out["cm"] = round(coh["median"], 3)
        out["ca"] = round(coh["mean"], 3)
    if "ionospherePhaseScreen" in grp:
        iono = _hist(grp["ionospherePhaseScreen"])
        if iono:
            out["im"] = round(iono["mean"], 2)
            out["imd"] = round(iono["median"], 2)
            out["is"] = round(iono["std"], 2)
    if "ionospherePhaseScreenUncertainty" in grp:
        unc = _hist(grp["ionospherePhaseScreenUncertainty"])
        if unc:
            out["iu"] = round(unc["mean"], 2)
    return out


def gslc_metrics(h5: h5py.File) -> dict:
    """Read the GSLC QA metrics described in the module docstring."""
    rfi = h5.get("science/LSAR/RFI/data")
    values: list[float] = []
    if rfi is not None:
        rfi.visititems(
            lambda name, obj: (
                values.append(float(obj[()]))
                if name.endswith("rfiLikelihood")
                else None
            )
        )
    finite = [v for v in values if v == v]
    return {"rl": round(max(finite), 3)} if finite else {}


def identification_flags(h5: h5py.File) -> dict[str, int]:
    """Read the ``j f m d`` granule flags from a QA file's identification group.

    A GUNW counts as a joint observation when either of its acquisitions is
    one, as ``collect_granule_flags.read_flags`` counts it.
    """
    ident = h5["science/LSAR/identification"]
    if "referenceIsJointObservation" in ident:
        joint = _flag(ident["referenceIsJointObservation"]) | _flag(
            ident["secondaryIsJointObservation"]
        )
    else:
        joint = _flag(ident["isJointObservation"])
    return {
        "j": joint,
        "f": _flag(ident["isFullFrame"]),
        "m": _flag(ident["isMixedMode"]),
        "d": _flag(ident["isDithered"]),
    }


def read_granule(gid: str) -> tuple[dict, dict | None]:
    """Download one granule's ``QA_STATS.h5`` and read its metrics and flags.

    Parameters
    ----------
    gid : str
        GSLC or GUNW granule id.

    Returns
    -------
    qa : dict
        Metrics keyed as described in the module docstring, plus ``qv``; empty
        when the granule is no longer in the archive.
    flags : dict or None
        The ``j f m d`` flags of :func:`identification_flags`, or None when the
        granule is no longer in the archive.

    """
    import h5py

    resp = _session().get(qa_url(gid), timeout=120)
    # A product withdrawn from the archive takes its QA files with it; caching
    # it as empty stops every later run from asking again.
    if resp.status_code == 404:
        return {}, None
    resp.raise_for_status()
    with h5py.File(io.BytesIO(resp.content)) as h5:
        out = gunw_metrics(h5) if "GUNW" in gid.split("_")[3] else gslc_metrics(h5)
        version = h5.get("science/LSAR/QA/processing/QASoftwareVersion")
        if version is not None:
            out["qv"] = _text(version)
        flags = identification_flags(h5)
    return out, flags


def read_qa(gid: str) -> dict:
    """Download one granule's ``QA_STATS.h5`` and read its metrics."""
    return read_granule(gid)[0]


def release_group(gid: str) -> tuple[str, str]:
    """Product type and composite release (CRID) of a granule.

    Examples
    --------
    >>> release_group("NISAR_L2_PR_GSLC_031_155_D_084_4005_DHDH_A_20260928T231125_"
    ...               "20260928T231159_P05023_N_F_J_001")
    ('GSLC', 'P05023')

    """
    parts = gid.split("_")
    return parts[3], parts[-5]


def catalog_flags(products: list) -> dict[str, dict]:
    """The granule flags a CMR record carries: ``j``, ``f`` and ``o``.

    ``JOINT_OBSERVATION``, ``FULL_FRAME`` and ``ORBIT_TYPE`` agree with the
    flags read from the products (a GUNW's joint observation counts either
    acquisition), so these come with the search at no extra cost; mixed mode
    and dithering do not, and are collected from the QA files.
    """
    out = {}
    for product in products:
        attrs = {
            a.get("Name"): a["Values"][0]
            for a in (product.metadata or {}).get("AdditionalAttributes") or []
            if a.get("Values")
        }
        value: dict = {}
        for key, name in (("j", "JOINT_OBSERVATION"), ("f", "FULL_FRAME")):
            if name in attrs:
                value[key] = int(str(attrs[name]).upper() == "TRUE")
        if "ORBIT_TYPE" in attrs:
            value["o"] = attrs["ORBIT_TYPE"]
        if value:
            out[product.name] = value
    return out


def rfi_by_release(flags: dict[str, dict]) -> dict[tuple[str, str], int]:
    """The RFI mitigation flag of each release group the cached flags agree on.

    A group whose cached granules disagree is left out, so its granules are
    read from their products rather than guessed.
    """
    seen: dict[tuple[str, str], set[int]] = {}
    for gid, value in flags.items():
        if "r" in value:
            seen.setdefault(release_group(gid), set()).add(value["r"])
    return {group: vals.pop() for group, vals in seen.items() if len(vals) == 1}


def _read_or_error(gid: str) -> tuple[dict, dict | None] | str:
    # An HTTP error can carry the signed download URL, so only the part before
    # its query string is reported.
    try:
        return read_granule(gid)
    except Exception as exc:  # noqa: BLE001 - one bad granule must not stop the run
        return f"{type(exc).__name__}: {str(exc).split('?', 1)[0]}"


def _learn_rfi(
    todo: list[str],
    rfi: dict[tuple[str, str], int],
    say: Callable[[str], None],
) -> None:
    # One product read for each release group the cached flags do not cover.
    for group in dict.fromkeys(release_group(g) for g in todo):
        if group in rfi:
            continue
        for gid in (g for g in todo if release_group(g) == group):
            try:
                rfi[group] = read_flags(gid)["r"]
            except Exception as exc:  # noqa: BLE001 - try the group's next granule
                say(f"  RFI flag of {gid}: {type(exc).__name__}")
                continue
            say(f"  RFI mitigation of {group[0]} {group[1]}: {rfi[group]}")
            break


def collect(
    ids: list[str],
    output: Path,
    workers: int = DEFAULT_WORKERS,
    save_every: int = 1000,
    progress: Callable[[str], None] | None = None,
    flags_output: Path | None = None,
    orbits: dict[str, str] | None = None,
    known_flags: dict[str, dict] | None = None,
) -> None:
    """Read the QA metrics (and flags) of the granules of ``ids`` not yet cached.

    Parameters
    ----------
    ids : list of str
        Granules to read.
    output : Path
        QA cache to create or extend.
    workers : int
        Downloads running at once.
    save_every : int
        Write the caches after this many granules.
    progress : callable, optional
        Called with a progress message; printed when not given.
    flags_output : Path, optional
        Flag cache to fill from the same downloads; a granule missing from
        either cache is read.
    orbits : dict, optional
        Granule id to its orbit type (CMR's ``ORBIT_TYPE``), stored as ``o``.
    known_flags : dict, optional
        Flags cached elsewhere (the repository's), to learn the RFI mitigation
        flag of each release group from.

    """
    # Progress goes to stdout unless a caller (the viewer helper) takes it.
    global _COOKIES
    say = progress or (lambda msg: print(msg, flush=True))
    qa = load_cache(output)
    flags = load_cache(flags_output) if flags_output is not None else None
    todo = [
        g
        for g in dict.fromkeys(ids)
        if g not in qa or (flags is not None and g not in flags)
    ]
    say(f"{len(qa)} cached, {len(todo)} to read with {workers} threads")
    if not todo:
        return
    rfi: dict[tuple[str, str], int] = {}
    if flags is not None:
        rfi = rfi_by_release({**(known_flags or {}), **flags})
        _learn_rfi(todo, rfi, say)
    orbits = orbits or {}

    def store(gid: str, result: tuple[dict, dict | None]) -> None:
        qa[gid], found = result
        if flags is None or found is None:
            return
        value = {"j": found["j"], "f": found["f"]}
        if gid in orbits:
            value["o"] = orbits[gid]
        if release_group(gid) in rfi:
            value["r"] = rfi[release_group(gid)]
        flags[gid] = {**value, "m": found["m"], "d": found["d"]}

    def save() -> None:
        save_cache(output, qa)
        if flags is not None and flags_output is not None:
            save_cache(flags_output, flags)

    failed: dict[str, str] = {}
    # The first download logs in; the threads then start from its cookies.
    first = _read_or_error(todo[0])
    if isinstance(first, str):
        failed[todo[0]] = first
    else:
        store(todo[0], first)
        _COOKIES = _session().cookies
    t0 = time.time()
    rest = todo[1:]
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(_read_or_error, gid): gid for gid in rest}
        for n, fut in enumerate(as_completed(futures), 1):
            gid = futures[fut]
            result = fut.result()
            if isinstance(result, str):
                failed[gid] = result
            else:
                store(gid, result)
            if n % save_every == 0 or n == len(rest):
                save()
                rate = n / (time.time() - t0)
                say(
                    f"  {n + 1}/{len(todo)} read, {len(failed)} failed, "
                    f"{rate:.1f}/s, ~{(len(rest) - n) / rate / 60:.0f} min left"
                )
    save()
    for gid, err in list(failed.items())[:20]:
        say(f"  failed {gid}: {err}")
    say(f"{len(qa)} granules in {output}; {len(failed)} failed (rerun to retry)")


CMR_SEARCH = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"


def cmr_orbit_types(ids: list[str], batch: int = 50) -> dict[str, str]:
    """Look up the orbit type (``ORBIT_TYPE``) of each granule in CMR.

    The granule ids go in the body of a POST, ``batch`` at a time, so a weekly
    run's few thousand new granules take a minute.
    """
    out: dict[str, str] = {}
    for kind, short_name in (
        ("GSLC", "NISAR_L2_GSLC_PROVISIONAL_V1"),
        ("GUNW", "NISAR_L2_GUNW_PROVISIONAL_V1"),
    ):
        names = [g for g in ids if g.split("_")[3] == kind]
        for i in range(0, len(names), batch):
            resp = requests.post(
                CMR_SEARCH,
                data={
                    "short_name": short_name,
                    "granule_ur[]": names[i : i + batch],
                    "page_size": 2 * batch,
                },
                timeout=120,
            )
            resp.raise_for_status()
            products = [
                SimpleNamespace(name=item["umm"]["GranuleUR"], metadata=item["umm"])
                for item in resp.json()["items"]
            ]
            out.update(
                {g: v["o"] for g, v in catalog_flags(products).items() if "o" in v}
            )
    return out


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--viewer-html",
        type=Path,
        required=True,
        help="Built viewer whose GSLC and GUNW granules to read.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("catalog/granule_qa.json.gz"),
        help="QA cache to create or extend.",
    )
    parser.add_argument(
        "--flags-output",
        type=Path,
        default=None,
        help="Flag cache to fill from the same files (orbit types from CMR).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Downloads running at once.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Read at most this many new granules."
    )
    args = parser.parse_args(argv)

    ids = list(dict.fromkeys(viewer_granule_ids(args.viewer_html)))
    qa_cached = load_cache(args.output)
    flags_cached = load_cache(args.flags_output) if args.flags_output else None
    todo = [
        g
        for g in ids
        if g not in qa_cached or (flags_cached is not None and g not in flags_cached)
    ]
    if args.limit is not None:
        todo = todo[: args.limit]
    orbits: dict[str, str] = {}
    if flags_cached is not None and todo:
        orbits = cmr_orbit_types([g for g in todo if g not in flags_cached])
        print(f"orbit types of {len(orbits)} granules from CMR", flush=True)
    collect(
        todo, args.output, args.workers, flags_output=args.flags_output, orbits=orbits
    )


if __name__ == "__main__":
    main()
