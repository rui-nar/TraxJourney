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

## Round 3 — 2026-10-29, reviewed at 46477da0

### R3-1 — The scripts exemption says their runbooks take the whole stack down, but the #237 reorder runbook runs with the API and workers up
- Trigger: a future script author, or the U14 implementer, reads "scripts need no compare-and-set because their runbooks stop the stack", writes a plain read-modify-write of photos_json and photo_order_json, and gives it a runbook like the #237 reorder step ("with the API running", docs/RELEASING.md:237-295) → the script and worker-poster's apply overwrite each other's photo lists. No current script does this: `_write_reorder` (scripts/reorder_polarsteps_memory_photos.py:172-196) is already a compare-and-set, dedupe_memory_photos.py and backfill_thumbnail_orientation.py need `--api-stopped` and `docker compose down`, and the other scripts never write photo lists. The exemption holds; its stated reason does not.
- Scores: trigger=theoretical, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: user: Fix now — the plan must not state a false reason, even for an exemption that holds (2026-10-30)
- Outcome: fixed

## U3 unit review — 2026-10-30, reviewed at fa88eaa5

### U3-R1-1 — U3 edited two test files outside its Scope without the X3 escalation
- Trigger (corrected by triage): the reviewer could not see the delivery. The U3 implementer did escalate under X3 before editing anything, and the orchestrator widened the Scope to tests/test_memories_photo_order.py and tests/test_reorder_polarsteps_memory_photos.py, only to add "sources": {} to full-state assertions. What remains needs an orchestrator that skips the DELIVERY.md §6 row, which no current step produces.
- Scores (corrected by triage): trigger=theoretical, impact=maintainability, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: —

## U6 unit review — 2026-10-30, reviewed at 112b884c

### U6-R1-1 — The three-table scan cascade is hand-written at each delete site, with no foreign-key enforcement to catch a site that forgets a child table
- Trigger: the U9 or U10 implementer writes one of the three later scan-deleting sites (U9 Do 5, the U10 DELETE route, the 30-day sweep) as a single `delete(DBPhotoUpgradeScan)` → the scan's asset and suggestion rows stay forever, with no error, since SQLite runs without `PRAGMA foreign_keys` (models/db.py:93-97).
- Scores (corrected by triage): trigger=theoretical, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: user: Fix now — owner chose to fix rather than ship known gaps (2026-10-30)
- Outcome: fixed

## U6 unit review round 2 — 2026-10-30, reviewed at 17ff74d5

### U6-R2-1 — delete_photo_upgrade_scans with no condition silently deletes every scan, album photo and suggestion of every user
- Trigger: a U9 or U10 implementer calls the helper with a condition list that is empty on some path → the three DELETEs carry no WHERE, and the commit empties all three tables for every user, with a non-zero count that looks like success. Both current callers pass one fixed condition, so nothing does this today.
- Scores: trigger=theoretical, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D4), flagged to the user
- Revisit when: —
- Guard: `if not where: raise ValueError(...)` at the top of the helper, with a test that a call with no condition raises and deletes nothing.
- Override: —
- Outcome: guard added

## U4 unit review — 2026-10-30, reviewed at 6b6c7725

Envelope question to the owner: is 100 MP still the bound for the 768 MB API container, given that a near-cap PNG needs about 700 MB at its RGB conversion and a HEIF decode holds two or three full copies, with uploads decoding in parallel?
Answer (owner, 2026-10-30): HEIC/HEIF capped at 50 MP on the coded size of every image item; one full decode at a time per process; other formats keep 100 MP. The plan's envelope is amended.

### U4-R1-1 — The HEIF pixel cap is read from the size after cropping, so a `clap`-cropped HEIC decodes at its full coded size
- Trigger: any signed-in user uploads a crafted HEIC whose `ispe` declares about 30000x30000 with a `clap` crop to 100x100 → the 100 MP check passes on (100, 100), libheif decodes the full image (about 2.7 GB) → the API container (768 MB) is OOM-killed, for every user, repeatably.
- Scores (corrected by triage): trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=inferred
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### U4-R1-2 — Under the cap, a HEIF decode holds libheif's and Pillow's full copies at once
- Trigger: a signed-in user uploads a 99 MP HEIC (a few hundred KB for a flat image) → libheif's buffer stays alive while Pillow fills its own (Pillow stores RGB at 4 bytes per pixel), about 700-800 MB at peak → the API container is OOM-killed.
- Scores (corrected by triage): trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D6). Depends on the envelope question: a lower HEIF cap is the fix; keeping 100 MP with a larger container budget would make this D1.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### U4-R1-3 — A HEIC with more than 65533 bytes of EXIF is refused as not a readable image
- Trigger: a user uploads a valid HEIC with an EXIF block over the JPEG marker limit → Pillow's JPEG writer raises, mapped to a 422. No known phone or camera writes such a block.
- Scores: trigger=theoretical, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: —

### U4-R1-4 — A HEIC whose rotation is only in its EXIF is stored sideways, and the original is not kept
- Trigger: a user uploads a HEIC with EXIF orientation and no `irot`/`imir` → pillow-heif resets the tag without rotating, and the JPEG is stored sideways. HEIF readers are specified to use `irot`/`imir`, and no known producer writes EXIF-only orientation.
- Scores: trigger=theoretical, impact=wrong-visible, detect=user-visible, later=expensive, fix=S/local, confidence=inferred
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: —

## U4 unit review round 2 — 2026-10-30, reviewed at c0817a59

Envelope question to the owner: the 50 MP cap refuses real files the 100 MP cap accepted (Android "Hi-Res" HEIF at 8192x6144, 50.3 MP; Sony A7R V / A7CR HEIF at 60.2 MP). Nudge the cap?
Owner answer (2026-10-30): raise it to 51 MP, and decode HEIC/HEIF in a child process with a hard memory limit, so the cap is a clear refusal and the limit is the protection. Both findings below are fixed that way, plus `decode_threads=1`. The plan's envelope Memory bullet is updated.

