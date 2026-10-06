# Review ledger — Photos: upright thumbnails and kept order (#511, #237)

Subject: docs/PHOTOS_ORDER_ORIENTATION_PLAN.md
Envelope: plan section "Review envelope" + REVIEW.md defaults

## Round 1 — 2026-10-04, reviewed at 622c5619 (plan uncommitted)

### R1-1 — Re-import clear commits outside photo_lock
- Trigger: Owner re-imports a Polarsteps trip whose memories pre-date step ids; a manual upload or late download runs `_write_memory_photo` between `_clear_memory_photos` and `_adopt_and_refresh`'s commit (api/memories.py:284-287) → the placed photo vanishes with its files orphaned and counted, or the stale pre-clear list and epoch 0 are re-committed and the re-import's own downloads are dropped as stale.
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan amended: D4, D4b, U3, U4, U5, U6, DoD)

### R1-2 — Download landing after its memory/entry was deleted orphans counted files
- Trigger: User deletes a memory or trip while Polarsteps downloads are in flight → `_save_photo_files` writes and counts the files, `_write_memory_photo` returns at `mem_row is None` (api/memories.py:562-564; journal 177-179) → files nobody can see or delete, counted forever (reconcile walks the directory).
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan amended: D4, D4b, U3, U4, U5, U6, DoD)

### R1-3 — Replace racing a delete: 500 and orphaned counted files
- Trigger: User deletes a photo on one device while the photo-upgrade screen replaces it → existence check outside the lock passes, new files are written and counted, `photos.index(old)` raises ValueError → 500, new files orphaned (api/memories.py:702-732; journal 511-542). U3's `replace` contract does not cover `old` absent.
- Scores: trigger=plausible, impact=silent-wrong, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan amended: D4, D4b, U3, U4, U5, U6, DoD)

### R1-4 — exif_transpose re-serialises EXIF and can raise on malformed EXIF
- Trigger: User uploads a JPEG with an orientation tag and a malformed EXIF directory → `ImageOps.exif_transpose` calls `Exif.tobytes()` (Pillow ImageOps.py:726), whose struct/Type/KeyError is outside `_thumbnail`'s catch → upload 500 for a photo accepted today.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager, Pillow source)
- Decision: Defer (D10)
- Revisit when: an upload 500 traceback in exif_transpose / Exif.tobytes appears in logs, or a user reports a photo accepted before is now refused.
- Guard: —
- Override: —
- Outcome: open

### R1-5 — Backfill idempotency check contradicts its own acceptance
- Trigger: Implementer builds U6 as written → originals are never modified (D1), so a second `--apply` re-selects and rewrites every rotated thumbnail, failing "a second --apply rewrites nothing"; orientation 3 cannot be detected by dimensions.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan amended: D4, D4b, U3, U4, U5, U6, DoD)

Envelope question O1 (duplicate photos on a retried step creation): user approved the table without choosing; plan default kept — follow-up issue at merge.

## Round 2 — 2026-10-04, reviewed at 622c5619 (plan amendments for R1-1/2/3/5, uncommitted)

### R2-1 — Replace racing a memory/entry delete still 500s and orphans counted files
- Trigger: User deletes the memory on one device while the photo-upgrade screen replaces one of its photos → the unlocked existence check passes, new files are written and counted, then under `photo_lock` `sess.get(DBMemory, id)` is None and is dereferenced → 500; new files left in a folder re-created after `delete_memory`'s rmtree, counted forever (api/memories.py:720-729; api/journal.py:528-537).
- Scores: trigger=plausible, impact=silent-wrong, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan amended: D4, D4b, envelope, U4, U5, DoD)

### R2-2 — SQLite rowid reuse defeats the row-gone check
- Trigger: During a Polarsteps import the user deletes the newest memory while its downloads are in flight, then a new memory is created and gets the same id (no AUTOINCREMENT, models/project_db.py:405-408) → its NULL `photo_order_json` reads epoch 0, matching the queued epoch 0 → the old step's photos land silently in the new memory.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan amended: D4, D4b, envelope, U4, U5, DoD); round 3 replaced the public_id check with AUTOINCREMENT ids (D4c)

