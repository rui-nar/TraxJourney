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
- Outcome: fixed in plan (D5: a 10 s fixture with a zig-zag leg turning 30–120°; the no-disk control must exceed the bound 2×; U2 escalate line)

### R2-2 — U1's identity test now runs the dense fixture for every (shot, state) pair, 4 modes, twice, with pie-slice joints, so R1-4's cost returns
- Trigger: CI runs test_video_route_clip.py → _draw_route on every frame of a fixture with no stated length → an estimated 10–20 minutes on every push
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U1 identity: the dense fixture sampled, ≤ 40 pairs per mode; small trips still on every pair)

## Round 3 — 2026-10-02, reviewed at a8c6902e (fixes since 86d1a88a)

Reviewer: adversarial-reviewer (Fable). No findings and no envelope questions: the round is clean, and the plan review is closed. Checked:
- the B_disk20 figures quoted in D5;
- that a 10 s fixture is buildable (pacing keeps every leg; 300 frames);
- that the 2× no-disk control and the D5 bounds can both hold on one fixture;
- that the identity sample stays bounded (a handful of integer zooms per mode).

## Integrated round 1 — 2026-10-02, reviewed at b4d390b1 (the #525 part: 473e65a4..b4d390b1)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus). No envelope questions.

The reviewer judged both delivery deviations acceptable; they are recorded here as accepted, not as findings:
- **U1's identity test samples 1080p:** every 12th pair, plus every pair where a leg has several runs. Every pair is checked at 320×180, and all of them with VIDEO_CLIP_ALL=1. Identity follows algebraically from the shared conversion formula, and every 1080p pair with a different run structure is included.
- **U2's bounds and control use only frames with a travelled line:** frames without one are identical for both candidate and control. Excluding them makes the D5 bounds stricter, and the 2× control easier in the same proportion, which is the ratio the control measures.

### I1-1 — docs/VIDEO.md's Overlay section mentions "clipping" but never describes it (#523's behaviour)
- Trigger: a maintainer reads the Overlay section to learn why a zoomed-in frame of a 500 k-point leg no longer peaks at 180 MB → finds only "with clipping alone" → has to recover the design from the plan and a test docstring
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (fix unit F1, 4abc379f: a Clipping bullet in docs/VIDEO.md's Overlay section)

## Integrated round 2 — 2026-10-02, reviewed at fec61585 (the F1 fix since 694723f6)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus). No envelope questions.

### I2-1 — The Clipping bullet's "180.5 MB to 0.13 MB" is a tracemalloc Python-heap figure without Pillow's layer buffers, shown next to RSS figures
- Trigger: a maintainer reads 0.13 MB beside the document's RSS figures → sizes or debugs worker-video memory as if a 1080p frame cost 0.13 MB → the frame's RGB+L layers at ROUTE_SS=3 (tens of MB) were never counted
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (fix unit F2, 222e8dd7: the figure is named as tracemalloc's Python heap, without Pillow's image buffers)

## Integrated round 3 — 2026-10-02, reviewed at 2f40ac39 (the F2 fix since 2c122aae)

Reviewer: adversarial-reviewer (Fable). No findings and no envelope questions: the round is clean, and the integrated review is closed. The changed sentence matches what tests/test_video_memory.py measures, and Pillow's image buffers are the only material exclusion from that measurement.