### U4-R2-1 — The HEIF cap reads `ispe`, but the HEVC decoder sizes its frame from the bitstream's SPS
- Trigger: a signed-in user uploads a HEIC whose `hvc1` item declares `ispe` 4000x3000 but carries an HEVC stream whose SPS declares 25000x25000 → the check passes on 12 MP → libde265 allocates a 625 MP frame → the API container is OOM-killed, repeatably.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=inferred (pillow-heif adopting the decoded size is confirmed; libheif not bounding libde265 to `ispe` is not)
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### U4-R2-2 — libheif decodes grid tiles on up to 4 threads, which the process lock cannot see
- Trigger: a signed-in user uploads a HEIC with a small grid of four tiles, each with an honest 48 MP `ispe` → every per-item check passes → libheif decodes the tiles concurrently, about 600-700 MB → OOM.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred (`decode_threads=4` confirmed; libheif's parallel tile decode not read in source)
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

## U4 unit review round 3 — 2026-10-30, reviewed at 228fbbc8

Envelope question to the owner: the child's limit was measured on 8-bit HEICs only. Are 10-bit HEIF files from mirrorless cameras (Canon R5, Nikon Z8/Z9, Fujifilm X-T5, 24-46 MP) inside the envelope?
Owner answer (2026-10-30): yes, in scope. A 10-bit file the child cannot decode must be refused early as too large, not as unreadable. The owner also asked for a fourth review round on the fixes.

### U4-R3-1 — The 576 MiB child limit is sized from 8-bit HEICs only; a 10-bit camera HEIF under 51 MP may exceed it and be refused as "not a readable image"
- Trigger: a user uploads a 10-bit HEIF from a 40-46 MP camera → the size check passes → the child runs out of address space while libheif converts 10-bit YUV to 8-bit RGB → a valid photo is refused as unreadable.
- Scores: trigger=plausible, impact=wrong-visible, detect=logged, later=cheap, fix=M/local, confidence=inferred (one conversion path the reviewer gives fits within 576 MiB; not measured)
- Decision: Defer (D10)
- Revisit when: the "HEIF decode failed" warning appears for an upload under `HEIF_MAX_PIXELS` that did not time out; a user reports a camera HEIF refused as unreadable; or a measured 40-46 MP 10-bit HEIF fails at `HEIF_CHILD_MEMORY`.
- Guard: —
- Override: user: Fix now — 10-bit camera HEIF is in scope; measure it, and refuse early by bit depth if it does not fit (2026-10-30)
- Outcome: fixed

### U4-R3-2 — The decoding child inherits the API's full environment, so a decoder exploit could read server secrets
- Trigger: an attacker uploads a HEIC exploiting a memory-safety bug in libheif/libde265 (none known in 1.23.6) → code runs in the child → reads the database URL, signing keys and Stripe/Immich credentials from its environment.
- Scores: trigger=theoretical, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified. Triage correction: not "no regression" — main never decoded HEIF, so U4 exposes libheif/libde265 to untrusted uploads for the first time.
- Decision: Guard (D4), flagged to the owner (F1 floor)
- Revisit when: the guard fires.
- Guard: in `_heif_to_jpeg`, log at ERROR "HEIF decoder crashed: signal N, sha256=<hash>, bytes=<len>" when the child dies on SIGSEGV, SIGBUS, SIGILL or SIGFPE (not SIGABRT/MemoryError, which is the normal limit outcome).
- Override: user: Fix now — start the child with a PATH-only environment as well as adding the guard (2026-10-30)
- Outcome: fixed

## U4 unit review round 4 — 2026-10-30, reviewed at e639129d

Run at the owner's request, past the 3-round cap. No findings.

Envelope question to the owner: with the child limit at 576 MiB, the measured caps refuse 10-bit 4:2:2 above 31 MP, which covers Canon R5 (45 MP) and Fujifilm X-T5 (40 MP) HEIF, while a JPEG of the same size from the same camera is accepted. The same limit also bounds the apply worker (1280 MB container), which could afford about 709 MiB. Is refusing those files intended for both uploads and Immich applies, or should the worker run the child with a higher limit and caps derived from it?
Owner answer (2026-10-30): the worker gets a higher limit. Each process sets its own limit through `HEIF_DECODE_MEMORY_MIB` (default 576 MiB), and the caps are computed from it. Plan envelope, U9 and U13 updated.

## U4 unit review round 5 — 2026-10-30, reviewed at 3ff50f45

Per-process child limit (`HEIF_DECODE_MEMORY_MIB`, default 576, worker-poster 896) and caps computed from it. Run at the owner's request. No findings.

Envelope question to the owner: an unusable `HEIF_DECODE_MEMORY_MIB` fails on the first HEIC decode (a 500 for that upload), not at process start. Is first-use failure intended, or should the API and the worker refuse to start?
Owner answer (2026-10-30): refuse to start, with a clear message naming the variable and why its value is unusable. Assigned to U9.

## U15 unit review — 2026-10-31, reviewed at 9f7936a2

### U15-R1-1 — ZIP Replace reads the kept memory and journal rows into the session before the bump, so an apply that finishes in that window leaves the upgraded photo orphaned and counted
- Trigger: the owner Replace-imports a trip ZIP while the last apply batch on that trip is finishing → `already_present` loads the rows (photos [A]) into the session → the worker commits [A'], unlinks A, sets `done` → `replace_project` bumps, sees `done`, and proceeds on the stale identity-map rows → A' is never removed: it stays on disk, unlisted, counted against the owner's storage.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/shared, confidence=verified (the implementer reported the same gap independently)
- Decision: Fix now (D3, F3 floor)
- Revisit when: —
- Guard: —
- Override: user: keep as a safeguard — not reproducible on 9f7936a2 (rows read before the bump are freed), but nothing guarantees it; expire_all after the bump, test keeps the early rows alive (2026-10-31)
- Outcome: guard added (3500e3a3; verifier pass, test fails without it)

## U8 unit review — 2026-10-31, reviewed at 37443e6a

Envelope question to the owner: the net-change quota check refuses an account already over its limit even a replacement whose net change is 0 or negative. Allow a replacement that does not increase storage?

Owner answer (2026-10-31): allow it. A replacement whose net change is 0 or negative skips the quota check, so an account over its limit after a downgrade can still swap or upgrade photos. Assigned to U8.

### U8-R1-1 — Both replace routes no longer contain a bust call in their own source, so tests/test_project_cache_invalidation.py fails on them
- Trigger: CI runs `test_every_mutating_project_route_busts_the_payload_cache` after U8 merges → it fails naming both `replace_photo` routes.
- Scores: trigger=concrete, impact=maintainability, detect=logged (triage corrected from user-visible), later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (69981a3e)

### U8-R1-2 — tests/test_photo_upload_processing.py still expects the replace routes to hand `_save_photo_files` to run_in_threadpool
- Trigger: CI runs the two `*_replace_uses_run_in_threadpool` tests → they fail on the expected callable.
- Scores: trigger=concrete, impact=maintainability, detect=logged (triage corrected), later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (69981a3e)

### U8-R1-3 — Pre-lock sha256 reads can raise FileNotFoundError when a concurrent delete removes the file, turning a 404 or skip into a 500
- Trigger: a companion deletes photo A while the owner's replace of A (or a re-import adopting the memory) is between `is_file()` and `read_bytes()` → 500 instead of 404; in U11's apply job, an unexpected error instead of `photo_gone`.
- Scores: trigger=plausible, impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: U11 runs `replace_photo_bytes` and a row ends with an unexpected error instead of `photo_gone`, or a 500 tracing to FileNotFoundError from `photo_replace.py` or `_hash_unhashed_photos` appears in the API logs.
- Guard: —
- Override: user: Fix now — U11's apply runs this code over whole batches; treat a vanished file as photo gone (404 on replace, skip on re-import) (2026-10-31)
- Outcome: fixed (69981a3e)

## U8 unit review round 2 — 2026-10-31, reviewed at 69981a3e

Verifier: pass (scope, no weakened assertion, 6 new tests fail without the fixes).

### U8-R2-1 — The "does not grow storage" skip is measured on the raw upload, but a HEIC/HEIF is stored as a larger JPEG, so an over-limit account can now grow its storage with no check
- Trigger: an account already over its storage limit replaces a 2.0 MB stored JPEG with a 1.9 MB HEIC (PUT …/replace or U11's Immich apply) → growth is -0.1 MB, so `ensure_storage_quota` is skipped → `write_photo_files` stores the quality-92 JPEG decoded from the HEIC (typically 2-3x the HEIC's bytes) and `record_written` counts it → the account ends several MB further over with nothing refused; repeated replaces compound.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=M/shared, confidence=verified
- Decision: Fix now (D3, F3 floor)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (4d8fdadd, with the memory and journal upload routes; verifier pass)

Note: the same `len(raw)` measurement sits in the upload routes (`api/memories.py:765`, `api/journal.py:248`). Since U4 (this branch) a HEIC upload near the limit is checked on its HEIC size and stored as a larger JPEG. Outside this round's subject; raised to the owner separately. Owner (2026-10-31): fix it in U8 together with U8-R2-1 — check the quota on the bytes that will be stored, for replace and upload alike.

## U15 unit review round 2 — 2026-10-31, reviewed at 3500e3a3

Clean: no findings, no envelope questions. Verifier: pass.

## U8 unit review round 3 — 2026-10-31, reviewed at 4d8fdadd

Verifier: pass (all upload paths in memories/journal check the stored size; one conversion; new tests fail without the fix).

### U8-R3-1 — The avatar upload route still checks the storage quota on the raw HEIC while storing its larger JPEG
- Trigger: an editor uploads a HEIC as a person's avatar (POST /api/people/{id}/avatar) on a trip whose owner has room for the HEIC but not its JPEG → `ensure_storage_quota` passes on `len(raw)` (`api/people.py:381-382`) → `write_photo_files` stores the quality-92 JPEG and `record_written` counts it → the owner's account ends over its limit, nothing refused.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3 floor — decided by the orchestrator: the floor fixes the outcome whatever the triage)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (62b284d5; verifier pass)

Round 3 is the cap (§6). Owner (2026-10-31): fix U8-R3-1 in U8 (X3: `api/people.py`), and run a fourth round on the fix.

## U8 unit review round 4 — 2026-10-31, reviewed at 62b284d5

Clean: no findings, no envelope questions. Verifier: pass.

## U9 unit review — 2026-11-01, reviewed at 0afff4b0

Verifier: pass (scope plus approved X3 extensions: `Asset.width/height`, `original_width/original_height` columns with migration b9d3e6f1a274). Verifier note: the step 1a message states the rule but gives no literal example value, as the plan asks; folded into the fix round. 8f196422 (each scan job named at its enqueue call, for `test_jobs_never_call_strava`) landed after this round's subject and is reviewed in round 2.

### U9-R1-1 — A rejection survives only while the matcher finds nothing else for the photo; a rescan that proposes another asset erases it, and the rejected asset comes back ticked
- Trigger: a user rejects burst frame A for photo P, then rescans → scan 2 excludes (P, A), proposes sibling B and deletes scan 1, the only copy of the rejection → the user leaves or rejects B and rescans → scan 3 proposes A again, ticked when best (D2) → Apply replaces P with the refused frame and deletes P's file (D5).
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=expensive, fix=M/local, confidence=verified
- Decision: Fix now (D3, F3 floor; F4 — the fix needs schema, unreleased so cheap now)
- Revisit when: —
- Guard: —
- Override: —
- Approved (2026-11-01): rejections carried forward on the scan record (one JSON column, merged from earlier scans, own migration). Owner also approved the related U5 change raised by the review: a frame rejected for the same photo, scoring within the ambiguity margin of the proposal, makes the proposal low-confidence (unticked).
- Outcome: fixed (0b8868fe)

### U9-R1-2 — A fingerprint batch holds all 250 thumbnail bodies in memory, each capped only at 20 MiB, so a hostile Immich can OOM-kill worker-poster
- Trigger: a signed-in user points their Immich connection at a server they control and scans an album of 250+ photos → each thumbnail answer is just under 20 MiB → `_fingerprint` holds all 250 (≈5 GB) before decoding any → worker-poster (1280 MB) is OOM-killed; queued poster or apply jobs are interrupted; repeatable at will.
- Scores: trigger=concrete (triage corrected from plausible: deliberate, repeatable, no race — as U4-R1-1/R2-1), impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Approved (2026-11-01): fingerprint each thumbnail as it arrives (at most the worker count held at once) and refuse thumbnail bodies over 2 MiB at download.
- Outcome: fixed (0b8868fe)

## U9 unit review round 2 — 2026-11-01, reviewed at 0b8868fe (fixes only: 8f196422, 0b8868fe)

Verifier on 0b8868fe: pass (488 tests, single head c5a1e8d27f40). Its out-of-scope notes are U9's own step 1a files (api/router.py, src/jobs/worker.py) and the 8f196422 job-naming change reviewed here.

### U9-R2-1 — A rejection made on the earlier scan while the new scan is matching is neither excluded nor carried, and the earlier scan is then deleted
- Trigger: a user with a ready scan 1 starts scan 2 and, during scan 2's match step, rejects frame A for photo P in scan 1 → scan 2 read the earlier rejections before that write (scan_job.py:632), writes its carry from the stale read (:662-666) and deletes scan 1 (:699-708) → the rejection is lost; A comes back, ticked when confident.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3 floor) — a window inside the R1-1 fix, not a duplicate of R1-1
- Revisit when: —
- Guard: —
- Override: — (approved 2026-11-02)
- Outcome: fixed (a7c615bc)

### U9-R2-2 — The carried rejection set only grows; a rejection the user reverses, or a rejected asset the user later chooses, stays excluded from every future scan
- Trigger: a user rejects A for P, rescans (scan 2 carries (P, A)), then in scan 2 picks A via "Choose another" or un-rejects it, and does not apply → scan 3 and later never propose A for P; nothing shows it is barred.
- Scores: trigger=plausible (triage corrected from theoretical: same planned U10 PATCH as R1-1), impact=degraded-ux (triage corrected from silent-wrong: nothing wrong is applied, A stays in "Choose another"), detect=silent, later=cheap, fix=M/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: the guard fires
- Guard: in `_earlier_rejections`, log a warning once per scan with the scan id and the count of carried pairs contradicted by a non-rejected suggestion row (same photo and asset id) in an earlier scan; no ids.
- Override: user: Fix now — the guard computes the very pairs the fix subtracts, so fix it at the same cost; give U10 the inverse of record_rejection (2026-11-02)
- Outcome: fixed (a7c615bc)

## U9 unit review round 3 — 2026-11-02, reviewed at a7c615bc (fixes only: a7c615bc)

Verifier on a7c615bc: pass (494 tests, single head). The one changed existing test (`test_a_rejection_recorded_on_the_scan_is_honoured_by_the_next`) follows from the approved R2-2 rule (a non-rejected row naming the asset beats the column); "rejected on the row, rescan, still barred" stays covered by three tests.

### U9-R3-1 — The late re-read only adds rejections; a rejection the user lifts on the earlier scan during the match stays carried on the new scan, and the earlier scan is then deleted
- Trigger: a user with ready scan 1 (P rejected with A) starts scan 2 and, during scan 2's match, un-rejects A for P on scan 1 or picks A back → scan 2 already excluded (P, A) from its stale read and wrote it to its column (and a rejected/A row when nothing else matched) → `_replace_earlier_scans` computes only `now - carried`, removes nothing, deletes scan 1 → later scans keep A barred for P; nothing shows the reversal was dropped.
- Scores: trigger=plausible, impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9) — mirror window of U9-R2-1 for U9-R2-2's reversal; not a duplicate. Disclosed by the implementer as a known edge.
- Revisit when: the guard fires
- Guard: in `_replace_earlier_scans`, inside the same transaction, compute `carried - now` and when non-empty log a warning once per scan with the scan id and the count; no ids.
- Override: user: Fix now — the guard computes the very pairs the fix lifts; round 4 allowed past the cap to review the fix (2026-11-03)
- Outcome: fixed (e677d0c7)

