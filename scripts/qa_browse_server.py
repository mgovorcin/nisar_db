#!/usr/bin/env python
"""Serve NISAR QA report images to the frame viewer from this machine.

The public browse PNG of a GUNW shows only its unwrapped phase. The wrapped
phase, coherence, connected components and ionosphere screen are drawn only in
the product's ``_QA_REPORT.pdf``, which sits behind the Earthdata login, and
ASF's download endpoint does not let a web page send that login. This helper
does it instead: when the viewer asks for a granule, it downloads the report
(~400 kB) with the credentials in ``~/.netrc`` (machine
``urs.earthdata.nasa.gov``), pulls the raster images out of it, and caches them
on disk. Nothing is hosted; each image is fetched once, on request.

It also reads the grid corners of the product (a few small byte-range reads),
so the viewer can place the images, and the public ``_LATLON`` browse, on the
map.

Endpoints (all JSON or PNG, with permissive CORS so the published viewer can
call it):

* ``/health`` -- liveness check;
* ``/qa/<gid>/index.json`` -- the layers extracted from the report;
* ``/qa/<gid>/<layer>.png`` -- one layer, ``?thumb=1`` for a small copy;
* ``/corners/<gid>.json`` -- the grid's corners and lon/lat bounding box.

Examples
--------
Start it, then open the viewer (local or published) in Chrome, Edge or Firefox::

    python scripts/qa_browse_server.py --cache-dir ~/.cache/nisar_db/qa_browse

"""

from __future__ import annotations

import argparse
import io
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from collect_granule_flags import BLOCK_SIZE, COLLECTIONS, _session, product_url

if TYPE_CHECKING:
    from PIL import Image

REPORT_URL = (
    "https://nisar.asf.earthdatacloud.nasa.gov/NISAR/{collection}/{gid}/"
    "{gid}_QA_REPORT.pdf"
)
GID = re.compile(r"^NISAR_L2_PR_(GSLC|GUNW)_[A-Za-z0-9_]+$")
# Report pages are found by their title, and their rasters taken in drawing
# order; colourbars and other small images are skipped. Most specific title
# first: the wrapped group's page also mentions coherence.
LAYER_PAGES = [
    ("Wrapped Phase Image Group", ["wrapped", "coherence_wrapped"]),
    ("Ionosphere Phase Screen", ["iono", "iono_unc"]),
    ("Connected Components", ["cc"]),
    ("Coherence Magnitude (Unwrapped Group)", ["coherence"]),
    ("Unwrapped Phase Image", ["unwrapped", "rewrapped"]),
]
LAYER_LABELS = {
    "wrapped": "Wrapped phase",
    "coherence_wrapped": "Coherence (wrapped group)",
    "coherence": "Coherence",
    "cc": "Connected components",
    "unwrapped": "Unwrapped phase",
    "rewrapped": "Unwrapped, rewrapped",
    "iono": "Ionosphere screen",
    "iono_unc": "Ionosphere uncertainty",
}
MIN_SIDE = 100
# The connected-component mask is drawn at full resolution; this is plenty to
# show which parts unwrapped.
MAX_SIDE = 720
THUMB_SIDE = 128
VALID_CC_COLOR = (77, 210, 201, 255)


def report_url(gid: str) -> str:
    """Return the HTTPS URL of a granule's QA report."""
    kind = gid.split("_")[3]
    return REPORT_URL.format(collection=COLLECTIONS[kind], gid=gid)


def _cc_mask(image: Image.Image) -> Image.Image:
    # The mask has two colours: background (no unwrapped data, inside the swath
    # or not) and valid. The corner pixel is always outside the swath.
    from PIL import Image

    rgb = image.convert("RGB")
    rgb.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.NEAREST)
    background = rgb.getpixel((0, 0))
    out = Image.new("RGBA", rgb.size, (0, 0, 0, 0))
    out.putdata(
        [(0, 0, 0, 0) if px == background else VALID_CC_COLOR for px in rgb.getdata()]
    )
    return out


