# Review ledger — Trip video preview (#519)

Subject: docs/VIDEO_PREVIEW_PLAN.md
Envelope: plan section "Review envelope" (REVIEW.md defaults + the trip video envelopes of docs/TRIP_VIDEO_PLAN.md and docs/VIDEO_CAMERA_QUALITY_PLAN.md; previews on the shared `default` queue in scope)

## Round 1 — 2026-09-30, reviewed at 88295660

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

Envelope question (to the user, not triaged): 8 fps doesn't divide 30 fps, so preview frames can only be the nearest video frame (±1/60 s). Accept nearest-frame sampling, or use 6 or 10 fps so every preview frame is an exact video frame?
Owner answer (2026-09-30): 8 fps with nearest-frame sampling.

### R1-1 — The preview's camera isn't the video's: absolute zoom floors/caps and per-frame thresholds are evaluated at 320×180 / 8 fps
- Trigger: user previews a Follow/Fixed video with hikes/rides, likes it, spends a video → clips framed 3–6× wider and cut/flown differently; nothing warns
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=M/shared, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D1, Current state, Convention 3, U2: camera built at target size and 30 fps, sampled, zoom offset; preview request carries the resolution; U4 marks stale on resolution change)

### R1-2 — Sweeps and the killed-horse handler email 'could not be made' for a failed preview
- Trigger: preview worker OOM-killed/restarted or preview goes stale → failure email for a video never requested
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a preview row is failed by the hourly sweep or the killed-horse handler; a preview hits its 300 s timeout; a user reports a failure email for a video never requested
- Guard: —
- Override: user: Fix now — small, cheap fixes worth making before the preview ships
- Outcome: fixed in plan (D7, U3 3 and acceptance: no email on any preview path)

### R1-3 — A lost preview spins in the dialog up to ~2 h: no client deadline or terminal handling; only the hourly sweep cleans up
- Trigger: deploy/restart during a preview → row stays running/pending, dialog spins, rate-limit slot used
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: #520 gives previews their own lane; a stuck preview spinner is seen after a deploy
- Guard: —
- Override: user: Fix now — small, cheap fixes worth making before the preview ships
- Outcome: fixed in plan (U4 deadlines and failed branch; D6 15-minute rule; envelope updated)

### R1-4 — On Flutter web in Safari/Firefox the animated WebP may show only its first frame
- Trigger: Safari/Firefox user taps Preview → a still title card
- Scores: trigger=plausible (triager: was concrete — engine fallback may animate; unverified either way), impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=inferred
- Decision: Defer (D10) — flagged: floor-adjacent rescoring
- Revisit when: the val check of U4 in Safari/Firefox shows a still image (then concrete, D6)
- Guard: —
- Override: user: Fix now — small, cheap fixes worth making before the preview ships
- Outcome: fixed in plan (U4: web shows the WebP through an <img> HtmlElementView; gate G2 checks Chrome/Safari/Firefox/Android)

### R1-5 — The preview routes omit ?owner=, so a companion on a shared trip gets 404
- Trigger: companion taps Preview on a shared trip → 404 while Create video works
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager: was inferred)
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U3: owner: OwnerParam on all preview routes; companion test)

### R1-6 — The rate limit is checked after consent and the timeline, so an over-limit request costs a consent dialog and a timeline build before the 429
- Trigger: user at 10/hour on an encrypted trip → decrypt, upload, timeline, then 429
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: preview 429s appear more than occasionally; the limit is lowered; a consent-then-429 is reported
- Guard: —
- Override: user: Fix now — small, cheap fixes worth making before the preview ships
- Outcome: fixed in plan (D6 and U3: non-authoritative check before consent; previews_left in the 409 body)

### R1-7 — D5's 'default jobs are short' is false (resolves up to 600 s, posters minutes); the client can't tell queued from rendering
- Trigger: a resolve and a poster occupy both default consumers → preview pending for minutes under 'Rendering preview…'
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — D5's wording to be corrected with the R1-1 edit
- Revisit when: #520 lands; at G1/val the queue wait often exceeds the render budget; a long misleading spinner is reported
- Guard: —
- Override: user: Fix now — small, cheap fixes worth making before the preview ships
- Outcome: fixed in plan (D5 reason corrected; U4 shows waiting vs rendering)

## Round 2 — 2026-09-30, reviewed 88295660..feaaf959

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

### R2-1 — U1 still specifies the round-1 rate-limit count, so D6's 15-minute rule has no unit implementing it
- Trigger: U1 implements the old count → a lost preview counts for the hour; U3 expects the new rule but entitlements.py is outside its scope
- Scores: trigger=concrete, impact=maintainability (triager: was wrong-visible), detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U1 Do 4 and acceptance implement the 15-minute rule)

### R2-2 — 'A FrameRenderer fed with those shots' leaves the frame state sampled at n/30 s, not at the sampled video frame
- Trigger: U2 feeds shots to a FrameRenderer → marker, route progress, HUD and card fade at video time n/30 s under the camera of frame round(n×30/8)
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U2: state sampled at round(n×30/8)/30 with the shot; FrameRenderer renders explicit (shot, state) pairs; state test)