## U9 unit review round 4 — 2026-11-03, reviewed at e677d0c7 (fixes only: e677d0c7; past the cap with the owner's OK)

Clean: no findings, no envelope questions. U9-R3-1 resolved (carry written from the in-transaction re-read; no row built from the stale read). Reviewer considered and dismissed: lock span, the rollback-on-delete-failure path (no nameable trigger under WAL once the lock is held; the resulting state is the safer one), confidence kept on late-rejected rows (no consumer).

## U11 unit review — 2026-11-03, reviewed at af34ae1a

Verifier: pass (194 tests; every Acceptance case asserted; departures refine the plan). Verifier note: D4 and U11 Do 2 still say RAW goes "through the RAW rendition per OD3", but OD3's answer makes RAW `unsupported_format`; the code follows OD3. Plan wording to be corrected in the Delivery section.

### U11-R1-1 — Recovery marks a row applied but never deletes the old photo's files when the batch died between the list commit and the file delete
- Trigger: OOM kill, docker stop at deploy, or RQ's 600 s timeout hits the work-horse after `mutate_photos` committed the new photo and before the old files are deleted → recovery sets the row `applied` → the old photo's files stay on disk, counted in storage usage, listed nowhere; the nightly reconcile recounts them.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3 floor) — not a duplicate of U8-R1-3 (sha256 read before the lock)
- Revisit when: —
- Guard: —
- Override: — (approved 2026-11-04: recovery deletes and uncounts the old photo's files when it marks a row applied)
- Outcome: fixed (7072d075)

### U11-R1-2 — A 403 from Immich on the original download is reported as "not found in Immich"
- Trigger: a user's Immich key lacks `asset.download` (connect and scan never need it) → JPEG rows succeed through fullsize; every HEIC/HEIF/PNG/WebP row fails `not_found_in_immich` → the app says the photos are gone from Immich when the fix is a key permission.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: — (approved 2026-11-04 with a new error code `immich_permission`, added to the plan's Conventions list)
- Outcome: fixed (7072d075)

