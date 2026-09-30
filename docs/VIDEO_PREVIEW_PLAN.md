# Trip video preview — Plan for #519

## Problem

A trip video costs one of the month's videos (one on Free) and takes minutes to
render. On the VPS a 90 s video at 1080p takes 695–920 s in variable mode and
about 300 s in overview (docs/VIDEO.md, G2). Since #518, users choose a camera
mode (Follow, Overview, Fixed zoom with or without pulling out for long legs) and
a length. They can't see what a choice looks like until the whole video is done.

#519 adds a **fast, low-resolution animated preview**. It uses the real timeline,
camera and overlay, but renders tiny and at a low frame rate, so a user can judge
the camera and the pacing before spending a video. A preview is not for judging
sharpness or map detail: the zoom level differs, so labels differ.

## Current state

- **Jobs** (merged in #501 and #518):
  - `DBVideoJob` (`models/project_db.py` ≈625-662) with status CAS in
    `src/video/job_runner.py` (`_cas`, `run_video_job`, `mark_video_job_interrupted`,
    `fail_unqueued_video_job`).
  - Two sweeps: `sweep_stale_running_video_jobs`, called by `src/jobs/worker.py`
    when a worker consumes `video` (≈142-148), which fails **every** running row
    because `video` has one consumer; and the hourly `sweep_video_jobs` (minute 40,
    `api/router.py` ≈174), covering stale, expired and stray geometry.
  - Paths come from `src/video/paths.py`. Emails are sent on every terminal path.
- **Quota:** `videos_this_month` (`src/billing/entitlements.py` ≈199-217) counts
  every non-failed videojob row. `ensure_video_quota` raises `QuotaExceeded`,
  which becomes 402.
- **API** (`api/video.py`):
  - `/video/plan`, and `POST /video` (checks in order: 503, height 402, quota,
    consent 409, timeline 422, `lock_account` plus a quota recheck, row, geometry
    file, `enqueue(QUEUE_VIDEO, max_retries=0, allow_inline=False)`).
  - Status and download routes plus token routes; `_file_response` hardcodes MP4.
  - `request_json = {length_s, height, width, camera}`.
- **Rendering** (`src/video/renderer.py`):
  - `render_video` builds the timeline with `timeline_for_project(..., fps=DEFAULT_FPS)`
    (`timeline.py`: `DEFAULT_FPS = 30`, and an `fps` parameter already exists).
  - `FrameRenderer` takes its fps from the timeline and its size from the request.
  - `encode()` pipes frames into libx264 MP4.
  - The camera's fit zoom is proportional to the frame size, but its mode floors
    and caps (`MODE_MIN_ZOOM`, `MAX_ZOOM`, `FIXED_TOP_ZOOM`, `FIXED_FLOOR`) are
    absolute zoom levels, and its cut, chase, pan and "instant sub-leg" thresholds
    are counted per frame (camera.py, timeline.py). So a camera path computed at
    320×180 and 8 fps is **not** the video's camera (review R1-1).
- **Queues** (`src/jobs/queue.py`, `docker-compose.yml.example`):
  - `default` is consumed by `worker` (1024M, `default`, `resolve`) and
    `worker-poster` (1280M, `poster`, `default`, `resolve`); bound 2.
  - `enqueue(..., max_retries, job_timeout, allow_inline)`; `queue_has_workers(name)`.
  - `tests/test_worker_topology.py` pins the topology.
- **Rate limiting:** only an in-process `KeyedRateLimiter` (`src/utils/rate_limit.py`),
  which is not durable across processes. Otherwise, quota-style counts of DB rows.
- **Client:**
  - `video_config_dialog.dart`: an `AlertDialog` with Length, Resolution and a
    Camera `RadioGroup`.
  - `video_job_notifier.dart` keeps the consented geometry in memory for the
    dialog session and re-sends it on plan and create.
  - Precedent for fetching and showing server-rendered image bytes: the poster
    preview (`poster_job_notifier.dart` `fetchPosterPreview` → `Image.memory`).
  - `Image.memory` animates WebP on Android and iOS. On the web, animation depends on
    the browser's image-decoding support, unverified for Safari and Firefox (review R1-4),
    so the web shows the WebP through a browser `<img>` element, which animates it natively.
- **Measured** (docs/VIDEO.md, G2, 1080p): the renderer peaks at about 725 MB, mostly
  from loading the trip. That cost doesn't shrink for a small frame.

## Decisions

| # | Decision | Reason | Rules out |
|---|---|---|---|
| D1 | **Tiny render of the video's own camera**: the timeline and `camera_path` are built exactly as for the final video, at the **target resolution** (720p or 1080p, sent with the preview request) and **30 fps**. Preview frame *n* shows video frame `round(n × 30 / 8)`, drawn at **320×180** with the same shot centre and the zoom lowered by `log2(target width / 320)`, so it shows the same geographic area. 8 fps over the full requested length at real pacing (owner, 2026-09-30). Nearest-frame sampling (at most 1/60 s off) is accepted (owner, review round 1). | The owner wants to judge the camera and the pacing exactly as they'll be. Computing the camera at 320×180 and 8 fps would frame clips differently, because of the absolute zoom floors and per-frame thresholds (review R1-1). | Computing the camera at preview size or rate; a client-side animation; a still storyboard; a sped-up or sampled preview; 6 or 10 fps. |
| D2 | **A preview is a videojob of `kind = 'preview'`**: a `kind` column with default `'video'`, added by an Alembic migration that backfills existing rows as `'video'`. | It reuses the CAS lifecycle, geometry handling, paths and sweeps. | A separate table (duplicates the runner and sweeps). |
| D3 | **The quota counts full videos only**: `videos_this_month` filters `kind = 'video'`. **Previews are free on every plan**, bounded by D6. | A preview must not cost the video it helps choose. | Charging previews. |
| D4 | **Output is animated WebP** written by Pillow (lossy, quality ≈ 50, looping), not MP4. There's no ffmpeg in the preview path. | The app shows it as an ordinary image on web, Android and iOS without a video-player package; Pillow is already in the image. | MP4 plus a player package; GIF (larger, 256 colours). |
| D5 | **Queue: the existing `default` queue for now** (owner, 2026-09-30), `max_retries=0`, `allow_inline=False` (the API never renders), `job_timeout = 300 s`. **#520's light lane** will move previews to their own queue later. Previews are **not** failed by the video worker's startup sweep, which is restricted to `kind='video'` because `default` has two consumers. They're failed only by age, in the hourly sweep: running longer than the timeout plus 5 min, or pending longer than 1 h. | No infrastructure change now. A preview can still wait minutes for a free worker, because both `default` consumers also take rail resolves (up to 600 s) and `worker-poster` takes posters; the dialog shows that wait honestly (U4; review R1-7). | Waiting for #520; putting previews behind 20-minute renders on `worker-video`. |
| D6 | **Rate limit, durable** (owner confirmed 2026-09-30): at most **10 previews per user per rolling hour**, counted from videojob rows of kind `preview` created in the last hour that are `done`, `failed` after starting (`started_at` set, so a timed-out or OOM-killed attempt still used a worker; review R3-1), or `pending`/`running` and younger than 15 minutes, so a lost preview stops counting (review R1-3). A non-authoritative check runs **before consent and the timeline build**, the same pattern as the video quota, and the 409 consent body carries `previews_left` (review R1-6). The authoritative check runs inside the same `lock_account` transaction as the insert. Over the limit ⇒ **429** with `Retry-After`. | Previews cost server CPU and Mapbox tiles; an in-process limiter isn't durable across workers and restarts. | The in-memory `KeyedRateLimiter`; no limit. |
| D7 | **No email on any path, no token routes and short retention:** a preview is shown only in the dialog that asked for it. No email is sent from the runner, the sweeps or the killed-horse handler (`mark_video_job_interrupted` skips emails for `kind='preview'`; review R1-2). Its file is deleted **1 h** after completion by the hourly sweep, so it is gone within 2 h. Status and bytes come from authenticated routes only. | It's disposable. | Emails and token links for previews. |
| D8 | **Encrypted trips reuse the dialog's consent.** The client re-sends the consented geometry it already holds with the preview request. The server handles it exactly as for a full video: the same size bounds, a per-job `geometry.json`, deleted on every terminal path and never logged. | One consent per dialog session, as today. | Asking for consent twice; storing geometry for reuse server-side. |
| D9 | **Explicit trigger** (owner): a "Preview" button in the dialog. The preview shows inline when ready, with a "low-resolution preview" caption. Changing the length, camera or resolution marks it out of date ("Preview is for the previous settings") until the user taps Preview again. | Predictable cost. | Regenerating automatically on every change. |
| D10 | **Time budget (measured at gate G1; owner confirmed 2026-09-30):** a 60 s preview renders in **≤ 45 s** on the VPS, and a 90 s one in ≤ 60 s. If G1 exceeds that, lower the fps to 6 before the size. The tile cache stays in #517 (owner). | It has to be fast enough to be worth waiting for; the first real numbers decide. | A shared tile cache in this plan. |

## Review envelope

REVIEW.md defaults, the trip video envelope (docs/TRIP_VIDEO_PLAN.md, including the
E6 exception for consented geometry) and #518's (docs/VIDEO_CAMERA_QUALITY_PLAN.md)
apply. New points:

- **Previews share the `default` queue** with share tiles and stats, on two
  consumers (`worker`, `worker-poster`), on a 2-vCPU host shared by prod and val.
  Starving those jobs, or running two previews in parallel beyond memory, is in scope.
  The renderer peaks at about 725 MB, mostly from loading the trip, which fits both
  services' limits.
- **The rate limit is a product control**, not a security boundary. A determined
  user can still spend their 10 an hour.
- **Deploy-order window (accepted, as in #518):** an old worker may briefly pick up a
  `default` job it can't run during `docker compose up -d`. An old image can't even load
  `run_video_preview_job`, so RQ fails the job without touching the row. The dialog's
  deadlines (U4) and the 15-minute rate-limit rule (D6) cover it, and the user can retry.

## Boundaries crossed

- **Schema:** `videojob.kind` (non-null, default `'video'`). One Alembic migration
  backfills existing rows as `'video'`. Check `alembic heads` after rebasing.
- **API:** new endpoints only: `POST /api/projects/{name}/video/preview`, and
  `GET …/video/preview/{job_id}` (status) and `…/bytes` (the WebP). Existing
  endpoints and their shapes are unchanged. Old apps don't see previews.
- **Quota semantics:** `videos_this_month` counts `kind='video'` only. Existing rows
  are all `'video'`, so no count changes.
- **Stored data:** a preview WebP under `data/users/<uid>/videos/<job>/`, which is
  already excluded from the storage count and removed on account deletion. It is
  deleted about 1 h after completion.
- **Sweeps:** the video startup sweep is restricted to `kind='video'`, and the
  hourly sweep gains the preview rules. Full-video behaviour is unchanged.
- **Export format:** animated WebP, used for previews only.
- **No deployment change:** no new service or queue, so there is no Upgrade-Note.

## Conventions

1. **Previews never touch full-video behaviour:** quota, emails, token routes,
   retention and the startup sweep act on `kind='video'` exactly as before. Each
   gets a test.
2. **The plaintext rules of #501 hold for previews:** the geometry file is written
   after the row, deleted in `finally` and on every sweep path; it is never logged
   and never in `request_json`; `error_message` holds fixed strings only.
3. **Same camera as the video:** a preview's frame *n* is video frame
   `round(n × 30 / 8)` of the same timeline, camera mode and target resolution, drawn
   at 320×180 with the zoom lowered by `log2(target width / 320)`. Only the frame size,
   the zoom offset and the sampled frames differ.

## Open decisions

None. The owner confirmed the rate limit (10 an hour) and the time budget (D10)
on 2026-09-30.

## Execution units

### Wave 1 — model and renderer (disjoint)

**U1 — Preview kind, migration, quota filter**
- **Goal:** Videojob rows carry a kind, and only full videos count toward the monthly quota.
- **Scope:** `models/project_db.py` (`DBVideoJob.kind`); a new Alembic migration; `src/billing/entitlements.py` (`videos_this_month` filters `kind='video'`, plus a new `previews_in_last_hour(sess, uid, now)`); new `tests/test_video_preview_quota.py`.
- **Context:** the U3 migration of #501 (`alembic/versions/…add_video_job_table.py`) and `DBVideoJob`, as examples; `videos_this_month` / `ensure_video_quota`; memory note: run `alembic heads` after rebasing, and tests must not pin their own head.
- **Do:** 1. Add `kind: str` (non-null, server default `'video'`, indexed with user_info_id and created_at if useful). 2. A migration adding the column with a default and backfill. 3. Filter the quota count to `kind='video'`. 4. Add `previews_in_last_hour`, counting rows of kind `'preview'` created in the last 3,600 s that are `done`, `failed` with `started_at` set (review R3-1), or `pending`/`running` and created within the last 900 s (D6's 15-minute rule; review R2-1).
- **Acceptance:** `pytest tests/test_video_preview_quota.py tests/test_video_quota.py tests/test_alembic_migrations.py tests/test_plan_features_backed.py` passes; `alembic heads` shows one head. New tests: preview rows don't count toward the monthly quota; video rows still do; the preview count honours its one-hour window, counts `done` rows and `failed` rows with `started_at` set, ignores `failed` rows that never started and `expired` rows, and counts `pending`/`running` rows only while they are younger than 15 minutes; the migration backfills existing rows as `'video'`.
- **Out of scope:** API, runner, sweeps.
- **Latitude:** local design
- **Escalate if:** X3; a second Alembic head appears.
- **Depends on:** —

**U2 — Preview render (animated WebP) and the bench**
- **Goal:** `render_preview` renders the video's own camera path (built at the target resolution and 30 fps), sampled at 8 fps and drawn at 320×180, into an animated WebP, and the bench can measure it.
- **Scope:** `src/video/renderer.py` (a new `render_preview` beside `render_video`, sharing trip loading and timeline building); `src/video/bench.py` (a `--preview` flag); new `tests/test_video_preview_render.py`.
- **Context:** `render_video`, `FrameRenderer`, `encode` and `_log_render_summary` in renderer.py (the example to follow); `timeline_for_project(..., fps=...)`; Pillow's `Image.save(..., save_all=True, append_images=..., duration=125, loop=0, format='WEBP', quality=50)`.
- **Do:** `render_preview(job_id, user_info_id, project_id, request, out_path, geometry, progress)`, with the same signature as `render_video` (Convention 5 of #501). It builds the timeline at 30 fps and `camera_path` at the request's target size (`width`/`height`, 1280×720 or 1920×1080) and camera, exactly as `render_video` does. For preview frame *n* it takes shot `round(n × 30 / 8)`, lowers its zoom by `log2(target width / 320)`, and samples the trip state at the **same video time**, `timeline.sample(round(n × 30 / 8) / 30)`. It draws that (shot, state) pair at 320×180 through the same basemap and overlay. `FrameRenderer` builds its own camera path and samples state at `n / fps`, so it gets a way to render explicit per-frame (shot, state) pairs (review R2-2). It then writes an animated WebP of all frames (frame duration 125 ms, looping). It logs the same render summary line with `kind=preview`. Streaming frames into the WebP writer is preferred; if Pillow needs the frames in memory, bound them (320×180 RGB × 720 frames at 90 s is about 124 MB) and say so. `bench --preview` renders a real trip that way and prints the summary.
- **Acceptance:** in the Linux image (CI=1), `pytest tests/test_video_preview_render.py tests/test_video_renderer.py tests/test_video_bench.py` passes. New tests: a 30 s preview of the test trip produces a WebP with `n_frames == 240` at 320×180, looping; for every camera mode on the renderer's synthetic trip (hike, run, ride and a train):
  - **camera:** each preview shot has the same centre as video shot `round(k × 30 / 8)` at the target size, and a zoom exactly `log2(W/320)` lower (Convention 3). For `variable`, shown to fail if the camera path is built at 320×180 or at 8 fps. Those negative controls are required for `variable` only: overview is one shot at any fps and size, and the fixed modes are invariant under (size ÷ 4, zoom − 2) on this trip, so the controls cannot fail there by construction (review R3-2).
  - **state:** each preview frame's state equals `timeline.sample(round(k × 30 / 8) / 30)` (marker position, travelled km, date and card phase). Shown to fail if the state is sampled at `k / 30` or `k / 8`.
  - **pixels:** preview frame *k* equals a direct 320×180 render of that same (shot, state) pair within the golden tolerance. The preview is **not** compared with a downscaled video frame: a lower zoom band and the overlay's minimum sizes make those differ by design (review R2-3). The comparison uses the frames **before** WebP encoding.
  - **WebP:** the file decodes with Pillow to the expected frame count, 320×180, `loop=0` and 125 ms per frame; each decoded frame is nearest to its own pre-encode frame and within a looser codec tolerance of it, set from measurement and recorded in the test (review R3-3). The golden tolerance is not loosened.

  The summary line carries `kind=preview`. The existing goldens are unchanged.
- **Out of scope:** jobs, API, client.
- **Latitude:** local design
- **Escalate if:** X3; Pillow in the image can't write animated WebP.
- **Depends on:** —

**Gate G1 (owner, between waves 1 and 2):** push the branch to `validation`, then on val
run `docker compose run --rm worker python -m src.video.bench --preview --project "<trip>"
--owner <uid> --length 60 --camera variable` and the same with `--camera overview`.
Paste the summary lines. The orchestrator checks D10's budget and, if needed,
lowers the fps to 6 before wave 2.

### Wave 2 — job lifecycle and API

**U3 — Preview jobs: API, runner, sweeps, rate limit**
- **Goal:** Users can request a preview and fetch it, within the rate limit, with the full-video lifecycle untouched.
- **Scope:** `api/video.py`; `src/video/job_runner.py`; `src/jobs/worker.py` (the killed-horse mapping entry for the preview runner, and restricting the startup sweep call to video rows); `tests/test_video_preview_api.py`, `tests/test_video_runner.py`.
- **Context:** `POST /video` and its ordering (503 → 402 → consent → timeline → lock_account → insert → geometry → enqueue), `_consent`, `_file_response`; `run_video_job` / `_cas` / `mark_video_job_interrupted` / `sweep_video_jobs` / `sweep_stale_running_video_jobs`; `lock_account`; `enqueue` / `queue_has_workers` in src/jobs/queue.py.
- **Do:**
  1. `POST /video/preview` takes the same body as `POST /video`: length, camera, `height` (720 or 1080, the target resolution the preview frames like; not checked against the plan, since only the preview's framing depends on it) and `decrypted_geometry`. **All three preview routes take `owner: OwnerParam`**, like every other video route, so companions on a shared trip can preview (review R1-5). Order:
     - 503 unless `queue_has_workers(QUEUE_DEFAULT)`;
     - the non-authoritative rate-limit check ⇒ 429 (D6), before any consent or timeline work (review R1-6);
     - consent 409, whose body carries `previews_left`;
     - timeline 422;
     - in one `lock_account` transaction, the rate limit (≥ 10 in the last hour ⇒ 429 with `Retry-After`) and inserting a row of `kind='preview'` with `request_json` `{length_s, camera, width, height}` (the target resolution; the preview size and rate are fixed in `render_preview`);
     - the geometry file;
     - `enqueue(QUEUE_DEFAULT, run_video_preview_job, job_id, max_retries=0, allow_inline=False, job_timeout=300)`. If that fails, the row is failed and the geometry deleted, with a 503.
  2. `GET /video/preview/{job_id}` (status, owner only, 404 otherwise) and `GET /video/preview/{job_id}/bytes` (`image/webp`, done only).
  3. `run_video_preview_job`: the same CAS lifecycle as `run_video_job`, calling `render_preview`. No emails, on any path: `mark_video_job_interrupted` and the sweeps skip `_notify_failed` for `kind='preview'` (review R1-2). `expires_at = completed + 3600`. Geometry is deleted in `finally`. Fixed failure reasons.
  4. `sweep_stale_running_video_jobs` acts on `kind='video'` only. `sweep_video_jobs` also fails previews running longer than 300 s + 5 min or pending longer than 1 h, and expires them after `expires_at`.
  5. Full-video routes ignore previews: the plan's quota figure, status and download of a preview id through the video routes ⇒ 404, and token routes never match a preview.
- **Acceptance:** `pytest tests/test_video_preview_api.py tests/test_video_api.py tests/test_video_runner.py` passes, with the concurrency tests on a file SQLite database. New tests:
  - the happy path with a fake renderer;
  - 409 consent, and the consent geometry is deleted after done, failed and the sweeps;
  - the 11th preview in an hour ⇒ 429 with `Retry-After`, and two concurrent requests at 9 ⇒ exactly one succeeds;
  - a preview never counts toward the monthly quota and sends no email — on success, on failure, when failed by the hourly sweep, and when interrupted through the killed-horse handler;
  - a companion (`?owner=`) can create, poll and fetch a preview of a shared trip;
  - over the limit on an encrypted trip ⇒ 429 **before** any 409 consent, and the 409 body carries `previews_left`;
  - a preview left `pending` or `running` for over 15 minutes no longer counts toward the limit;
  - the video startup sweep leaves a running preview alone;
  - the hourly sweep expires and fails previews by age;
  - no broker or no `default` worker ⇒ 503 with nothing written;
  - video routes 404 on a preview id.
- **Out of scope:** client; renderer.
- **Latitude:** local design
- **Escalate if:** X3; `lock_account` plus the count can't be made race-free as for the quota.
- **Depends on:** U1, U2, G1

### Wave 3 — client

**U4 — Preview in the video dialog**
- **Goal:** A Preview button renders the current settings and shows the animated result inline, marked out of date when the settings change.
- **Scope:** `flutter_client/lib/src/api/video_api.dart`, `flutter_client/lib/src/projects/video_job_notifier.dart`, `flutter_client/lib/src/projects/video_config_dialog.dart`; new `flutter_client/lib/src/projects/video_preview_image.dart` with web and non-web implementations chosen by conditional import (`video_preview_image_web.dart`, `video_preview_image_io.dart`); `flutter_client/test/video_job_notifier_test.dart`, `flutter_client/test/video_config_dialog_test.dart`, new `flutter_client/test/video_preview_image_test.dart`.
- **Context:** the poster preview fetch-and-show (`poster_job_notifier.dart` `fetchPosterPreview`, `app_screen.dart` `Image.memory`); the notifier's consent geometry (kept for the session and re-sent); the create/poll flow in the notifier; the widget-test traps (pump fixed frames while a spinner shows; swap the global `api`; a 360 dp phone test as in #518 F-c).
- **Do:** Add a "Preview" button under the camera list.
  - **Request:** on tap it posts to `/video/preview` (with `?owner=` for shared trips, as the other calls do) with the current length, camera, resolution and any consented geometry. On a 409 it runs the same consent flow, then retries.
  - **Waiting (review R1-3, R1-7):** it polls the status and shows "Waiting for a worker…" while `pending` and "Rendering preview…" while `running`. It gives up after 10 min pending ("Workers are busy — try again later") or 6 min running, which is past the server's 300 s job timeout, so a slow render that succeeds is never abandoned ("The preview took too long — try again"; review R2-4). A `failed` status shows "The preview couldn't be made — try again", and `expired` behaves like no preview.
  - **Showing it (review R1-4):** it fetches the bytes and shows them in a 16:9 box, captioned "Low-resolution preview", through `VideoPreviewImage`. That is `Image.memory` on Android and iOS, and on the web an `HtmlElementView` wrapping an `<img>` whose `src` is a Blob URL of the WebP (revoked on dispose), so every browser animates it natively.
  - **Out of date:** changing the length, camera or resolution greys it and shows "Preview is for the previous settings".
  - **Errors:** on 429, "Too many previews — try again in N min"; on 503, "Preview unavailable".
- **Acceptance:** `flutter analyze` is clean; in the container, the video tests and then the full suite pass; `flutter build web` succeeds. New tests: the preview request carries the length, camera, resolution, `owner` and consented geometry; the pending and running messages; both deadlines and the failed branch (with a fake clock); the out-of-date state after a length, camera or resolution change; the 429 and 503 messages; the preview box fits at 360 dp; consent is asked once for the preview and then the final video; the non-web `VideoPreviewImage` uses `Image.memory`.
- **Out of scope:** server.
- **Latitude:** local design
- **Escalate if:** X3; the web `<img>` approach can't be built with the app's web setup.
- **Depends on:** U3

**Gate G2 (owner, after wave 3):** on val, open the video dialog on a trip in Chrome,
Safari and Firefox (desktop), and in the Android app. Tap Preview and confirm it
**animates** in each, and that the Follow framing looks like the final video's.
If any browser shows a still image, R1-4 returns as concrete.

## Definition of done

- In the video dialog, "Preview" shows an animated low-resolution preview of the
  chosen length, camera and resolution. It uses the video's own camera path, sampled
  at 8 fps, and renders within D10's time budget measured on the VPS at G1. It
  animates in Chrome, Safari, Firefox and on Android (G2).
- The dialog tells waiting from rendering, gives up with a message after its
  deadlines, and handles failed previews.
- Companions can preview shared trips. No preview ever sends an email, on any path.
- Previews don't count toward the monthly quota, send no email, have no token link,
  and are deleted about 1 h after completion. At most 10 per user per hour; the 11th
  is a 429.
- Encrypted trips need one consent per dialog session. Preview geometry follows the
  #501 plaintext rules (tests for each deletion path).
- Full-video behaviour (quota, emails, sweeps, routes) is unchanged (tests).
- `alembic heads` shows one head. The server and Flutter suites pass, video tests
  pass in the Linux image, and CI is green.
- The PR carries a `Release-Note:` trailer. No Upgrade-Note.
