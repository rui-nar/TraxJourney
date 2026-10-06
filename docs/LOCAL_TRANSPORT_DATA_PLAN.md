# Local transport data, phases 4–6 — Plan for #345

Follows `docs/LOCAL_RAIL_DATA_PLAN.md`, whose phases 1–3 and delivery step are
merged. That plan's phases 4–6 were written before the cutover happened and
before anyone measured what still talks to Overpass; this plan replaces them.

## Problem

**The local rail data is live and nobody is watching it.** `RAIL_SOURCE=local`
is set on production and validation (owner, 2026-10-04). The data reaches the
box only when someone runs `scripts/fetch_rail_data.py` by hand
(`docs/DEPLOYMENT_VPS.md` §9), nothing records or reports how old it is, and the
`Rail extract` workflow notifies no one when it fails. The proof: the scheduled
run of 2026-10-02 failed — Geofabrik moved Germany behind a mirror redirect and
the checksum 404'd (fixed in PR #560) — and it was found two days later only
because an unrelated investigation looked. Both boxes are still on
`rail-data-2026-09-08`. A silently stale extract looks exactly like success.

**There is no regression baseline for routing.** #359, #363 and #364 were each
validated by an ad-hoc script against a store built by hand. Nothing stops the
next data refresh or resolver change from re-breaking Paris Est → Strasbourg,
and the next routing fix has nothing to measure itself against.

**Ferry and bus are where rail was in September.** Every ferry and bus resolve
goes to a public Overpass instance (`api/segments.py:130,136` →
`src/services/overpass_service.py:1202-1207`), with no local path. The quota
and ban risk that #345 removed for rail is still structural for them, and it is
why all of the Overpass scaffolding has to stay.

**Nobody knows how much still reaches Overpass.** No metric counts Overpass
requests, by caller or at all, and a resolve does not record which source
answered it. "Retire the scaffolding" cannot be decided without that number.

## Current state

- **Refresh.** Manual only. `scripts/fetch_rail_data.py` installs the newest
  non-draft `rail-data-*` release (`pick_release`, `:126-139`) into
  `RAIL_DATA_DIR` under an exclusive lock (`.incoming/.lock`), by atomic rename,
  and writes `manifest.json` (`:361-405`). `generated_at` advances only when
  every region installed; a failed region keeps its previous entry. The release
  tag is not recorded on disk. New data is live within `_LOCAL_SOURCE_TTL_S =
  300` s (`overpass_service.py:311`), no restart.
- **Data age.** Each manifest entry has `source_date`; each store has a `meta`
  table with `source_date` and `built_at` (`src/rail/builder.py:351-363`),
  loaded into `RailStore.meta` (`src/rail/store.py:194-196`). Nothing reads
  either.
- **Scheduling.** APScheduler in the API process only
  (`api/router.py:142-189`), guarded by `TRAXJOURNEY_ROLE`; every job gets run,
  duration and last-success metrics through `record_job_event`. RQ workers run
  no scheduler. Long work is enqueued to RQ from a scheduled job.
- **Metrics.** Prometheus at `GET /metrics` (`api/metrics.py`), definitions in
  `src/utils/metrics.py`. No rail, Overpass or route-source metric exists.
  Grafana alert rules are documented (`docs/OBSERVABILITY.md:143-156`) but none
  are provisioned.
- **Workflow.** `.github/workflows/rail-extract.yml`: `plan` → `build` (matrix)
  → `publish`. `publish` alone has `contents: write`, and
  `tests/test_rail_extract_workflow.py::test_only_the_publish_job_can_write`
  pins that. No failure notification.
- **Overpass scaffolding.** All of it is inside `_overpass()`
  (`overpass_service.py:1437-1533`), the only function that sends to Overpass:
  the 24 h response cache (`src/jobs/upstream_cache.py`), the per-host
  concurrency slot (`src/jobs/upstream_slots.py:51-108`, concurrency 1), host
  cooldowns on 429/5xx/unreachable (`upstream_slots.py:123-150`), and mirror
  order (`_OVERPASS_ENDPOINTS`, `:91-94`). The resolve deadline, `_SLOT_RETRIES`
  and the circuit breaker named in the old phase 6 are already gone.
  `_RAIL_BBOX_MAX_AREA` (`:1079`) is marked not to be retired: it bounds local
  memory too. MOTIS/Transitous, Nominatim and Strava use none of this.
- **Who reaches Overpass under `RAIL_SOURCE=local`.** Rail only on a local miss
  (degraded result, outside coverage, unset or unusable data dir —
  `overpass_service.py:333-399`), and not on `RailSourceOverload`. Ferry and
  bus always (`_get_route_geometry`, `:1212`): strategy A `rel["route"=MODE]`
  with member geometry in a clamped bbox, B `way["route"=MODE]`, C (ferry only)
  `way["ferry"="yes"]`, both B and C through `_build_rail_graph` /
  `_nearest_node` / `_dijkstra`.
- **Measured routing today** (France store from `rail-data-2026-09-08`, current
  code, no fallback): Toulouse → Paris Austerlitz 712 km via Brive and Limoges;
  Toulouse → Paris Montparnasse 792 km via Bordeaux and Poitiers; Montparnasse ↔
  Bordeaux 536 km both ways, endpoints within 0.21 km — the station-throat gap
  noted under #359 no longer reproduces. All `relation_uic`, none degraded.