### U11-R1-3 — The batch budget is only checked between rows, so one slow original can run past RQ's 600 s job timeout
- Trigger: a row started near the 300 s budget whose download takes more than about 300 s (25 MB under about 0.7 Mbit/s) passes RQ's 600 s timeout → the apply stops `interrupted`; on resume the row is first with the full 600 s, so it fails again only if its download alone exceeds 600 s (about 0.33 Mbit/s).
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=inferred (triage narrowed the trigger)
- Decision: Defer (D10)
- Revisit when: the log shows a JobTimeoutException from run_apply_batch, a user reports an apply that keeps stopping on the same photo, or a single row is measured over about 300 s on val/prod.
- Guard: —
- Override: —
- Outcome: —

### U11-R1-4 — The apply replaces whatever `photo_uuid` names; a re-proposed applied row only works if U10 rewrites `photo_uuid` to the listed `applied_uuid`
- Trigger: U10's PATCH picks another asset on an applied row, sets `proposed` and leaves `photo_uuid` → on resume `_owner_dir` finds the old photo unlisted → `stale`/`photo_gone`; the upgraded photo is not replaced and the user is told it is gone.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triage corrected from inferred)
- Decision: Defer (D10) — the fix belongs to the unit writing the PATCH; approved 2026-11-04, written into U10 Do 4 in the plan (U10 sets `photo_uuid` to `applied_uuid`)
- Revisit when: U10's PATCH for an applied row is written: either U10 sets `photo_uuid` to the listed `applied_uuid` or the job targets `applied_uuid`, with an end-to-end test including recovery.
- Guard: —
- Override: —
- Outcome: fixed in U10 (9a2609ee, as the decision directs)

## U11 unit review round 2 — 2026-11-04, reviewed at 7072d075 (fixes only)

Verifier on 7072d075: pass (200 tests). Its gap (no journal-entry recovery test) closed by 0f6d2d65 (test-only; breaking the owner-dir choice fails it). Reviewer: U11-R1-1 and U11-R1-2 resolved.

### U11-R2-1 — A 401 from Immich (key revoked or invalid) fails every row as "immich_unreachable", while the scan calls the same answer a key problem
- Trigger: a user deletes or rotates their Immich API key after a scan is ready, then applies → every download gets 401 → each row `failed`/`immich_unreachable` → the app says the server could not be reached when the fix is the key; a rescan on the same key says forbidden.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — not a duplicate of U11-R1-2 (403 only)
- Revisit when: an apply fails "Immich unreachable" while the server was up, logs show immich_unreachable rows on a 401, or the Immich key reconnect flow next changes.
- Guard: —
- Override: user: Fix now — a few lines in code already being changed; 401 maps to immich_permission like 403, matching the scan (2026-11-04)
- Outcome: fixed (f1af58a4)

## U11 unit review round 3 — 2026-11-04, reviewed at f1af58a4 (fixes only: 0f6d2d65, f1af58a4)

Clean: no findings, no envelope questions. Verifier: pass (203 tests). U11-R2-1 resolved. The reviewer noted the Conventions wording for `immich_permission` named only `asset.download`; widened in the plan to cover a revoked or invalid key (401) for U12's sentence.

## U10 unit review — 2026-11-04, reviewed at 7ccb4424

Verifier: pass (254 tests in the container; every Acceptance case asserted; departures refine the plan: optional album_name and at least one include flag, photo_counts in latest, DELETE allowed for a removed companion, BackgroundTasks, sweep at minute 25, reuse of private helpers). X3 granted: one `_EXEMPT` entry in tests/test_project_cache_invalidation.py for `create_photo_upgrade_scan` (/meta reads no photo-upgrade table).

Owner approved the table as recommended (2026-11-04). U10-R1-1 fix design A: the sweep clears an old scan instead of deleting it, keeping its rejection list; plan D8 and U10 Do 5 revised, docs/ENCRYPTION.md wording aligned.

### U10-R1-1 — The 30-day sweep deletes the only record of a person's rejections, so a rescan a month later proposes the refused frame again, ticked
- Trigger: a user rejects frame A for photo P, applies the rest, and rescans after 30 days → the sweep deleted every scan of theirs on the trip, `rejected_pairs_json` included → `_earlier_rejections` returns nothing → A is proposed for P, ticked when confident → Apply replaces P with the refused frame and deletes P's file.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3 floor) — the U9-R1-1 harm through the retention sweep, which D8's 30 days predates; not a duplicate
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (40257921)

