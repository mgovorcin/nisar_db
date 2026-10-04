"""Unit tests for the volcano list the frame viewer embeds."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trim_keeps_the_shown_fields_and_drops_non_points() -> None:
    gvp = {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [-121.75812, 46.85299]},
                "properties": {
                    "Volcano_Number": 321030,
                    "Volcano_Name": "Rainier",
                    "Primary_Volcano_Type": "Stratovolcano",
                    "Last_Eruption_Year": 1450,
                    "Elevation": 4392,
                    "Country": "United States",
                    "Region": "Canada and Western USA",
                    "Evidence_Category": "Eruption Dated",
                    "Geological_Summary": "a long text the viewer does not carry",
                },
            },
            {"geometry": None, "properties": {"Volcano_Number": 1}},
            {
                "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                "properties": {"Volcano_Number": 2, "Last_Eruption_Year": None},
            },
        ]
    }

    out = _load("make_volcano_list").trim_volcanoes(gvp)["features"]

    assert [f["properties"]["v"] for f in out] == [2, 321030]
    rainier = out[1]
    assert rainier["geometry"]["coordinates"] == [-121.7581, 46.853]
    assert rainier["properties"] == {
        "v": 321030,
        "n": "Rainier",
        "t": "Stratovolcano",
        "y": 1450,
        "e": 4392,
        "c": "United States",
        "r": "Canada and Western USA",
        "ev": "Eruption Dated",
    }
    assert out[0]["properties"]["y"] is None


def test_every_page_embeds_the_shipped_volcano_list() -> None:
    gen = _load("generate_scope_viewer")
    shipped = gen.load_volcanoes()
    assert len(shipped["features"]) > 1000

    html = gen.render_html(
        {"type": "FeatureCollection", "features": []}, {"title": "viewer"}
    )

    start = html.index("const VOLCANO_DATA = ") + len("const VOLCANO_DATA = ")
    embedded, _ = json.JSONDecoder().raw_decode(html, start)
    assert len(embedded["features"]) == len(shipped["features"])
