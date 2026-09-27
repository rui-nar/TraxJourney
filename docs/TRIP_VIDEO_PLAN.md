# Trip video export — Plan (P1 timeline engine + P2 server render)

## Problem

A trip in TraxJourney can be seen as a map, a poster and stats, but not as the
journey itself: the order of the legs and how fast each transport mode moved.
The old desktop client (GetTracks, `src/animation/`, v1.5.0) had a trip animation
with MP4 export; the web and mobile clients have nothing like it. Users with long
trips (up to 365 days on paid plans) are the ones who most want it.

This plan covers the first cut: a pure timeline/camera engine (P1) and a
server-side render job that produces an MP4 (P2). An in-app live preview (P3),
memory photo pop-ups / stat cards / 9:16 (P4) and keeping Strava time streams
(P5) are later work.

## Current state

- **Poster (#14)** is the pattern to copy: `api/poster.py` (job row, token routes),
  `src/poster/poster_job_runner.py` (lifecycle, emails, orphan sweep),
  `src/poster/tile_stitcher.py` (Mapbox raster tiles, `render_basemap`, tile cap),
  `src/poster/poster_renderer.py` (`_Projector`, `_decimate_pixels`, `_draw_route`),
  `src/poster/typography.py` (fonts), Flutter `poster_*.dart`,
  `poster_download_screen.dart` and the `/poster/:token` route +
  auth-redirect exemption in `flutter_client/lib/src/core/app_router.dart:89,289`.
- **Jobs:** RQ via `src/jobs/queue.py` (`enqueue` always passes
  `Retry(max=max_retries)` and falls back to in-process work when there is no
  broker); `src/jobs/worker.py` `_work_horse_killed_handler` handles only
  `run_poster_job`; the poster orphan sweep runs only in the API lifespan.
  `tests/test_worker_topology.py` pins queues ↔ compose services.
- **Geometry:** `api/geo.py` `_activity_feature` (decoded `summary_polyline`,
  2-point fallback, skips encrypted envelopes) and `_segment_feature` (resolved
  `route_polyline` for rail/ferry/bus, else a 50-point great-circle arc).
- **Timing data:** activities have `moving_time` / `elapsed_time`, no per-point
  timestamps. Segments (`ConnectingSegment`, types train/flight/boat/bus) have a
  `date` only.
- **Billing:** `src/billing/plans.py` `Limits` + `_DEFAULT_LIMITS` (env-tunable per
  plan); `features_for` generates bullets from limits and
  `tests/test_plan_features_backed.py` requires every bullet to be backed.
  `src/billing/entitlements.py` `ensure_*_quota` → `QuotaExceeded` → 402.
  `src/billing/usage.py:124` reconciles storage as `dir_size(data/users/<uid>)`.
- **Account deletion** removes `data/users/<uid>` (`src/auth/account_deletion.py:503`).
- **E2EE:** encrypted `summary_polyline` / start/end latlng are unreadable by the
  server; segments stay plaintext (`docs/ENCRYPTION.md`).

## Decisions

| # | Decision | Reason | Rules out |
|---|---|---|---|
| D1 | Render **server-side** (Pillow frames → ffmpeg), poster-style job with poll, token download and email. | One code path for web, Android, iOS; reuses the poster stack. | Client encoders per platform (WebCodecs / ffmpeg.wasm / native). |
| D2 | **Encrypted trips: per-job consent.** Client uploads decrypted activity geometry for one render; the server deletes it on every terminal path. | Owner decision; the only way a server render can include encrypted tracks. | Refusing encrypted trips; storing decrypted geometry. |
| D3 | **Quota:** `max_videos_per_month` = free 1, tier_1 10, tier_2 30, tier_3 unlimited (env-tunable). Counts pending/running/done/expired jobs created in the current UTC calendar month; failed jobs don't count. Billing off ⇒ unlimited. | Owner decision; render CPU and Mapbox tiles cost money. | Per-trip limits; local-time months (gameable, ambiguous). |
| D4 | **Pacing option A:** user picks 30 / 60 / 90 s; clip time ∝ √(real duration) × mode weight. Option B (real pace × speed factor) is the fallback, so pacing is a swappable policy. | Predictable length, readable legs; owner wants mode speeds visible (speed badge + per-mode counters carry that). | Global "1 h → 1 s" (unpredictable length). |
| D5 | **Every trip fits every offered length (R1-5).** Legs are grouped into *clips* by adjacent merging until the clip count fits the budget; day cards are replaced by a continuous date ticker. | Trips can span 365 days; a per-day floor made trips over ~31 active days unrenderable. | Refusing long trips; per-day cards. |
| D6 | Videos live in `data/users/<uid>/videos/<job_id>/`, and **the billing storage reconcile skips `videos/`** (R1-3). | Account deletion's single `rmtree` keeps covering them; videos don't eat the photo quota. | A separate `data/videos/` tree (account deletion would leave it behind). |
| D7 | **The API never renders.** No broker ⇒ 503 before anything is written; enqueue failure ⇒ job failed + geometry deleted (R1-7). | A render takes minutes; in-process would starve the API. | The poster's in-process fallback. |
| D8 | MP4 H.264 yuv420p, 30 fps, 16:9, `+faststart`; retention 30 days. | Plays everywhere; streams from the download URL. | GIF; 9:16 (P4). |
| D9 | Speeds: activities use `distance / moving_time`; segments use `REAL_SPEED_KMH` flight 800, train 100, bus 60, boat 25 (env-overridable). | Segments have no times. | Asking users for departure/arrival times (later). |
| D10 | **Resolution by plan:** free 720p (1280×720), paid tiers 1080p (1920×1080), as a limit `max_video_height` (720 / 1080 / 1080 / 1080, env-tunable) so the pricing bullet is generated from it. Billing off ⇒ 1080p. | Owner decision (2026-09-28); render cost scales with pixels. | 1080p for all; a hard-coded tier check the pricing page can't see. |
| D11 | **Lengths: 30, 60, 90 s only**, for every plan. | Owner decision; with D5 no trip needs longer. | A paid 120 s option. |
| D12 | **Shared trips: the requester renders and is charged.** A companion's render counts toward the companion's own quota and resolution limit; the job, emails and download belong to the requester. | Owner decision; the requester is who asked for the cost. | Charging the trip owner. |
| D13 | **Stored MP4s don't count toward storage** (owner confirmed; implemented by D6). | Owner decision. | Counting them. |

## Review envelope

REVIEW.md defaults E1–E5 apply unchanged. Additions and one stated exception:

- **E6 exception (D2).** For an encrypted trip the server receives plaintext
  activity geometry *only* when the user consents for that one render job. That
  transient plaintext is in scope (leaks, retention, logging, reuse beyond the
  job). E6 holds for everything else: the server never decrypts, never asks for
  keys, never keeps the geometry after the job.
- **ffmpeg** is a local binary in the worker image, fed only frames the server
  drew and integer parameters the server chose. Not a third-party service.
- **Mapbox** is the existing basemap provider (E2 applies to its responses);
  cost per render is in scope.
- **Render worker:** one `video` RQ worker, one job at a time (E1). A render may
  take minutes; the API process never renders.

## Boundaries crossed

- **API contract:** new endpoints only (`/api/projects/{name}/video…`,
  `/api/video/{token}…`); no existing endpoint changes shape. Old clients (E5)
  simply don't offer the feature.
- **Schema:** new table `videojob` (one Alembic migration). New limit in `Limits`
  (code + env, no stored data).
- **Stored data:** MP4 files under `data/users/<uid>/videos/`, deleted after 30 days
  by a sweep; transient `geometry.json` (plaintext, D2) deleted on every terminal
  path and by the sweep as a backstop.
- **Export format:** MP4 (D8) — new, nothing to stay compatible with.
- **Billing storage accounting:** `reconcile_usage` stops counting `videos/`
  (existing users have no `videos/` dir, so no stored number changes).
- **Image / deployment:** worker image gains `ffmpeg` (apt); new compose service
  `worker-video` with a memory limit; `docs/DEPLOYMENT_VPS.md` updated.

## Conventions

Binding on every unit.

1. **Video paths** come from one helper, `src/video/paths.py`
   (`video_dir(uid, job_id)`, `geometry_path(...)`, `result_path(...)`), under
   `data/users/<uid>/videos/<job_id>/`. No unit builds these paths by hand.
2. **Plaintext geometry (D2)** is never logged, never put in `request_json`,
   Redis, an exception message or an email. It exists only as
   `geometry.json` (mode 0600) written by the API after the job row commits, and
   is removed by `delete_job_geometry(job_id)` — idempotent, called on done,
   failed, interrupted, enqueue failure, the worker-startup sweep and the hourly sweep.
3. **Determinism:** frame *n* is a pure function of (timeline, camera params, *n*).
   No wall clock, no randomness in `src/video/`.
4. **`src/video/` pure modules** (`legs`, `pacing`, `timeline`, `camera`) import no
   Pillow, ffmpeg, DB or FastAPI.
5. **Renderer interface** (so U6 and U5 can be built in parallel):
   `render_video(job_id: int, user_info_id: int, project_id: int, request: dict,
   out_path: Path, geometry: dict[int, str] | None, progress: Callable[[float, str], None]) -> Path`
   in `src/video/renderer.py`. U6 calls it lazily and tests inject a fake.
6. Job statuses: `pending | running | done | failed | expired`. **Transitions are
   compare-and-set** (R2-1): `pending → running` only from `pending`,
   `running → done|failed` only from `running`; a job that finds its row in any
   other state stops, deletes its geometry and its partial output, and sends no
   email. Stale jobs are failed only by the owner of the queue (the worker at
   startup) or by age (the hourly sweep) — never by the API at its own startup.

## Pacing and timeline design (P1, reference for U1/U2)

**Legs.** One leg per trip item in map order. Activity: decoded polyline (or the
consent geometry), fallback 2-point line; `real_s = moving_time` → `elapsed_time`
→ `distance / mode speed`. Segment: resolved route or great-circle arc,
`real_s = length / REAL_SPEED_KMH`. Legs with fewer than 2 points are dropped and
reported in `skipped`. Each leg keeps its date, mode, km and speed.

**Clips (D5, fixes R1-5).**
1. Merge consecutive legs with the same mode and date (three city walks → one).
2. Budget: `T_fixed = title 2.5 s + end 3.0 s`; `T_clips = T_total − T_fixed`;
   `N_max = floor(T_clips / 1.5)` (30 s → 16, 60 s → 36, 90 s → 56).
3. While `n_clips > N_max`, merge the adjacent pair with the lowest merge cost:
   `cost = w_a + w_b`, × 0.5 when both are the same mode (so multi-day stretches of
   one mode merge first); ties break on the lower index. Priority-queue
   implementation, O(n log n).
4. A clip is an ordered list of legs. It keeps each leg's own mode, geometry and
   speed: the icon and badge switch as the marker crosses sub-legs.
5. Clip time: `w = √(Σ real_s) × mode_weight(dominant mode)` (flight 0.5, else 1),
   shared over `T_clips`, clamped to floor 1.5 s and cap `max(0.25, 1/n) × T_clips`,
   water-filled until stable. Inside a clip, sub-legs share the clip time by
   `√(real_s)` with no floor; a sub-leg shorter than 2 frames is drawn at once
   (route appears, counters jump).
6. **No day cards.** A date ticker ("Day 34 · 12 May 2026") updates continuously
   from the sub-leg being drawn. So `T_fixed` doesn't grow with trip length.
7. Result: any trip fits any offered length; the only 422 is "nothing to
   animate". A 365-day trip at 30 s shows ~16 clips; at 90 s, ~56.

**Sampling.** `Timeline.sample(t) → FrameState`: clip kind, clip/sub-leg index,
lon/lat, heading, travelled km per mode, speed badge, date ticker. Position is
by distance within the sub-leg, with smoothstep ease over the first/last 0.3 s
of each clip (≤ 20 % of it).

**Camera (U2).** Title/end: fit whole trip. Clip: follow the marker, zoom target
from the clip bbox fitted to ~60 % of the frame, clamped by the current sub-leg's
mode (foot ≥ 12, ride ≥ 10, train/bus/boat ≥ 6, flight free); critically damped
spring on centre and zoom evaluated on the frame clock (Convention 3); 0.6 s
eased transition between clips.

**Policy swap (D4).** `PacingPolicy.allocate(clips, total_s) -> list[float]`;
`SqrtBudgetPolicy` is the only implementation. Option B would add
`RealPacePolicy(factor)` without touching the renderer.

## Open decisions

None. Q1–Q4 were decided by the owner on 2026-09-28 and are now D10–D13.

## Execution units

### Wave 1 — engine and infrastructure (disjoint files; one migration: U3)

**U1 — Legs, clips and pacing**
- **Goal:** Build a deterministic `Timeline` of clips from a project, with the D4/D5 pacing, and a CLI that prints the allocation.
- **Scope:** new `src/video/__init__.py`, `src/video/legs.py`, `src/video/pacing.py`, `src/video/timeline.py`, `src/video/__main__.py` (CLI); `api/geo.py` (extract a shared `feature_coords(item, geometry_override)` helper used by `_activity_feature`/`_segment_feature`, behaviour unchanged); new `tests/test_video_timeline.py`.
- **Context:** this plan's "Pacing and timeline design"; `api/geo.py:748-842`; `src/models/project.py` (`ConnectingSegment`, `Activity`); GetTracks' `E:\Dev\GetTracks\src\animation\animation_state.py` as the example (prefix distances, bisect interpolation, bearing).
- **Do:** 1. Extract the geometry helper from `api/geo.py` without changing its output. 2. `legs.py`: build legs (modes, real_s, km, date, speed) with `skipped`. 3. `pacing.py`: same-date merge, adjacent-merge to `N_max`, `SqrtBudgetPolicy` with floor/cap water-filling. 4. `timeline.py`: clips with absolute times, `sample(t)`, date ticker, per-mode counters, easing. 5. CLI `python -m src.video --project <name> --owner <uid> --length 60` printing clips, durations, merged legs.
- **Acceptance:** `pytest tests/test_video_timeline.py tests/test_geo*.py` pass; tests cover: durations sum to `T_total`; every clip ≥ 1.5 s and ≤ cap; monotone in real duration; same-mode-same-date merge; a synthetic 365-day, 1,000-leg trip fits 30/60/90 s with clip count ≤ `N_max`, identical output twice, allocation + 1,800 samples < 1 s; counters end at the per-mode totals; sampling at clip boundaries; existing geo tests unchanged.
- **Out of scope:** camera, rendering, reading `activity_geo_prepared` (R1-6 is deferred), option B.
- **Latitude:** local design
- **Escalate if:** a file outside Scope is needed (X3); the geo helper extraction changes any existing geo test's output.
- **Depends on:** —

**U3 — Video job model and quota**
- **Goal:** Add the `videojob` table and the monthly video quota.
- **Scope:** `models/project_db.py` (`DBVideoJob`); new Alembic migration under `alembic/versions/`; `src/billing/plans.py`; `src/billing/entitlements.py`; `src/billing/usage.py` (skip `videos/` in `reconcile_usage`); `.env.example` (limit docs); new `tests/test_video_quota.py`; `tests/test_plan_features_backed.py` (env reset list, and registering the bullet if needed).
- **Context:** `DBPosterJob` in `models/project_db.py` (example to follow); `ensure_project_quota` in `entitlements.py` (example); `_DEFAULT_LIMITS`/`features_for` in `plans.py`; `reconcile_usage` in `usage.py:117-137`; memories [[feedback_db_migrations]] / parallel heads: run `alembic heads` after rebase.
- **Do:** 1. `DBVideoJob`: id, project_id, user_info_id, status, stage, progress, request_json, created_at, started_at, completed_at, result_path, size_bytes, download_token, expires_at; index on (user_info_id, created_at). 2. Migration. 3. `Limits.max_videos_per_month` and `Limits.max_video_height` (D3, D10) as new tuple entries, env `<PREFIX>_MAX_VIDEOS_PER_MONTH` / `<PREFIX>_MAX_VIDEO_HEIGHT` with the existing `_ENV_PREFIX` values (`FREE_…`, `TIER_1_…`, like `FREE_MAX_PROJECTS`) (R2-6), documented in `.env.example` next to the other limits and cleared by `tests/test_plan_features_backed.py`'s env reset; bullets from `features_for` ("1 video per month · 720p"). 4. `videos_this_month(sess, uid, now)` (status ≠ failed, UTC month) and `ensure_video_quota` (only when `quotas_enforced()`). 5. `reconcile_usage` excludes `data/users/<uid>/videos/`.
- **Acceptance:** `pytest tests/test_video_quota.py tests/test_plan_features_backed.py tests/test_billing*.py` pass; `alembic heads` shows one head; tests: failed jobs don't count, month boundary at UTC midnight, billing off ⇒ unlimited, free second video ⇒ `QuotaExceeded`, `max_video_height` is 720 on free and 1080 on paid and billing-off, `TIER_1_MAX_VIDEOS_PER_MONTH=20` in the env changes tier_1's limit and its bullet, reconcile ignores a file under `videos/` but counts one under `memories/`.
- **Out of scope:** the insert transaction (U6), retention sweep (U6).
- **Latitude:** local design
- **Escalate if:** X3; a second Alembic head appears after rebase.
- **Depends on:** —

**U4 — Video queue, worker and image**
- **Goal:** A `video` queue that never runs in-process, never retries, has a long timeout, and whose killed jobs are failed.
- **Scope:** `src/jobs/queue.py`; `src/jobs/worker.py`; `docker-compose*.yml`; `Dockerfile`; `tests/test_worker_topology.py`; new `tests/test_video_queue.py`.
- **Context:** `enqueue` and `QUEUE_MAX_CONCURRENCY` in `queue.py`; `_work_horse_killed_handler` in `worker.py` (example of the kill path); the `worker-poster` compose service (example).
- **Do:** 1. `QUEUE_VIDEO = "video"`, concurrency 1, in `ALL_QUEUES`. 2. `enqueue(..., max_retries=0)` passes **no** `retry` (fixes R1-2); add `job_timeout` and `allow_inline: bool = True` parameters; with `allow_inline=False` a missing/failing broker returns False without running anything. 3. `_work_horse_killed_handler` dispatches by dotted function path through a mapping `{"src.poster.poster_job_runner.run_poster_job": "...mark_job_interrupted"}` with lazy imports, so U6 adds the video entry with one line (fixes R1-1 structurally). 4. Compose `worker-video` service (same image, memory limit, `video` queue). 5. `apt-get install ffmpeg` in the Dockerfile.
- **Acceptance:** `pytest tests/test_worker_topology.py tests/test_video_queue.py tests/test_jobs*.py` pass; tests: `max_retries=0` enqueues without a `Retry` (fake queue), `allow_inline=False` with no broker returns False and does not call the function, the killed-handler dispatch still fails a poster job and ignores unknown functions; `docker build` succeeds and `ffmpeg -version` runs in the image.
- **Out of scope:** the video runner itself; changing poster retry behaviour (noted, not touched).
- **Latitude:** local design
- **Escalate if:** X3; the topology test needs rules beyond adding the new queue.
- **Depends on:** —

### Wave 2 — camera and job lifecycle

**U2 — Camera**
- **Goal:** Deterministic follow camera over a `Timeline`.
- **Scope:** new `src/video/camera.py`; new `tests/test_video_camera.py`.
- **Context:** "Camera (U2)" above; `src/poster/tile_stitcher.py` `zoom_for_target_size` / `lonlat_to_pixel` (reuse the math); U1's `Timeline`.
- **Do:** `camera(timeline, frame_n, fps, size) -> (lon, lat, zoom: float)` per the design; spring integrated on the frame clock from frame 0 (or cached per frame index).
- **Acceptance:** `pytest tests/test_video_camera.py`: identical output twice; zoom within per-mode clamps; centre/zoom change between consecutive frames under a threshold; overview frames contain the whole trip bbox.
- **Out of scope:** tiles, drawing.
- **Latitude:** local design
- **Escalate if:** X3; the timeline API from U1 lacks something the camera needs.
- **Depends on:** U1

**U6 — Video API, runner, sweeps and emails**
- **Goal:** Endpoints and job lifecycle for video, with the consent path and every plaintext-deletion path.
- **Scope:** new `api/video.py`; `api/router.py` (mount routers; **no** video startup sweep); new `src/video/paths.py`; new `src/video/job_runner.py`; `src/jobs/worker.py` (the killed-horse mapping line, and the startup sweep call when the worker consumes `video`); the module that registers the scheduled jobs (for the hourly sweep); `src/email/templates.py` (video ready/failed); new `tests/test_video_api.py`, `tests/test_video_runner.py`; `docs/ENCRYPTION.md`.
- **Context:** `api/poster.py` and `src/poster/poster_job_runner.py` (examples to follow for routes, emails and file handling — **not** for status transitions or the startup sweep, see Convention 6); `lock_account` in `src/billing/subscriptions.py:49` (the write-lock idiom: a no-op UPDATE of the account row as the transaction's first write); where `api/router.py` registers its APScheduler jobs (for the hourly sweep); `docs/ENCRYPTION.md` known-exposures section.
- **Do:**
  1. `POST /video/plan`: timeline summary (clips, skipped, per-length clip counts), quota remaining, allowed resolutions, `consent_required` ids, `available` (broker + ffmpeg). Lengths are validated as 30/60/90 (D11); a resolution above the requester's `max_video_height` ⇒ 402 (D10). Quota and resolution are always the **requester's** (`current_user`), also on a shared trip via `?owner=` (D12).
  2. `POST /video` in this order (fixes R1-7, R2-2, R2-5): (a) `queue_available()` else 503, nothing written; (b) consent: encrypted activities without `decrypted_geometry` ⇒ 409 `consent_required`. An activity is **encrypted** when `summary_polyline` is an envelope **or** `start_latlng_json`/`end_latlng_json` is (one helper, `is_encrypted_activity`, used by both routes); for one with no track the client sends its decrypted 2-point line, which is accepted (fixes R3-2) — checked **before** the timeline, so a trip whose only legs are encrypted activities still reaches consent; accept geometry only for ids that are in this project **and** encrypted; (c) build the timeline with that geometry, 422 only if nothing is left to animate; (d) one session whose **first write is `lock_account(sess, requester)`**, then `ensure_video_quota`, then insert the row, then commit — the lock serialises the requester's concurrent POSTs; (e) write `geometry.json` if any; (f) `enqueue(QUEUE_VIDEO, run_video_job, job_id, max_retries=0, allow_inline=False, job_timeout=1800)`; if it returns False ⇒ mark failed (doesn't count), `delete_job_geometry`, 503. `/video/plan` applies the same consent-before-timeline rule and reports `consent_required` even when no plaintext leg exists.
  3. Status, download (range requests) and token routes, same shapes and 404 semantics as the poster's.
  4. `run_video_job` (Convention 6): CAS `pending → running` and set `started_at`, else stop; `render_video` (Convention 5); CAS `running → done|failed`, and if the CAS fails (the row was failed meanwhile) delete the output and send no email; `delete_job_geometry` in `finally`; emails only after a successful terminal CAS; `expires_at = completed_at + 30 d`.
  5. `mark_video_job_interrupted` (CAS to failed from pending/running, deletes geometry, sends the failed email; no-op otherwise) + its line in the worker mapping (fixes R1-1). `sweep_stale_running_video_jobs`, called by `src/jobs/worker.py` **when the worker starts consuming `video`**, before `.work()`: it is the queue's only consumer, so any `running` row at that moment belongs to a dead work-horse; `pending` rows are left alone because they may still be queued in Redis (fixes R2-1). The API does not sweep video jobs at startup.
  6. Hourly sweep (APScheduler, API process): fail via `mark_video_job_interrupted` any `running` row whose `started_at` is older than job timeout + 5 min (RQ has already killed it) and any `pending` row older than 24 h (its queue entry was lost) (fixes R2-4); expire MP4s past `expires_at` (row → `expired`); delete any `geometry.json` whose job is terminal — **never** one whose job is `pending` or `running`, however old: a waiting job still needs it, and a stale one is failed above first, which deletes it (fixes R3-1).
  7. ENCRYPTION.md: document the consent path.
- **Acceptance:** `pytest tests/test_video_api.py tests/test_video_runner.py` pass, with a file-backed SQLite DB for the concurrency test (not StaticPool); tests: happy path with a fake renderer; ownership 404s; token routes; 422; 409 consent; a trip with only encrypted activities and no geometry gets 409 (not 422) from both `/video` and `/video/plan`; a job failed by a sweep and then picked up by the runner stays failed, leaves no MP4 and sends no second email; the worker-startup sweep fails `running` rows and leaves `pending` ones; the hourly sweep fails a `running` row older than the timeout and a `pending` row older than 24 h, and leaves younger ones; the hourly sweep never deletes the `geometry.json` of a `pending` or `running` job, even one older than the job timeout; an activity with no track and encrypted endpoints is listed in `consent_required` and its decrypted 2-point line is accepted and drawn; geometry rejected for plaintext or foreign activities; 402 on a second free video; 402 on 1080p for free; length other than 30/60/90 ⇒ 422; a companion's render on a shared trip is charged to the companion, not the owner; failed job doesn't count; two concurrent POSTs on free ⇒ exactly one job; no broker ⇒ 503 with no row and no file; enqueue failure ⇒ failed row, no file; geometry file gone after done, failed, interrupted, worker-startup sweep and hourly sweep; request logs never contain the geometry payload.
- **Out of scope:** the real renderer (U5); Flutter (U7); R1-6 (plan built from `activity_geo_prepared`).
- **Latitude:** local design
- **Escalate if:** X3; `lock_account` can't be called from `api/video.py` without a circular import (then move the idiom, don't copy it).
- **Depends on:** U1, U3, U4