### U10-R1-2 — A failed row is a dead end: it cannot be re-ticked or re-chosen with the same asset
- Trigger: a key without `asset.download`, a rotated key or a brief outage fails rows (`immich_permission`, `immich_unreachable`) → the user reconnects → PATCH `selected: true` answers 409, the same `asset_id` is a no-op, select and resume take proposed rows only → the only way back is reject then un-reject per row, or a rescan that discards their picks.
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (40257921, completed by U10-R2-1 in 28ac632a)

### U10-R1-3 — The 30-minute heartbeat rule fails a scan as "interrupted" while its next step is only waiting in the photos queue
- Trigger: a scan step hands the next one to the queue; poster jobs and other users' apply batches run first → over 30 minutes pass → the sweep fails the healthy scan as interrupted → the queued step exits; the user restarts a scan for nothing.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local (triage corrected from shared), confidence=verified
- Decision: Defer (D10) — not a duplicate of R1-6 (that fix covered the pending branch)
- Revisit when: the sweep fails a scan as interrupted while worker-poster was alive or its next step was queued; a photos-queue job waits more than about 15 minutes on val/prod; or a user reports a scan stopping as interrupted with no worker restart.
- Guard: —
- Override: —
- Outcome: —

## U10 unit review round 2 — 2026-11-04, reviewed at 40257921 (fixes only)

Verifier: pass (189 tests in the container, seven required cases asserted; re-choosing a failed row's own asset also resets it, inside U10-R1-2's intent). U10-R1-1 resolved. U10-R1-2 only partly resolved, see U10-R2-1. Owner approved U10-R2-1 as Fix now (2026-11-04); plan U10 Do 4 and U12 Do 7 / Acceptance revised.

### U10-R2-1 — After a key or outage failure every selected row is `failed`, and there is still no bulk way to put them back
- Trigger: a user applies with a key lacking `asset.download`, or during an Immich outage → each row ends `failed` and the chain goes on, so every selected row fails → the user reconnects → `select` with `all_proposed` or `confident` touches `proposed` rows only → the way back is one PATCH per row, or a rescan that drops their picks.
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7) — not a duplicate: the bulk half of U10-R1-2's own trigger was left in place. Fix as a new additive `which` value that puts back only rows failed for a temporary reason (`immich_permission`, `immich_unreachable`); plan U10 Do 4 and U12 gain a "Retry failed" action
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (28ac632a)

## U10 unit review round 3 — 2026-11-04, reviewed at 28ac632a (fixes only)

Clean: no findings, no envelope questions. Verifier: pass (194 tests in the container). U10-R2-1 resolved. The reviewer noted that PATCH still lets a single `failed` row be ticked during an apply while the bulk route refuses: a stricter rule on the new route, not a harm. U10 merged into the feature branch at f43e6f20.

## Integrated review round 1 — 2026-11-04, reviewed at 65cf5e88 (feat/immich-album-upgrade against main 1a226fb1)

Three reviewers in parallel, by area: A (photo-upgrade jobs, API, Immich client, queue, migrations, models), B (photo endpoints, store, transfer, account deletion, config, docs), C (Flutter client). Envelope question from C, put to the owner: does tap-to-compare's per-tap download of the Immich original (up to 25 MB) and the stored full photo sit inside the envelope, or should compare use the Immich preview and the stored thumbnail?

Owner decisions (2026-11-04): compare uses Immich's `preview` rendition beside the current photo, never the original (plan U12 Do 6 revised; the thumbnail proxy gains an optional `size`). The table is approved as recommended: five Fix now, four Defer. INT-R1-A1's fix retries fetches that can pass and reports the rest as `unreadable_photos` (plan U9 Do 4 revised).

### INT-R1-A1 — An album photo whose thumbnail fetch fails once is dropped from the scan, so its trip photo reads "No match" and the chooser cannot offer it
- Trigger: a user scans a large album while Immich or its proxy answers a few thumbnail requests with 5xx or a timeout → those assets are marked `fetch_failed` and never retried → the match and the candidates list see fingerprinted rows only → their trip photos read "No match", with no count or banner, only an INFO log line.
- Scores: trigger=plausible, impact=silent-wrong, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3 floor)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (0f456e88, retry passes and `unreadable_photos`; down check completed by INT-R2-1)

### INT-R1-B1 — The README's LGPL source offer names library versions nothing ties to the locked pillow-heif
- Trigger: Dependabot (uv ecosystem) or the owner upgrades the lock past pillow-heif 1.8.1 → the image ships other libheif/libde265/x265 versions than README names → the source offer is wrong, and no test fails.
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (27e3d3f5)

### INT-R1-B2 — The release runbook quotes a 503 message the server does not send
- Trigger: the owner follows the post-deploy step looking for "No worker is processing photo upgrades" → the server says "No worker is processing photo jobs".
- Scores: trigger=concrete, impact=cosmetic, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (27e3d3f5)

### INT-R1-C1 — A failed row shows ticked while the bar counts 0, and the first tap unticks it
- Trigger: a row fails during an apply → `_take`/`_end_row` leave `selected` true → the review draws it ticked, the bar reads "Replace 0 photos" → the user taps to retry → the client sends `selected: false`, the row stays failed and unticked; a second tap puts it back.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (b39af48b)

### INT-R1-C3 — The "Immich is not connected" card stays after the user connects in Settings and comes back
- Trigger: the user taps "Open Settings", connects Immich, goes back → `connected` is read only in `load()` → the card stays until the screen is reopened.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (b39af48b)

### INT-R1-A2 — The matcher ignores format, so the RAW half of an unstacked RAW+JPEG pair can be the proposal, which apply never stores
- Trigger: an album holds unstacked RAW+JPEG pairs → both halves tie, the tie breaks on asset_id → about half the proposals name the RAW, low confidence and unticked → the user ticks them → each fails `unsupported_format` and must be re-chosen.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=M/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: an apply on val/prod ends with `unsupported_format` rows whose asset has a same-day JPEG twin in the album; a user reports RAW proposed where the JPEG is in the album; or the matcher's tie-break or D7 is next changed.
- Guard: —
- Override: —
- Outcome: —

### INT-R1-A3 — Applying after Immich was disconnected fails every row as `immich_unreachable`
- Trigger: a user with a ready scan disconnects Immich, presses Apply or Resume → 202, every row fails `immich_unreachable` → the sentence says the server could not be reached; "Retry failed" fails the same way.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: logs show apply rows failed `immich_unreachable` for a user with no Immich token; a user reports "could not be reached" after disconnecting.
- Guard: —
- Override: —
- Outcome: —

### INT-R1-C2 — The setup view reached by polling never loads the albums, so it reads "No albums found" with Scan disabled
- Trigger: a user opens the screen while a first scan is pending or scanning → no albums are fetched → the scan fails, or the ready scan is discarded elsewhere or expires → setup shows "No albums found in your Immich library." with Scan disabled.
- Scores: trigger=plausible (triage corrected from concrete), impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a user reports "No albums found" while their Immich has albums; a test or device run reaches setup from a failed or vanished scan without albums; or the setup view or polling is next changed.
- Guard: —
- Override: —
- Outcome: —

