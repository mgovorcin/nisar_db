"""``/api/v1``: catalog queries over the viewer's datasets, and jobs."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError

from nisar_db.api import store
from nisar_db.api.jobs import KINDS, Jobs
from nisar_db.api.security import COOKIE, JOBS, READ, Caller, match_key
from nisar_db.api.settings import Settings

router = APIRouter(prefix="/api/v1")

# ---------------------------------------------------------------------------
# Datasets and frames
# ---------------------------------------------------------------------------


def dataset(
    request: Request,
    dataset: str = Query("published", description="'published' or a rebuilt view id"),
) -> store.Dataset:
    """Resolve the ``dataset`` query parameter."""
    try:
        return request.app.state.store.get(dataset)
    except KeyError as exc:
        raise HTTPException(
            404, f"no dataset {dataset!r}; see /api/v1/datasets"
        ) from exc


def _csv(text: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (text or "").split(",") if v.strip())


def frame_query(
    product: Literal["gslc", "gunw"] = "gslc",
    track: str | None = Query(None, description="e.g. 12, 10-20 or 12,34"),
    frame: str | None = Query(None, description="same syntax as track"),
    direction: Literal["A", "D", "asc", "desc"] | None = None,
    bbox: str | None = Query(None, description="west,south,east,north"),
    cycle: str | None = Query(
        None, description="e.g. 23 or 20-25 (a GUNW pair matches on either date)"
    ),
    start: date | None = None,
    end: date | None = None,
    modes: str | None = Query(
        None, description="comma-separated modes, e.g. 2005,4005"
    ),
    pols: str | None = Query(None, description="comma-separated polarizations"),
    crids: str | None = None,
    has_data: bool | None = Query(
        None,
        description="only frames with (true) / without (false) granules or pairs left",
    ),
    calval: bool | None = None,
    land: bool | None = None,
    rollout: str | None = Query(
        None, description="comma-separated rollout options, 'none' for none"
    ),
    consistent_mode: str | None = None,
) -> store.FrameQuery:
    """Collect the frame filters from the query string."""
    box = None
    if bbox:
        try:
            w, s, e, n = (float(v) for v in bbox.split(","))
        except ValueError as exc:
            raise HTTPException(400, "bbox is west,south,east,north") from exc
        box = (w, s, e, n)
    for name, text in (("track", track), ("frame", frame), ("cycle", cycle)):
        try:
            store.int_set(text)
        except ValueError as exc:
            raise HTTPException(
                400, f"{name}: use numbers, ranges (10-20) and commas"
            ) from exc
    return store.FrameQuery(
        product=product,
        track=track,
        frame=frame,
        direction=direction[0].upper() if direction else None,
        bbox=box,
        cycle=cycle,
        start=start,
        end=end,
        modes=_csv(modes),
        pols=_csv(pols),
        crids=_csv(crids),
        has_data=has_data,
        calval=calval,
        land=land,
        rollout=_csv(rollout),
        consistent_mode=consistent_mode,
    )


@router.get("/datasets", tags=["catalog"])
def datasets(request: Request, _: Caller = Depends(READ)) -> list[dict]:
    """List the datasets: the published page and every rebuilt view."""
    st = request.app.state.store
    return [st.header(ds_id) for ds_id in st.paths()]


@router.get("/datasets/{dataset_id}", tags=["catalog"])
def dataset_info(dataset_id: str, request: Request, _: Caller = Depends(READ)) -> dict:
    """One dataset's header (parses it if needed)."""
    return dataset(request, dataset_id).summary()


@router.get("/frames", tags=["catalog"])
def frames(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    format: Literal["json", "geojson"] = "json",  # noqa: A002
    limit: int = Query(500, ge=1, le=50000),
    offset: int = Query(0, ge=0),
    _: Caller = Depends(READ),
) -> Any:
    """Frames passing the filters, as summaries (or GeoJSON with geometry).

    ``n_selected`` counts each frame's granules (GSLC) or pairs (GUNW) left by
    the cycle / date / mode / polarization / CRID filters.
    """
    sel = store.select(ds, q)
    page = sel[offset : offset + limit]
    if format == "geojson":
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": f["geometry"],
                    "properties": store.frame_summary(f, q),
                }
                for f in page
            ],
            "total": len(sel),
        }
    return {
        "total": len(sel),
        "offset": offset,
        "frames": [store.frame_summary(f, q) for f in page],
    }


def _frame(ds: store.Dataset, key: str) -> dict:
    f = ds.frame(key)
    if f is None:
        raise HTTPException(404, f"no frame {key!r} in {ds.id}")
    return f


@router.get("/frames/{key}", tags=["catalog"])
def frame(
    key: str,
    ds: store.Dataset = Depends(dataset),
    geometry: bool = False,
    _: Caller = Depends(READ),
) -> dict:
    """One frame by id (``8109``) or track_frame (``34_19``, ``T34_F19``)."""
    f = _frame(ds, key)
    out = store.frame_summary(f)
    if geometry:
        out["geometry"] = f["geometry"]
    return out


