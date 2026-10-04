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