### INT-R1-C4 — Two quick chip taps can leave one filter's rows under another chip
- Trigger: on a slow connection the user taps Failed then Confident → the Failed reply lands last and fills the list under Confident → "Load more" pages with the wrong cursor.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a user reports rows under the wrong filter, or duplicates after "Load more"; or the review's filter or paging code is next changed.
- Guard: —
- Override: —
- Outcome: —

## Integrated review round 2 — 2026-11-04, reviewed at 6f1dd066 (fixes only: fe04e42c..6f1dd066, fix units F1–F3)

Verifiers: F1 pass (285 tests), F2 pass, F3 pass (80 tests). Full Flutter suite at 6f1dd066: analyze clean, 2827 passed. INT-R1-B1, B2, C1, C3 resolved; INT-R1-A1 resolved except the down check, see INT-R2-1.

Owner decisions (2026-11-04): INT-R2-1 Fix now as recommended; INT-R2-2 moved to Fix now so its case gets its own test.

### INT-R2-1 — A scan whose every thumbnail answers 5xx still ends `ready`, all "No match", and deletes the earlier review
- Trigger: a user with a ready review (ticks, "Choose another" picks) presses New scan while Immich restarts behind its reverse proxy → every thumbnail answers 502/503 (`ImmichRejected`, so the first-pass down check, which needs `ImmichUnreachable`, does not fire) → retried, then all `fetch_failed` → `_match` runs with no fingerprinted asset, goes `ready` and deletes the earlier review with its picks.
- Scores: trigger=plausible, impact=data-loss (triage corrected from wrong-visible: the earlier review's picks are deleted), detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F2 floor). Fix: judge "Immich is down" over the whole scan — before `_match`, no row fingerprinted and any failure that can pass → fail the scan, keep the earlier review; the per-batch first-pass check fires only while nothing is fingerprinted yet and counts 5xx/408/429 as well as unreachable
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (9635cd88; regression found in round 3, see INT-R3-1)

### INT-R2-2 — A small final fingerprint batch whose few thumbnails all time out fails the whole scan, after 250 successes
- Trigger: an album with 251 photos in the trip's dates; the lone last request times out → a 1-row pass-0 batch counts as "all failed" → the scan fails as unreachable (earlier review kept).
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — nothing is lost; the INT-R2-1 fix rewrites the same check ("nothing fingerprinted yet"), which closes this
- Revisit when: the INT-R2-1 fix keeps a batch-shape-only check; logs show a scan failed unreachable after rows of it were fingerprinted; or a user reports "could not reach Immich" on a scan that showed progress.
- Guard: —
- Override: user: Fix now — fold into the INT-R2-1 fix and give the 251-photo case its own test (2026-11-04)
- Outcome: fixed (9635cd88; verifier pass)

## Integrated review round 3 — 2026-11-04, reviewed at 9635cd88 (fixes only: 48d8cd56..9635cd88, fix unit F4)

Verifier: F4 pass (71 scan-job tests, 350 in the photo-upgrade and Immich group; the four new tests for INT-R2-1 and INT-R2-2 fail on 48d8cd56). INT-R2-1 and INT-R2-2 resolved for the cases they name; R3-1 is a regression the fix introduced. Round 3 is the cap: a re-review of R3-1's fix needs the owner's OK (§6).

Owner decisions (2026-11-04): the table is approved as recommended. INT-R3-1's share is 10% of the album's photos still unread after the retry passes. A round 4, fixes only, is allowed past the cap to review the INT-R3-1 fix.

### INT-R3-1 — An Immich outage after the first batch has been read lets a mostly-unreadable scan replace the earlier review
- Trigger: a user with a ready review (ticks, "Choose another" picks) on a trip whose album has more than 250 photos in its dates presses New scan; Immich goes down after batch 1 and stays down past the 2 s + 5 s retry pauses → the per-batch check no longer fires (250 rows fingerprinted), the end check needs nothing read → every remaining row ends `fetch_failed` → `_match` goes `ready`, mostly "No match", and deletes the earlier review with its ticks and picks. Before F4 the all-unreachable first-pass batch failed the scan. Triage widens it: an outage starting mid-batch, during a short last batch after progress, or answering 5xx after progress was never caught.
- Scores: trigger=plausible, impact=data-loss, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F2 floor) — not a duplicate of INT-R2-1 (nothing read) or INT-R2-2 (the opposite failure). Fix: an outcome rule judged once before `_match` — a retry pass ran and the rows still unread are at least a set share of the album → fail unreachable, keep the earlier review; the per-batch "nothing fingerprinted yet" raise stays as a fast path. The reviewer's narrowing (fail any full first-pass batch that fails entirely) is not taken: it is a batch-shape check, skips the retry passes, and misses mid-batch and short-last-batch outages.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (1a231eef for outages leaving at least 10% unread; bfcc20b1, the control fetch, for the 251–277 range; verifier pass)

### INT-R3-2 — A scan whose every album thumbnail is an undecodable body fails as "could not reach Immich" instead of going ready (verifier note)
- Trigger: a user whose Immich sits behind a proxy or forward-auth that answers 200 with an HTML page presses New scan → every fetch decodes to nothing → retried, then all `fetch_failed` → the end check fails the scan as unreachable; the earlier review is kept. Before F4 the scan went ready, all "No match", and deleted the earlier review.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — intended by `_can_pass` (an undecodable body is "a proxy's error page or a cut answer"); only the wording says unreachable. The R3-1 fix adds the missing test, since its rule must decide how undecodable rows count.
- Revisit when: a user reports "could not reach Immich" while Immich answers normally; logs show a scan failing unreachable with no Immich error on its last pass; or the R3-1 end rule treats undecodable rows differently from unreachable ones.
- Guard: —
- Override: —
- Outcome: —

## Integrated review round 4 — 2026-11-04, reviewed at 1a231eef (fixes only: 3547528f..1a231eef, fix unit F5; past the cap with the owner's OK)

Verifier: F5 pass (77 scan-job tests, 356 in the photo-upgrade and Immich group; the four INT-R3-1 boundary tests fail on 3547528f; four existing small-album tests padded with readable photos, no expected value changed).

Envelope question from the reviewer, put to the owner: INT-R3-1's own trigger (an outage after batch 1 of an album over 250 photos) fails the scan only from 278 photos up under the 10% share; for 251–277 photos the scan goes ready and still deletes the earlier review. Was the 10% decision taken knowing that range is uncaught?

Owner decisions (2026-11-04): the 251–277 range is in scope, so INT-R3-1 is not resolved by F5 and stays Fix now. INT-R4-1 moves to Fix now; both are fixed by the control fetch, which drops the 10% share (the transport-failure refinement is not taken: it would bring back INT-R2-2). A round 5, fixes only, is allowed past the cap.

