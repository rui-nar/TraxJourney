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
- Outcome: —

### U9-R1-2 — A fingerprint batch holds all 250 thumbnail bodies in memory, each capped only at 20 MiB, so a hostile Immich can OOM-kill worker-poster
- Trigger: a signed-in user points their Immich connection at a server they control and scans an album of 250+ photos → each thumbnail answer is just under 20 MiB → `_fingerprint` holds all 250 (≈5 GB) before decoding any → worker-poster (1280 MB) is OOM-killed; queued poster or apply jobs are interrupted; repeatable at will.
- Scores: trigger=concrete (triage corrected from plausible: deliberate, repeatable, no race — as U4-R1-1/R2-1), impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Approved (2026-11-01): fingerprint each thumbnail as it arrives (at most the worker count held at once) and refuse thumbnail bodies over 2 MiB at download.
- Outcome: —