### Ferry and bus spike (2026-10-04)

Filtered with exactly the three Overpass selections above, pyosmium, one pass
per element type, filtered files written with metadata dropped as phase 1 does.
Rail sizes are the published `rail-data-2026-09-08` assets.

| | raw | rail file | ferry rels / ways kept | ferry file | bus rels / ways kept | bus file | bus ÷ rail |
|---|---|---|---|---|---|---|---|
| Denmark | 495 MB | 0.80 MB | 51 / 177 | 0.06 MB | 730 / 36,297 | 3.59 MB | 4.5× |
| Croatia | 200 MB | 0.64 MB | 36 / 220 | 0.06 MB | 938 / 25,226 | 3.81 MB | 6.0× |

- **Ferry is negligible** everywhere it was measured.
- **Bus is 4.5–6× rail.** Projected over the 49 regions: 0.5–0.65 GB of filtered
  files against rail's 0.11 GB, and — at the 3× file-to-store ratio France
  shows (18 MB → 55 MB) — **1.5–2 GB of bus stores per stack**. Germany alone
  projects to ~125 MB filtered, ~375 MB as a store.
- **Every node of every kept way was located** in both countries: Geofabrik's
  extracts carry complete ways.
- **What is missing is cross-border, and our coverage holds the other side.**
  Ferry relations with absent members: Denmark 5 of 51 (Świnoujście–Ystad,
  Hirtshals–Stavanger, Hirtshals–Tórshavn–Seyðisfjörður), Croatia 2 of 36
  (Pula–Venezia). Bus relations: Denmark 694 of 730 complete, 26 missing ≥75 %
  of their ways; Croatia 785 of 938 complete, 108 missing ≥75 % — international
  coach lines whose route is mostly abroad. Poland, Sweden, Norway, the Faroe
  Islands, Iceland and Italy are all in `config/rail_regions.yml`, so the
  cross-region merge (Decision 8) is what reunites them.

## Decisions

1. **Alerts reach the owner as a GitHub issue plus a server metric.** (Owner,
   2026-10-04.) A failed `Rail extract` run opens — or comments on, if one is
   already open — a single issue labelled `rail-data`, and a later successful
   run closes it. The server exports the installed data's age as a gauge and
   logs a `WARNING` once a day while it is over the threshold, so Grafana can
   alert on it once rules exist. *Rules out* email (needs an owner-alert
   address and SMTP in the alert path) and Grafana-only (its rules are not
   provisioned and the stack is unverified live).
2. **Data age is the oldest `source_date` among `ok` regions in the installed
   `manifest.json`, threshold 40 days.** The workflow builds on the 2nd; val
   refreshes on the 4th and prod on the 5th (Decision 5); 40 days is one
   cycle plus a missed retry for either. The oldest region, not `generated_at`, because a partial refresh
   keeps `generated_at` and a region that failed three months running would
   otherwise hide behind its neighbours. *Rules out* store-file mtimes (a
   reinstall of the same data resets them).
3. **A route corpus gates publishing.** (Owner.) A YAML list of real legs with
   expectations — endpoints within a distance of the stations, length within a
   band, must pass near / must not pass near named towns, strategy not
   straight, not degraded — run by `scripts/route_corpus.py` against a store
   directory. The `Rail extract` publish job builds the stores the corpus needs
   from this run's artifacts and runs it **before** uploading anything; a
   failure publishes nothing, so the box (which installs the newest published
   release) never sees it. `force_publish` overrides, as it already does for
   coverage. The same script runs locally and in pytest. *Rules out* a
   comparison against live Overpass: production already runs local, the box's
   IP is banned on the main instance, and Overpass's answer is not the
   reference — the legs' known-correct shape is.
4. **The corpus records today's failures instead of hiding them.** A leg the
   current code gets wrong is entered with `known_bad: <reason>` and its
   current outcome; the gate then fails if it changes *without* the entry
   being updated, in either direction. That makes the corpus phase 4's quality
   baseline: a routing fix flips an entry and says so in its diff.
5. **The box refreshes itself, monthly, through RQ.** (Owner.) An APScheduler
   cron job in the API process (04:10 UTC on `RAIL_AUTO_REFRESH_DAY`, default
   the 5th; val sets the 4th, so a bad release reaches val a day before prod
   and the two stacks never build at once on the shared host) enqueues one job
   on the `default` queue with `allow_inline=False`. With no broker configured
   it does not run in the API process: it logs a `WARNING` that the refresh is
   manual on this deployment and returns (R1-4); the job runs `scripts/fetch_rail_data.py` as a subprocess
   with the same arguments §9 documents. Subprocess, not import: it bounds the
   store build's memory to a process that exits, and it runs exactly the
   command the runbook already proves. The script's own lock makes a
   concurrent manual run harmless. On only when `RAIL_SOURCE=local`; the
   kill switch is `RAIL_AUTO_REFRESH=0`. *Rules out* a host cron (needs an SSH
   step per box and is invisible to the job metrics) and running it in the API
   process (minutes of CPU and disk on the request path).
6. **Overpass traffic is counted by purpose, and every resolve says which
   source answered.** `_overpass(query, purpose)` with `purpose` in `rail`,
   `rail_station`, `ferry`, `bus`; a counter by purpose and outcome (cache hit,
   ok, back-off, unreachable, cooling). A resolve counter by mode, source
   (`local`, `overpass`, `motis`) and degraded. Metrics only — no column.
   *Rules out* persisting the source on the segment: a schema change for a
   number we need in aggregate.
