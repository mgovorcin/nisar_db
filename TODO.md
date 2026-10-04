# TODO

## Granule flags

- **GUNW orbit type of both acquisitions.** The flags now come from each
  product's small `QA_STATS.h5` plus CMR (`scripts/collect_granule_qa.py`), and
  CMR's `ORBIT_TYPE` for a GUNW is only the reference acquisition's. The old
  byte-range reader (`collect_granule_flags.read_flags`) read
  `science/LSAR/GUNW/metadata/orbit/{reference,secondary}/orbitType` from the
  product and stored `REF/SEC` (e.g. `MOE/FOE`) when they differed, which
  happened for 6 of ~10,000 GUNW checked (2026-10-04). Reading both orbits
  back means one byte-range read per GUNW product (~5 granules/s, ~3.5 h for
  the ~62,000 GUNW in the archive), so it would be an opt-in pass, e.g. only
  for the frames on screen or run once into the shared caches. Neither the
  `QA_STATS.h5` nor the `.rc.yaml` sidecar records the orbit files.
