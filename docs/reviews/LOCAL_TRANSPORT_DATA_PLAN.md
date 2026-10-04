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
