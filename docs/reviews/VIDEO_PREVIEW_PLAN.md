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
