---
name: prep-disp-nisar-run
description: >
  Prepare data and configs for a disp-nisar processing run: resolve `frame_id`
  from track/frame, stage DEM and water-mask ancillaries, and generate the
  algorithm-parameters + runconfig YAMLs for a GSLC stack. Use when the user
  wants to set up, stage inputs for, or generate a runconfig for a disp-nisar
  run given a directory of NISAR GSLC `.h5` files.
---

# Prepare a disp-nisar run

Turns a directory of NISAR GSLC `.h5` files (e.g. `data/T062_F017/`) into a
validated disp-nisar runconfig, by chaining scripts that already exist in the
`disp-nisar` working project (`disp-nisar/scripts/`, not this repo). This
skill documents the pipeline and the environment quirks required to run it;
it does not itself invoke a CLI.

## Prerequisites: environment

The Whirlwind checkouts of `disp_nisar` / `opera_utils` / `dolphin` / `nisar_db`
are usually **ahead of** whatever is `pip install -e`'d into any given conda
env (envs get pointed at other checkouts of the same repos). Never trust
`which disp-nisar` alone. Before running anything, put the Whirlwind repos
first on `PYTHONPATH` in whichever env has the compiled deps (GDAL, h5py,
geopandas, isce3, dolphin's C extensions, etc — `my-disp-nisar-env` is the
known-good one on this machine):

```bash
source <conda>/etc/profile.d/conda.sh
conda activate my-disp-nisar-env
WW=/u/aurora-r0/govorcin/03_DISP_NI/Whirlwind
export PYTHONPATH="$WW/code/disp-nisar/src:$WW/code/opera-utils/src:$WW/code/dolphin/src:$WW/code/nisar_db/src"
```

Confirm the imports actually resolve to the Whirlwind paths (not some other
checkout) before doing real work:

```bash
python3 -c "import disp_nisar, opera_utils, dolphin, nisar_db; \
  print(disp_nisar.__file__, opera_utils.__file__, dolphin.__file__, nisar_db.__file__)"
```

If `opera_utils` fails with `ModuleNotFoundError: No module named 'opera_utils._version'`,
that repo's `_version.py` (a gitignored setuptools-scm build artifact) is
missing from the checkout — write a trivial stub (see any other repo's
`_version.py` for the shape) rather than trying to build/install the package.

The staging scripts under `disp-nisar/scripts/` insert the Whirlwind `src/`
dirs onto `sys.path` themselves (so they're runnable without the `export`
above), but tools invoked via their installed CLI entry points (e.g.
`disp-nisar make-runconfig`) still resolve through whatever env is active —
keep `PYTHONPATH` set for those.

## Pipeline

Run from a project directory (referred to below as `disp-nisar/`, sibling to
`code/` and `data/`) holding `INPUTS.md`, `scripts/`, `ancillary/`, `config/`.

### 1. Resolve `frame_id`

The disp-nisar `frame_id` (the key in `frame_to_bounds.json`) is **not** the
NISAR frame number in a directory name like `T062_F017` — it's the row index
of the source NISAR TrackFrame GeoPackage, which has no relation to track/frame
except through that table. Resolve it with:

```bash
python3 disp-nisar/scripts/get_frame_id.py --track <track> --frame <frame> \
    [--pass-direction Ascending|Descending]   # default Ascending
```

This downloads (and caches under `disp-nisar/ancillary/trackframe_db/`) the
public NISAR TrackFrame GeoPackage via `nisar_db.geodb.get_trackframe_db`
(needs Earthdata Login in `~/.netrc`) and looks up the row directly — no GSLC
file needed. It cross-checks the result against `frame_to_bounds.json` and
prints the resolved `frame_id` to stdout.

### 2. Stage DEM + water mask

```bash
python3 disp-nisar/scripts/stage_ancillary.py \
    --frame-id <frame_id> \
    --gslc-dir data/T<track>_F<frame>/ \
    --output-dir disp-nisar/ancillary
```