### INT-R4-1 — One album photo whose thumbnail Immich cannot serve locks a small album out of scanning, reported as "could not reach your Immich server"
- Trigger: a user whose album has 8 photos in the trip's dates, one of them an asset Immich answers 500 for on every fetch (its thumbnail missing on disk) presses Scan → 7 read, the 500 retried twice then `fetch_failed` → 1 unread of 8 is over 10% → the scan fails "Could not reach your Immich server" on every attempt while Immich is online; nothing names the cause. Before F5 the scan went ready with `unreadable_photos=1`.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — nothing lost, the earlier review is kept. Fix if taken: a control fetch in the last retry pass — each batch also fetches one already-fingerprinted thumbnail; control fails for a reason that can pass → Immich down, fail and keep the review; control decodes → the failed rows are per-asset breakage, `fetch_failed`, scan ready; control 404/size cap → next fingerprinted row, up to 3, then fail safe. Drops the share. Closes this and the 251–277 range. Rejected: a minimum count beside the share (moves the lockout, leaves the range); storing the failure kind (a per-asset 500 and an outage 503 are the same kind).
- Revisit when: the owner brings the 251–277 range into scope (then INT-R3-1 is unresolved and both are fixed together by the control fetch); logs show a scan failing unreachable with fewer than 10 unread rows; a user reports "could not reach your Immich server" while Immich is online; or the end rule changes again.
- Guard: —
- Override: user: Fix now — fixed with INT-R3-1 by the control fetch, at no extra cost (2026-11-04)
- Outcome: fixed (bfcc20b1; verifier pass)

## Integrated review round 5 — 2026-11-04, reviewed at bfcc20b1 (fixes only: 05c59e94..bfcc20b1, fix unit F6; past the cap with the owner's OK)

Verifier: F6 pass (87 scan-job tests, 366 in the photo-upgrade and Immich group; 20 fail on 05c59e94, including the small album with one broken photo and the 251/260/277 outages; the F5-padded tests are back to their pre-F5 form, counts changed only by the control fetch; a 403 control still ends `REASON_FORBIDDEN`). INT-R3-1 and INT-R4-1 resolved.

Owner decisions (2026-11-05): INT-R5-2's guard as recommended; INT-R5-1 moves to Fix now with the sequential re-ask, not the reviewer's fix. A round 6, fixes only, is allowed past the cap.

