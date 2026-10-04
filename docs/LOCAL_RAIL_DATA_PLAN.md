# Local rail data — implementation plan

Tracking issue: **#345**. This plan covers acquiring the data, storing it,
keeping it current, changing the resolver to use it, and the tests at each step.

The measurements it rests on are in #345 and are not repeated here beyond what a
decision needs.

---

## The shape of the thing

Today `get_rail_geometry` asks Overpass three questions and Overpass answers them
over the network:

1. *What station is near this coordinate, and what is its UIC code?* (`_enrich_uic`)
2. *Which `route=train` relation covers both endpoints?* (strategies A and B)
3. *Give me every railway way in this bounding box.* (strategy C)

Only question 3 is expensive, and all three are lookups against a slowly-changing
dataset. **Overpass is a data source here, not an algorithm** — `_build_rail_graph`,
`_nearest_node` and `_dijkstra` are already ours and already run locally. So this
is a substitution of the source, not a rewrite of the resolver.

That framing sets the acceptance bar: **the strategies keep their current shape and
their current tests; only where the elements come from changes.**

---

## Phase 0 — Coverage *(DECIDED)*

**Europe, per-country files. Overpass remains the fallback outside Europe.**
Coverage widens gradually once this is proven in production.

That choice is deliberately conservative in both directions. Europe is where the
product's trips are, and per-country granularity is what keeps RAM bounded
(Phase 2) — Germany alone is 319 MB resident, so a single Europe-wide graph is
not an option. Keeping Overpass for the rest of the world means a user who
travels outside coverage still gets a route rather than an apology, and it
reduces the migration's blast radius to "the regions we hold".

The fallback is safe *because* of the volume maths that motivated this work: once
European segments resolve locally, Overpass sees a handful of requests a day
instead of several per resolve. That is comfortably inside fair use, so the
dependency stops being a structural ban risk even though it still exists.

**Consequences that bind later phases:**

- Region selection must be able to say "not covered" and hand off, so the source
  interface in Phase 3 needs a third outcome besides success and failure.
- Coverage is configuration, not code. Adding a country is a manifest change and
  a rebuild, never a deploy of new logic.
- The comparison in Phase 4 only applies within covered regions; outside them
  both sources are the same source.

---

## Phase 1 — The extract pipeline *(CI, not the VPS)*

**The 34.9 GB Europe raw file must never touch the VPS.** It does not fit
alongside prod and val on 40 GB. Build the artifact in CI and ship only the result.

- A scheduled GitHub Actions workflow downloads the Geofabrik extracts for the
  regions from Phase 0, filters each to rail with the `osmium` CLI (not pyosmium —
  the spike measured 2,022 s for Germany in Python; the C++ tool is far faster),
  and publishes the filtered artifacts.
- Filter selection **must match the current Overpass queries exactly**, or results
  change for reasons unrelated to this work:

  | | selection | serves |
  |---|---|---|
  | ways | `railway` in `(rail, narrow_gauge, light_rail)` **without** `service` | `_via_coordinate_fallback` |
  | relations | `route` in `(train, railway, light_rail)` | `_route_relation_segment`, `_via_train_relations_endpoints` |
  | nodes | **any** node carrying `uic_ref` | `_route_relation_segment` |
  | stations | node, **way or relation** with `railway` in `(station, halt)` and `uic_ref` | `_find_station_near` |

  The last two rows are wider than they look and the width is load-bearing. An
  earlier version of this contract specified "station/halt nodes with `uic_ref`"
  for both, derived from `_find_station_near` alone — but `_route_relation_segment`
  matches `node["uic_ref"=X]` with **no** railway filter, and relations reference
  the *stop* node rather than the station node. Stop nodes are routinely untagged
  as stations: in Luxembourg, 20 relation members carry a `uic_ref` and none is
  tagged `station` or `halt`, so the narrow filter made strategy A find nothing
  where Overpass finds relations today. `_find_station_near` likewise matches ways
  and relations, not just nodes, so polygon-mapped stations were being dropped.

  Verify any change to this table against `src/services/overpass_service.py`
  directly — the queries there are the specification, not this document.
- Publish per-region, versioned by the source extract's date, with a manifest
  recording region, source date, checksum and size.

