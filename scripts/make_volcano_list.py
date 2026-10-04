#!/usr/bin/env python
"""Write the Holocene volcano list the frame viewer draws.

The Smithsonian Global Volcanism Program's *Volcanoes of the World* list is
served by a WFS that sends no CORS header, so a web page cannot fetch it. This
script takes a trimmed snapshot of it (point, number, name, type, last eruption,
elevation, country, region, evidence) into
``scripts/gvp_holocene_volcanoes.geojson``, which the viewer embeds. The long
geological summaries are left out; the viewer links each volcano to its GVP
page. The list changes rarely, so refresh it now and then::

    python scripts/make_volcano_list.py

Live US alert levels are not part of the snapshot: the viewer fetches them from
USGS (which allows any origin) when its volcano layer is shown.

Source: Global Volcanism Program, Smithsonian Institution,
https://volcano.si.edu (Volcanoes of the World, Holocene list).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests

GVP_WFS = (
    "https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows?service=WFS"
    "&version=1.0.0&request=GetFeature"
    "&typeName=GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes"
    "&outputFormat=application/json"
)
OUTPUT = Path(__file__).resolve().parent / "gvp_holocene_volcanoes.geojson"


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def trim_volcanoes(collection: dict) -> dict:
    """Keep only the fields the viewer shows, with short keys.

    Parameters
    ----------
    collection : dict
        The GVP WFS GeoJSON ``FeatureCollection``.

    Returns
    -------
    dict
        A ``FeatureCollection`` whose features carry ``v`` (volcano number),
        ``n`` (name), ``t`` (primary type), ``y`` (last eruption year, negative
        for BCE, null when unknown), ``e`` (elevation, m), ``c`` (country),
        ``r`` (region) and ``ev`` (evidence category), at ~10 m precision.

    Examples
    --------
    >>> fc = {"features": [{"geometry": {"type": "Point", "coordinates": [6.851, 50.172]},
    ...        "properties": {"Volcano_Number": 210010, "Volcano_Name": "West Eifel",
    ...                       "Primary_Volcano_Type": "Volcanic field",
    ...                       "Last_Eruption_Year": -8300, "Elevation": 600,
    ...                       "Country": "Germany", "Region": "European",
    ...                       "Evidence_Category": "Eruption Dated"}}]}
    >>> trim_volcanoes(fc)["features"][0]["properties"]["y"]
    -8300

    """
    features: list[dict] = []
    for f in collection["features"]:
        p = f["properties"]
        geom = f.get("geometry")
        if not geom or geom.get("type") != "Point":
            continue
        lon, lat = geom["coordinates"][:2]
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(float(lon), 4), round(float(lat), 4)],
                },
                "properties": {
                    "v": _int_or_none(p.get("Volcano_Number")),
                    "n": p.get("Volcano_Name") or "",
                    "t": p.get("Primary_Volcano_Type") or "",
                    "y": _int_or_none(p.get("Last_Eruption_Year")),
                    "e": _int_or_none(p.get("Elevation")),
                    "c": p.get("Country") or "",
                    "r": p.get("Region") or "",
                    "ev": p.get("Evidence_Category") or "",
                },
            }
        )
    features.sort(key=lambda f: f["properties"]["v"] or 0)
    return {"type": "FeatureCollection", "features": features}


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=GVP_WFS, help="GVP WFS GeoJSON URL.")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    resp = requests.get(args.source, timeout=120)
    resp.raise_for_status()
    trimmed = trim_volcanoes(resp.json())
    args.output.write_text(json.dumps(trimmed, separators=(",", ":")) + "\n")
    print(f"Wrote {args.output}: {len(trimmed['features'])} volcanoes")


if __name__ == "__main__":
    main()
