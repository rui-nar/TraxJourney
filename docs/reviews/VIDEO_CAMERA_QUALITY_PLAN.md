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

## Round 2 — 2026-09-29, reviewed a526a909..707cf012

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

### R2-1 — FIXED_MAX_PAN = 0.15 exceeds what the spring can follow (≈0.05/frame) and the test's 0.1 follow bound; the marker is leash-pinned off-centre
- Trigger: user picks Fixed on a trip with a leg panning 0.05–0.15 frame/frame → spring lag > LEASH → marker pinned 0.3 off-centre for the whole leg
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D3: FIXED_MAX_PAN_PER_S = 1.5 frame widths/s ≈ 0.05/frame from LEASH·ω/2; U2 cites the 0.1 follow bound and adds a leash-clamp check)

### R2-2 — D3's fast set, median and tile-lowered Z depend on each other with no evaluation order; several valid results
- Trigger: trip with walks and several slow long legs → one reading gives Z=6 (strict-like), another Z=10 with long legs flown; tests pass either way
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D3: one top-down search order recomputing fastness per z; U2 test separating fixed from fixed_strict)

### R2-3 — G1 asks for seven full 90 s 1080p renders on the shared VPS to pick CRF/tune, a choice that doesn't depend on the VPS
- Trigger: owner runs G1 → ~2.6 h saturating 2 vCPUs of prod+val → a user's job in that window runs at half speed toward the timeout
- Scores: trigger=plausible, impact=wrong-visible (triager: was degraded-ux), detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a user video job is queued/running when G1 is about to start; a video job hits JOB_TIMEOUT_S or is visibly slowed during G1; G1 has to run more than once
- Guard: —
- Override: user: Fix now — comparing encoder settings doesn't need the production host
- Outcome: fixed in plan (G1 = one VPS timing run; encoder candidates compared on the dev box)

### R2-4 — D8's overview budget has no 'before' figure (overview can't render before U3; G1 is variable-only) and U4 can't benchmark overview
- Trigger: orchestrator reaches U3/U4 acceptance or G2 → nothing to compare overview against; U4 can't render overview
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D8: overview baseline = variable-before × 1.25; U4 times Overlay.draw at an overview shot, depends on U2; full overview check at wave-2 integration and G2)

### R2-5 — U6's fixed vs fixed_strict negative controls are unsatisfiable on the reference fixture (both give Z=9, identical frames)
- Trigger: implementer uses the Paris–Lyon fixture → no leg fast at Z → identical frames → control fails on correct code
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U6: dedicated Paris–New York fixture with a precondition that the two fixed zooms differ)

## Round 3 — 2026-09-29, reviewed 707cf012..e9601589

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus). Round 3 is the cap (REVIEW.md §6).

### R3-1 — The pan bound is in frame widths only; a north–south leg below it still leash-pins the marker vertically
- Trigger: user picks Fixed zoom on a trip with a mostly north–south leg at ~1.2 frame widths/s → not "fast", followed at Z → vertical lag 2v/ω·16/9 ≈ 0.43 frame heights > LEASH 0.3 → marker pinned off-centre for the whole leg
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7) — not a duplicate of R2-1 (new evidence: the vertical axis); approved by the user 2026-09-29
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D3: fast = larger of width and height speeds, as _off_frame; U2: north–south leg test)

## Review closed — 2026-09-29

Three rounds; all Fix now items fixed in the plan (overrides: R1-4, R1-6, R2-3). The user approved R3-1 and asked to prepare the plan for execution; no round 4. Nothing deferred remains open.

## Unit U3 review, round 1 — 2026-09-29, diff e949d83a..a7e14706 (before integration, DELIVERY.md §5 point 3)

Reviewer: adversarial-reviewer (Fable). No findings, no envelope questions.

## Integrated review, round 1 — 2026-09-30, feat/518-video-camera-quality at 0288f214 vs ca5355f3 (DELIVERY.md §5 point 2)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