**Where the artifact lives** is a real decision with a wrong answer: baking it into
the Docker image couples data refresh to a code release and inflates every image.
Prefer a release asset or object store the container fetches on boot and caches on
the mounted volume, so data and code move independently.

### Tests
- The filter selection is asserted against a checked-in fixture extract (a small
  bbox, a few hundred KB) so a tag-selection change fails CI rather than silently
  altering coverage.
- Manifest schema and checksum verification.
- A guard that the workflow never writes a raw extract into the image or the repo.


---

## The Phase 1 / Phase 2 contract

Fixed here so the two phases can be built independently and in parallel.

**Phase 1 produces**, per region, and publishes as a versioned artifact:

- `<region>-rail.osm.pbf` — the filtered extract.
- `manifest.json` — one entry per region:

```json
{
  "schema": 2,
  "generated_at": "2026-09-06T18:00:00Z",
  "regions": [
    {
      "region": "europe/germany",
      "status": "ok",
      "file": "germany-rail.osm.pbf",
      "source": "https://download.geofabrik.de/europe/germany-latest.osm.pbf",
      "source_date": "2026-09-05",
      "sha256": "...",
      "bytes": 10000000,
      "ways": 130713,
      "relations": 2179,
      "stations": 5483,
      "bbox": [5.87, 47.27, 15.04, 55.06]
    },
    {
      "region": "europe/andorra",
      "status": "empty",
      "source": "https://download.geofabrik.de/europe/andorra-latest.osm.pbf",
      "source_date": "2026-09-05"
    }
  ]
}
```

### Three outcomes, not two

A region's build ends as `ok`, `empty` or `failed`, and the manifest records the
first two. **`empty` is the Phase 0 "third outcome" made concrete on the data
side**: the pipeline ran correctly and the region holds **no rail ways**.

- `ok` — rail ways exist. The `.pbf` is published and the entry is the full
  record above.
- `empty` — no rail ways. The matrix job exits **0**, **no artifact is
  published**, and the entry carries `status: "empty"` with no `file`, `sha256`,
  `bytes` or `bbox` — there is nothing to download and nothing it covers.
  **Phase 3 must read an `empty` region as "we know there is no rail here":**
  fall back to Overpass exactly as for an unlisted region, and never treat it as
  a build that has yet to happen.
- `failed` — anything else. The job exits non-zero and the region is simply
  absent from the manifest.

The discriminator is rail ways and nothing else, which is the same predicate
`src/rail/builder.py` refuses a store on (`no railway ways — not a rail
extract`). The two must agree or Phase 1 publishes files Phase 2 rejects: four
configured regions sit in that gap today — Andorra, Malta and the Azores have no
railway at all, and Liechtenstein's only line is currently tagged
`railway=construction`, which gives it stations and UIC nodes but no ways.

Absence therefore means failure, and the publish job treats it that way: a run
whose manifest covers fewer regions than the run was supposed to build refuses
to publish and names them, because an uncovered region falls back to Overpass —
the service this whole plan exists to stop depending on. A subset rebuild
(`workflow_dispatch` with `regions: europe/denmark`) merges into the manifest of
the release it patches rather than replacing it.

### `bbox`

`bbox` is what Phase 3 uses to decide which region covers a coordinate, so it is
required for an `ok` region and must be the extract's true extent, not the
country's nominal one. It is `[min_lon, min_lat, max_lon, max_lat]` **over the
nodes of the rail ways only** — the same extent `src/rail/builder.py` writes to
the store's `meta` and `RailStore.bbox` reports. Bare `uic_ref` nodes and
platform or siding geometry are excluded from it deliberately: they are in the
file for other reasons and would claim coverage the routable data does not have.

**Region boxes overlap, and Phase 3 decided not to break the tie at all.**
These are country extracts, so their true extents interleave: Luxembourg City
falls inside four configured regions' boxes, Bratislava three, Zurich three.
Nothing in Phase 1 or Phase 2 ranks them, and picking the first match in
manifest order would be an accident rather than a decision. Phase 3's answer is
to query *every* candidate and merge the results (see *Region selection* below),
so no ranking is needed and a coordinate near a border is not forced to choose
the wrong side.