Produces `ancillary/dem.vrt` (+ tile `.tif`, EPSG:4326, full-frame bounds with
margin) and `ancillary/water_mask.tif` (binary, warped to the GSLC's native
UTM grid). Needs AWS SSO credentials for the `saml-pub` profile
(`opera-dem` / `opera-water-mask` S3 buckets) — check with
`aws sts get-caller-identity --profile saml-pub` first; if that's expired,
tell the user to re-authenticate rather than trying to work around it.

Troposphere staging is **not** included — `troposphere_files` is optional in
the runconfig; only add it if the user explicitly asks for tropospheric
correction and provide/point to weather-model files yourself.

### 3. Generate algorithm parameters + runconfig

```bash
disp-nisar make-runconfig \
    data/T<track>_F<frame>/*.h5 \
    --frame-id <frame_id> \
    --output-dir disp-nisar \
    --mode historical \
    --mask-file disp-nisar/ancillary/water_mask.tif \
    --dem-file disp-nisar/ancillary/dem.vrt \
    --frame-to-bounds-json <path-to-frame_to_bounds.json>
```

Pick `--mode historical` for a full backfill over an existing GSLC stack (many
dates at once); `forward` for incremental/catch-up processing. This CLI
doesn't currently wire up `ionosphere_algorithm_parameters_file` or
`azimuth_blocks`, so patch the generated runconfig YAML by hand afterward:

- `azimuth_blocks: 8` — NISAR GSLCs aren't OPERA-burst-shaped, so this splits
  each into synthetic bursts (the CLI/pydantic default is `1`, i.e. no split;
  the shipped sample runconfigs in `code/disp-nisar/configs/` use `8`).
- `ionosphere_algorithm_parameters_file: <path>` — required in `historical`
  mode; point it at `code/disp-nisar/configs/algorithm_parameters_ionosphere_historical.yaml`
  (or the user's equivalent).

### 4. Validate

Always validate before telling the user it's ready — this actually checks the
`frame_id` against the GSLCs' real bounds/EPSG (tolerance ~1000 m), not just
YAML syntax:

```bash
python3 -c "
from disp_nisar.pge_runconfig import RunConfig
rc = RunConfig.from_yaml('<runconfig.yaml>')
rc.to_workflow()   # raises on frame_id/EPSG/bounds mismatch
print('OK')
"
```

## Known gaps this pipeline does not fill

- **`gunw_files`** — there is no staging step for it here, so it's left `[]`.
  This is *not* a hard blocker: `disp_nisar.main` handles an empty list by
  running a full second `frequencyB` displacement workflow and doing
  split-spectrum ionosphere estimation instead (configured via
  `ionosphere_algorithm_parameters_file`) — but only if the GSLCs actually
  have `frequencyB` grids (check with `h5py`/`h5ls` before assuming it'll
  work). It's computationally heavier than GUNW-based correction (2x
  phase-linking) and loses the SET/static-geometry layers GUNW would provide.
  Tell the user which path they're getting and let them choose, rather than
  silently picking one.
- **`reference_date_database_json`** — the repo only ships a dummy
  (`opera-disp-nisar-reference-dates-dummy.json`, referenced in sample configs
  but not present in `static_ancillary_files/`). Ask the user whether the
  dummy is acceptable or a real one is needed before finalizing a run meant
  for actual product generation.

## Known upstream bugs fixed along the way (check they're still fixed)

- `disp_nisar/_dem.py::download_dem()` used to build a DEM VRT referencing
  tile GeoTIFFs, then delete those same tiles — producing a broken VRT. Fixed
  to keep the tiles (matching how `_water.py`'s water-mask VRT already does
  it). If DEM staging ever produces a VRT that `gdalinfo` can't open (missing
  referenced files), check this hasn't regressed.
- `opera_utils`'s NISAR frame-to-bounds fetcher
  (`opera_utils.nisar.fetch_nisar_frame_to_bounds_file`) used to point at a
  broken GitHub `tree/` URL (an HTML page, not raw file content) with a stale
  checksum. It now pulls from the real release asset:
  `https://github.com/opera-adt/nisar_db/releases/download/v0.1.0/opera-nisar-disp-0.1.0-frame-to-bounds.json`.
