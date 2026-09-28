# Review ledger — Trip video export P1 + P2

Subject: docs/TRIP_VIDEO_PLAN.md (round 1 reviewed its draft at C:\Users\rui_n\.claude\plans\trip-video-p1-p2.md, before it was reshaped to the write-plan format)
Envelope: plan section "Review envelope" (REVIEW.md defaults + stated E6 exception for per-job consent)

## Round 1 — 2026-09-27, reviewed at eaaeee78 (plan draft, uncommitted)

Reviewer: Fable (general-purpose agent carrying adversarial-reviewer's instructions; the project agents were not registered in the session). Triager: Opus, same arrangement.

### R1-1 — OOM-killed video work-horse is never failed; consent plaintext kept indefinitely
- Trigger: user consents on an encrypted trip → worker-video horse SIGKILLed by the memory limit → job stays "running", no email, decrypted geometry.json stays on disk until the next API restart (worker.py's killed-horse handler only handles run_poster_job; the orphan sweep runs only in the API lifespan)
- Scores: trigger=plausible, impact=security, detect=silent, later=expensive, fix=S/shared, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan U4 step 3 dispatch mapping + U6 step 5 mark_video_job_interrupted; Convention 2 delete paths; daily sweep backstop)

### R1-2 — "retries = 0" through enqueue() builds Retry(max=0), which RQ rejects
- Trigger: any user starts a video → Retry(max=0) raises inside enqueue's try → falls to the in-process branch → API renders for minutes, or a false 503 with Redis healthy
- Scores: trigger=concrete, impact=wrong-visible (triager: was degraded-ux), detect=logged, later=cheap, fix=S/shared, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan U4 step 2: max_retries=0 passes no Retry; test in U4 acceptance)

### R1-3 — MP4s under data/users/<uid>/ are counted by the nightly storage reconcile
- Trigger: tier_1 user renders a few videos → 03:30 reconcile counts videos/ → next photo upload refused with 402, contradicting "videos don't count"
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan D6: reconcile_usage skips videos/, files stay under users/<uid> so account deletion still removes them; U3 step 5)

### R1-4 — Email token link goes to a Flutter route the plan never adds
- Trigger: render finishes → user opens FRONTEND_ORIGIN/video/{token} with no session → no GoRoute, no auth-redirect exemption → login/unknown route, no download
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan U7: video_download_screen, /video/:token route + auth-redirect exemption, router test)

### R1-5 — Per-active-day floor makes long trips unrenderable (>~31 active days at 90 s, >65 under 180 s)
- Trigger: tier_1/2 user with a 40-active-day trip opens the dialog → min runtime ≈ 4.3 + 2.7 × active_days s → every offered length overflows
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=L/local, confidence=verified
- Decision: Defer (D8)
- Revisit when: the U1 allocation CLI on the owner's longest real trips shows no offered length fits, or a user reports an unrenderable trip; also correct §3.2's "120-activity trip" claim then
- Guard: —
- Override: user: Fix now — trips can have 365 days
- Outcome: fixed (plan D5 + "Clips" design: adjacent merge to N_max, date ticker replaces day cards; U1 acceptance has a 365-day, 1,000-leg case)

### R1-6 — Synchronous timeline build decodes every polyline on the API process
- Trigger: user with a ~200-activity trip opens the dialog → /video/plan decodes all polylines on the request thread → 2–4 s GIL hold, other users' requests stall (documented 502 mechanism)
- Scores: trigger=concrete, impact=degraded-ux, detect=logged, later=cheap, fix=M/local, confidence=verified
- Decision: Defer (D8)
- Revisit when: /video/plan or POST /video exceeds 1 s server-side on a real trip, or a 502/latency spike coincides with a plan request; revisit before U5 if U1's helper can't reuse activity_geo_prepared for free
- Guard: —
- Override: —
- Outcome: open

### R1-7 — No-broker 503 not ordered before the quota insert and geometry write
- Trigger: Redis recreated during compose up, or self-hoster without Redis → row inserted + geometry.json written → enqueue finds no broker → 503; the pending row burns the free monthly video and the plaintext has no terminal path
- Scores: trigger=plausible, impact=security (triager: was wrong-visible), detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager: was inferred)
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan D7 + U6 step 2 ordering: broker check before any write; enqueue(allow_inline=False) failure ⇒ failed row + geometry deleted)

## Round 2 — 2026-09-28, reviewed docs/TRIP_VIDEO_PLAN.md at eaaeee78 (uncommitted; fixes for R1-1..R1-5, R1-7 + owner decisions D10–D13)