**Phase 2 consumes** a filtered `.pbf` and owns everything after it: the store
format, the builder, and the reader. **Where the store is built — in CI as part of
the artifact, or on the box at first use — is Phase 2's decision**, to be made on a
measurement rather than in advance. If it turns out to belong in CI, Phase 1 gains
one step that calls Phase 2's builder; nothing else moves.

Neither phase may import from the other except through these two surfaces.


## Phase 2 — The local store

The spike used a pickle of way geometries. That was right for measuring and wrong
to ship: pickle is not a stable format, loads all-or-nothing, and 319 MB resident
for Germany is the whole budget for a single country.

Requirements, in priority order:

1. **Bounded memory.** Never hold more than a couple of regions at once.
2. **Fast point lookup.** `_nearest_node` is a linear scan today — 1.43 s over
   Germany's 1.1 M nodes, called twice per resolve. This is the single largest
   avoidable cost and needs a spatial index.
3. **Fast load.** A cold region should cost well under the 8.41 s the spike's
   naive build took.
4. **Inspectable.** A format we can query by hand when a route looks wrong.

SQLite with an R-tree index fits all four, matches the existing stack, and needs no
new service. One file per region — `src/rail/store.py` reads what
`src/rail/builder.py` writes: ways with packed geometry, station/UIC lookup,
relation membership, and R-tree indices over way and station bounding boxes.
An in-process LRU bounds how many region files are open at once.

