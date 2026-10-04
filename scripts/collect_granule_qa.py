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

"""

from __future__ import annotations

import argparse
import io
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from collect_granule_flags import (
    COLLECTIONS,
    _session,
    _text,
    load_cache,
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


def read_qa(gid: str) -> dict:
    """Download one granule's ``QA_STATS.h5`` and read its metrics.

    Parameters
    ----------
    gid : str
        GSLC or GUNW granule id.

    Returns
    -------
    dict
        Metrics keyed as described in the module docstring, plus ``qv``; empty
        when the granule is no longer in the archive.

    """
    import h5py

    resp = _session().get(qa_url(gid), timeout=120)
    # A product withdrawn from the archive takes its QA files with it; caching
    # it as empty stops every later run from asking again.
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    with h5py.File(io.BytesIO(resp.content)) as h5:
        out = gunw_metrics(h5) if "GUNW" in gid.split("_")[3] else gslc_metrics(h5)
        version = h5.get("science/LSAR/QA/processing/QASoftwareVersion")
        if version is not None:
            out["qv"] = _text(version)
    return out


def _read_or_error(gid: str) -> dict | str:
    # Runs in a worker process: an HTTP error can carry unpicklable response
    # objects, so failures come back as text rather than as the exception.
    try:
        return read_qa(gid)
    except Exception as exc:  # noqa: BLE001 - one bad granule must not stop the run
        return f"{type(exc).__name__}: {str(exc).split('?', 1)[0]}"


def collect(
    ids: list[str],
    output: Path,
    workers: int,
    save_every: int = 500,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Read the QA metrics of every granule in ``ids`` not already in ``output``."""
    # Progress goes to stdout unless a caller (the viewer helper) takes it.
    say = progress or (lambda msg: print(msg, flush=True))
    qa = load_cache(output)
    todo = [g for g in dict.fromkeys(ids) if g not in qa]
    say(f"{len(qa)} cached, {len(todo)} to read with {workers} workers")
    failed: dict[str, str] = {}
    t0 = time.time()
    with ProcessPoolExecutor(workers) as pool:
        futures = {pool.submit(_read_or_error, gid): gid for gid in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            gid = futures[fut]
            result = fut.result()
            if isinstance(result, str):
                failed[gid] = result
            else:
                qa[gid] = result
            if n % save_every == 0 or n == len(todo):
                save_cache(output, qa)
                rate = n / (time.time() - t0)
                say(
                    f"  {n}/{len(todo)} read, {len(failed)} failed, "
                    f"{rate:.1f}/s, ~{(len(todo) - n) / rate / 60:.0f} min left"
                )
    for gid, err in list(failed.items())[:20]:
        say(f"  failed {gid}: {err}")
    say(f"{len(qa)} granules in {output}; {len(failed)} failed (rerun to retry)")


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
        "--workers", type=int, default=16, help="Worker processes reading at once."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Read at most this many new granules."
    )
    args = parser.parse_args(argv)

    ids = viewer_granule_ids(args.viewer_html)
    if args.limit is not None:
        cached = load_cache(args.output)
        ids = [g for g in ids if g not in cached][: args.limit]
    collect(ids, args.output, args.workers)


if __name__ == "__main__":
    main()