### R2-3 — U2's 'frame matches the video's frame downscaled' test can't pass (different tile bands, overlay size clamps), so it would be loosened
- Trigger: U2 writes the test with fake_tile → every frame fails the golden tolerance
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U2: pixel test against a direct 320×180 render of the same (shot, state), not a downscaled video frame)

### R2-4 — The dialog gives up on a running preview at 3 min while the server allows 5 min
- Trigger: slow render under load passes 3 min → dialog says 'took too long', the job finishes unseen and still counts; the retry repeats it
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: G1 or val logs show a preview running > 120 s; the budget is raised or fps falls back; a 'took too long' then duplicate render is reported; #520 changes the job timeout
- Guard: —
- Override: user: Fix now — client and server must agree on when a preview has failed
- Outcome: fixed in plan (U4: running deadline 6 min, past the 300 s job timeout)

## Round 3 — 2026-09-30, reviewed feaaf959..630cd90c

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus). Round 3 is the policy cap.

### R3-1 — The counting rule makes failed previews free, so a preview that times out or OOMs can be retried without limit on the shared default queue
- Trigger: a preview killed at the 300 s job timeout or OOM-killed → user retries repeatedly → each attempt ends failed, which the limit ignores → one default consumer held up to 5 min per tap
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: G1 or val logs show a preview hitting the 300 s timeout or being OOM-killed; one user has 3+ failed previews in an hour; default jobs reported waiting behind previews; #520 slips and previews stay on default in production
- Guard: —
- Override: user: Fix now — a failed attempt still used a worker
- Outcome: fixed in plan (D6 and U1 count failed previews that started)

### R3-2 — The camera test's negative controls cannot fail for overview (either control) or fixed/fixed_strict (the 320×180 control) on the synthetic trip
- Trigger: U2 writes the required negative controls for every mode → overview and fixed modes are size/fps-invariant by construction → the controls pass → dropped, or acceptance unmet
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U2 negative controls required for variable only, invariance stated)

### R3-3 — The pixel test does not say whether frame k is decoded from the lossy WebP or taken before encoding
- Trigger: U2 compares decoded q50 WebP frames under the golden tolerance → 4:2:0 smears 2-px lines past it → tolerance loosened; or it compares pre-encode frames and never checks the WebP
- Scores: trigger=plausible, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: U2's pixel test fails on decoded WebP frames; an implementer proposes loosening _differs or a preview tolerance; U2 lands a pixel test that never checks the WebP content
- Guard: —
- Override: user: Fix now — remove the ambiguity before delivery
- Outcome: fixed in plan (U2 pixel test on pre-encode frames; separate WebP check with a measured codec tolerance)

Review closed after round 3 (policy cap); round-3 fixes applied without a further round, as the owner approved.

## Unit U1 review, round 1 — 2026-09-30, reviewed 27b3eb3d..a999bc26 (DELIVERY.md §5.3, migration)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

### U1R1-1 — U1 exposes only a count, so U3 cannot compute D6's Retry-After without copying the counting predicate
- Trigger: U3 needs 429 + Retry-After → previews_in_last_hour returns an int and entitlements.py is outside U3's scope → predicate copied into api/video.py → copies drift; U4's "try again in N min" is wrong
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (a2df39fe)

### U1R1-2 — A preview orphaned in `running` stops counting at 15 min, then counts again once the hourly sweep fails it
- Trigger: default worker stopped hard mid-preview → row stays running → drops out at 900 s → user makes more → hourly sweep fails it keeping started_at → it re-counts until created_at + 3600 → unexpected 429
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: U3 gives previews a stale-running threshold shorter than STALE_RUNNING_S; logs show a preview failed by the hourly sweep; a user reports a 429 with fewer than 10 recent previews
- Guard: —
- Override: user: Fix now — the plan already fails previews after ~10 min (D5), so the re-count window is ~45 min
- Outcome: fixed (a2df39fe)

## Unit U1 review, round 2 — 2026-09-30, reviewed a999bc26..a2df39fe (fixes only)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

### U1R2-1 — preview_slot_frees_at forecasts an in-flight preview as lost, so Retry-After is too short in the normal case
- Trigger: 10th preview still rendering when the user taps again → 429 says ~15 min (created+900) → the 10th finishes and counts to created+3600 → refused again at 15 min
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (f367b892)

### U1R2-2 — The plan still describes the round-1 counting rule and never names preview_slot_frees_at
- Trigger: U3 implementer reads the plan for 429 + Retry-After → copies the predicate or tests the old failed-row rule
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (634d37f8)

## Unit U1 review, round 3 — 2026-09-30, reviewed a2df39fe..f367b892 and plan 7cc83cb9..634d37f8 (fixes only)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus). U1R2-1 and U1R2-2 found fixed; the U1 code came back clean.

### U1R3-1 — U3 acceptance demands two Retry-After values be "the same" although each is ceil()'d from its own now
- Trigger: U3 test on real time asserts exact equality → the two nows straddle a second boundary → CI flakes on a green change
- Scores: trigger=plausible, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: a U3 test compares Retry-After exactly against real time or early check vs locked check without a fixed now; any CI run fails on it. U3's brief should say: inject a fixed now, or check within 1 s
- Guard: —
- Override: user: Fix now — one sentence in U3's brief prevents a flaky CI run
- Outcome: fixed in plan (U3 acceptance: fixed now or within 1 s)
