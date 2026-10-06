# Review ledger — Local transport data, phases 4–6 (#345)

Subject: docs/LOCAL_TRANSPORT_DATA_PLAN.md
Envelope: the plan's `## Review envelope` section (E7 GitHub Actions, E8 Geofabrik, scale and concurrency notes) on top of REVIEW.md defaults; E1 covers SQLite only (owner, 2026-09-28)

## Round 1 — 2026-10-04, reviewed at 0ce8ab09

Envelope question raised (to owner): both stacks refresh at 04:10 on the 4th — is the host-level sum of two simultaneous refreshes inside E1, or should val and prod be staggered?
Answer: not given at approval; the plan applies the recommended stagger (val on the 4th, prod on the 5th, via RAIL_AUTO_REFRESH_DAY) confirmed by the owner on 2026-10-05.

### R1-1 — Ferry strategies B and C need two way classes the rail store schema cannot express
- Trigger: a user adds an archipelago ferry leg mapped with both route=ferry and ferry=yes → the single `rail` flag forces B to run over both classes (different line, silently) or C to always miss locally → wrong polyline or permanent Overpass traffic
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap (triager: corrected from expensive — stores are derived), fix=M/shared, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (in plan)

### R1-2 — Local relations_in_bbox for bus/ferry strategy A has no size ceiling
- Trigger: a user adds a bus segment inside a capital → strategy A's box matches every bus relation → the local source decodes all member geometry unbounded → memory spike in the 1 GB worker
- Scores: trigger=concrete (triager: corrected from plausible), impact=degraded-ux (corrected from wrong-visible; OOM not shown), detect=logged, later=cheap, fix=M/shared, confidence=inferred
- Decision: Defer (D8)
- Revisit when: U10's city bus leg shows peak RSS above 300 MB during the resolve, or an OOM/worker kill is logged during a bus or ferry resolve — then add a blob-length ceiling in relation_geometry like _MAX_BBOX_VERTICES
- Guard: —
- Override: —
- Outcome: open

### R1-3 — Germany's projected 125 MB bus layer trips the build job's 100 MB guard
- Trigger: U7's dispatch builds europe/germany → ~125 MB bus file in dist/rail → "nothing raw leaves" guard fails the region → manifest refused → no release that month
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (in plan)

### R1-4 — The refresh runs inline in the API process when no broker is configured
- Trigger: a self-hoster on the documented no-worker compose sets RAIL_SOURCE=local → on the 4th, enqueue runs the refresh inline in the APScheduler thread → 49 store builds inside the API's 768 MB cgroup
- Scores: trigger=concrete, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (in plan)

### R1-5 — A successful subset rebuild closes the rail-data issue although the month's release was never published
- Trigger: the scheduled run fails → owner dispatches a subset recovery → it patches last month's release and succeeds → notify closes the issue → nothing says this month's release is missing until the 40-day warning
- Scores: trigger=plausible (triager: corrected from concrete), impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: (to add in U1) on success with inputs.regions set and the patched tag not of the current month, a ::warning:: and the same line in the closing comment: "subset run patched <tag>; no rail-data release exists for <YYYY-MM>"
- Override: —
- Outcome: guard added (in plan)

### R1-6 — The data-age alert cannot see a region dropped from the installed manifest
- Trigger: owner force-publishes a release missing one region → the box's refresh rewrites the installed manifest without it → that country routes via Overpass → the age gauge still reports fresh
- Scores: trigger=plausible, impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: (to add) fetch_rail_data.py logs a WARNING naming each region (region+layer after U8) the previous installed manifest listed and the new release does not
- Override: —
- Outcome: guard added (in plan)

### R1-7 — Refresh peak RSS is measured with rail only, before bus multiplies the data
- Trigger: the first schema-3 release lands → the monthly refresh builds Germany's ~125 MB bus layer with a with_locations() index in a 1 GB worker → OOM every month, bus never installs
- Scores: trigger=concrete (triager: corrected from plausible), impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (in plan)

### R1-8 — U9's acceptance needs api/segments.py, which U9 does not scope
- Trigger: U9's implementer finishes the local source → the resolve counter in _compute_segment_geometry is hard-coded to overpass for ferry/bus → acceptance impossible without an out-of-scope edit
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (in plan)

### R1-9 — U8 needs layer-aware store file names and per-layer keys outside its scope
- Trigger: U8's implementer installs a schema-3 release → store_filename(region) gives one name for every layer → collision, or the unit stalls on escalation
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/shared, confidence=verified
- Decision: Defer (D8)
- Revisit when: U8's implementer escalates on store_filename or the sidecar/carried keying — answer by adding store.py to U8's scope with store_filename(region, layer="rail") and (region, layer) keys
- Guard: —
- Override: user: Fix now — one plan line now; deferring only schedules a known escalation
- Outcome: fixed (in plan)