Envelope question (to the user, not triaged): during `docker compose up -d` the API and worker-video are recreated separately; an old worker could briefly render a job stored with a new camera value as variable (silently). Is deploy ordering in scope (an ordering note would need an Upgrade-Note, which the DoD says isn't needed)?
Owner answer (2026-09-30): accept it — the window is seconds long, around a deploy only, and yields a normal variable video; no Upgrade-Note.

### F1-1 — In both fixed modes a clip with no followed sub-leg is parked at the clip centre at Z, so its legs and the marker can play off-screen
- Trigger: Fixed zoom on a 30–60 s video of a multi-day trip; a day of legs each < 1 s (not followed) is aimed at the clip centre at Z → drives/marker outside the frame for the whole clip
- Scores: trigger=concrete, impact=wrong-visible (triager: was degraded-ux), detect=user-visible, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D6) — approved by the user 2026-09-30
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (F-a, 57861420)

### F1-2 — The Camera SegmentedButton's three segments don't fit the dialog on phones; 'Fixed zoom' wraps
- Trigger: Android/iOS user on a 360–412 dp phone opens Create video → content ~232–284 dp → 'Fixed zoom' squeezed
- Scores: trigger=concrete, impact=cosmetic (triager: was degraded-ux), detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7) — approved by the user 2026-09-30
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (F-c)

### F1-3 — Overview mode puts route corners and the finale marker under the translucent HUD panels
- Trigger: Overview render of a one-way diagonal trip → its end corner lies under the bottom-right counters panel
- Scores: trigger=concrete (triager: was plausible), impact=cosmetic, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7) — approved by the user 2026-09-30
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (F-d, 48213b65) — at shipped 16:9 sizes Paris–Lyon never reached a panel; guard uses a SE-ending trip at 1280×720; diagonal trips reaching opposite corners take the draw-over path

## Delivery