Envelope question (round 2): does the thumbnail backfill run with the API stopped, or live alongside it? User: (a) API stopped — envelope, U6 `--api-stopped` gate, U7 runbook amended.

## Round 3 — 2026-10-04, reviewed at 622c5619 (plan amendments for R2-1/R2-2 + backfill envelope, uncommitted)

### R3-1 — Runbook scripts are not in the image
- Trigger: Admin follows the U7 runbook on the VPS with `docker compose run --rm --entrypoint python traxjourney scripts/<name>.py` → "can't open file": `.dockerignore:22-33` excludes `scripts/` except four whitelisted scripts; both scripts import `src.*` so need the image. The DoD's owner actions cannot be performed.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan amended: D4c, envelope, boundaries, U2, U4, U5, U6, U7, DoD)

### R3-2 — The id-reuse guard covers from-url only, not the memory upload route
- Trigger: User uploads to the newest memory; during the threadpool thumbnail step the memory is deleted and a new one gets the same rowid → `_write_memory_photo(memory_id, uuid)` (no public_id) places the photo in the new memory (other owner: broken tile + orphaned counted files).
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan amended: D4c, envelope, boundaries, U2, U4, U5, U6, U7, DoD)

Envelope question (round 3): journal upload has the same id-reuse hazard and journal entries have no public_id — accept as residual, or give the table a stable identity? User: (a) rebuild `memory` and `journalentry` with AUTOINCREMENT in U2 (D4c); the public_id threading from R2-2 is replaced by it.
Round cap reached (§6): a fourth round needs the user to ask for it.

## Round 4 — 2026-10-04, reviewed at 622c5619 (plan amendments for R3-1/R3-2 + D4c, uncommitted; round requested by the user past the cap)

### R4-1 — Runbook "DB copy" step unspecified; a raw cp of the WAL-mode DB is stale
- Trigger: Implementer writes U7's "DB copy" as `cp db/traxjourney.db ...`; admin runs it on prod, where the DB is WAL with `wal_autocheckpoint=0` (models/db.py:93-96) → the copy lacks commits still in `-wal`; restoring it after a bad reorder `--apply` loses those writes. docs/DEPLOYMENT_VPS.md:290-292 warns against raw cp.
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified (triager)
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan amended: U7 step 3 backup + restore commands)

Envelope question (round 4): the reorder script writes photos_json/photo_order_json via raw sqlite3 outside photo_lock; run it in the same API-stopped window as the backfill, or accept a live run? User: (b) live run accepted as a residual — recorded in the plan envelope.

## Unit U2 review — Round 1 — 2026-10-05, reviewed at b5abb3e9 (worktree branch, a7b181cf..b5abb3e9; DELIVERY §5 point 3)

### U2R1-1 — An interrupted first run leaves the migration unable to re-run
- Trigger: Admin deploys; the API container is killed (or a worker write hits the busy timeout) while `4b9d2e7a1c63` runs → the `drop_index` of the partial unique index and `CREATE TABLE _alembic_tmp_<t>` were autocommitted (pysqlite legacy mode, no BEGIN hook in alembic/env.py:94-102) while the copy rolled back and `alembic_version` is unchanged → every restart fails `alembic upgrade head` ("no such index" / "table _alembic_tmp_memory already exists"), API restart loop until a manual schema repair. Rows are safe.
- Scores: trigger=plausible, impact=wrong-visible, detect=logged, later=expensive (corrected by triager: fix only possible before the migration ships), fix=S/local, confidence=verified (source reading, not reproduced)
- Decision: Fix now (D5)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (5ba6669e)

## Unit U2 review — Round 2 — 2026-10-05, reviewed at 5ba6669e (b5abb3e9..5ba6669e, fixes only)

No findings. Reviewer traced alembic 1.20 batch ordering: only the pre-INSERT DDL autocommits; both table rebuilds, the compaction and the alembic_version update share one transaction, so the only reachable leftovers (index dropped, empty `_alembic_tmp_<t>`) are the ones the fix handles. U2R1-1 outcome: fixed (5ba6669e).