### Wave 3 — rendering and client

**U5 — Frame renderer and encoder**
- **Goal:** Implement `render_video` (Convention 5): band basemaps, overlay, ffmpeg pipe.
- **Scope:** new `src/video/renderer.py`, `src/video/basemap_bands.py`, `src/video/overlay.py`; new `assets/video/icons/`; new `tests/test_video_renderer.py`; new `docs/VIDEO.md`.
- **Context:** `src/poster/poster_renderer.py` (`_Projector`, `_decimate_pixels`, `_draw_route` supersampling — example to follow); `src/poster/tile_stitcher.py` `render_basemap` and its injected `tile_fetcher` in `tests/test_poster_renderer.py` (example for tests); design tokens for activity colours ([[design_system]]).
- **Do:** 1. Quantise camera zoom into integer bands; per band, stitch the union of visited viewports once with `render_basemap` (split by clip if too large); job tile budget checked before frame 1, lowering zoom clamps instead of failing. 2. Per frame: crop/scale from the band image, cross-fade between bands, faint full route, solid travelled route, icon + halo, speed badge, per-mode counters, date ticker, title and end cards. 3. Pipe rgb24 frames into `ffmpeg -f rawvideo … -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -movflags +faststart`; progress every N frames.
- **Acceptance:** `pytest tests/test_video_renderer.py`: a 2 s 320×180 render with a fake tile fetcher exits 0, `ffprobe` reports 60 frames and 2.0 s; three sampled frames match golden images within tolerance; follow camera actually moves (frames 0 and 59 differ in basemap offset); a manual benchmark (skipped in CI) records ms/frame at 1080p.
- **Out of scope:** photos, elevation strip, 9:16, music.
- **Latitude:** local design
- **Escalate if:** X3; 1080p exceeds 150 ms/frame on the benchmark.
- **Depends on:** U1, U2

