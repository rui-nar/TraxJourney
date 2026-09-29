# Review ledger — Trip video camera options and sharper overlay (#518)

Subject: docs/VIDEO_CAMERA_QUALITY_PLAN.md
Envelope: plan section "Review envelope" (REVIEW.md defaults + the trip video envelope of docs/TRIP_VIDEO_PLAN.md; render time and tile cost in scope)

## Round 1 — 2026-09-29, reviewed at a526a909

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

Envelope question (to the user, not triaged): with D3 lowering one zoom until tiles fit, a trip with a long flight or train sets the zoom for every walk too. Should long legs be flown (pull-out) rather than followed at the fixed zoom, so the fixed zoom stays near the median?
Owner answer: implement both, selectable — `fixed` (pull out for fast legs) and `fixed_strict` (one zoom throughout).

### R1-1 — Fixed mode has no pan-speed bound: a flight at the median zoom cuts every frame (strobing map) or drags every leg's zoom down via the tile budget
- Trigger: user with walks + one 1,000 km flight picks Fixed → aim moves ~0.6 frame/frame > CUT → every flight frame is a cut, no fly-to → basemap teleports each frame
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D3: integer zoom, FIXED_MAX_PAN; owner chose both approaches: `fixed` pulls out for fast legs, `fixed_strict` lowers Z until no leg is fast; U2 tests)

### R1-2 — D9 basemap reuse hands the overlay the same Image twice; the overlay draws in place, so frames accumulate
- Trigger: Overview render (or any mode's cards) → cached basemap image reused → marker trail, card darkens, HUD stacks
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D9 + U3 step 3: copy each frame; accumulation test)

### R1-3 — Overview (and cards) put the supersampled route box at ~85% of the frame; no benchmark measures that mode
- Trigger: 90 s 1080p Overview → ROUTE_SS× layer over ~1632×918 every frame → well past +25% on the VPS while the variable-mode benchmark stays green
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local (triager: was M), confidence=inferred
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (bench --camera; D8/U3/U4/G2 measure overview as the worst case)

### R1-4 — No VPS measurement after wave 2; at +25% a 90 s 1080p render is ~1,641 s vs the 1,800 s timeout
- Trigger: deploy wave 2 → render on a busy host → > 1,800 s → killed, "took too long"
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible (triager: was logged), later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: the G1 figure scaled by wave 2's measured overhead exceeds 1,600 s, or any video job hits JOB_TIMEOUT_S in production
- Guard: —
- Override: user: Fix now — without a VPS run after wave 2 the definition of done can't be checked
- Outcome: fixed in plan (Gate G2 after wave 2; DoD projects < 1,600 s)

### R1-5 — The fixed zoom isn't floored; a fractional zoom in the top 20% of a level cross-fades two bands on every frame
- Trigger: Fixed on a trip with median fit 11.9 → two bands per frame → double sheets, tiles and blend work
- Scores: trigger=concrete, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D3: Z floored to an integer; estimate models the fade)

### R1-6 — G1 runs the bench with docker compose exec inside the live worker-video container (shared 896 MB)
- Trigger: owner runs the bench while a user's job starts → two renders in 896 MB → OOM kills one
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible (triager: was logged), later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: the bench becomes routine rather than the one-off G1 run, or a video job is OOM-killed during a bench run
- Guard: —
- Override: user: Fix now — one-line doc change (docker compose run --rm)
- Outcome: fixed in plan (U1 step 3 and G1: docker compose run --rm)

### R1-7 — G1's frame pairs use today's encoder settings, so -tune animation is chosen unmeasured
- Trigger: G1 dumps frames at crf 23 without tune → wave 2 ships -tune animation on Mapbox imagery → softer labels, found after release
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (bench --crf/--tune; G1 dumps candidates; D7 picks by measurement)

### R1-8 — U4's alpha-level-count acceptance rests on a false baseline and doesn't measure the staircase
- Trigger: implementer writes the test as specified → passes on unchanged code or fails on correct code → loosened until green
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U4: edge-straightness residual, shown failing on today's code)

### R1-9 — U6's negative control can't hold for the overview end-card golden (identical to variable)
- Trigger: implementer adds overview_end + swap check → identical frame → acceptance unsatisfiable, dropped or bent
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U6: negative controls limited to frames that differ; end-card identity asserted)
