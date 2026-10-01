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

## Round 3 — 2026-10-01, reviewed at 60535c20 (fixes since a64b734b)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus). No envelope questions. This is the last round REVIEW.md §6 allows.

### R3-1 — The thread-name checks are process-wide, but the "abandoned" test leaves idle tile-prefetch- threads, so the U2b checks fail or flake depending on test order
- Trigger: a developer runs U2a's acceptance command after wave 2 → the "abandoned" test leaves idle daemon tile-prefetch- threads → U2b "failure cleanup" and "dropped renderer" find them in threading.enumerate() → they fail although the renderer cleaned up
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed in plan (U2a Do 3: the prefetcher exposes its own threads; U2b "failure cleanup" and "dropped renderer" check only that prefetcher's threads, or those new since a snapshot)

### R3-2 — Cutting the request timeout to the time left can pass a timeout ≤ 0: urllib3 raises ValueError (not APIError), and the preview shows a garbled warning
- Trigger: a 429 wait ends milliseconds before the poster preview's deadline → remaining ≤ 0 → session.get raises ValueError, which escapes fetch_tile → the grey map shows "Map imagery unavailable: Attempted to set connect timeout to -0.003…"
- Scores: trigger=plausible, impact=cosmetic, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: a preview warning or API log contains "timeout cannot be set" or a ValueError from MapboxTileClient.fetch_tile, or a caller that passes a deadline relies on the client raising only APIError
- Guard: —
- Override: user: Fix now — one line, with the R2-1 deadline work; keeps the client raising only APIError
- Outcome: fixed in plan (D8 and U1 Do 3: MIN_REQUEST_S = 0.5; U1 acceptance)

## Integrated round 1 — 2026-10-01, reviewed at 4885c3b8 (feat/517-video-render-time against origin/main 8888d3d4)

Reviewer: adversarial-reviewer (Fable). No findings and no envelope questions, so nothing was triaged. The round is clean.

The reviewer checked the verifier's note that TilePrefetcher assumes a single consumer thread. No production path breaks it: only encode, _write_preview_webp and the bench's --dump-frames loop call frame()/basemap(), each on the renderer's own thread, and the dump loop runs after close() and so fetches directly. The only call from another thread is close(), which takes the pool's lock. Considered but not raised: up to 4 in-flight tiles are still billed after a failed render (D7 accepts this), and requests applies its timeout to connect and read separately, a behaviour that predates this change.

## Delivery

Branch `feat/517-video-render-time`, from the plan branch at 1bfd2ca8 (based on `origin/main` 8888d3d4). The orchestrator worked in worktree `E:\Dev\TraxJourney-517-plan`; unit worktrees `.claude/worktrees/v517-*` were made by hand from the branch.

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Tile client: one session per thread, 429 waits, deadline | Opus | S5 | 1 | — | yes | R1-2, R2-1, R2-3, R3-2 |
| U2a | Tile prefetcher and `Sheet.requests` / `plan_requests` | Opus | S5 | 1 | — | yes | R1-1, R1-3, R2-4, R3-1 |
| U3 | Bench `--profile` | Sonnet | — | 1 | — | yes | — |
| U2b | Prefetch in every render; `tile_ms` and `prefetch_misses` in the summary | Opus | S5 | 1 | — | yes | R1-1, R2-2, R3-1 |

Owner decisions during delivery: none beyond the plan.

Reviews: plan (3 rounds, 9 findings, all fixed in the plan); integrated (round 1 clean).

Checks at the final merge (4885c3b8):
- server suite: 5533 passed, 38 skipped (the rail extract workflow test was left to CI on this machine; it fails locally on the base commit too);
- video and tile tests in the Linux image: 562 passed, 3 skipped;
- memory tests: unchanged with prefetching on and off (81–112 B per point, bound 120);
- frames: bit-identical with prefetching on and off in every camera mode; goldens unchanged.

Gate G1 passed (owner, 2026-10-01, VPS val, 90 s 1080p variable, same trip as #518's G2): `fetch_ms` 0.63, against 44.6 before and a target of ≤ 10; `prefetch_misses` 0. Total 200.8 ms per frame (542 s), against 257.4 (695 s) at #518's G2. Recorded in docs/VIDEO.md. The profile went into follow-up #525: the route's round joints (`joint="curve"`) and the basemap resize.