**U7 — Flutter: request, track, download**
- **Goal:** Users can plan, request, follow and download a video, including from the email link without a session (fixes R1-4).
- **Scope:** new `flutter_client/lib/src/projects/video_config_dialog.dart`, `video_job_notifier.dart`, `video_status_card.dart`, `video_download_screen.dart`, `video_consent_dialog.dart`; `flutter_client/lib/src/core/app_router.dart` (`/video/:token` route + auth-redirect exemption); the trip menu file that hosts the "Poster" entry; `flutter_client/lib/src/api/` client file for the new endpoints; new widget tests under `flutter_client/test/`.
- **Context:** `poster_config_dialog.dart`, `poster_job_notifier.dart`, `poster_status_card.dart`, `poster_download_screen.dart` and the `/poster/` lines in `app_router.dart:89,289` (examples to follow); memory [[feedback_settings_screen_widget_tests]] (pump, don't pumpAndSettle, while busy); E2EE decrypt helpers used by `client_geo_builder.dart`.
- **Do:** 1. Menu entry → dialog using `/video/plan` (lengths 30/60/90 with clip counts, resolutions the requester's plan allows, quota remaining, unavailable state). 2. On 409 show the consent dialog; on accept decrypt locally and resend with `decrypted_geometry`. 3. Status card polling; download. 4. `/video/:token` screen + redirect exemption. 5. 402 shows the upgrade message.
- **Acceptance:** `flutter test` for the new tests: consent flow sends geometry only after accept; 402 message; unavailable state; `/video/<token>` opens without a session and shows the download (router test); `flutter analyze` clean.
- **Out of scope:** live preview (P3), sharing (P4).
- **Latitude:** local design
- **Escalate if:** X3; the endpoints' shapes differ from U6's.
- **Depends on:** U6

## Definition of done

- A user picks 30/60/90 s for any trip, including a 365-day one, and receives an MP4 by email and in-app; the email link works with no session.
- The video follows the route with a moving camera, mode icon, speed badge, per-mode counters and date ticker; faster modes cover visibly more distance per second.
- Free users get one 720p video per UTC month; paid tiers get 1080p and their own monthly caps; a failed render doesn't use it; billing off ⇒ unlimited at 1080p; a companion's render is charged to the companion.
- Encrypted trips render only after consent (also when every leg is an encrypted activity), and no `geometry.json` survives a terminal job, a killed work-horse, a killed worker container, a broker outage or a restart (tests for each).
- No video job stays `pending`/`running` past its time limit, and a job failed by a sweep never later reports done.
- The API process never renders; no broker ⇒ 503 with nothing written.
- MP4s don't count toward storage and are deleted after 30 days.
- Server and Flutter suites pass; `alembic heads` has one head; `graphify update .` run after merge on main.
- `Release-Note:` trailer on the user-visible merge; `docs/VIDEO.md`, `docs/ENCRYPTION.md`, `docs/DEPLOYMENT_VPS.md` updated.
