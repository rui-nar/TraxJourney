# Review ledger — Local transport data, phases 4–6 (#345)

Subject: docs/LOCAL_TRANSPORT_DATA_PLAN.md
Envelope: the plan's `## Review envelope` section (E7 GitHub Actions, E8 Geofabrik, scale and concurrency notes) on top of REVIEW.md defaults; E1 covers SQLite only (owner, 2026-09-28)

## Round 1 — 2026-10-04, reviewed at 0ce8ab09

Envelope question raised (to owner): both stacks refresh at 04:10 on the 4th — is the host-level sum of two simultaneous refreshes inside E1, or should val and prod be staggered?
Answer: not given at approval; the plan applies the recommended stagger (val on the 4th, prod on the 5th, via RAIL_AUTO_REFRESH_DAY) pending owner confirmation.

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
- Outcome: open

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
- Override: —
- Outcome: open

### R3-2 — The checkpoint's stated consequence is wrong
- Trigger: the orchestrator or owner reads "a schema-3 release reaching a schema-2 reader sends every train back to Overpass" → acts on an outage that cannot happen; in fact a pre-U8 box refuses the refresh (read_manifest raises before writing) and keeps routing on its installed stores while its data ages
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7) — also correct the same sentence in Boundaries crossed so the two agree
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open
