"""Unit tests for the frame viewer's basemap sources."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_scope_viewer.py"


def _app_js() -> str:
    spec = importlib.util.spec_from_file_location("generate_scope_viewer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.APP_JS)


def test_light_and_dark_basemaps_are_keyless_esri_canvases() -> None:
    js = _app_js()
    assert "cartocdn" not in js
    for service in ("World_Light_Gray", "World_Dark_Gray"):
        assert f"Canvas/{service}_Base/MapServer" in js
        assert f"Canvas/{service}_Reference/MapServer" in js


def test_no_tile_url_carries_an_api_key() -> None:
    tiles = re.findall(r'tiles:\["([^"]+)"', _app_js())
    assert tiles
    assert not [t for t in tiles if re.search(r"key=|token=|access_token", t, re.I)]


def test_every_basemap_layer_is_switchable() -> None:
    js = _app_js()
    layer_ids = set(re.findall(r'\{ id:"(bm-[a-z0-9-]+)"', js))
    switch = re.search(r"const BASEMAP_LAYERS = (\{.*?\});", js)
    assert switch is not None
    assert layer_ids == set(re.findall(r'"(bm-[a-z0-9-]+)"', switch.group(1)))
