# Review ledger — Trip video route drawing: speed and memory (#525, with #523)

Subject: docs/VIDEO_ROUTE_DRAWING_PLAN.md
Envelope: plan section "Review envelope" (REVIEW.md defaults; no new concurrency or service; trips up to ~1.6 M points, single legs up to 500 k)

## Round 1 — 2026-10-02, reviewed at 66c33d29

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus). No envelope questions.

### R1-1 — D5's fidelity bounds are failed by the chosen option in overview mode on the plan's own probe data
- Trigger: the owner renders a dense trip in overview after U2 → the route differs by mean 0.029, 0.072% of pixels, 43.8 dB (probe overview run) → that exceeds the approved mean ≤ 0.02 and PSNR ≥ 48, and no test detects it
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: user: approved; option (a), separate overview bounds (2026-10-02)
- Outcome: fixed in plan (table per camera; D5 overview bounds ≤ 0.04 / ≤ 0.15% / ≥ 42 dB)

### R1-2 — On the renderer's synthetic trips the D5 bounds are vacuous, so the no-joints negative control cannot fail
- Trigger: the bounds test runs on timeline30/timeline30_ny (20–25-point legs, straight walks) → dropping joints entirely stays inside the bounds → the acceptance is unachievable, or the test is weakened
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local (triager corrected from M), confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D5 dense fixture; U2 bounds test on it, with the no-disk control)

### R1-3 — D4 specifies a disk of the line's full width, but the probe used Pillow's joint radius width/2 − 1 (and no joints at width ≤ 4)
- Trigger: the implementer follows D4 literally → disks one layer pixel past the line's edge, and a scalloped edge → a fatter route than was measured
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D4: box vertex ± (width/2 − 1), only for width > 4)

### R1-4 — The identity test renders every frame of every trip at 1080p through the full renderer, twice, in four modes
- Trigger: CI runs test_video_route_clip.py → ~14,400 full 1080p frames plus the 320×180 pass → an estimated 12–15 minutes on every push
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U1 identity on _draw_route over a flat frame; a few full frames as smoke)

### R1-5 — The "no pie slices" test conflicts with _overview_hud, which keeps joint="curve" and is outside U2's scope
- Trigger: the patched-pieslice render runs in overview at 1080p → Overlay.__init__ → _overview_hud draws its footprint with joint="curve" at width 11 → the test fails before frame 1, for code U2 may not touch
- Scores: trigger=concrete (triager corrected from plausible), impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U2 patches pieslice after the Overlay is built; DoD: none while frames are drawn)

## Round 2 — 2026-10-02, reviewed at 86d1a88a (fixes since 66c33d29)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus). No envelope questions.

### R2-1 — The dense fixture's joints are shallow turns, which the D5 bounds don't detect, so the no-disk control isn't shown able to fail
- Trigger: the U2 implementer builds a _track-like fixture (heading drift σ 0.08 rad) → the no-disk control stays inside the bounds → the implementer densifies the fixture or weakens the control, with no number in the plan to steer by
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=inferred (probe B_disk20: joints under 20° dropped and still inside the bounds)
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### R2-2 — U1's identity test now runs the dense fixture for every (shot, state) pair, 4 modes, twice, with pie-slice joints, so R1-4's cost returns
- Trigger: CI runs test_video_route_clip.py → _draw_route on every frame of a fixture with no stated length → an estimated 10–20 minutes on every push
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open