### R1-10 — After U7, a subset rebuild against a schema-2 release is refused
- Trigger: U7 merges mid-month → a subset recovery is needed → the manifest command refuses the schema-2 base → no recovery until the next full run
- Scores: trigger=plausible (triager: corrected from concrete), impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a subset recovery is needed while the newest rail-data release is still manifest schema 2 after U7 has merged
- Guard: —
- Override: user: Fix now — one plan line now; deferring leaves a known gap in the switchover recovery path
- Outcome: fixed (in plan)

### R1-11 — No ferry/bus-only switch; rolling back a bad local ferry answer sends rail to Overpass too
- Trigger: a ferry/bus release ships with a plausible-but-wrong crossing → the only rollback is RAIL_SOURCE=overpass → trains return to the instance that banned the box
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a local ferry or bus defect reaches production, or a U10 corpus leg is known_bad locally while Overpass resolves it correctly
- Guard: —
- Override: —
- Outcome: open

## Round 2 — 2026-10-04, reviewed at 6dca31ec (fixes only)

Triager flag (not a round-2 finding, outside the fixes reviewed): Wave 1 (U2 and U3 share src/utils/metrics.py and docs/METRICS.md) and Wave 2 (U5 and U6 share docs/DEPLOYMENT_VPS.md) also break DELIVERY.md W1. Owner approved fixing them with R2-5.

### R2-1 — U9 changes the ferry/bus getters' return shape; tests/test_overpass_fallback.py is outside its scope
- Trigger: U9's implementer returns polyline+source → test_overpass_fallback.py indexes the old list shape → the suite fails on an out-of-scope file and the unit stops
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (in plan)

### R2-2 — A ferry/bus layer the release omits is dropped from the installed manifest although last month's store is on disk
- Trigger: CI's bus filter fails for one region → the release publishes without (region, bus) (U7 step 4) → the box's installed manifest drops it → that country's bus goes to Overpass for a month while a valid store sits on disk
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — not a D2 duplicate of R1-6: U7 step 4 makes the omission the designed outcome of a single-layer failure
- Revisit when: the R1-6 WARNING fires for a ferry/bus layer whose store is still on disk, or two consecutive releases warn of the same missing layer — then carry an omitted (region, layer) whose sidecar and store are present
- Guard: —
- Override: user: Fix now — one plan line; U7 made the omission a designed outcome, so the box keeps last month's store
- Outcome: fixed (in plan)

### R2-3 — Ferry and bus "empty", extent and builder refusal are still defined "as for rail" (bit 0)
- Trigger: a bus layer's routable class is route=bus ways, which OSM rarely uses (bus routes are relations) → U7 marks most bus layers empty or U8's builder refuses them / computes a near-zero extent → no local bus anywhere
- Scores: trigger=concrete (triager: corrected from plausible), impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified (corrected from inferred)
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (in plan)

### R2-4 — "Redis unreachable at refresh time" is logged as "refresh is manual on this deployment"
- Trigger: prod has a broker; Redis is restarting at 04:10 on the 5th → enqueue returns False as with no broker → the job logs the manual message, records success, no retry until next month
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a rail_data_refresh run on a box with REDIS_URL logs queue.py's "not run" ERROR, or the age gauge passes 40 days there — then distinguish with queue_available() and raise or reschedule on a broker failure
- Guard: —
- Override: —
- Outcome: fixed (U6, delivery)

### R2-5 — Wave 3 holds a deploy gate and intra-wave dependencies DELIVERY.md's wave model cannot express
- Trigger: deliver-plan runs Wave 3's units in parallel → U7/U9/U10 depend on U8 in the same wave, U8 and U9 share rail_source.py → the orchestrator serialises by hand or integrates U7 before U8 is deployed (the schema-3-before-readers outage)
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (in plan)

### R2-6 — Rolling the API image back across U8 after a refresh refuses every rail store; no recovery named
- Trigger: owner deploys U8, the refresh rebuilds stores as schema 3, an unrelated defect forces an image rollback → the old reader refuses every store → all trains go to Overpass until fetch is re-run with the old image
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: an image rollback past U8 is needed — add the §9 runbook line (re-run fetch with the rolled-back image, --tag of the last manifest-schema-2 release once U7 has published)
- Guard: —
- Override: user: Fix now — one runbook line; it belongs in section 9 before U8 ships, not during an outage
- Outcome: fixed (in plan)

### R2-7 — Decision 2 still says the box refreshes on the 4th
- Trigger: a maintainer re-derives the threshold → Decision 2 says the 4th, Decision 5 says the 5th for prod → inconsistent cross-reference
- Scores: trigger=concrete, impact=cosmetic, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (in plan)

## Round 3 — 2026-10-05, reviewed at f2f2e301 (fixes only; last round under the cap)