@router.get("/frames/{key}/granules", tags=["catalog"])
def granules(
    key: str,
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    _: Caller = Depends(READ),
) -> dict:
    """List a frame's GSLC granules or (``product=gunw``) GUNW pairs."""
    f = _frame(ds, key)
    entries = q.entries(f["properties"])
    return {
        "frame_idx": f["properties"]["frame_idx"],
        "product": q.product,
        "total": len(entries),
        "items": entries,
    }


@router.get("/frames/{key}/blackout", tags=["catalog"])
def frame_blackout(
    key: str, ds: store.Dataset = Depends(dataset), _: Caller = Depends(READ)
) -> dict:
    """Return a frame's blackout windows, monthly shares and reference dates."""
    return store.blackout(_frame(ds, key))


@router.get("/cycles", tags=["catalog"])
def cycles(
    ds: store.Dataset = Depends(dataset),
    product: Literal["gslc", "gunw"] = "gslc",
    _: Caller = Depends(READ),
) -> list[dict]:
    """Every cycle in the dataset with its date span and frame count."""
    return store.cycles(ds, product)


@router.get("/summary", tags=["catalog"])
def summary(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    _: Caller = Depends(READ),
) -> dict:
    """Summarise consistent modes and rollout options over the filtered frames."""
    sel = store.select(ds, q)
    return {
        "consistent": store.consistent_summary(sel),
        "rollout": store.rollout_summary(ds, sel),
    }


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def jobs(request: Request) -> Jobs:
    """Return the app's job runner."""
    return request.app.state.jobs


def _visible(caller: Caller, owner: str) -> bool:
    return caller.key is None or caller.allows("admin") or caller.name == owner


@router.get("/jobs/kinds", tags=["jobs"])
def job_kinds(request: Request, _: Caller = Depends(READ)) -> list[dict]:
    """Job kinds with their parameter schemas, and whether this service runs them."""
    refused = request.app.state.jobs.refuse
    return [
        {
            "kind": k.name,
            "summary": k.summary,
            "enabled": k.name not in refused,
            "inputs": list(k.inputs),
            "params": k.params.model_json_schema(),
        }
        for k in KINDS.values()
    ]


@router.post("/jobs", tags=["jobs"], status_code=202)
def submit(
    kind: str = Body(..., embed=True),
    params: dict = Body(default_factory=dict, embed=True),
    runner: Jobs = Depends(jobs),
    caller: Caller = Depends(JOBS),
) -> dict:
    """Start a ``nisar-db`` command; poll ``/jobs/{id}`` for its state."""
    try:
        return runner.submit(kind, params, caller.name).public()
    except KeyError as exc:
        raise HTTPException(
            404, f"no job kind {kind!r}; see /api/v1/jobs/kinds"
        ) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            422, exc.errors(include_url=False, include_context=False)
        ) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/jobs", tags=["jobs"])
def list_jobs(
    runner: Jobs = Depends(jobs), caller: Caller = Depends(JOBS)
) -> list[dict]:
    """Jobs, newest first (on a shared service, your own unless your key is admin)."""
    return [j.public() for j in runner.listed() if _visible(caller, j.owner)]


def _job(runner: Jobs, job_id: str, caller: Caller):
    job = runner.get(job_id)
    if job is None or not _visible(caller, job.owner):
        raise HTTPException(404, f"no job {job_id!r}")
    return job


@router.get("/jobs/{job_id}", tags=["jobs"])
def job(
    job_id: str, runner: Jobs = Depends(jobs), caller: Caller = Depends(JOBS)
) -> dict:
    """Return a job's record and the tail of its log."""
    j = _job(runner, job_id, caller)
    return {**j.public(), "log": runner.log_tail(job_id)}


@router.delete("/jobs/{job_id}", tags=["jobs"])
def cancel(
    job_id: str, runner: Jobs = Depends(jobs), caller: Caller = Depends(JOBS)
) -> dict:
    """Cancel a queued or running job."""
    _job(runner, job_id, caller)
    return runner.cancel(job_id).public()


@router.get("/jobs/{job_id}/files/{name:path}", tags=["jobs"])
def job_file(
    job_id: str,
    name: str,
    runner: Jobs = Depends(jobs),
    caller: Caller = Depends(JOBS),
) -> Response:
    """Download one of a job's outputs."""
    _job(runner, job_id, caller)
    try:
        path = runner.output_path(job_id, name)
    except KeyError as exc:
        raise HTTPException(404, f"job {job_id} has no output {name!r}") from exc
    return FileResponse(path, filename=path.name)


# ---------------------------------------------------------------------------
# Browser session (shared mode)
# ---------------------------------------------------------------------------


@router.post("/session", tags=["auth"])
def session(request: Request, key: str = Body(..., embed=True)) -> Response:
    """Trade an API key for an HttpOnly cookie (a browser opening a private viewer)."""
    settings: Settings = request.app.state.settings
    k = match_key(settings, key)
    if settings.shared and k is None:
        raise HTTPException(401, "unknown API key")
    resp = JSONResponse(
        {
            "ok": True,
            "name": k.name if k else "local",
            "scopes": sorted(k.scopes) if k else [],
        }
    )
    resp.set_cookie(
        COOKIE,
        key,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=7 * 86400,
    )
    return resp


@router.delete("/session", tags=["auth"])
def end_session() -> Response:
    """Forget the browser's key cookie."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp
