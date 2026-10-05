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
- Outcome: open

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
- Outcome: open