### R3-1 — A retired (region, layer) is carried forever and pins the data-age gauge
- Trigger: owner removes a region or layer from config/rail_regions.yml → later releases omit it → the R2-2 carry rule keeps it with no end → its source_date never advances, the age gauge sits above 40 days naming it, a genuinely stale region hides behind it, and the old store keeps answering
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — not a D2 duplicate of R2-2: the carry rule's missing end condition is new
- Revisit when: a commit removes a region or layer from config/rail_regions.yml, Open decision 3 withdraws bus, or U2's WARNING names an entry absent from the config — give the carry rule an end and add a retire step to §9
- Guard: —
- Override: user: Fix now — one line in U8: carry for one release, drop with a retirement WARNING on the second omission
- Outcome: fixed (in plan)

### R3-2 — The checkpoint's stated consequence is wrong
- Trigger: the orchestrator or owner reads "a schema-3 release reaching a schema-2 reader sends every train back to Overpass" → acts on an outage that cannot happen; in fact a pre-U8 box refuses the refresh (read_manifest raises before writing) and keeps routing on its installed stores while its data ages
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7) — also correct the same sentence in Boundaries crossed so the two agree
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (in plan)

Review closed after round 3 (cap). Open deferrals: R1-2, R1-11, R2-4 — each with its revisit trigger above.

## Delivery

Branch `feat/345-local-transport-data`, base cc52978b (plan + origin/main e96f8613). Unit worktrees made by hand from the feature branch.

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Failed Rail extract run opens a rail-data issue | Opus | S3 | 1 | — | yes | R1-5 |
| U3 | Count Overpass requests by purpose, resolves by source | Sonnet | — | 2 | X3 (scope +3 test files) | yes | R1-8 |
| U4 | Route corpus and runner | Opus | S2 | 1 | owner question (Hamburg → Munich) | yes | — |
| U2 | Daily rail-data age check | Opus | S5 | 1 | — | yes | R1-6 |
| U5 | Corpus gates the Rail extract publish | Opus | S3 | 1 | — | yes | — |
| U6 | The box refreshes its rail data monthly | Opus | S5 | 1 | — | yes | R1-4, R1-6, R2-4 |
| U8 | Stores and readers learn layers (store schema 3) | Opus | S4 | 1 | — | yes | R1-1, R1-9, R2-2, R2-6, R3-1, U8R1-1 |
| F1 | Correct the manifest sort comment | Sonnet | — | 1 | — | yes | U8R1-1 |
| F2 | Age warning names a stale ferry/bus layer | Sonnet | — | 1 | — | yes | U8R1-2 |
| U7 | Ferry and bus layers in the extract build | Opus | S3 | 1 | escalate (Germany bus 160 MB, store build 2.1 GB) → owner | yes (code) | R1-3, R1-7, R1-10, R2-3 |
| F4 | Ferry/bus files keep only the tags the builder reads | Opus | S3 | 1 | — | yes | — |
| F3 | Store builder runs in bounded memory | Opus | S4 | 1 | — | yes | R1-7 |

Notes:
- U1: the `rail-data` label did not exist; owner approved creating it (created 2026-10-05). `_working_bash()` in the workflow test now prefers Git Bash on Windows (a bare `bash` is the WSL launcher and drops env vars); no change on Linux.
- U3: `_overpass(query, purpose)` broke one-argument mocks in test_rail_source.py, test_vr_hafas.py and test_resolve_route_async.py; Scope widened to them (no assertion changed). Orchestrator decision: count every `_overpass` outcome — added `no_slot`, `client_error`, `bad_body`. Verifier note: a ferry/bus/rail resolve that raises is not counted in `traxjourney_route_resolves_total` (no answering source); accepted, the request counter still counts its Overpass traffic.
- U4: Hamburg → Munich resolves via Berlin–Leipzig–Erfurt (934 km) rather than Hannover–Würzburg (~790 km); owner: known_bad, the direct line is the expected answer. Corpus passes unchanged on rail-data-2026-09-08 and rail-data-2026-10-05. The gate (U5) must build seven regions the legs name: France, Spain, Germany, Denmark, Austria, Netherlands, Sweden.
- Wave 1 integration (45220d04): full suite 6112 passed, 39 skipped (py3.14 CI container).
- U2: also runs once at start-up (`next_run_time=now`, API process only) so the gauge exists right after a deploy; the age gauge is NaN when the manifest is unusable.
- U5: the gate builds every corpus region the merged manifest holds (carried ones fetched from the patched release and sha256-checked), `--require-all` on full runs only; publish installs requirements.txt. Live check (owner-approved): dispatch 37278688216, `europe/france` subset on d39d9d6a — all jobs green, corpus 11 pass / 1 known-bad-unchanged / 0 skip, publish job 1 m 19 s; it patched France in rail-data-2026-10-05.
- Wave 2 integration (d39d9d6a): full suite 6140 passed, 39 skipped.
- U6: distinguishes "no broker" (one WARNING a month, returns) from "broker refused the job" (raises, so the job metrics record the month as failed) — this resolves deferred finding R2-4 inside the unit's latitude. Peak RSS of a full 49-region refresh of rail-data-2026-10-05, Linux container: 361 MB, 162 s, 304 MB on disk; an up-to-date re-run 54 MB / 1.5 s. Val measurement owed by the owner at the U8 checkpoint.
- Wave 3 integration (75505d64): full suite 6166 passed, 39 skipped, 1 failed — tests/test_video_camera.py::test_ninety_seconds_of_a_long_trip_is_fast, a timing test under machine load; passed 3/3 alone; the branch does not touch src/video.