7. **Ferry and bus get local data through the same pipeline, as separate
   layers.** (Owner chose to plan this.) The per-region build job filters three
   layers from the one raw download — `rail`, `ferry`, `bus` — with the exact
   selections the Overpass queries make, and publishes one store per region
   per layer (`europe-denmark.bus.sqlite`). Separate stores, not one graph:
   bus ways are roads, and a joint graph would let a rail Dijkstra walk down a
   high street. Same release, same manifest, same fetch, same lock. *Rules
   out* a separate `transport-data-*` release (a second delivery path to
   monitor) and one Europe-wide ferry file (needs the 35 GB Europe extract in
   CI; the cross-region merge already answers cross-border routes).
8. **Cross-border routes use the existing merge contract.** A bbox query asks
   every region whose layer bbox overlaps and merges, deduplicating ways and
   relations by id (`docs/LOCAL_RAIL_DATA_PLAN.md`, "Region bboxes overlap").
   A relation whose members are split across Denmark and Germany is whole
   again after the merge. What is in neither extract — open-sea legs outside
   every polygon — is a local miss, and **a local miss always falls back to
   Overpass**, as for rail.
9. **Ferry and bus resolution moves behind a source interface, logic
   unchanged.** The three strategies stay as they are and read their elements
   from a `RouteSource` (local or Overpass), exactly as phase 3 did for rail.
   Behaviour changes are corpus-measured follow-ups, not part of the move.
   "Unchanged" needs the store to answer each strategy's own selection, so
   **store schema 3 replaces the single `way.rail` flag with a `way.cls`
   bitmask**: bit 0 the layer's routable class (`railway` without `service`
   for rail — today's `rail = 1`; `route=MODE` for ferry and bus), bit 1
   `ferry=yes`, bit 2 relation member. Strategy B asks for bit 0, strategy C
   for bit 1, so a way tagged both is in both answers and a `ferry=yes`-only
   way is in C's alone — exactly what the two Overpass queries return (R1-1).
   *Rules out* keeping the rail schema for the new layers: one flag cannot
   tell B's ways from C's.

   Each layer also defines its **routable set** — the ways that decide
   whether the layer is `empty` in CI (U7), whether the builder refuses the
   store (U8), and which ways the layer's bbox is measured over (both). One
   table, used by both units (R2-3):

   | Layer | Routable set | Why |
   |---|---|---|
   | rail | bit 0 | unchanged from today |
   | ferry | bits 0, 1 or 2 | B, C and A each answer from their own class; a region with only `ferry=yes` ways is not empty |
   | bus | bits 0 or 2 | bus routes are mapped as relations; `route=bus` ways are rare, so bit 0 alone would make almost every bus layer empty |
10. **Retiring scaffolding is decided from the counters, not in this plan.**
    See Open decision 2.

## Review envelope

REVIEW.md defaults apply, with these additions:

- **E7 — GitHub Actions.** The workflow runs on GitHub-hosted runners with the
  repository's `GITHUB_TOKEN`. The new notify job gets `issues: write` and
  nothing else; no other job's permissions change.
- **E8 — Geofabrik is untrusted input** (E2), and now feeds three layers. The
  size guards of phase 1 apply per layer.
- **Scale.** 49 regions × 3 layers. Box disk: the VPS has 40 GB for both
  stacks; the plan's budget for all installed stores is 3 GB per stack.
  Worker memory: 1 GB per RQ worker; the refresh subprocess runs inside one.
- **Concurrency.** A scheduled refresh and a manual one may overlap; the
  existing lock decides. Readers see a store change at the next
  `_LOCAL_SOURCE_TTL_S` boundary.

## Boundaries crossed

- **Release manifest schema 2 → 3.** Each entry gains `layer` (`rail`,
  `ferry`, `bus`). Readers on the box treat a missing `layer` as `rail`.
  **Ship order is the contract:** the reader that accepts schemas 2 and 3
  (`fetch_rail_data.py`, `rail_source.load_coverage`) is deployed to both
  boxes *before* the workflow publishes a schema-3 release. A box without U8
  that meets a schema-3 release refuses the whole refresh before writing
  anything (`read_manifest` raises), so it keeps routing on the stores it has
  while their data ages — every refresh fails until U8 deploys, and U2's age
  gauge is what eventually says so (R3-2). The Overpass fallback for every
  train happens only the other way round: an image rolled back past U8 meeting
  stores already rebuilt as schema 3 (see the next item). U8 pins both orders
  with tests.
- **Store schema 2 → 3** (Decision 9). The reader accepts schemas 1, 2 and 3
  and reads a v1/v2 `rail` flag as `cls` bit 0, so a rail store of any
  version keeps routing. The fetch sidecar already records the store schema,
  so each box rebuilds its rail stores at the first refresh after U8 deploys,
  with no new release. Same ship order as the manifest: U8 deploys first.
  **Rolling the image back past U8** once a refresh has run leaves schema-3
  stores the old reader refuses, and every train goes to Overpass. Recovery is
  one command, written into §9 by U8 (R2-6): re-run `fetch_rail_data.py` with
  the rolled-back image — its sidecar check rebuilds every store at the old
  schema — adding `--tag <last manifest-schema-2 release>` once U7 has
  published schema-3 releases.
