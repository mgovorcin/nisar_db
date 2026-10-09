"""The viewer helper's routes, served by the API so the page works unchanged.

``/``, ``/view/<id>``, ``/health``, ``/build``, ``/login``, ``/logout``,
``/qa/<gid>/...`` and ``/corners/<gid>.json`` keep the paths and payloads of
``scripts/qa_browse_server.py``, whose classes do the work: the page calls
them on its own origin. In shared mode the page login is off (the server's
``~/.netrc`` is used), rebuilds need the ``jobs`` scope, and the Earthdata-
backed QA images and corners are rate limited.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from nisar_db.api.security import JOBS, LIMITED_READ, READ, Caller
from nisar_db.api.settings import Settings
from nisar_db.api.viewer import page_response

router = APIRouter(tags=["viewer helper"])


def load_scripts(settings: Settings) -> ModuleType:
    """Import ``qa_browse_server`` from the checkout's ``scripts/`` folder.

    Raises
    ------
    RuntimeError
        When the API runs outside a checkout (an installed package has no
        ``scripts/``); the catalog and job routes still work then.

    """
    if settings.repo_dir is None:
        raise RuntimeError("the viewer helper needs a nisar_db checkout (--repo-dir)")
    scripts = str(Path(settings.repo_dir) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("qa_browse_server")


class Helper:
    """The QA cache, viewer page and rebuilds, built once per app."""

    def __init__(self, settings: Settings) -> None:
        """Load the helper's classes from the checkout and set them up."""
        qa = load_scripts(settings)
        self.qa = qa
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        auth = qa.EarthdataAuth()
        self.cache = qa.QaCache(settings.cache_dir, auth)
        source = settings.viewer_html or default_viewer(settings) or qa.PUBLISHED_VIEWER
        self.viewer = qa.ViewerPage(str(source))
        self.builds = qa.ViewBuilds(settings.cache_dir, settings.trackframe_gpkg)


def default_viewer(settings: Settings) -> Path | None:
    """Return the checkout's built viewer page, if there is one."""
    if settings.repo_dir is None:
        return None
    page = Path(settings.repo_dir) / "scripts" / "opera_nisar_db_viewer.html"
    return page if page.exists() else None


def helper(request: Request) -> Helper:
    """Return the app's helper, or 503 when the checkout is missing."""
    h = request.app.state.helper
    if h is None:
        raise HTTPException(
            503, request.app.state.helper_error or "viewer helper unavailable"
        )
    return h


def _error(exc: Exception) -> JSONResponse:
    # Same shape as the old helper: the page shows ``error``. URLs can carry
    # signed query strings, so only the part before ``?`` is reported.
    if isinstance(exc, PermissionError):
        return JSONResponse({"error": str(exc)}, status_code=401)
    return JSONResponse(
        {"error": f"{type(exc).__name__}: {str(exc).split('?', 1)[0]}"}, status_code=502
    )


@router.get("/", include_in_schema=False)
@router.get("/index.html", include_in_schema=False)
def index(
    request: Request, h: Helper = Depends(helper), _: Caller = Depends(READ)
) -> Response:
    """Serve the viewer page (the published one, or ``--viewer-html``)."""
    try:
        body = h.viewer.html()
    except (OSError, requests.RequestException) as exc:
        return _error(exc)
    return page_response(request, body, h.viewer.etag)


@router.get("/view/{view_id}", include_in_schema=False)
def view(
    view_id: str,
    request: Request,
    h: Helper = Depends(helper),
    _: Caller = Depends(READ),
) -> Response:
    """Serve a page a rebuild wrote."""
    body = h.builds.page(view_id)
    if body is None:
        return JSONResponse({"error": "no such view"}, status_code=404)
    return page_response(request, body, f'"{view_id}"')


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    """Liveness, the mode, and where the Earthdata login comes from."""
    settings: Settings = request.app.state.settings
    h = request.app.state.helper
    return {
        "ok": True,
        "service": "nisar_db api",
        "mode": settings.mode,
        "auth": h.cache.auth.source if h is not None else "none",
        "page_login": settings.page_login,
    }


@router.get("/build")
def build_status(h: Helper = Depends(helper), _: Caller = Depends(READ)) -> dict:
    """Report the current (or last) rebuild."""
    return h.builds.status()


@router.post("/build")
def build_start(
    body: dict = Body(default_factory=dict),
    h: Helper = Depends(helper),
    _: Caller = Depends(JOBS),
) -> Response:
    """Search CMR and rebuild the viewer for ``na``, ``globe`` or a ``bbox``."""
    try:
        status = h.builds.start(
            str(body.get("scope", "na")),
            body.get("bbox"),
            collect_flags=bool(body.get("flags")),
            collect_qa=bool(body.get("qa")),
            land_only=bool(body.get("land")),
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(status)


@router.post("/login")
def login(
    request: Request,
    body: dict = Body(default_factory=dict),
    h: Helper = Depends(helper),
) -> Response:
    """Hand the helper an Earthdata login (local mode only)."""
    if not request.app.state.settings.page_login:
        return JSONResponse(
            {
                "error": (
                    "this shared service uses its own Earthdata login; "
                    "the page login is off"
                )
            },
            status_code=403,
        )
    username, password = str(body.get("username") or ""), str(
        body.get("password") or ""
    )
    if not username or not password:
        return JSONResponse({"error": "send username and password"}, status_code=400)
    try:
        h.cache.auth.login(username, password)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except requests.RequestException as exc:
        return JSONResponse(
            {"error": f"could not reach Earthdata: {type(exc).__name__}"},
            status_code=502,
        )
    # Never echo or log the password.
    return JSONResponse({"ok": True, "auth": h.cache.auth.source})


@router.post("/logout")
def logout(request: Request, h: Helper = Depends(helper)) -> Response:
    """Forget the page login and fall back to ``~/.netrc``."""
    if not request.app.state.settings.page_login:
        return JSONResponse(
            {"error": "the page login is off on a shared service"}, status_code=403
        )
    h.cache.auth.logout()
    return JSONResponse({"ok": True, "auth": h.cache.auth.source})


@router.get("/qa/{gid}/{leaf}")
def qa_layer(
    gid: str,
    leaf: str,
    thumb: int = 0,
    h: Helper = Depends(helper),
    _: Caller = Depends(LIMITED_READ),
) -> Response:
    """Serve a QA report's layers: ``index.json``, or one ``<layer>.png``."""
    if not h.qa.GID.match(gid):
        return JSONResponse({"error": "bad granule id"}, status_code=400)
    try:
        names = h.cache.layers(gid)
    except Exception as exc:
        return _error(exc)
    if leaf == "index.json":
        return JSONResponse(
            {"layers": [{"name": n, "label": h.qa.LAYER_LABELS[n]} for n in names]}
        )
    name = leaf.removesuffix(".png")
    if name not in names:
        return JSONResponse({"error": f"no layer {name}"}, status_code=404)
    body = (
        h.cache.root / gid / f"{name}{'_thumb' if thumb == 1 else ''}.png"
    ).read_bytes()
    return Response(
        body, media_type="image/png", headers={"Cache-Control": "max-age=86400"}
    )


@router.get("/corners/{name}")
def corners(
    name: str, h: Helper = Depends(helper), _: Caller = Depends(LIMITED_READ)
) -> Response:
    """Return a granule's grid corners and lon/lat bounding box."""
    gid = name.removesuffix(".json")
    if not h.qa.GID.match(gid):
        return JSONResponse({"error": "bad granule id"}, status_code=400)
    try:
        return JSONResponse(h.cache.corners(gid))
    except Exception as exc:
        return _error(exc)
