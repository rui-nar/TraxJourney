# Review ledger — Immich album photo upgrade

Subject: docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md
Envelope: the plan's `## Review envelope` section, on top of REVIEW.md §2 defaults

## Round 1 — 2026-10-28, reviewed at a86e36b5

### R1-1 — photo_lock is per-process, so the apply job in worker-poster races the API's photo writers
- Trigger: the owner starts an apply (worker-poster, D9) and, mid-batch, deletes a photo of the same memory or a Polarsteps from-url download lands on it → both read photos_json and the later commit wins: a deleted photo reappears without files, or the upgraded original drops out of the list after the old files were unlinked, or an imported photo is orphaned.
- Scores: trigger=plausible, impact=data-loss, detect=user-visible, later=cheap, fix=M/shared, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-2 — Unique (project, user) scan index contradicts carrying rejections over to the next scan
- Trigger: a user rejects a pair in scan 1, then starts scan 2 → the old row must be deleted (its rejections go with it, so the pair comes back ticked and is applied) or reused (old and new rows mix); inserting a second row fails on the index.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-3 — The F5 guard only protects photos upgraded by the new apply job
- Trigger: a user upgrades photos through the per-memory dialog (no source recorded), then a Polarsteps import adopts those memories → retain keeps only sourced photos, so the dialog-upgraded originals are deleted and the thumbnails come back.
- Scores: trigger=plausible, impact=data-loss, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-4 — The album iterator's 10-page cap equals the 10,000 limit, so a larger album is silently truncated
- Trigger: a user picks an album with more than 10,000 assets in the trip's dates → the iterator stops at 10,000, the "over 10,000" failure never fires, and the photos whose originals sit past the cap show "no match".
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-5 — A killed apply batch leaves apply_status=running with no sweep to recover it
- Trigger: a deploy or OOM kill hits worker-poster mid-batch → no next batch is enqueued, the apply stays running and rows stay applying; new scans answer 409 and delete is refused until the 30-day sweep.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a photo-upgrade scan or apply is found stuck in scanning/running, or rows left applying, after a worker restart, OOM kill or job timeout (val/prod data, worker logs, or a user told a new scan is refused).
- Guard: —
- Override: user: Fix now — owner chose to fix rather than ship known gaps (2026-10-28)
- Outcome: fixed

### R1-6 — The 15-minute pending sweep fails jobs legitimately queued behind the single photos consumer
- Trigger: user A's long scan or a poster render occupies worker-poster; user B's scan or apply waits in Redis → after 15 minutes the sweep fails it with a false "No worker is processing photo jobs".
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: the pending sweep fails a photo job while worker-poster was alive, or photo jobs from more than one user overlap on val/prod, or a scan runs longer than about 10 minutes.
- Guard: —
- Override: user: Fix now — owner chose to fix rather than ship known gaps (2026-10-28)
- Outcome: fixed

### R1-7 — D9's "a poster waits behind at most one batch" is false for scans
- Trigger: a user scans a 10,000-photo album while another user requests a poster → the poster waits for the whole scan, up to 30 minutes.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a poster job waits behind a photo scan for more than a few minutes (created_at to started_at), or scans on val/prod regularly run longer than about 5 minutes.
- Guard: —
- Override: user: Fix now — owner chose to fix rather than ship known gaps (2026-10-28)
- Outcome: fixed

### R1-8 — U10 imports U11's start_apply, but U11 runs in parallel and is not a dependency
- Trigger: the U10 implementer imports start_apply while U11 is written in a sibling worktree → the import fails or a stub must be matched at integration.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-9 — U10 orders suggestions by a position column U6 does not define
- Trigger: the U10 implementer writes the cursor query → no column orders photos within a memory, so the review shows them out of trip order or loads every photo list per page; U6 cannot add it (latitude none).
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