## Unit U7 review — Round 1 — 2026-10-05, reviewed at 244c9e75 (worktree branch, c551c757..244c9e75; DELIVERY §5 point 3)

### U7R1-1 — One failed or non-200 source download makes --apply scramble a correctly ordered memory
- Trigger: Admin runs the reorder `--apply` on prod; for one photo of an already correctly ordered memory (the SELECT takes every memory with a polarsteps_step_id, post-#239 ones too) Polarsteps answers 403/404 (hashed without `raise_for_status`) or times out → the photo is unmatched, `matched + unmatched` moves it to the end, the guards (`failures > attempted/2`, `unmatched > len/2`) pass, the order is written and ranks reset (scripts/reorder_polarsteps_memory_photos.py:119-147, 200, 274). Runbook's "re-running is harmless" (docs/RELEASING.md:69-72) does not hold.
- Scores: trigger=plausible, impact=silent-wrong, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (b7ffb411)

### U7R1-2 — Script connection keeps sqlite3's 5 s busy timeout
- Trigger: Admin runs `--apply` with the API live during a parallel import → the per-memory UPDATE waits past 5 s (app uses 30 s, models/db.py:93) → uncaught `database is locked`, run dies with a traceback; committed memories kept, re-run needed.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: an owner run of the reorder script dies with "database is locked", or the runbook changes to run it unattended or during import-heavy windows.
- Guard: —
- Override: —
- Outcome: open

Envelope question (U7 round 1): the script selects every memory with a polarsteps_step_id, not just pre-#239 ones; restrict it (e.g. id/date ceiling or --project filter) or accept? User: (a) restrict — memories created before #239 shipped (v0.48.0, 2026-08-27), optional --project filter. Tables for U6/U7 round 1 approved as presented (no overrides).

## Unit U6 review — Round 1 — 2026-10-05, reviewed at 5c01cff5 (worktree branch, c551c757..5c01cff5; DELIVERY §5 point 3)

### U6R1-1 — Idempotency threshold leaves a sideways thumbnail alone when a small subject sits on a flat field
- Trigger: Admin runs `--apply --api-stopped` over an orientation-3 (or square 6/8) photo whose subject is small on a near-uniform background → old and upright thumbnails have the same size and mean per-channel difference ≤ 2.0 (`UPRIGHT_MAX_DIFFERENCE`, scripts/backfill_thumbnail_orientation.py:63, 88-92) → counted "already upright", never rewritten; user keeps seeing it upside down.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible (corrected from silent by triager), later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: a user reports a thumbnail still sideways/upside down after the prod backfill, or a prod backfill run reports far fewer rewrites than orientation-3/6/8 candidates.
- Guard: —
- Override: —
- Outcome: open

### U6R1-2 — A hard kill mid-write leaves a counted temp file the next run never sees
- Trigger: Admin's `--apply` run is SIGKILLed/OOM-killed/SIGTERMed between `mkstemp` and `os.replace` → `.<uuid>_thumb.<rand>.tmp` stays; `_photos` globs `*.jpg` only; nightly reconcile (src/admin/storage.py:43-53) counts it to the owner.
- Scores: trigger=plausible, impact=cosmetic, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: (to add) per walked folder, print "leftover temp file: <path>" for each `.*_thumb.*.tmp` and count them in the summary line, deleting nothing.
- Override: —
- Outcome: guard added (e6637145)

## Unit U6 review — Round 2 — 2026-10-05, reviewed at e6637145 (5c01cff5..e6637145, guard only)

### U6R2-1 — monkeypatch.undo() in the killed-rewrite test also reverts the engine fixture's DB patch
- Trigger: A maintainer later makes the test's second run `--apply` → `undo()` has already restored `models.db.engine`, so `record_delta` writes to the process-wide DB (tests/test_backfill_thumbnail_orientation.py:215-219). Today the second run is a dry run; nothing misbehaves.
- Scores: trigger=theoretical, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

No Fix now in this round: U6 review stops (REVIEW.md §6).

## Unit U7 review — Round 2 — 2026-10-05, reviewed at b7ffb411 (244c9e75..b7ffb411, fixes only)

### U7R2-1 — A memory none of whose photos match the source is reported "already in correct order"
- Trigger: Admin runs the reorder on a selected pre-#239 memory whose source bytes no longer match what was stored (Polarsteps re-encoded or changed the photos, or the user replaced them all) → `matched` is empty, the "too few matched" guard is gated on `if matched and ...` (scripts/reorder_polarsteps_memory_photos.py:147-156) → new order == current → "already in correct order", which the runbook (docs/RELEASING.md:214-216, 247-250) presents as success → a scrambled memory is signed off.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (019e9ce0)

### U7R2-2 — Runbook commands hard-code the tag date the prose says not to rely on
- Trigger: Admin copies the documented commands (`--imported-before 2026-08-27`, the v0.48.0 tag date; also the script's usage examples) while v0.48.0 reached prod later → memories imported between tag and deploy are not selected and stay scrambled (only counted as "not selected").
- Scores: trigger=concrete, impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=inferred (deploy date not in repo; contradiction verified)
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (019e9ce0)

Envelope question (U7 round 2): old installed mobile builds that predate the client sending `order` may still append scrambled photos after the cutoff; accept as residual, or note in the runbook that the run can be repeated with a later date once the min-version gate (Package C) refuses those builds? User: (b) accepted silently as a residual. Round 2 table approved.

## Unit U7 review — Round 3 — 2026-10-05, reviewed at 019e9ce0 (b7ffb411..019e9ce0, fixes only; last round under the cap)

### U7R3-1 — Runbook's remedy for flagged memories does not exist
- Trigger: Admin follows docs/RELEASING.md:255-261 for a "flagged for manual review" memory and tells the owner to re-import the trip from Polarsteps → every flagged memory has a polarsteps_step_id, so the import UI greys the step "Already imported" (polarsteps_import_screen.dart:476-484) and `create_memory` returns the existing row untouched by step id (api/memories.py:334-336); the clear (`_adopt_and_refresh`) only runs for memories without a step id → nothing happens, or (if a POST gets through) duplicate ranked copies land in front (residual O1). The only real path, delete + re-import, loses the whole memory, which the runbook does not say.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (584dc437)

Envelope question (U7 round 3): is a real remedy for flagged memories in this plan's scope, or does the runbook state that a flagged memory stays as is, with a follow-up issue for a per-memory re-sync/reorder? User: (a) runbook states a flagged memory stays as is; follow-up #565 filed for a per-memory re-sync; no fourth round (docs-only fix, verified). O1 filed as #566.
Round cap reached for U7 (§6): a fourth round needs the user to ask for it.

## Integrated diff review — Round 1 — 2026-10-06, reviewed at 79842a2f (a7b181cf..79842a2f, DELIVERY §5 point 2)

### IR1-1 — delete_memory / delete_journal run outside photo_lock
- Trigger: User deletes a journal entry (or memory) while an upload or download to it is between writing its files and placing the photo → the writer takes photo_lock, still finds the row, places the photo and commits; the delete (no lock, stale photo list, api/memories.py:474-508, api/journal.py:411-443) then removes the row → journal: the new photo's files stay orphaned and counted forever; memory: rmtree removes the files but their bytes are never subtracted (counter overstated until the nightly reconcile, a no-op without billing). Existing tests only cover the delete-before-write interleaving. New evidence vs R1-2.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (b667fcd8)

## Integrated diff review — Round 2 — 2026-10-06, reviewed at b667fcd8 (974abd37..b667fcd8, fix for IR1-1)

### IR2-1 — Memory replace's post-lock cleanup can double-subtract with the delete's folder sweep
- Trigger: User replaces a memory photo on one device while deleting the memory on another → replace commits under the lock, then unlinks the old photo outside it (api/memories.py:779); the delete's sweep (:492-493) finds the old files still on disk → both run `unlink_and_record`, which stats before unlinking (src/billing/usage.py:110-114) → bytes subtracted twice; counter under until the nightly reconcile. Introduced by the IR1-1 sweep.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (db0e287b)

### IR2-2 — The sweep uncounts files a writer has written but not yet counted
- Trigger: User deletes a memory while an upload/download to it is between writing its files and `record_written` (src/utils/photo_store.py:102-111, 125-126) → the delete's sweep subtracts their size, `record_written` then adds 0 for files that are gone → counter under by one photo until the nightly reconcile.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=M, fix_risk=shared (photo_store.save_photo_files, also used by the archive import), confidence=verified
- Decision: Fix now (D3)
- Revisit when: a storage reconcile reports drift on a memory that was deleted during an upload, or uploads move to a path where the counter is not reconciled nightly.
- Guard: —
- Override: user: Defer — self-correcting at the nightly reconcile, millisecond window; not worth changing shared photo_store code now.
- Outcome: open

## Integrated diff review — Round 3 — 2026-10-06, reviewed at db0e287b (b667fcd8..db0e287b, fix for IR2-1)

No findings. Reviewer traced re-entrancy (no helper under the moved cleanup takes photo_lock), the error path (lock released on exception, same 500-after-commit as before), lock hold time (same work delete_photo already does under the lock), the journal change, and the test's forced interleaving. Review loop stops (REVIEW.md §6).

## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Upright thumbnails (#511) | Sonnet | — | 1 | — | yes | R1-4 (deferred) |
| U2 | photo_order_json column, null compaction, AUTOINCREMENT ids | Opus | S4 | 1 (+1 review fix) | X3 → Scope widened to tests/test_import_zip.py | yes | U2R1-1 |
| U3 | Rank placement helpers | Opus | S2 | 1 | — | yes | — |
| U4 | Memories use rank placement and epochs | Opus | S4 | 1 | — | yes | R1-1, R1-2, R1-3, R2-1, R3-2 |
| U5 | Journal parity | Opus | S4 | 1 | — | yes | R1-2, R1-3, R2-1 |
| U6 | Thumbnail orientation backfill script | Opus | S4 | 1 (+1 guard) | — | yes | R1-5, U6R1-1, U6R1-2, U6R2-1 |
| U7 | Reorder script on the rank model, runbook | Opus | S4 | 1 (+3 review fixes) | — | yes | R3-1, R4-1, U7R1-1, U7R1-2, U7R2-1, U7R2-2, U7R3-1 |
| F1 | Deletes and replace cleanup serialised against placement | Opus | S5 | 1 (+1 review fix) | X3 reported (archive-import replace window); decided D1, out of envelope, no new unit | yes | IR1-1, IR2-1, IR2-2 |

Notes:
- Split overrides: U5 moved from wave 2 to its own wave 3 (it depends on U4) and its Scope gained tests/test_memories_photo_order.py.
- Wave 2 was launched while the wave-1 full suite was still running in the CI container (slow under shared load); no wave-2 unit was merged before that suite came back green (6243 passed).
- The local Windows .venv reproduces the known FastAPI router race (405/404 on threaded app startup); the authoritative suite runs are in the traxjourney-py314-citest container.
- Mid-delivery another session switched the shared checkout to main and stashed the uncommitted ledger; recovered from the stash by SHA, and integration moved to a dedicated worktree (.claude/worktrees/photos-int).
- Final full suite on caf94dc3 (traxjourney-py314-citest, CI command): 6297 passed, 39 skipped, 1 failed — tests/test_video_camera.py::test_ninety_seconds_of_a_long_trip_is_fast, a timing test in code this branch does not touch, under load from two other sessions' suites; it passed 3/3 when re-run alone.
- Follow-ups filed: #565 (per-memory re-sync from Polarsteps), #566 (duplicate photos on a retried step creation, plan O1).
- Pre-existing gaps reported by F1, outside this plan: deleting a trip leaves its photo files on disk and counted (repo_core.delete_project); account deletion leaves companion-authored journal photos on the deleted user's trips counted; archive-import replace reads photos_json without photo_lock.