**Decided, on measurements (see the PR for issue #345 Phase 2):**

- **The store holds no graph**, which answers the open question about persisting
  the adjacency graph: neither. Strategy C already works on a bounding box, so a
  bbox query returns only the ways the route needs — Hamburg→Flensburg is 4,422
  of Germany's 130,713 ways and a 33.5k-node graph, not the country's 1,137,563.
  Resolving it costs 62 MB resident against the 319 MB the whole-country graph
  needs, and 0.15 s end to end. Requirement 3 (fast load) stops applying: there
  is nothing to load, and opening a region is 0.8 ms.
- **The memory did not disappear, it moved to the caller's bounding box** — about
  268 bytes per vertex, doubling once `_build_rail_graph` runs. Hamburg→Munich is
  284,607 vertices (~150 MB, measured 204 MB resident for the whole resolve); the
  whole-Germany box is 1,270,497 (~680 MB) on a 1 GB worker. `ways_in_bbox`
  therefore enforces its own vertex ceiling and raises rather than allocate
  (Phase 3's merge across overlapping regions bounds the *result* the same way,
  by counting every candidate before it decodes any of them; its peak can still
  reach about twice the ceiling when candidates overlap heavily — see *Region
  selection* below), and
  `_RAIL_BBOX_MAX_AREA` in the resolver guards the same failure from the other
  end: it is a memory bound now, not an Overpass workaround, and must survive
  Phase 6.
- **A spatial index replaces the linear snap, at parity.** `nearest_node` over
  Germany is 0.4–1.5 ms against 0.67 s for the same scan over the whole-country
  graph in this process (1.43 s in the spike), and it returns the *identical*
  vertex — including `_nearest_node`'s squared-degree ordering, which is wrong as
  geometry and kept anyway because this phase substitutes the source and nothing
  else. Ranking by metres instead moved 0.53% of ±200 m snaps into a different
  connected component. Snapping is Phase 4's to fix, against a stable baseline.
- **The store is built in CI**, as one more step in the Phase 1 pipeline calling
  `python -m src.rail.builder`. Germany builds in 23.9 s at 196 MB peak — small
  enough that first-use building on the box would work, and pointless: it would
  put the `.pbf` and the builder on a VPS that needs neither, and pay that cost
  again after every container restart with an empty volume. Cost of the choice is
  transfer size: the store is larger than the extract it is built from (Germany
  58 MB from 23 MB, 28 MB gzipped).

### Tests
- Round-trip: filtered extract → store → the same element shapes
  `_build_rail_graph` consumes today.
- Spatial index correctness: nearest-station and nearest-node queries against
  brute-force results on a fixture region.
- LRU eviction bounds resident memory below a stated ceiling.
- Load time for the largest region stays under budget (a perf test with a
  generous threshold, to catch regressions rather than police milliseconds).

---

## Phase 3 — Resolver integration

Introduce a source interface with two implementations — the existing Overpass one
and the local one — so the three strategies are unchanged and switchable by config.
That is what makes the comparison period in Phase 4 possible at all.

- `_enrich_uic` becomes a local station lookup. Note the spike's finding that UIC
  coverage swings from 0.3 % (Denmark) to thousands of stations (Germany); locally
  the lookup is free either way, so no behaviour need change.
- Strategies A and B query local relations.
- Strategy C loads the region's graph instead of issuing a bbox query.
- **Region selection** from the segment's endpoints, including the case where a
  route crosses a border — the plan's first genuinely new logic. Two adjacent
  regions must be loadable and joinable, or cross-border rail silently degrades.

**Decided in Phase 3 (PR #351), on the user's call:**

- **Query every candidate region and merge; do not rank them.** Candidates are
  every region whose manifest bbox intersects the query area, computed *per
  query* rather than per resolve. Point lookups take each region's minimum and
  then the global minimum, which is the answer a single merged store would give.
  `relations_near` unions. `ways_in_bbox` merges and deduplicates by way id.
  Relation members are merged too: each extract holds only its own side of a
  border while Overpass returns the whole relation, so merging moves toward
  parity rather than away from it. This is why no tiebreak is needed.
- **A local failure defers to Overpass, with one deliberate exception.** One
  mechanism at the top of `get_rail_geometry`: if the local attempt degrades,
  the whole segment is retried against Overpass. That covers near-zero regions
  (Cyprus publishes 2 rail ways, Iceland 1), an `empty` entry, a missing
  directory or manifest, a manifest that is malformed in any way, a store file
  that is absent, or present and unreadable **when it is opened**, and a
  coordinate outside every region. The exception is the vertex ceiling, which
  straight-lines on purpose — see the next bullet. The guard is deliberately
  wide: a narrow `except` here took down every train resolve in review, because
  the file is still corrupt on each of RQ's three retries
  (`src/jobs/queue.py`: `Retry(max=3, interval=[10, 30, 60])`) and the job then
  fails for good. Per-operation fallback was rejected — it would fire an
  Overpass query whenever a covered region legitimately has no relation for a
  pair, which is common, and would have preserved essentially all of today's
  traffic.

  **Closed in #352:** the guard wrapped opening a store, not querying one, so a
  `sqlite3.DatabaseError` from an already-open connection escaped into the job.
  Both halves are now in place. The delivery step publishes by atomic rename
  only (see *Delivery*), so a refresh cannot pull a file out from under an open
  connection — and `_ask` in `rail_source.py` guards the query calls anyway,
  because SQLite reads pages lazily and a file whose header and `meta` are
  intact can still meet a corrupt page on the first query that touches it. That
  guard is narrower than it first reads: `sqlite3.ProgrammingError` and
  `NotSupportedError` are subclasses of `DatabaseError` and both mean *we* built
  the query wrong, so they are re-raised and only genuine unreadability is
  caught. Wide is right for *opening* a file, where what it raises is not ours
  to enumerate, and wrong for *querying* one, where answering a defect in this
  module's own merging or scoping with "region not covered" is the one answer
  that looks exactly like success.
- **A bbox query over the vertex ceiling straight-lines and does not fall
  back.** The ceiling bounds *our* memory, not Overpass's patience: if Overpass
  answered, `_build_rail_graph` would rebuild the allocation just refused, on
  the same worker, after the most expensive query we know how to ask. Note this
  is **not** the pre-Phase-3 behaviour: `OverpassRailSource.ways_in_bbox` has no
  vertex ceiling, and the only size guard before Phase 3 was
  `_RAIL_BBOX_MAX_AREA`, so a 9 sq° box holding 1.2 M vertices used to be
  answered and the ~680 MB graph built. The local path is deliberately more
  conservative, and Phase 4's comparison must expect that difference rather than
  read it as a regression. The budget is charged for *deduplicated* vertices, so
  overlapping candidates do not each spend it — charging raw totals degraded the
  effective ceiling toward `_MAX_BBOX_VERTICES / N` exactly at borders, where
  overlap is greatest.

  **Closed in #352, by counting before decoding.** Deciding the merged total
  *after* decoding each region left the regions already merged and the region in
  hand alive at once, so two candidates near the ceiling transiently held twice
  it. Now `RailStore.vertex_counts_in_bbox` answers the same box as
  `ways_in_bbox` with `{way id: vertex count}` — the per-way form of the
  `SUM(LENGTH(geom))/8` scan `ways_in_bbox` already did, ids included so the
  merge can drop the border ways both extracts hold — and
  `LocalRailSource.ways_in_bbox` collects that from every candidate, merges by
  id, checks the exact total, and only then decodes. Both passes keep the
  *first* region's copy of a shared id, so what is counted is what is kept: an
  id present in two extracts at different lengths — an OSM edit between two
  refresh dates, or a node `builder.py` dropped in one extract and not the
  other — cannot be charged as one copy and returned as another.

  **What this bounds is the result, not the peak.** Duplicates are still
  dropped after decoding, so fully overlapping candidates hold their shared
  ways more than once in flight: the peak is the answer plus the duplicated
  share of two consecutive candidates, up to about twice the ceiling. On real
  cross-border boxes the duplication is 0.4-0.5% of the result, so this is
  theory rather than a live risk — but it stops being theory if
  `config/rail_regions.yml` ever gains one of the Geofabrik aggregates it
  currently excludes for exactly that reason. Disjoint candidates do get the
  full benefit:

  | | peak allocated |
  |---|---|
  | 2 regions at the ceiling, before | 392.2 MB |
  | 2 regions at the ceiling, after | 0.3 MB |
  | 4 regions at the ceiling, after | 0.5 MB |
  | 2 regions merging to the ceiling (accepted) | 196.1 MB before, 196.2 MB after |

  Measured with synthetic stores and disjoint way ids, `tracemalloc` around the
  call. Four fully overlapping regions measure 9.4 MB against 6.3 MB at small
  scale, the difference being one region's decoded rows kept alive by the loop
  variables; those are released explicitly.

  The cost is one more index scan per candidate region, and it is a fixed cost
  per region, so it is heaviest on the *smallest* boxes rather than the largest.
  On Germany's real store a 9 sq° Rhine-Ruhr box goes from 278 ms to 328 ms for
  one region and 829 ms to 961 ms for two — about +18% — while a Hamburg to
  Flensburg box goes from 22 ms to 42 ms, and a small cross-border box from
  4.3 ms to 6.0 ms. Quote the box, not the average.

  About two thirds of the added time is `ways_in_bbox` re-running its own `SUM`
  after the merge already counted the same rows (52 ms of the 77 ms added on
  Rhine-Ruhr). That guard is kept unconditional deliberately, as a backstop no
  caller can opt out of — it is what turns a miscounted merge into a refusal
  rather than an allocation — and buying the 50 ms back would mean adding one.
- **`RailStore.nearest_node` is deliberately not in the interface.** The
  resolver builds its own graph and snaps with `_nearest_node` over it;
  exposing the store's spatial index would change snapping, which is Phase 4's
  to change against a stable baseline.
- **Config is `RAIL_SOURCE` (default `overpass`) and `RAIL_DATA_DIR`**, so
  merging Phase 3 is a production no-op until Phase 4 flips it deliberately.
  Coverage is re-read periodically rather than cached for the life of the
  worker: a refresh is picked up without a restart, and a manifest read that
  loses a race with a rebuild does not pin that worker to Overpass forever.

### Tests — what was actually built

- **Parity per strategy**, in `tests/test_rail_source.py`: each of A, B, C and
  the station lookup returns a byte-identical polyline from the local store and
  from an Overpass response carrying the same data. The oracle is independent
  SQL, not a call into `RailStore`. Note its limit honestly: both sides read the
  same file, so this proves the local source returns what the *store holds*, not
  what *Overpass* returns — a builder defect would be invisible to it. That is
  the right thing for a phase whose contract is "substitute the source"; closing
  the remaining gap is Phase 4's comparison, against live Overpass.
- **Cross-border resolution** over a synthetic two-region fixture whose
  endpoints are far enough apart that a single-endpoint scope reaches only one
  region — the earlier fixture's endpoints were 0.10° apart and could not tell
  the two apart.
- **Outside coverage**, and every way the local source can fail: no directory,
  no manifest, a malformed manifest of each shape, an unknown schema, a missing
  store file, a store file present but truncated or of the wrong schema, and an
  `empty` entry.
- **No network access**, at two levels: the transport asserted uncalled, and
  `socket.socket` refused outright on a local hit.

The existing rail tests in `tests/test_vr_hafas.py` and the resolve tests keep
running against the Overpass source, which the default leaves in place; running
the whole of them against the local source needs fixtures for every route they
cover and is deferred to Phase 4, where the comparison harness builds them.

---

## Delivery — getting the data onto the box *(BUILT)*

Phase 1 publishes extracts, Phase 2 reads stores and Phase 3 chooses between
them by config. Nothing populated `RAIL_DATA_DIR`: that step fell between
phases and belonged to nobody. It is `scripts/fetch_rail_data.py`, run on the
box, and it is what Phase 5 schedules and monitors rather than something Phase
5 still has to invent.

**It fetches extracts and builds the stores here, rather than downloading
prebuilt stores**, because the release holds extracts and only extracts. Phase
2 chose to build in CI and the pipeline does not yet do it; that decision is
not reversed, and if the workflow gains the step this script gets simpler
rather than obsolete. Building on the box is affordable in the meantime —
Germany is ~12 s and ~200 MB peak — and the extract is a fraction of the size
of the store it produces (Germany 10 MB against 58 MB, a sixth; Luxembourg,
measured on the published asset, 0.33 MB against 1.36 MB, a quarter), so it
moves less over the network, not more. It needs `pyosmium`, which is in
`requirements.txt` and therefore already in the image. The image was missing
`libexpat1`, without which `import osmium` fails on `python:*-slim`; the
Dockerfile now installs it, and `.dockerignore` un-excludes the one script in
`scripts/` that has to be *in* the image rather than in CI.

**The contract, which Phases 4 and 5 may rely on:**

- **`RAIL_DATA_DIR` holds `manifest.json` (schema 2, entries verbatim from the
  release) and one `<region>.rail.sqlite` per `ok` region installed**, plus a
  `.sha256` sidecar per store recording the asset it was built from, and a
  `.incoming/` working directory holding nothing but its `.lock` between runs.
  Each run stages in its own subdirectory of it and removes it when it ends; a
  run killed outright (an OOM kill is the realistic case) leaves that
  subdirectory behind, and the next run clears it while holding the lock.
- **One refresh at a time.** A refresh takes an exclusive `flock` on
  `.incoming/.lock`; a second one does nothing and exits 0 rather than waiting.
  This is not tidiness: without it two runs share a staged path, `build_store`
  removes and rebuilds that path, and one run publishes the other's
  half-written file — with the *correct* digest recorded beside it, so every
  later run skips the region as up to date and the corruption is permanent.
  Whatever Phase 5 schedules must not assume a run has finished before the
  next one starts; it does not have to, because this holds.
- **Every file appears by atomic rename, never by being written in place** —
  the stores, the manifest and the `.sha256` sidecars alike.
  The work directory is inside `RAIL_DATA_DIR` so the rename cannot cross a
  filesystem. **This closes the known gap Phase 3 left open above** — "either
  widen the guard to the query calls or make the refresh contract
  atomic-rename-only" (#352, finding 2) — by taking the second option: a
  worker holding a store open keeps the old inode and its query finishes
  against whole, valid data. #352 then took the first option as well, for the
  corruption a rename cannot prevent — a page that goes bad on disk under a
  connection that already opened the file. It is a contract, not an implementation detail,
  and `tests/test_rail_data_fetch.py` holds a `RailStore` open across a real
  refresh to prove it. Replacing the rename with an in-place copy makes that
  test fail by returning *zero* ways rather than by raising — the silent shape
  of the failure. That test alone does not catch *building* straight into
  `RAIL_DATA_DIR`, because `build_store` unlinks its output first and the
  held-open reader survives that too; a second test covers the two failures
  that would reintroduce — a reader *opening* the store part-way through a
  ~12 s build, and a build that raises leaving the region with no store at
  all — by asserting the destination is byte-identical to what it held before
  and never partial while the build runs.
- **The installed manifest describes what is on disk, never what was
  intended.** A region that fails keeps the entry it had, because the store it
  describes is still there; a region installed this run gets the new entry.
  `generated_at` advances only when every region converged, so a partial
  refresh cannot report a freshness it does not have — Phase 5 alerts on data
  age, and an optimistic timestamp is the failure that looks like success.
  Per-region age is each entry's `source_date`.
- **Re-running converges and costs only what changed.** A region whose sidecar
  matches the manifest's `sha256` is skipped without a download.
- **Nothing is trusted unverified.** An asset whose size or sha256 disagrees
  with the manifest is refused, the region keeps its existing store, and the
  run exits non-zero naming what was refused. A manifest of an unknown schema
  is refused whole, before anything is fetched, and so is an entry whose
  `status` this version does not know or that carries no `sha256` at all —
  there is nothing to verify against, so nothing may be installed.
- **A refresh needs no restart.** `_local_rail_source` rebuilds the source, and
  with it the store cache, every `_LOCAL_SOURCE_TTL_S`, so new data is live
  within five minutes — which is why the atomic rename has to hold for those
  five minutes rather than merely for an instant.
- **Bounded**: one region's extract plus its store, and the extract is deleted
  as soon as the store exists.
- **Unauthenticated.** The repository is public, so the releases API and the
  asset URLs answer with no token (verified, not assumed). The step takes no
  credentials.

Deliberately *not* built here: any schedule. Running it at container boot was
rejected — it would block startup or race the workers on a first install, and
the data is on a volume that outlives the container, so boot is not when the
question arises. The operational procedure is in `docs/DEPLOYMENT_VPS.md`, §9.

---

## Phase 4 — Comparison and cutover

> **Superseded (2026-10-04)** for phases 4–6 by `docs/LOCAL_TRANSPORT_DATA_PLAN.md`:
> the cutover happened, ferry and bus still depend on Overpass, and what follows
> was written before either was known.

Do not switch on faith. For a period, resolve through both sources and record where
they differ — strategy chosen, point count, path length, degraded flag.

This is also where the **known routing defects** get addressed, because they become
cheap to iterate on (0.15 s per experiment instead of a 45 s network round trip):

- **Snapping.** `_nearest_node` picks the geometrically closest node regardless of
  which connected component it is in, so a siding beside the platform beats the
  through line. Fix: prefer a node that can reach the destination.
- **Fragmentation.** Denmark 50 components (largest 65.6 %), Germany 1,143
  (87.6 %). Decide deliberately whether to include `service` ways — the spike
  showed doing so raises the largest component but breaks routes that previously
  worked, because it interacts with the snapping bug.

Treat these as **quality work enabled by the migration, not part of it** — land the
substitution first, with parity, then improve routing against a stable baseline.

### Tests
- A corpus of real segments with expected strategy and rough geometry, run against
  both sources, asserting parity within tolerance.
- Regression tests for each routing defect, written to fail against current
  behaviour first.

---

## Phase 5 — Maintenance

- Scheduled refresh (monthly is ample; rail alignment changes over years).
- Metrics: region load time, cache hit rate, resolve duration, outside-coverage
  rate, and **data age** — a silently stale extract is the failure mode here, and
  it looks exactly like success.
- An alert on data age exceeding the refresh interval.
- Document the refresh procedure and the manual-rebuild path in
  `docs/DEPLOYMENT_VPS.md`.

---

## Phase 6 — Retire the scaffolding

Once the local source is authoritative, most of the Overpass remediation exists
only to manage a quota we no longer depend on: the resolve deadline, 429 backoff,
host cooldowns, `upstream_slots`, the concurrency limiter, and arguably
`upstream_cache`.

Keep the Overpass source itself if it remains the outside-coverage fallback — but
then it serves a handful of requests a day rather than several per resolve, which
is comfortably inside fair use and stops the ban risk being structural.

---

## Risks

| risk | mitigation |
|---|---|
| Local results differ from Overpass in edge cases | Phase 4 comparison period; parity tests before cutover |
| Extract goes stale unnoticed | data-age metric and alert; it is the failure that looks like success |
| RAM regression as coverage grows | LRU ceiling test in Phase 2 |
| CI build cost grows with coverage | per-region artifacts, refreshed independently |
| A user travels outside coverage | explicit Phase 0 decision, not an accident |

## What this does not change

HAFAS/MOTIS train lookup is a separate service and is untouched. The
`route_quality` modelling (D2), the job-liveness endpoint (D3) and distinct map
styling for approximate routes (D4) remain wanted and are independent of this work.