- **Store files.** `store_filename(region, layer="rail")`: rail stores keep
  their name (`<region>.rail.sqlite`); new `<region>.ferry.sqlite` and
  `<region>.bus.sqlite` beside them. Sidecars and installed-manifest entries
  are keyed by `(region, layer)` (R1-9).
- **Config.** New `RAIL_AUTO_REFRESH` (default on when `RAIL_SOURCE=local`)
  and `RAIL_AUTO_REFRESH_DAY` (default 5; val sets 4).
  `RAIL_SOURCE=local` now also enables local ferry and bus; no separate switch,
  because a local miss falls back to Overpass for every layer.
- **API contract, database, exports:** none.
- **Old clients:** unaffected; geometry shape is unchanged.

## Conventions

- Every new local path falls back to Overpass on a miss, with the same wide
  `except Exception` guard rail uses, and the vertex ceiling still
  straight-lines without fallback.
- Every new metric is defined in `src/utils/metrics.py` and documented in
  `docs/METRICS.md`.
- Tests that need a store build one from a fixture PBF in `tmp_path`; nothing
  downloads in pytest.
- The corpus is the only place a routing expectation for real geography lives.

## Open decisions

1. **Data-age threshold** — 40 days (Decision 2). Affects U2's constant only.
   Decide before U2 merges.