Reviewer: Fable; triager: Opus (same arrangement as round 1).

### R2-1 — API-startup orphan sweep fails video jobs the separate worker is still running or has queued; the worker then completes them without their consent geometry
- Trigger: API container restarts alone, or a deploy recreates containers while `video` has a job running/queued in Redis → API sweep marks it failed, deletes geometry.json, emails "failed" → worker renders it anyway (runner has no terminal-state guard, poster example) → "ready" email, encrypted trip's video missing every consented track
- Scores: trigger=plausible, impact=wrong-visible (triager: was silent-wrong), detect=user-visible (triager: was silent), later=cheap, fix=M/local, confidence=verified
- Decision: Defer (D10) — floor removed by the triager's rescoring; flagged to the user
- Revisit when: a videojob goes failed → running/done, a user gets both failed and ready emails for one job, or a deploy happens with a video job queued or running
- Guard: —
- Override: user: Fix now — renders take minutes, so a deploy during one is realistic
- Outcome: fixed (Convention 6 compare-and-set transitions; U6 step 4 runner guards; U6 step 5 worker-startup sweep of running rows only, no API video sweep)

### R2-2 — POST /video returns 422 "nothing to animate" before checking consent, so an encrypted activity-only trip never reaches the consent dialog
- Trigger: E2EE user with an activity-only trip taps Video → every activity yields no geometry → zero legs → 422 at step (b) before the 409 at step (c) → the trip can never be rendered
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (U6 step 2: consent checked before the timeline, also in /video/plan; test for an encrypted-only trip)

### R2-3 — queue_available() caches a failed first Redis probe for the API process lifetime
- Trigger: Redis slow/restarting when the API starts → cached None → video "unavailable"/503 for everyone until the API restarts
- Scores: trigger=plausible, impact=degraded-ux (triager: was wrong-visible), detect=logged (triager: was user-visible), later=cheap, fix=S/shared, confidence=verified
- Decision: Defer (D10)
- Revisit when: the API logs "REDIS_URL is set but Redis is unreachable" while workers are processing jobs, or video shows unavailable/503 while Redis is healthy
- Guard: —
- Override: —
- Outcome: open

### R2-4 — No path fails a running video row when the whole worker-video container dies and the API stays up
- Trigger: worker-video container killed (cgroup OOM on the parent, compose restart) mid-render → killed-horse handler never runs, API sweep never runs → row stays running forever, status spins, no email, free monthly video consumed
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a videojob stays pending/running past job_timeout (1800 s), or a user reports a stuck video or a quota used by a video that never finished
- Guard: —
- Override: user: Fix now — renders take minutes, so a deploy during one is realistic
- Outcome: fixed (U6 step 6: hourly sweep fails running rows past timeout + 5 min and pending rows older than 24 h; U3 adds started_at)

### R2-5 — U6 step 2(d) relies on a SQLite write-lock helper that doesn't exist
- Trigger: U6 implementer reaches the quota+insert transaction → no helper in models/db.py → escalation condition already true, the concurrency acceptance can't be met
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7) — triager: the idiom exists as `lock_account` in src/billing/subscriptions.py:49 (no-op UPDATE, like repo_core.bump_lock_version); point U6 at it
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (U6 step 2(d): lock_account as the first write; escalation reworded)

### R2-6 — New limit env vars use a PLAN_<PLAN>_ prefix the other limits don't use
- Trigger: operator sets TIER_1_MAX_VIDEOS_PER_MONTH like the other limits in .env.example → ignored, defaults stay, nothing logs it
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (U3 step 3: <PREFIX>_MAX_VIDEOS_PER_MONTH / <PREFIX>_MAX_VIDEO_HEIGHT, .env.example, env-reset test)

## Round 3 — 2026-09-28, reviewed docs/TRIP_VIDEO_PLAN.md at eaaeee78 (uncommitted; fixes for R2-1, R2-2, R2-4, R2-5, R2-6)

Reviewer: Fable; triager: Opus (same arrangement as rounds 1–2).

