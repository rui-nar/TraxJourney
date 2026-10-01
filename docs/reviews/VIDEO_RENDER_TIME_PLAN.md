# Review ledger — Trip video render time: tile fetching (#517)

Subject: docs/VIDEO_RENDER_TIME_PLAN.md
Envelope: plan section "Review envelope" (REVIEW.md defaults + fetch threads inside one render, Mapbox's per-account rate limit, RQ timeout)

## Round 1 — 2026-10-01, reviewed at 2dc0cb22

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

Envelope question 1 (to the user, not triaged): the Flutter clients' interactive satellite map draws the same Mapbox style on the same account. Is end-user map browsing part of the 6,000/min budget the render must share?
Owner answer (2026-10-01): yes, assume map browsing shares the budget. Plan envelope updated.

Envelope question 2 (to the user, not triaged): in the bench, a plain process, concurrent.futures' atexit joins in-flight fetch threads, so after a failed render the bench may take up to about 2 minutes to exit. The RQ work horse is not affected (os._exit). Is that acceptable, or should the bench's shutdown be bounded too?
Owner answer (2026-10-01): asked for faster alternatives (daemon fetch threads with a stop event / stop event only / os._exit in the bench); chose A, daemon fetch threads. Plan D7, envelope, and U2a Do 3 and acceptance "exit" updated.

### R1-1 — A prefetcher started by frame() is only closed by encode/_write_preview_webp; other frame-loop drivers leak a parked prefetcher
- Trigger: CI runs the video tests that call FrameRenderer.frame() directly (test_video_renderer, test_video_camera_render, test_video_memory, test_video_preview_render) → each starts a prefetcher whose producer fills the window and waits for a consumer that never comes; nothing closes it → idle pools and blocked threads pile up; the atexit join can hang the run
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager checked call sites)
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D7, U2a Do 3 and acceptance "abandoned", U2b Do 3 and acceptance "dropped renderer": no producer thread, top-up by the consumer, weakref.finalize)

### R1-2 — D8 keys the 429 backoff on Retry-After, which Mapbox doesn't document; a 429 that lasts the rest of the minute still fails the render after 1 s + 2 s
- Trigger: the account's per-minute budget is used up by other consumers → Mapbox answers 429 with X-Rate-Limit-Reset and no Retry-After → the client retries at 1 s and 2 s, then raises APIError → the render fails and the user sees "could not be rendered"
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible (triager corrected from logged), later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: a render fails with a Mapbox 429 APIError in the worker logs, or posters plus prod and val renders on one Mapbox account come near the 6,000/min limit
- Guard: —
- Override: user: Fix now (approved with override, 2026-10-01)
- Outcome: fixed in plan (D8, U1 Do 2 and acceptance, DoD: X-Rate-Limit-Reset, then Retry-After, then backoff, clamped to [1, 30] s, malformed counts as absent)

### R1-3 — tile_ms / fetched / net_seconds are accumulated from four pool threads, but the plan's shared-state list omits them
- Trigger: a render with 4 threads → unlocked `+=` from several threads → lost updates → the summary under-reports tile_ms
- Scores: trigger=plausible (triager corrected from concrete: under the GIL a plain += rarely switches mid-update), impact=cosmetic, detect=silent, later=cheap, fix=S/local, confidence=inferred
- Decision: Guard (D9)
- Revisit when: —
- Guard: (proposed) the U2a tests assert, after an in-order run with threads=4, that `prefetcher.fetched` equals the fake fetcher's thread-safe call count; close() logs a warning if fetched + misses differs from the completed fetches
- Override: —
- Outcome: guard added in plan (D9, U2a acceptance "counters"); its close() warning was replaced in round 2 by consumer-side counting (R2-4)

## Round 2 — 2026-10-01, reviewed at a64b734b (fixes since 2dc0cb22)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus). No envelope questions.

### R2-1 — D8's 30 s 429 wait runs inside the synchronous poster preview, so its grey-basemap fallback becomes a client timeout
- Trigger: the account budget is used up by others → a user opens the poster editor (synchronous POST /poster/preview) → the first tile gets a 429 with the reset ~60 s away → fetch_tile sleeps 30 s twice, and render_basemap's 10 s deadline is never checked during a sleep → the client's ~20 s HTTP timeout fires; the user sees "preview was not available" instead of the grey map, and an API thread stays busy for about 60 s
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/shared, confidence=verified
- Decision: Defer (D10)
- Revisit when: a POST /poster/preview takes longer than the client's ~20 s timeout, a Mapbox 429 is logged on the poster preview path, or the owner overrides because this undoes the #14 preview budget
- Guard: —
- Override: user: Fix now — a regression this plan's own R1-2 fix introduces
- Outcome: fixed in plan (D8 deadline, U1 Scope, Do 3 and acceptance, DoD)

### R2-2 — The R1-1 "dropped renderer" test passes whether or not the weakref.finalize fires
- Trigger: CI runs the test against `weakref.finalize(self, self.close)` → the callback holds a strong reference, so the renderer is never collected → the pool threads sit idle, so "no prefetch thread fetching" holds → the test passes while the R1-1 fix does nothing
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (D7, U2a thread names, U2b Do 3 and acceptance "dropped renderer")

### R2-3 — D8's two 30 s waits add up to exactly 60 s, so the last attempt lands on the reset boundary with no margin
- Trigger: the budget is used up at the start of a window → 429 with the reset ~60 s ahead → sleep 30, another 429, sleep ~30 → the third and last attempt is sent at the reset instant → clock skew or limiter lag returns 429 again → APIError → the render fails
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: a render fails with a Mapbox 429 APIError after the full D8 waits, or R2-1 is reopened (settle the cap and the margin together)
- Guard: —
- Override: user: Fix now — settled together with R2-1
- Outcome: fixed in plan (D8: +1 s margin, 65 s per-tile 429 budget, U1 Do 2 and acceptance)

### R2-4 — The R1-3 guard compares counters while in-flight fetches are still completing, so it can warn spuriously on a failed render
- Trigger: a render fails mid-way → close() runs while pool threads are still fetching → the counters change between the reads → a spurious "counters disagree" warning
- Scores: trigger=plausible, impact=cosmetic, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: the warning appears in a worker or bench log; first check whether that render failed with fetches in flight
- Guard: —
- Override: user: Fix now — count on the consumer's thread, which removes the race
- Outcome: fixed in plan (D9, U2a Do 3: counters updated on the consumer's thread; the R1-3 close() warning dropped)