Feature branch `feat/518-video-camera-quality` (from `plan/518-video-camera-quality` at 68d874f9). Split approved by the user 2026-09-29 without overrides. Gates G1 (after wave 1) and G2 (after wave 2) are owner-run benchmarks on the VPS.

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Stage timings + VPS bench CLI | Sonnet | — | 1 | — | yes | — |
| U2 | Overview / fixed / fixed_strict camera paths | Opus | S2 | 1 (+resume after X2) | X2 → owner amended D3 (width-dependent fixed_strict floor) | yes | — |
| U1a | ffmpeg's own peak memory in the render summary (fix unit: G1 showed identical RSS figures) | Sonnet | — | 1 | — | yes | — |
| U3 | Camera mode through API/job/renderer; basemap reuse; encoder defaults | Opus | S4 | 1 | — | yes | — |
| U4 | Sharper route, chips and panels | Opus | S2 | 1 | — | yes | — |
| U5 | Camera choice in the video dialog | Opus | S5 | 1 | — | yes | — |
| U6 | Golden frames for the new modes, docs | Sonnet | — | 1 | — | yes | — |
| F-a | Fixed modes follow the marker on short-leg runs (fix unit for F1-1) | Opus | S2 | 1 | X2 on the F1-3 part → owner moved F1-3 to F-d | yes | F1-1 |
| F-c | Camera choice as a vertical list (fix unit for F1-2) | Sonnet | — | 2 | escalated: labels couldn't fit → owner chose a vertical list | no | F1-2 |
| F-d | Overview HUD panels keep clear of the route (fix unit for F1-3) | Opus | S2 | 1 | — | yes | F1-3 |
| U1b | Stage timing coverage test on a longer render (fix unit: PR #521 CI failed on a timing test that only 90 frames made fragile) | Sonnet | — | 1 (+resume after session restart) | — | yes | — |

U2 escalation (X2, 2026-09-29): D3's `fixed_strict` floor of 2 gave a 337°-wide frame at 1920 px on the Tokyo–Los Angeles test, contradicting the antimeridian test's < 180° check. Owner chose option (a): a width-dependent floor `max(2, ceil(log2(width/256)))`; D3 amended; U2 resumed with the rule.

Wave 2 integration (2026-09-29): full pytest 5348 passed / 0 failed. Paired 1080p variable benchmark on the dev box, before wave 2 vs after: 93.6 vs 98.8 ms/frame (+5.6%); overview after: 52.6 ms/frame (ceiling ≈117). A first unpaired run showed 140.4 ms/frame; the paired runs showed it was machine noise.

Gate G2 (2026-09-29, VPS, owner-run): variable 257.4 ms/frame (changed stages overlay+write 89.4 → 143.2, +17% of G1's total, within D8); overview 110.6 ms/frame, 9 tiles; projected 90 s 1080p ≤ ~920 s < 1,600 s. Recorded in docs/VIDEO.md. Wave 3 may start.

U5: verifier confirmed no consent/decryption/geometry logic changed (only a `camera` argument passed through), so no unit review under DELIVERY.md §5 point 3.

Integrated-review fix wave (2026-09-30):
- F-a (Opus) fixed F1-1 (57861420): a non-followed run in a clip wider than the frame at Z follows the marker, within a chase bound; runs the camera can't follow smoothly keep today's framing. Owner: keep as is (no pull-out for them).
- F-a escalated F1-3 (camera-side fix breaks the single-view overview test, D2's wording and raises tiles 12 → 33). Owner chose option (c): fix it in the overlay. New unit F-d (Opus): in overview mode, put the HUD panels in the corners the route doesn't reach; if it reaches all four, draw route and marker over the panels.
- F-c (Sonnet) escalated F1-2: "Overview" (67 px) can't fit a 53 px segment at 360 dp even with "Follow"/"Fixed". Owner chose a vertical list of the three camera choices, each with a one-line description, and the fixed-zoom switch under Fixed.

Fix-wave checks at 5be15b77 (2026-09-30): Linux-image video tests 352 passed; flutter analyze clean, flutter test 1819 passed; pytest 5191 passed with 0 failures from this change. The only failures (24 failed + 54 errors) were the rail-data tests (test_rail_data_fetch/source/store, plus test_rail_extract and test_rail_store_schema2 at collection): Windows Smart App Control blocked osmium's native module on the dev box. Those files are untouched by this delivery and CI runs them on Linux. Docker Desktop crashed once (WSL bootstrap 0xc00000fd); the owner restarted it.

## Integrated review, round 2 — 2026-09-30, fixes 0288f214..5be15b77

Reviewer: adversarial-reviewer (Fable). No findings, no envelope questions. Review closed.

### Delivery summary — 2026-09-30

- **Units:** 6 plan units and 4 fix units (U1a after G1 found invalid memory figures; F-a, F-c and F-d for the integrated review). By model: Opus 7 (U2, U3, U4, U5, F-a, F-d, and the resumed U2), Sonnet 3 (U1, U1a, U6, F-c — F-c on two attempts). Escalations: U2 (X2, owner amended D3 with the width-dependent fixed_strict floor), F-a on its F1-3 part (owner moved F1-3 to the overlay, F-d), F-c (owner chose a vertical list). No X1 re-routes.
- **Gates:** G1 on the VPS (variable 317.4 ms/frame, tile fetch 40%) plus the dev-box encoder comparison, which chose crf 20 with no tune. G2 on the VPS: variable changed stages +17% of G1's total (within D8), overview 110.6 ms/frame, 9 tiles.
- **Reviews:** plan (3 rounds), U3 unit review (clean), integrated round 1 (3 Fix now, all fixed), integrated round 2 (clean).
- **Final checks:** pytest 5,191 passed with 0 failures from this change (rail tests blocked locally by Smart App Control; they run in CI); Linux-image video tests 352 passed; flutter analyze clean; flutter test 1,819 passed.
- **Open:** none in this ledger. Related issues: #517 (render time, numbers posted), #519 (preview), #520 (two-lane workers, memory note posted).
- **Environment lessons:** automatic worktrees start from main, so create them by hand from the feature branch. Another session switched the main checkout, so the delivery ran from its own worktree. Benchmarks on the dev box must be paired: a lone run read 140 ms against 93. Docker Desktop crashed or auto-updated twice. Smart App Control now blocks osmium locally.

PR #521 CI (2026-09-30): the `test` job failed on one test only, test_video_bench.py::test_stage_totals_sum_to_the_measured_wall_time (0.605 s against 0.673 s, 10.1% off on a 90-frame render). Fix unit U1b renders the full 900-frame timeline and asserts that the stages cover 85–101% of the wall time (measured 0.969–0.976). It still fails when the stage figures are zeroed.
