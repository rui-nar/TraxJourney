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

## Round 2 — 2026-10-28, reviewed at 9ad99045

### R2-1 — U14 exempts archive import, but a ZIP replace-import rewrites existing photo lists blind
- Trigger (corrected by triage): the owner replace-imports a trip ZIP while an apply runs on it → (a) the worker's compare-and-set commits [A'] just before the import's write transaction, the import still finds A on disk and stores [A], deleting A' as dropped; or (b) a staged photo fails placement and `_drop_unplaced` (api/project_transfer.py:545-561) writes its stale read over [A'], serialised only by the API-local photo_lock → A listed with no file, A' deleted or orphaned and still counted. The reviewer's original interleaving cannot happen on SQLite: bump_lock_version (src/project/repo_transfer.py:331) opens the write transaction before the kept rows are read.
- Scores: trigger=plausible, impact=data-loss, detect=user-visible, later=cheap, fix=M/shared, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R2-2 — Apply is not refused while a newer scan is active, so the new scan's match deletes the scan being applied
- Trigger: a user starts a new scan, then presses Apply on the earlier ready review from a second tab or device → when the new scan becomes ready it deletes the earlier scan; the next apply batch finds no rows, the review answers 404, remaining ticks and picks are gone, no stop reason is shown.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a client shows an earlier ready review with its Apply bar while a newer scan is pending or scanning, or a user reports a review disappearing or an apply stopping partway with no reason.
- Guard: —
- Override: user: Fix now — owner chose to fix rather than ship known gaps (2026-10-29)
- Outcome: fixed

### R2-3 — Two concurrent POST scans: the second fails on the partial unique index as a 500
- Trigger: a user double-taps Scan or retries a slow request → both pass the active-scan check, the second insert violates the partial unique index → logged 500 while the first scan runs.
- Scores (corrected by triage): trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: the unhandled-exception log (api/router.py:417) shows an IntegrityError from POST /api/projects/{name}/photo-upgrade/scans, or a user reports an error on Scan although the scan started.
- Guard: —
- Override: user: Fix now — owner chose to fix rather than ship known gaps (2026-10-29)
- Outcome: fixed