def extract_layers(pdf: bytes) -> dict[str, Image.Image]:
    """Pull the raster layers out of a GUNW QA report.

    Parameters
    ----------
    pdf : bytes
        The ``_QA_REPORT.pdf`` contents.

    Returns
    -------
    dict
        Layer name (see ``LAYER_LABELS``) to an RGBA image covering the
        product's grid; layers the report does not draw are absent.

    """
    import numpy as np
    import pypdf
    from PIL import Image
    from pypdf.generic import ContentStream

    reader = pypdf.PdfReader(io.BytesIO(pdf))
    layers: dict[str, Image.Image] = {}
    for page in reader.pages:
        text = page.extract_text() or ""
        names = next((n for title, n in LAYER_PAGES if title in text), None)
        if names is None or names[0] in layers:
            continue
        # Every page shares one resource dictionary, so the page's own drawing
        # operations say which images are on it. Each is drawn through the
        # matrix set just before it, and the QA rasters are stored bottom row
        # first and flipped back by a negative height there.
        images = []
        matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        ops = ContentStream(page.get_contents(), reader).operations
        for operands, op in ops:
            if op == b"cm":
                matrix = [float(v) for v in operands]
            elif op == b"Do" and str(operands[0]) in page.images.keys():
                image = page.images[str(operands[0])].image
                if min(image.size) < MIN_SIDE:
                    continue
                if matrix[3] < 0:
                    image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                if matrix[0] < 0:
                    image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                images.append(image)
        for name, image in zip(names, images, strict=False):
            layers[name] = _cc_mask(image) if name == "cc" else image.convert("RGBA")
    # The mask's plot paints the grid's fill value like a valid component, as a
    # band along the grid edge outside the swath; the coherence image, drawn on
    # the same grid, is transparent there.
    if "cc" in layers and "coherence" in layers:
        swath = (
            layers["coherence"]
            .getchannel("A")
            .resize(layers["cc"].size, Image.Resampling.NEAREST)
        )
        alpha = np.minimum(np.asarray(layers["cc"].getchannel("A")), np.asarray(swath))
        layers["cc"].putalpha(Image.fromarray(alpha))
    return layers


def grid_corners(gid: str) -> dict:
    """Read a product's grid corners from its HDF5 file.

    Parameters
    ----------
    gid : str
        GSLC or GUNW granule id.

    Returns
    -------
    dict
        ``epsg``; ``quad``, the grid's outer corners as ``[lon, lat]`` in the
        order upper-left, upper-right, lower-right, lower-left (how the QA
        images, which are drawn on the grid, are placed); and ``bbox``,
        ``[west, south, east, north]`` of the whole grid in lon / lat (how the
        public ``_LATLON`` browse, the grid reprojected to EPSG:4326, is).

    """
    import fsspec
    import h5py
    import numpy as np
    from pyproj import Transformer

    signed = (
        _session()
        .get(product_url(gid), headers={"Range": "bytes=0-0"}, timeout=120)
        .url
    )
    with (
        fsspec.filesystem("http").open(
            signed, block_size=BLOCK_SIZE, cache_type="blockcache"
        ) as fo,
        h5py.File(fo) as h5,
    ):
        if gid.split("_")[3] == "GUNW":
            unw = h5["science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram"]
            grid = unw[sorted(k for k in unw if isinstance(unw[k], h5py.Group))[0]]
        else:
            grids = h5["science/LSAR/GSLC/grids"]
            grid = grids[sorted(k for k in grids if k.startswith("frequency"))[0]]
        x = grid["xCoordinates"][()]
        y = grid["yCoordinates"][()]
        dx = float(grid["xCoordinateSpacing"][()])
        dy = float(grid["yCoordinateSpacing"][()])
        epsg = int(grid["projection"][()])
    # Coordinates are pixel centres; the image covers the pixels' outer edges.
    left, right = x[0] - dx / 2, x[-1] + dx / 2
    top, bottom = y[0] - dy / 2, y[-1] + dy / 2
    to_lonlat = Transformer.from_crs(epsg, 4326, always_xy=True)
    quad = [
        list(to_lonlat.transform(cx, cy))
        for cx, cy in ((left, top), (right, top), (right, bottom), (left, bottom))
    ]
    # The reprojected browse spans the lon / lat bounds of the whole grid edge,
    # which bow outwards between the corners.
    t = np.linspace(0.0, 1.0, 64)
    edge_x = np.concatenate(
        [
            left + (right - left) * t,
            np.full(64, right),
            right - (right - left) * t,
            np.full(64, left),
        ]
    )
    edge_y = np.concatenate(
        [
            np.full(64, top),
            top + (bottom - top) * t,
            np.full(64, bottom),
            bottom - (bottom - top) * t,
        ]
    )
    lon, lat = to_lonlat.transform(edge_x, edge_y)
    bbox = [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())]
    return {"epsg": epsg, "quad": quad, "bbox": bbox}