2. **What to retire, and when.** After local ferry and bus have run for 30
   days, U3's counters say how many requests a day still reach Overpass and
   for what. Proposed rule: if all purposes together stay under 100 requests
   a day (the main instance's fair-use figure, `docs/LOCAL_RAIL_DATA_PLAN.md`),
   remove the concurrency slot and its Redis set, and keep the cache and the
   cooldowns, which are the cheap part and the ban insurance. Decided by the
   owner from the numbers; a follow-up issue, not a unit here.
3. **Bus coverage.** If the per-region bus stores exceed the 3 GB budget
   (Review envelope) — the spike projects 1.5–2 GB, inside it but not by much —
   bus ships for a subset of regions first. Decided from U7's full-run
   measurement, before U9 starts.

## Execution units

Waves are ordered so that no two units in one wave share a file or depend on each other (DELIVERY.md W1); every dependency points to an earlier wave.

### Wave 1 — watch it, count it

U1, U3 and U4 share no file and depend on nothing.

#### U1 — A failed Rail extract run opens an issue
- **Goal:** a failed run of `rail-extract.yml` opens or updates one `rail-data` issue, and a successful run closes it.
- **Scope:** `.github/workflows/rail-extract.yml`, `tests/test_rail_extract_workflow.py`.
- **Context:** the workflow's job graph and the `permissions` blocks; `test_only_the_publish_job_can_write` (`tests/test_rail_extract_workflow.py:247`) and how its siblings load the YAML.
- **Do:**
  1. Add a `notify` job: `needs: [plan, build, publish]`, `if: always()`, `permissions: {issues: write}`, no checkout.
  2. On any needed job failing: find the open issue labelled `rail-data` (`gh issue list --label rail-data --state open`); comment with the run URL and the failed job names if found, else create it titled `Rail extract failed` with the same body.
  3. On success: close any open `rail-data` issue with a comment linking the run. When `inputs.regions` is set and the patched tag is not of the current month, emit a `::warning::` and put the same sentence in the closing comment: `subset run patched <tag>; no rail-data release exists for <YYYY-MM>` (guard R1-5).
  4. Adjust `test_only_the_publish_job_can_write` so it pins: `publish` alone has `contents: write`, `notify` alone has `issues: write`, and no job has both.
- **Acceptance:**
  - New tests: `notify` needs every other job, runs on `always()`, has exactly `issues: write`; the failure branch and the success branch each reference the `rail-data` label; the success branch carries the subset-of-an-old-tag warning.
  - `pytest tests/test_rail_extract_workflow.py` passes.
- **Out of scope:** alerting from the box; labels other than `rail-data`.
- **Latitude:** local design.
- **Escalate if:** the label has to be created by the job (it needs `issues: write` on labels too — say so rather than widening permissions); a file outside Scope is needed.
- **Depends on:** —

#### U3 — Count Overpass requests by purpose and resolves by source
- **Goal:** Prometheus shows how many requests reach Overpass and for what, and which source answered each resolve.
- **Scope:** `src/services/overpass_service.py`, `src/utils/metrics.py`, `api/segments.py`, `docs/METRICS.md`, `tests/test_overpass_failover.py`, new `tests/test_route_source_metrics.py`.
- **Context:** `_overpass()` (`overpass_service.py:1437-1533`) and its five outcome branches (cache hit `:1462`, ok `:1525`, back-off `:1504`, unreachable `:1495`, cooling `:1470`); `get_rail_geometry` (`:353-402`) and the `RailGeometry` class (`:42`); `_compute_segment_geometry` (`api/segments.py:60-139`). Follow `JOB_RUNS` in `src/utils/metrics.py` for a labelled counter.
- **Do:**
  1. `_overpass(query, purpose)`; every caller passes one of `rail`, `rail_station`, `ferry`, `bus`. Counter `traxjourney_overpass_requests_total{purpose,outcome}`.
  2. `RailGeometry` gains `source` (`local` | `overpass`), set where `get_rail_geometry` decides.
  3. Counter `traxjourney_route_resolves_total{mode,source,degraded}` incremented once per resolve in `_compute_segment_geometry` (`motis` for a matched train).
- **Acceptance:**
  - Tests: each `_overpass` outcome increments its label once; a local rail hit counts `source="local"`, a local miss that falls back counts `overpass`; a MOTIS match counts `motis`; ferry and bus count `overpass`.
  - `pytest tests/test_overpass_failover.py tests/test_route_source_metrics.py tests/test_rail_source.py` passes.
- **Out of scope:** any change to what is requested or when; persisting the source.
- **Latitude:** none.
- **Escalate if:** a caller of `_overpass` exists that is none of the four purposes; a file outside Scope is needed.
- **Depends on:** —

#### U4 — The route corpus and its runner
- **Goal:** `scripts/route_corpus.py <store dir>` checks a YAML list of real legs and exits non-zero on any unexpected outcome.
- **Scope:** new `scripts/route_corpus.py`, new `config/route_corpus.yml`, new `tests/test_route_corpus.py`, new fixture under `tests/fixtures/`.
- **Context:** `_resolve_rail(stops, source)` with `LocalRailSource(dir)` is the entry point — no fallback, so nothing reaches the network. The legs and expectations in this plan's *Current state* were measured exactly this way. `config/rail_regions.yml` is the example of a config file the workflow reads.
- **Do:**
  1. Leg format: `name`, `mode: rail`, `from`/`to` (lat, lon, label), `regions`, `expect` — `max_endpoint_km`, `length_km: [min, max]`, `via: [{label, lat, lon, within_km}]`, `avoid: [...]`, `strategy_not: [straight]`, `degraded: false`; or `known_bad: <reason>` plus the current outcome.
  2. Seed: the five legs in *Current state*, Paris Est → Strasbourg (#359), Lyon → Marseille, Nantes → Rennes, Hamburg → Flensburg, Hamburg → Munich, Berlin → Cologne, Copenhagen → Aarhus, Copenhagen → Odense. Expectations measured against `rail-data-2026-09-08` stores built locally, reviewed by hand, and the measurement noted in the file's header.
  3. The runner skips legs whose regions are absent from the store dir and says so; with `--require-all` a missing region fails.
  4. Output: one line per leg, pass / fail / known-bad-unchanged / known-bad-changed, and a non-zero exit on fail or changed.
- **Acceptance:**
  - Tests against a store built from a fixture PBF: a passing leg, a failing `via`, a failing length band, a `known_bad` leg that stays bad (exit 0) and one that starts passing (exit non-zero), a missing region skipped and failed under `--require-all`.
  - The seeded corpus passes against `rail-data-2026-09-08` stores; the command and output are pasted in the PR.
  - `pytest tests/test_route_corpus.py` passes.
- **Out of scope:** fixing any leg; ferry and bus legs (U10).
- **Latitude:** local design.
- **Escalate if:** a seed leg cannot be expressed in the format; a seed leg is wrong today in a way not already known (record it `known_bad` and report it).
- **Depends on:** —

### Wave 2 — the age gauge and the gate

U2 follows U3 (both edit `src/utils/metrics.py` and `docs/METRICS.md`); U5 needs U4's runner. They share no file.

#### U2 — The server reports how old its rail data is
- **Goal:** a daily scheduled check exports the installed data's age and logs a `WARNING` while it is over 40 days or unreadable.
- **Scope:** new `src/jobs/rail_data_jobs.py`, `src/utils/metrics.py`, `api/router.py` (one `add_job`), `docs/METRICS.md`, new `tests/test_rail_data_age.py`.
- **Context:** `src/jobs/prepared_geo_jobs.py` — `sweep_unprepared_geometry` and its gauge — is the example: a self-contained function, a `try/except` that logs and never raises into the scheduler, a gauge set at the end. Manifest shape: `scripts/fetch_rail_data.py:361-405`.
- **Do:**
  1. `check_rail_data_age(directory=None) -> float | None`: reads `RAIL_DATA_DIR/manifest.json`; age in days = today − oldest `source_date` among `status == "ok"` entries.
  2. Gauges `traxjourney_rail_data_age_days` and `traxjourney_rail_data_regions{status}`; `multiprocess_mode="mostrecent"` as `PREPARED_GEOMETRY_BACKLOG`.
  3. `WARNING` when age > `RAIL_DATA_MAX_AGE_DAYS = 40`, naming the oldest region and its date; `WARNING` when `RAIL_SOURCE=local` and the manifest is missing or unreadable; nothing when `RAIL_SOURCE` is not local.
  4. Schedule daily, 05:15 UTC, `id="rail_data_age"`.
- **Acceptance:**
  - Tests: fresh manifest → age and no warning; one stale region among fresh ones → warning naming it; `empty` entries ignored; missing manifest under `RAIL_SOURCE=local` → warning, and silence when not local; a malformed manifest does not raise.
  - `pytest tests/test_rail_data_age.py tests/test_metrics*.py` passes.
- **Out of scope:** refreshing anything (U6); reading store `meta`.
- **Latitude:** local design.
- **Escalate if:** the scheduler test harness cannot see a new job without touching another file; a file outside Scope is needed.
- **Depends on:** —

#### U5 — The corpus gates the Rail extract publish
- **Goal:** a release is published only if the corpus passes against stores built from this run's extracts.
- **Scope:** `.github/workflows/rail-extract.yml`, `tests/test_rail_extract_workflow.py`, `docs/DEPLOYMENT_VPS.md` (§9, one paragraph).
- **Context:** the publish job's steps from "Build and verify the manifest" to "Publish the release"; `python -m src.rail.builder <pbf> <out> --region` builds a store (France ≈ 33 s).
- **Do:**
  1. After the manifest is verified and before any upload: build stores for the regions the corpus names that this run built, plus carried regions' stores from the base release when a subset run needs them; run `scripts/route_corpus.py`.
  2. A failure stops the job before upload; `force_publish` skips the gate and prints that it did.
- **Acceptance:**
  - New tests: the corpus step precedes every upload step; it is skipped only under `force_publish`; it is in the publish job, so no new job gets write access.
  - One `workflow_dispatch` subset run on the PR branch (`europe/france`) is green, with the corpus output visible in the log.
- **Out of scope:** changing the corpus.
- **Latitude:** local design.
- **Escalate if:** building the needed stores pushes the publish job past 30 minutes; a file outside Scope is needed.
- **Depends on:** U4.

### Wave 3 — the box refreshes itself

U6 needs U2's module and edits `docs/DEPLOYMENT_VPS.md`, as U5 does.

#### U6 — The box refreshes its rail data monthly
- **Goal:** each box installs the newest published release on its own, once a month, and the job metrics show whether it worked.
- **Scope:** `src/jobs/rail_data_jobs.py`, `api/router.py`, `.env.example`, `docs/DEPLOYMENT_VPS.md` (§9), `tests/test_rail_data_refresh.py` (new), `scripts/fetch_rail_data.py`, `tests/test_rail_data_fetch.py`.
- **Context:** U2's module; how `sweep_stale_resolver_segments` is scheduled and enqueues (`api/router.py:168-169`, `src/jobs/route_jobs.py:364`); `src/jobs/queue.py:157-204` — `enqueue`'s inline fallback when no broker is configured, which this job must refuse (`allow_inline=False`), and `docker-compose.yml.example:41-46` for why; `_installed_manifest` (`scripts/fetch_rail_data.py:377-389`); the refresh command in §9.
- **Do:**
  1. `enqueue_rail_data_refresh()`, scheduled cron `day=RAIL_AUTO_REFRESH_DAY` (default 5), `hour=4, minute=10`, `id="rail_data_refresh"`: does nothing unless `RAIL_SOURCE=local` and `RAIL_AUTO_REFRESH` is not `0`; else enqueues `run_rail_data_refresh` on `QUEUE_DEFAULT` with a timeout of 3,600 s and `allow_inline=False`. With no broker it logs a `WARNING` (refresh is manual on this deployment) and returns — it never runs the refresh in the API process (R1-4).
  2. `run_rail_data_refresh()`: `subprocess.run([sys.executable, "scripts/fetch_rail_data.py", "--dest", RAIL_DATA_DIR], timeout=3300)`; non-zero exit raises, so RQ records the failure; then calls `check_rail_data_age()` so the gauge moves the same day.
  3. §9: scheduled refresh replaces "deliberately no timer"; the manual command stays as the recovery path; val's `.env` sets `RAIL_AUTO_REFRESH_DAY=4`.
  4. Guard R1-6: `fetch_rail_data.py` logs a `WARNING` naming each region the previously installed manifest listed and the new release does not.
- **Acceptance:**
  - Tests: not enqueued when `RAIL_SOURCE` is not local or `RAIL_AUTO_REFRESH=0`; enqueued once otherwise; with no broker nothing runs in-process and a `WARNING` is logged; the cron day follows `RAIL_AUTO_REFRESH_DAY`; a non-zero script exit raises; the age check runs after a success; a release missing a previously installed region logs a `WARNING` naming it.
  - Measured on val before merge: peak RSS of the subprocess for a full rail refresh, pasted in the PR (bus is measured again in U7).
  - `pytest tests/test_rail_data_refresh.py tests/test_rail_data_age.py tests/test_rail_data_fetch.py` passes.
- **Out of scope:** choosing a release other than the newest published one.
- **Latitude:** local design.
- **Escalate if:** peak RSS exceeds 700 MB (the worker limit is 1 GB); a file outside Scope is needed.
- **Depends on:** U2.

### Wave 4 — stores and readers learn layers

Ships alone, before any layer data exists (*Boundaries crossed*).

#### U8 — Stores and readers learn layers (ships first)
- **Goal:** store schema 3 with `way.cls`, layer-aware store names, and a box that reads manifest schemas 2 and 3 — all before any layer data exists.
- **Scope:** `src/rail/store.py`, `src/rail/builder.py`, `scripts/fetch_rail_data.py`, `src/services/rail_source.py`, `docs/DEPLOYMENT_VPS.md` (§9), `tests/test_rail_store.py`, `tests/test_rail_store_schema2.py`, `tests/test_rail_data_fetch.py`, `tests/test_rail_source.py`, new `tests/test_rail_store_schema3.py`.
- **Context:** the schema 1 → 2 bump in PR #361 is the example to follow — `_SUPPORTED_SCHEMAS`, v1 roles read as `""`, the sidecar holding `<digest> <schema>`, and the three traps recorded for it in `docs/LOCAL_RAIL_DATA_PLAN.md`. `store_filename` (`store.py:110-115`) and its users (`fetch_rail_data.py:186-187`, `:264-276`, `:378-389`; `store.py:606`); `load_coverage` (`rail_source.py:164-208`).
- **Do:**
  1. Store schema 3: `way.cls` bitmask per Decision 9; `_SUPPORTED_SCHEMAS = (1, 2, 3)`, a v1/v2 `rail` flag read as `cls` bit 0. Every query that filters `w.rail = 1` filters `cls & 1`; `ways_in_bbox` / `vertex_counts_in_bbox` take a class mask defaulting to bit 0.
  2. The builder takes `--layer` (default `rail`), fills `cls` for that layer's selection, and refuses a store and measures its extent over the layer's **routable set** (Decision 9's table) — not bit 0 alone.
  3. `store_filename(region, layer="rail")`; rail names unchanged (R1-9).
  4. Manifest schemas `(2, 3)` on the box; entries keyed by `(region, layer)`, a missing `layer` read as `rail`; an unknown layer ignored with a `WARNING`. Fetch installs every layer's store; one sidecar per `(region, layer)`. A `(region, layer)` the previously installed manifest held and the new release omits is **carried** when its store and sidecar are on disk — as a failed install already is — and still named in U6's R1-6 `WARNING`, so the age gauge keeps seeing it age instead of losing it (R2-2). Carrying lasts **one release**: the carried entry is marked `carried: true`, and when the next release omits it again it is dropped from the installed manifest with a `WARNING` that it is treated as retired — a layer that failed in CI one month comes back, one removed from `config/rail_regions.yml` leaves without a manual step (R3-1).
  5. §9: the rollback-past-U8 recovery from *Boundaries crossed* (R2-6).
- **Acceptance:**
  - Tests for both ship orders: a schema-2 manifest still routes rail; a schema-3 manifest with ferry and bus entries leaves rail coverage and rail store names unchanged; v1, v2 and v3 rail stores answer the same rail queries; a way tagged `route=ferry` and `ferry=yes` is returned for bit 0 and bit 1, a `ferry=yes`-only way for bit 1 alone; the store schema bump makes fetch rebuild an existing rail store; a ferry store holding only `ferry=yes` ways and a bus store holding only relation members are built, not refused, with a non-empty extent; a layer the release omits is carried when its store is on disk and dropped when it is not; a layer omitted by two consecutive releases is dropped on the second with the retirement `WARNING`, and one that reappears is installed normally and loses its `carried` mark.
  - `pytest tests/test_rail_store*.py tests/test_rail_data_fetch.py tests/test_rail_source.py tests/test_rail_issue_359.py tests/test_rail_issue_363.py` passes.
- **Out of scope:** filtering ferry or bus in CI (U7); resolving from them (U9).
- **Latitude:** local design.
- **Escalate if:** a rail query's result changes on any existing store; a file outside Scope is needed.
- **Depends on:** —

### Checkpoint — owner deploys U8 to val and prod

Not a unit. Nothing in Wave 5 merges until both boxes run U8's reader: a box without it refuses every refresh once a schema-3 release exists, and keeps routing on data that only gets older (R3-2).

### Wave 5 — ferry and bus layers in CI

#### U7 — Filter ferry and bus layers in the extract build
- **Goal:** each region's build publishes up to three filtered files — rail, ferry, bus — from one download, and a missing ferry or bus layer never blocks the rail release.
- **Scope:** `scripts/build_rail_extract.py`, `.github/workflows/rail-extract.yml`, `tests/test_rail_extract.py`, `tests/test_rail_extract_workflow.py`, `tests/fixtures/` (fixture extended), `docs/LOCAL_RAIL_DATA_PLAN.md` (contract table).
- **Context:** `select()` and its predicates (`is_rail_way`, `is_route_relation`, `is_station`); the Phase 1/Phase 2 contract in `docs/LOCAL_RAIL_DATA_PLAN.md`; the selections in `_via_route_relation_type`, `_via_way_type_fallback`, `_via_ferry_yes_fallback`; the size guard at `rail-extract.yml:110-114`; the base-schema refusal at `build_rail_extract.py:709-721`.
- **Do:**
  1. Predicates for the ferry layer (`route=ferry` relations and their member ways; `route=ferry` ways; `ferry=yes` ways) and the bus layer (`route=bus` relations and member ways; `route=bus` ways).
  2. `select()` writes one file per layer in the same passes. A layer is `empty`, and publishes no file, when its **routable set** (Decision 9's table) is empty — the same set U8's builder refuses on and both measure the bbox over (R2-3).
  3. Entries carry `layer`; manifest schema 3. The `manifest` command accepts a schema-2 `--base` and carries its entries as `layer: rail` (R1-10).
  4. Completeness requires the `rail` layer only; a missing `ferry` or `bus` entry is a `::warning::` naming it, never a refusal — that region's mode keeps going to Overpass, as today.
  5. Size guard per layer: rail 100 MB as now, ferry 20 MB, bus 300 MB (R1-3).
  6. Record per-layer sizes for all 49 regions from one dispatch run, in the PR. In the same run, build Germany's bus store with U8's builder under `/usr/bin/time -v` and record its peak RSS (R1-7).
- **Acceptance:**
  - Tests: each layer holds exactly its selection on the fixture (a bus relation's member road is in `bus`, not `rail`; a `ferry=yes` way is in `ferry`); bbox per layer over its routable set; an empty layer is `empty`, while a ferry layer of only `ferry=yes` ways and a bus layer of only relation members are not; a missing bus entry warns and publishes while a missing rail entry refuses; a schema-2 base merges; the workflow guard's per-layer limits.
  - The full dispatch run's per-layer sizes and Germany's bus-store peak RSS are in the PR, and Open decision 3 is answered from them.
- **Out of scope:** readers (U8); resolving (U9).
- **Latitude:** local design.
- **Escalate if:** any single region's bus layer exceeds 250 MB filtered (raised from 150 MB by the owner on 2026-10-06, after Germany measured 160 MB; unused tags are now stripped, ~112 MB); Germany's bus-store build peaks above 700 MB RSS (the box builds it inside a 1 GB worker); a file outside Scope is needed.
- **Depends on:** U8 merged **and deployed** to both boxes.

### Wave 6 — ferry and bus resolve locally

#### U9 — Ferry and bus resolve from local stores first
- **Goal:** ferry and bus resolves read local stores through a source interface, falling back to Overpass on any local miss, and the resolve counter says which source answered.
- **Scope:** `src/services/overpass_service.py` (ferry/bus section), `src/services/rail_source.py` (or a new `src/services/route_source.py`), `api/segments.py`, new `tests/test_route_source_ferry_bus.py`, `tests/test_route_source_metrics.py`, `tests/test_overpass_fallback.py` (it indexes the getters' return value — R2-1).
- **Context:** phase 3 of `docs/LOCAL_RAIL_DATA_PLAN.md` and its implementation (`RailSource`, `LocalRailSource`, `OverpassRailSource`, `get_rail_geometry`'s fallback, `overpass_service.py:333-399`) is the example to mirror; U8's class mask; U3's resolve counter in `_compute_segment_geometry` (`api/segments.py:60-139`).
- **Do:**
  1. `RouteSource` with `relations_in_bbox(mode, bbox)` and `ways_in_bbox(mode, cls_mask, bbox)`; local and Overpass implementations; strategy A reads relations, B asks for bit 0, C for bit 1 — logic unchanged.
  2. Fallback: local result missing or the strategy chain fails → the same chain against Overpass, with the wide guard and the vertex ceiling as for rail.
  3. `get_ferry_geometry` / `get_bus_geometry` return the polyline with its source; `_compute_segment_geometry` labels the resolve counter with it (R1-8).
- **Acceptance:**
  - Tests on fixture stores: a ferry relation resolves locally with no network; a relation split across two regions resolves after the merge; a leg in neither region falls back to Overpass exactly once; a corrupt store falls back, never raises; a leg mapped only with `ferry=yes` resolves through strategy C locally.
  - The resolve counter shows `source="local"` for the local cases and `overpass` for the fallback.
  - `pytest tests/test_route_source_ferry_bus.py tests/test_route_source_metrics.py tests/test_rail_source.py tests/test_overpass_failover.py` passes.
- **Out of scope:** changing any strategy's logic or thresholds.
- **Latitude:** local design.
- **Escalate if:** a strategy cannot be expressed against the interface without changing its result; a file outside Scope is needed.
- **Depends on:** U3, U7, U8.

### Wave 7 — ferry and bus in the corpus

#### U10 — Ferry and bus legs in the corpus
- **Goal:** the corpus covers ferry and bus, including one cross-border ferry and one leg that must fall back.
- **Scope:** `config/route_corpus.yml`, `scripts/route_corpus.py`, `tests/test_route_corpus.py`.
- **Context:** U4's format; U9's entry points.
- **Do:** add `mode: ferry|bus`; seed Rødby → Puttgarden, Helsingør → Helsingborg, Split → Supetar, one Croatian island hopper using `ferry=yes`, one city bus line and one regional bus line; record each leg's current outcome, `known_bad` where it is wrong today.
- **Acceptance:** the runner passes against stores built from the U7 dispatch run; tests for the new mode field; output pasted in the PR.
- **Out of scope:** fixing legs.
- **Latitude:** local design.
- **Escalate if:** a leg resolves only through Overpass today and should not; a file outside Scope is needed.
- **Depends on:** U4, U9.

## Definition of done

- A failed `Rail extract` run opens a `rail-data` issue within the run, and the next green run closes it.
- `/metrics` exports `traxjourney_rail_data_age_days`, and the server logs a `WARNING` daily while it exceeds 40.
- `traxjourney_overpass_requests_total{purpose,outcome}` and `traxjourney_route_resolves_total{mode,source,degraded}` are exported and move under test.
- No release is published unless `scripts/route_corpus.py` passed against its own stores, or `force_publish` was set and the log says so.
- Both boxes install the newest release monthly without anyone logging in — val on the 4th, prod on the 5th — and the job metrics record the run; a deployment without workers logs that its refresh is manual instead of running it in the API process.
- Ferry and bus resolves inside coverage are answered locally (`source="local"`), and outside coverage fall back to Overpass.
- The corpus holds rail, ferry and bus legs, each `known_bad` entry names its reason, and it runs green in CI and locally.
- Open decision 2 is filed as an issue with the 30-day counter numbers attached.