### R3-1 — Hourly sweep deletes geometry.json of a job that is still legitimately pending
- Trigger: E2EE user consents while the single video worker is busy or restarting → row pending > 30 min (allowed up to 24 h) → hourly sweep deletes geometry.json by age → worker later runs the job without it → "ready" video missing the consented tracks, or a "failed" email for valid input
- Scores: trigger=plausible, impact=wrong-visible (triager: was silent-wrong), detect=user-visible (triager: was silent), later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — floor removed by rescoring, consistent with R2-1; flagged to the user
- Revisit when: a video job stays pending > 30 min before running, a consented job reaches running without geometry.json, or a user reports an encrypted trip's video or failed email missing consented tracks
- Guard: —
- Override: user: Fix now — same failure as R2-1/R2-4, a few words in the plan; no further review round
- Outcome: fixed (U6 step 6: geometry of pending/running jobs is never deleted by age; acceptance test added) — not re-reviewed, by user decision

### R3-2 — "Encrypted activity" is defined by the polyline only; a trackless activity with encrypted endpoints is dropped and its geometry refused
- Trigger: E2EE user with a GPX/private activity (no polyline, encrypted start/end) taps Video → not flagged for consent, leg dropped into skipped, a decrypted 2-point line from the client is rejected as plaintext
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: an encrypted trip's /video/plan lists in skipped an activity with start_latlng_enc/end_latlng_enc and no polyline, or a user reports a trackless activity missing from an encrypted trip's video; or before U6 ships if its implementer defines "encrypted activity" in one place anyway
- Guard: —
- Override: user: Fix now — one-line definition; no further review round
- Outcome: fixed (U6 step 2(b): is_encrypted_activity covers envelope polyline or endpoints; 2-point line accepted; acceptance test added) — not re-reviewed, by user decision

## Review closed — 2026-09-28

Round 3 had no Fix now under the rules; the user overrode R3-1 and R3-2 to Fix now and chose no round 4. Open deferred entries: R1-6, R2-3.

## Unit U3 review, round 1 — 2026-09-28, diff 6459a9ba..2a7cc06f (before integration, DELIVERY.md §5 point 3)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

### U3R1-1 — videojob has no error_message column, but the video status route copies the poster's shape that has one
- Trigger: a render fails → U6's status route has no column to report why → the client shows "failed" with no reason, or U6 needs a second migration outside its scope
- Scores: trigger=plausible (triager: was concrete), impact=degraded-ux, detect=user-visible, later=expensive, fix=S/local, confidence=verified
- Decision: Fix now (D5) — approved by the user 2026-09-28
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (U3a, ead73b3d)

## Unit U6 review, round 1 — 2026-09-28, diff 3bf84285..94367a15 (before integration, DELIVERY.md §5 point 3)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

### U6R1-1 — decrypted_geometry has no size bound; an oversized body is decoded on the API request thread, on a route with no quota
- Trigger: a hostile authenticated client posts /video/plan on its own encrypted trip with a tens-of-MB polyline, repeatedly → decoded twice and haversine-summed on the request thread → other users' requests stall (the R1-6 mechanism, but with client-supplied input); /plan has no quota and writes nothing
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — not a duplicate of R1-6 (client-supplied input, not stored tracks)
- Revisit when: a /video/plan or POST /video body over 1 MB or over 1 s server-side appears in logs; a 502/latency spike coincides with a video request; R1-6 is picked up; or before the feature PR merges if the owner wants the request contract bounded before clients ship
- Guard: —
- Override: user: Fix now — cheap while the request contract is unshipped
- Outcome: fixed (U6a, 3ecbc07e) — residual: a request at the 6M-char total still costs ~5 s CPU and /video/plan has no quota; only a rate limit would stop repeats

## Unit U7 review, round 1 — 2026-09-28, diff 44be198f..957682fc (before integration, DELIVERY.md §5 point 3)

Reviewer: adversarial-reviewer (Fable); triager: review-triager (Opus).

### U7R1-1 — Consent geometry built from /meta activities, whose polylines are deferred: a tracked encrypted activity is sent as a 2-point line
- Trigger: E2EE user opens a trip and creates a video before the background details fetch has merged (or it failed / trip loaded offline) → summary_polyline is null because /meta defers it → client sends start/end as a 2-point line → server accepts it → video draws straight lines, ready email, quota used, nothing tells the user
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D3) — approved by the user 2026-09-28
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (U7a)

### U7R1-2 — Cancel stays enabled while POST /video is in flight; the job is created and charged but the client never learns about it
- Trigger: user taps Create, then Cancel before the response → dialog disposes → server commits, counts and enqueues the job → no status card; later a ready email and "0 of 1 left"
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6) — approved by the user 2026-09-28
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (U7a)