## Unit review U8 — Round 1 — 2026-10-05, reviewed at 2a8e9cd4 (t345/u8, DELIVERY.md §5 point 3)

Reviewer hand-off note: `.github/workflows/rail-extract.yml:284` (U5's gate) calls `store_filename(e["region"])` with no layer — U7 must pass the layer before it publishes a schema-3 manifest.

### U8R1-1 — Sort-order comment says pre-layer readers key the installed manifest by region; only the old fetch does
- Trigger: a maintainer relies on the comment at fetch_rail_data.py:500-502 → after a rollback the old load_coverage lists every entry (a bus-only region as rail coverage, deduped, logged) → the stated property is false for two of three old readers
- Scores: trigger=plausible, impact=maintainability, detect=logged (triager: corrected from silent), later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: an image rollback past U8 happens, or the manifest sort is next touched — reword the comment to name only the old fetch's region-keyed lookup
- Guard: —
- Override: user: Fix now — one-line comment fix before U8 merges
- Outcome: fixed (F1)

### U8R1-2 — Age-gauge warning names a carried ferry/bus layer by region only
- Trigger: once U7 publishes layers, a ferry layer fails in CI and is carried with an old source_date → the age WARNING names "europe/denmark" → the operator checks Denmark's rail, finds it current; the stale layer is not named
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/shared, confidence=verified
- Decision: Defer (D10)
- Revisit when: U7 publishes the first release with ferry or bus layers, or the age warning names a region whose rail entry is current — add the layer to the oldest tuple and message in src/jobs/rail_data_jobs.py
- Guard: —
- Override: user: Fix now — U7 is next after the deploy and creates exactly this case; a one-line change
- Outcome: fixed (F2)
- U8: the box's installed manifest stays manifest schema 2 (entries may carry layer / carried / omitted_by) so U2's age check reads it unchanged; readers accept manifest schemas (2, 3); coverage is per layer (`load_coverage(dir, layer="rail")`). On real data, 14 stores rebuilt at schema 3 answer every rail query identically to their schema-2 originals; corpus 11 pass / 1 known-bad on both. Verifier: every changed assertion in existing tests traces to the plan. Unit review (§5 point 3): 2 findings, both owner-overridden to Fix now (F1, F2).
- Wave 4 integration (099a6d3a): full suite 6202 passed, 39 skipped.
- CHECKPOINT: owner deploys U8 to val and prod before Wave 5 (U7) merges.
- U7 escalated on its own Germany run: bus layer 160 MB filtered (> 150 MB) and the U8 builder peaked at 2,127,220 kB building Germany's bus store (> 700 MB; the box builds in a 1 GB worker). Owner (2026-10-06): fix the builder first (F3) and raise U7's size line to 250 MB with unused tags stripped (F4). Results: Germany bus 160 → ~112 MB filtered; bus-store build 2.1 GB → 418 MB (verifier re-measured 416 MB); rail-store build 337 → 131 MB; every table identical to the old builder's on Germany rail/bus/ferry and the 7 corpus regions. Germany's bus store is 660 MB on disk (input to Open decision 3). Owner: F3 ships with part 2, not PR #563 — so the first ferry/bus Rail extract dispatch waits until part 2 is on prod.
- U7 notes for U9: ferry/bus files carry no stop nodes (`_bridge_to_named_stops` reads stop positions from Overpass's `out geom`); ferry bboxes are wide (Denmark −6.8…25.2 lon). `osmium.index.IdSet` is dense (1.5 GB per 300k scattered ids) — Python sets used instead.
- Orchestrator incident (2026-10-06 09:56): a one-off check's failed `cd` let `git reset --hard` run in the shared main checkout; HEAD unchanged, no staged blob lost (fsck), checkout believed clean. Lesson recorded.
- Wave 5 integration (550515ff): full suite 6249 passed, 39 skipped, 1 failed — the same load-sensitive video timing test as wave 3; passed 3/3 alone.
