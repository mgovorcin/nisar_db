# Background: CMR drops granules from UMM-JSON

Since October 2026, NASA's Common Metadata Repository (CMR) leaves some
granules out of `granules.umm_json` search results while still counting them
in the `CMR-Hits` header. The same search in atom-JSON (`granules.json`)
returns every granule. The bug is on the CMR side and also affects other ASF
collections, such as `SENTINEL-1_BURSTS` (`C2709161906-ASF`), which is how
Vertex comes to show existing bursts as missing.

nisar_db works around it: every UMM-JSON search is checked against the
atom-JSON listing, and the missing records are fetched by concept id.

## Two ways a granule goes missing

| Failure | What UMM-JSON does | What still returns it |
|---|---|---|
| Paging skip | Leaves the granule out of a paged search, but returns it when asked for its concept id | UMM-JSON with `concept_id[]` |
| Never returned | Counts the granule in `CMR-Hits` but returns zero records, however it is asked | `search/concepts/<id>` (native UMM-G), atom-JSON |

`echo10` is not a reliable fallback: in a batch search it returned only 403 of
3,607 never-returned NISAR granules. The concepts endpoint returned a complete
record for every one of a 200-granule sample.

## Scope on 2026-10-21

Every page of each collection was walked in both formats and the concept ids
compared.

| Collection | Atom-JSON | UMM-JSON | Dropped | Of which never returned |
|---|---|---|---|---|
| `NISAR_L2_GUNW_PROVISIONAL_V1` | 89,826 | 83,802 | 6,024 | 3,603 |
| `NISAR_L2_GSLC_PROVISIONAL_V1` | 155,847 | 155,655 | 197 | 4 |
| `NISAR_L2_GSLC_BETA_V1` | 23,450 | 23,450 | 0 | 0 |

The dropped GUNWs are mostly composite release `P05023`. Of the 602 dropped
GUNWs already in the viewer's flag cache, 597 had an orbit type from an
earlier CMR lookup, so the drop appears recent.

## What nisar_db does about it

| Path | Endpoint | Status |
|---|---|---|
| Catalog builders (`create_gslc_catalog`, `create_gunw_catalog`) | `granules.json` | Never affected |
| `nisar-db search`, `search_nisar_products`, the weekly viewer's GSLC list, local search-and-rebuild | `granules.umm_json` | Recovered by `fetch_umm_granules` |
| QA orbit types (`scripts/collect_granule_qa.py`) | `granules.umm_json` by `granule_ur[]` | Recovered by `fetch_umm_by_granule_ur`; cached flags missing an orbit type are backfilled on the next run |

The recovery in [`search/cmr.py`](https://github.com/opera-adt/nisar_db/blob/main/src/nisar_db/search/cmr.py) runs in three steps:

1. Walk the search in UMM-JSON and in atom-JSON, and diff the concept ids.
2. Ask UMM-JSON for the missing ids, 100 per POST. This recovers the paging
   skips.
3. Fetch whatever is still missing from `search/concepts/<id>`, one granule
   per request, with 4 workers. This recovers the never-returned granules.

The page walk also no longer ends at a short page while `CMR-Hits` says more
remain, and it warns whenever it comes up short of `CMR-Hits`. Before this
change, a page shortened by drops ended the walk and lost every later page.

## Runtime cost

Full-archive `search_nisar_products` runs on 2026-10-21, with 4 workers. Each
was run once, so expect network variance.

| Search | Before (est.) | After | Atom-JSON listing | Re-ask by id | Concepts endpoint |
|---|---|---|---|---|---|
| Full GUNW archive | ~1.5 min | 5.1 min | 44 s | 21 s | ~2.8 min (3,603 granules) |
| Full GSLC archive | ~2.7 min | 4.1 min | 80 s | 1 s | 4 granules |

"Before" is the UMM-JSON walk plus parsing from the same run, which is all the
old code did. In practice:

- The weekly viewer build runs only the full GSLC search, so it takes about
  1.5 minutes longer.
- A local search-and-rebuild searches one bounding box, so its extra cost is
  small.
- The atom-JSON listing is paid on every search, even when nothing is
  dropped. The concepts-endpoint cost shrinks as CMR fixes the bug.

Two options would cut the cost if it matters: run the atom-JSON listing at the
same time as the UMM-JSON walk, or raise the concepts-endpoint workers, which
CMR may rate-limit.

## How to check one granule

A dropped granule shows one hit and no records in UMM-JSON, and comes back
from the concepts endpoint:

```bash
id=G4245361810-ASF   # a dropped P05023 GUNW
curl -s "https://cmr.earthdata.nasa.gov/search/granules.umm_json?concept_id=$id"
# {"hits":1,"took":34,"items":[]}
curl -s "https://cmr.earthdata.nasa.gov/search/granules.json?concept_id=$id" | head -c 200
curl -s "https://cmr.earthdata.nasa.gov/search/concepts/$id" | head -c 200
```

## When to remove the workaround

Keep it after CMR fixes the bug. When nothing is missing, it costs only the
atom-JSON listing, and it catches the bug if it comes back. The warning
`CMR UMM-JSON left out N of M granules` in a search's log shows whether the
bug is still present.