### U7R1-3 — "Download video" plays the MP4 inline instead of saving it; on phones the email link is the only download path
- Trigger: user opens the ready email on iOS/Android (or web), taps Download → server sends no Content-Disposition → browser plays inline, no save
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local (triager: was M — fix is `filename=` on FileResponse in api/video.py), confidence=inferred
- Decision: Fix now (D7) — approved by the user 2026-09-28
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (U6c, 97baeca3)

## Integrated review, round 1 — 2026-09-28, feat/trip-video at 449073f6 vs eaaeee78 (DELIVERY.md §5 point 2)

Reviewer: adversarial-reviewer (Fable; its output numbered F1-2..F1-4, no F1-1); triager: review-triager (Opus). F1-5 came from the U2 implementer's report, reproduced by the orchestrator, triaged in the same round.

### F1-2 — A broker with no `video` consumer looks available; the job is accepted, charged and left pending for 24 h
- Trigger: a deployment runs Redis but no worker-video (or it is dead) → plan says available, POST 201 → nothing consumes `video` → failed only after 24 h, the free monthly video locked meanwhile
- Scores: trigger=plausible (triager: was concrete — the stale runbook path is fixed by F1-3), impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a videojob stays pending > 30 min with nothing running on `video`; a prod/val deploy goes out without worker-video; worker-video crash-loops while the API reports available; a user reports a video that never started
- Guard: —
- Override: user: Fix now — the compose file already promises "no worker-video, no feature", and the failure costs the free monthly video
- Outcome: open

### F1-4 — Encrypted trip asks for consent and uploads decrypted tracks before the user learns the month's quota is used up
- Trigger: free user with an encrypted trip and no video left taps Create → /video/plan 409 before quota → consent, decrypt, upload → plan then shows 0 left with Create enabled → upload again → 402
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7) — approved by the user 2026-09-28
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### F1-3 — docs/DEPLOYMENT_VPS.md still describes two worker services; plan requires it updated for worker-video
- Trigger: operator follows the runbook on prod/val → adds only worker and worker-poster → `video` has no consumer (F1-2)
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7) — approved by the user 2026-09-28
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### F1-5 — A flight crossing ±180° is animated and drawn the long way round the world
- Trigger: user with a transpacific flight (Tokyo → Los Angeles) creates a video → arc coords jump 172.0 → -175.2 → marker sweeps ~347° across the map, route drawn across the world, camera fits the whole world
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=L/local (triager: was M — spans legs, camera and renderer), confidence=verified
- Decision: Fix now (D6) — approved by the user 2026-09-28
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

## Delivery

Feature branch `feat/trip-video` (from `docs/trip-video-plan`, PR #498 to be closed in favour of the feature PR). Split approved by the user 2026-09-28 without overrides. The implementer and verifier project agents were not registered in the session; their instructions ran on general-purpose agents with the routed model.

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Legs, clips and pacing engine + CLI | Opus | S2 | 1 | — | yes | — |
| U3 | videojob table, migration, video quotas, storage reconcile | Opus | S4 | 1 | — | yes | U3R1-1 |
| U3a | error_message on videojob (fix unit for U3R1-1) | Opus | S4 | 1 | — | yes | U3R1-1 |
| U4 | video queue, worker kill dispatch, compose, ffmpeg | Opus | S5 | 1 | — | yes | — |
| U2 | Deterministic follow camera | Opus | S2 | 1 | — | yes | — |
| U6 | Video API, runner, consent, sweeps, emails | Opus | S5 | 1 | X3 → scope widened (4 email templates) | yes | U6R1-1 |
| U6a | Bound decrypted_geometry size (fix unit for U6R1-1) | Opus | S5 | 1 | — | yes | U6R1-1 |
| U6b | Exempt video routes from the payload-cache guard (wave 2 integration failure) | Sonnet | — | 1 | — | yes | — |
| U5 | Frame renderer and encoder | Opus | S4 | 1 (+1 resume after network outage) | X3 → scope widened (CI ffmpeg step) | yes | — |
| U7 | Flutter request/consent/status/download | Opus | S5 | 1 | — | yes | U7R1-1, U7R1-2, U7R1-3 |
| U7a | Real encrypted tracks for consent; Cancel locked while submitting (fix unit for U7R1-1/-2) | Opus | S5 | 1 | — | yes | U7R1-1, U7R1-2 |
| U6c | Video download as attachment (fix unit for U7R1-3) | Sonnet | — | 1 | — | yes | U7R1-3 |

Note: every wave-1 worktree was created from `main` (27a2d645), not from `feat/trip-video`; each implementer reset its clean branch to 6459a9ba before starting. Later waves must check the base first.