class QaCache:
    """Disk cache of extracted layers and corners, one folder per granule."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        # Two thumbnails of one granule asked for at once must not download
        # the report twice.
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def layers(self, gid: str) -> list[str]:
        """Return the granule's layer names, extracting them on first use."""
        folder = self.root / gid
        index = folder / "index.json"
        with self._lock(f"layers:{gid}"):
            if not index.exists():
                names: list[str] = []
                if gid.split("_")[3] == "GUNW":
                    from PIL import Image

                    resp = _session().get(report_url(gid), timeout=120)
                    resp.raise_for_status()
                    folder.mkdir(parents=True, exist_ok=True)
                    for name, image in extract_layers(resp.content).items():
                        image.save(folder / f"{name}.png", optimize=True)
                        thumb = image.copy()
                        thumb.thumbnail(
                            (THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS
                        )
                        thumb.save(folder / f"{name}_thumb.png", optimize=True)
                        names.append(name)
                folder.mkdir(parents=True, exist_ok=True)
                index.write_text(json.dumps(names))
        return json.loads(index.read_text())

    def corners(self, gid: str) -> dict:
        """Return the granule's grid corners, reading them on first use."""
        path = self.root / gid / "corners.json"
        with self._lock(f"corners:{gid}"):
            if not path.exists():
                corners = grid_corners(gid)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(corners))
        return json.loads(path.read_text())


def make_handler(cache: QaCache) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to ``cache``."""

    class Handler(BaseHTTPRequestHandler):
        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Access-Control-Allow-Origin", "*")
            # Chrome asks before a public page may reach a local server.
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()

        def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode()
            self._headers(status, "application/json", len(body))
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802 - http.server naming
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            try:
                if parts == ["health"]:
                    self._json({"ok": True, "service": "nisar_db qa_browse_server"})
                elif len(parts) == 3 and parts[0] == "qa" and GID.match(parts[1]):
                    gid, leaf = parts[1], parts[2]
                    names = cache.layers(gid)
                    if leaf == "index.json":
                        self._json(
                            {
                                "layers": [
                                    {"name": n, "label": LAYER_LABELS[n]} for n in names
                                ]
                            }
                        )
                        return
                    name = leaf.removesuffix(".png")
                    if name not in names:
                        self._json({"error": f"no layer {name}"}, HTTPStatus.NOT_FOUND)
                        return
                    thumb = parse_qs(url.query).get("thumb") == ["1"]
                    body = (
                        cache.root / gid / f"{name}{'_thumb' if thumb else ''}.png"
                    ).read_bytes()
                    self._headers(HTTPStatus.OK, "image/png", len(body))
                    self.wfile.write(body)
                elif len(parts) == 2 and parts[0] == "corners":
                    gid = parts[1].removesuffix(".json")
                    if not GID.match(gid):
                        self._json({"error": "bad granule id"}, HTTPStatus.BAD_REQUEST)
                        return
                    self._json(cache.corners(gid))
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:  # noqa: BLE001 - report it to the page
                self._json(
                    {"error": f"{type(exc).__name__}: {str(exc).split('?', 1)[0]}"},
                    HTTPStatus.BAD_GATEWAY,
                )

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            print(f"{self.address_string()} {format % args}", flush=True)

    return Handler


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8797, help="Port to listen on.")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache" / "nisar_db" / "qa_browse",
        help="Where extracted images and corners are kept.",
    )
    args = parser.parse_args(argv)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(QaCache(args.cache_dir))
    )
    print(f"Serving QA images on http://127.0.0.1:{args.port} (cache {args.cache_dir})")
    server.serve_forever()


if __name__ == "__main__":
    main()