### INT-R5-1 — One transient failure of the control request fails the scan even when every retried row beside it just decoded
- Trigger: a user with a ready review presses New scan; a few thumbnails failed twice for a passing reason; on the last pass they all decode but the single control request answers 503, times out or is cut → the scan fails "Could not reach your Immich server" while Immich just served the batch; the earlier review is kept, the new fingerprints are discarded.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — nothing lost. The reviewer's fix (the control decides only when no batch row decoded) is not safe: an outage starting partway through the final last-pass batch leaves early rows decoded and the rest failed, so the scan would go ready and delete the earlier review (INT-R3-1). If taken: when the control fails for a reason that can pass and a batch row decoded, ask the next control one at a time after the pool (up to `_CONTROLS`); it decodes → Immich is up; it fails → down.
- Revisit when: a scan fails on the control (see INT-R5-2's log line) with rows of its last-pass batch decoded; a user reports "Could not reach your Immich server" on a scan that showed progress while Immich was online; or the owner moves it to Fix now — then take the sequential re-ask, not the reviewer's fix.
- Guard: —
- Override: user: Fix now — take the sequential re-ask: a false alarm users would hit after a brief hiccup, in code already being changed (2026-11-05)
- Outcome: fixed (b5f035ab; verifier pass)

### INT-R5-2 — A scan failed by the control logs nothing about what the control answered
- Trigger: an Immich outage, or every control failing permanently, fails a scan → the worker log reads only `Photo scan N stopped in fingerprint: _ScanFailed` → nothing tells an outage from the fail-safe, and the INT-R3-2, INT-R4-1 and INT-R5-1 revisit triggers cannot be checked.
- Scores: trigger=plausible (triage corrected from concrete: the event is a third-party outage), impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: one `_log.info` before the control's raise giving scan id, retry pass, batch size, rows decoded, and each control's outcome class (with status for `ImmichRejected`, or undecodable / too large) — no server address, no asset ids; the pass-end "nothing read" raise gets its own line so the two are told apart.
- Override: —
- Outcome: guard added (b5f035ab; verifier pass)

## Integrated review round 6 — 2026-11-05, reviewed at b5f035ab (fixes only: b70c949f..b5f035ab, fix unit F7; past the cap with the owner's OK)

Verifier: F7 pass (103 scan-job tests, 382 in the photo-upgrade and Immich group; the new R5-1, mixed-batch, re-ask and guard tests fail on b70c949f; the partway-outage test fails against the rejected fix; no existing assertion changed). INT-R5-1 and INT-R5-2 resolved.

Owner decision (2026-11-05): INT-R6-1 stays Defer, and the review loop on this rule stops here. The findings had started alternating between the two errors of one detector (R3-1 false ready, R4-1 and R5-1 false alarm, R6-1 false ready): a sampled control can tell up from down, not from intermittent. Only a design change would end that class: keep the earlier review until the user accepts a scan with unread photos, or carry ticks and picks over.

### INT-R6-1 — Under intermittent failure, a re-asked control that reads lets a batch's failed rows count as unreadable and the new review replace the earlier one
- Trigger: a user with a ready review presses New scan while Immich or its proxy answers 5xx/429 intermittently → on the last pass some rows and the first control fail, a re-asked control reads → the failed rows are `fetch_failed`, the scan goes `ready` with those photos "No match" and counted in `unreadable_photos`, and the earlier review is deleted. At b70c949f the same batch failed safe.
- Scores: trigger=plausible, impact=wrong-visible (data-loss considered and declined: a good rescan deletes the same ticks and picks by design, the new review is mostly real at realistic failure rates, says how many photos it could not read, and a rescan repairs it; if this were data loss, F6's own "first control reads" path would be the same defect), detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — not a new harm: a higher probability of a state F6 already allows (the transport-failure refinement was not taken, round 4). Per batch, ready goes from 1-f to 1-f³; a row must fail three times at least 2 s and 5 s apart. Small for ordinary hiccups, real only for sustained heavy failure on large albums (f=0.5, 10,000 photos: ready 0.001 → 0.26, independence assumed, not measured). The reviewer's fix (re-fetch failed rows once more) is rejected: a fourth pass in disguise, with the review still replaced. If overridden: narrow the re-ask to batches with no row failing for a passing reason (no extra fetches; a broken photo coinciding with a control blip then fails safe, as before F7).
- Revisit when: a user reports the unreadable notice or "No match" on a scan while Immich or its proxy was overloaded or rate-limiting, and a later scan reads those photos; logs show a ready scan with more than about 5% of its album unreadable; a rate limiter in front of Immich is found to be a common setup; or the control rule or the rule replacing the earlier review next changes.
- Guard: —
- Override: —
- Outcome: —

## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Immich probe and calibration script (owner runs it on val) | Opus to build; the owner runs the probe (OD1-OD5) | S5 | 1 | — | yes | — |
| U7 | Per-memory dialog uses local capture day (F2 app side) | Sonnet | — | 1 | — | yes | — |
| U3 | `sources` in the photo order state, `fill_hashes` | Opus | S4 | 1 | X3: scope widened to `tests/test_memories_photo_order.py` and `tests/test_reorder_polarsteps_memory_photos.py`, only to add `"sources": {}` to whole-state assertions | yes | U3-R1-1 (Reject: it claimed U3 edited those two files without an X3, but U3 had escalated) |
| U6 | Three scan tables, migration `f4a8c2d61e37`, scan cleanup on trip and account deletion | Opus | S4 | 1 | — | yes | U6-R1-1 (Reject, then owner override to Fix now); U6-R2-1 (Guard) |
| U14 | Photo-list writes become compare-and-set (`api/photo_write.py`, memories, journal, `_drop_unplaced`) | Opus | S4 | 1 | — (it made one decision in `api/memories.py` at no-latitude and reported it; the verifier judged it correct) | yes | no unit review. Plan-round R1-1 and R3-1 concern this unit's design (inferred from their text) |
| U4 | HEIC to JPEG on ingest, sniffed media types (F4) | Opus | S4 | 1 | X3: scope widened to `_save_avatar_files` in `api/people.py`, its 422 mapping, and the avatar tests | yes | U4-R1-1, U4-R1-2 (Fix now); U4-R1-3, U4-R1-4 (Reject); U4-R2-1, U4-R2-2 (Fix now); U4-R3-1 (Defer, then fixed), U4-R3-2 (Guard). Rounds 4 and 5 found nothing |
| U-W1fix | Fix 4 test failures at wave-1 integration (not in the original split) | Sonnet | — | 1 | — | yes | — |
| U8 | Shared replace core, net-delta quota, adopt keeps photos (F3, F4, F5) | Opus | S4 | 1 | X3 (1): `tests/test_journal_photo_order.py`, for two TestDeletedEntry tests, plus approval to rewrite the tests that relied on adopt clearing photos. X3 (2): `tests/test_photo_write_cas.py`. Later review fixes had the scope widened with owner approval (see notes) | yes | U8-R1-1, U8-R1-2 (Fix now), U8-R1-3 (Defer, then fixed); U8-R2-1; U8-R3-1 |
| U15 | Replace import refused during an apply (R2-1) | Opus | S4 | 1 | — | yes | U15-R1-1 (implementer reported it did not reproduce; kept as a safeguard); plan-round R2-1 |
| U2 | Immich client module, album listing, search fixes (F1, F2 server side) | Opus | S4 | 2 launches (the first stopped at once because its worktree base was wrong) | — | yes | none at unit level. Plan-round R1-4 concerns it (inferred) |
| U5 | Matching engine | Sonnet | — | 3 (2 launches, the first aborted on the worktree base; plus 1 continuation after the verifier flagged a D7 deviation) | — (this was a verifier-flagged fix, not X1 to X3) | no (verdict "pass", but it flagged a deviation from D7's confidence rule that had to be fixed before merge) | INT-R1-A2 (Defer; `matching.py`) |
| U9 | Scan job chain and the `photos` queue | Opus | S5 | 1 | X3 (1): `src/immich/client.py` and `tests/test_immich_client.py` (asset width/height from `exifInfo`). X3 (2): `models/project_db.py` (the `DBPhotoUpgradeAsset` model only), one new Alembic migration `b9d3e6f1a274`, and `tests/test_photo_upgrade_tables.py` (original_width/original_height columns) | yes | U9-R1-1, U9-R1-2; U9-R2-1, U9-R2-2; U9-R3-1; INT-R1-A1 (`scan_job.py`); INT-R2-1; INT-R2-2 (fixed through F1 and F4) |
| U11 | Apply job chain (`apply_job.py`, `src/jobs/worker.py`) | Opus | S5 | 1 | — (the fix round used private helpers from `api/photo_replace.py` without editing it, and the implementer reported "no X3") | yes | U11-R1-1, U11-R1-2 (Fix now); U11-R1-3, U11-R1-4 (Defer); U11-R2-1 (Defer, then moved to Fix now); INT-R1-A3 (Defer) |
| U10 | Photo-upgrade API and sweeps | Opus | S5 | 1 | X3: one `_EXEMPT` entry for `create_photo_upgrade_scan` in `tests/test_project_cache_invalidation.py` | yes | U10-R1-1, U10-R1-2 (Fix now); U10-R1-3 (Defer); U10-R2-1 (Fix now). U11-R1-4 was fixed in U10 |
| U13 | Docs, release notes, owner actions | Sonnet | — | 1 | — | yes | INT-R1-B1, INT-R1-B2 |
| U12 | Flutter "Upgrade photos from Immich" screen | Sonnet (Latitude none, except layout choices that the plan grants) | — | 1 | — | yes | INT-R1-C1, INT-R1-C3 (Fix now); INT-R1-C2, INT-R1-C4 (Defer) |
| F1 | Scan retry passes with `unreadable_photos`, and a `size` parameter on the thumbnail proxy | Opus | S5 (S4 also applies: the shape of an existing endpoint changes) | 1 | — | yes | INT-R1-A1 (fixed in `0f456e88`) |
| F2 | README third-party notice test and runbook 503 wording | Sonnet | — | 1 | — | yes | INT-R1-B1, INT-R1-B2 (fixed in `27e3d3f5`) |
| F3 | Flutter review fixes | Sonnet | — | 1 | — | yes | INT-R1-C1, INT-R1-C3 (fixed in `b39af48b`) |
| F4 | "Immich down" judged over the whole scan | Opus | S5 | 1 | — | yes | INT-R2-1, INT-R2-2 (fixed in `9635cd88`); INT-R3-1, INT-R3-2 found in it |
| F5 | Fail a scan left at least 10% unread | Opus | S5 | 1 | — | yes | INT-R3-1 (partly, `1a231eef`); INT-R4-1 found in it |
| F6 | Control fetch replaces the share | Opus | S5 | 1 | — | yes | INT-R3-1, INT-R4-1 (fixed in `bfcc20b1`); INT-R5-1, INT-R5-2 found in it |
| F7 | Sequential re-ask and control log line | Opus | S5 | 1 | — | yes | INT-R5-1, INT-R5-2 (`b5f035ab`); INT-R6-1 found in it (Defer) |

Owner overrides of the split or routes: none (the split was answered with "go"). Units added after it: U-W1fix and F1–F7. "Attempts" counts implementer launches and continuations after a verifier fail or a verifier-flagged defect; continuations for X3 answers and review fixes are not attempts. U5's third attempt is a judgement call: its verifier passed it but flagged a D7 deviation that had to be fixed before merge.

Findings traced for U2, U14 and U15 rest on plan-round findings, which have no location field, so those links are inferred. Whether U4, U14, U8, U15 and U-W1fix actually started on the wrong base could not be established from their reports.

Worktree-base incidents (`isolation: worktree` branches from main, not the feature head): wave 1 U3 and U6 started at main and reset themselves; U2 and U5 stopped before doing any work and were relaunched in worktrees made from the feature branch. From then on, unit worktrees were made by hand from the feature head.

Integration: no merge conflicts. Wave 1 had 4 test failures between units built separately (U14's hand-built photo-order state against U3's `sources` key; `.env.example` missing what U4 reads), fixed by U-W1fix.

Reviews past the cap, each with the owner's OK: U4 rounds 4 and 5, U8 round 4, U9 round 4, integrated rounds 4, 5 and 6.

Final checks at the merged head (db30413a plus ledger commits): server suite 7955 passed, 47 skipped, 1 deselected in the py314 container, and 11 passed on Windows (the tests that need git). Flutter unchanged since 6f1dd066: analyze clean, 2827 passed.
