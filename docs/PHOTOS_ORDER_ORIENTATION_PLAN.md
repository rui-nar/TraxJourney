# Photos: upright thumbnails and kept order — Plan for #511 and #237

## Problem

**#511 — sideways thumbnails.** A portrait photo taken on a phone is stored
with an EXIF orientation tag (usually 6 or 8) instead of rotated pixels. The
thumbnail generator ignores that tag, so the 400×400 thumbnail is sideways.
Every place that shows thumbnails shows it sideways: the memory list, journal,
companions, share-link viewers and the poster's photo cards
(`src/poster/poster_renderer.py:424` reads `_thumb.jpg`). The metadata-free
share copy (#430) is transposed correctly, which makes the mismatch visible.
Every thumbnail already on disk for a rotated photo is wrong and stays wrong
until it is regenerated.

**#237 — photo order.** The owner's observed symptom is memories imported from
Polarsteps **before PR #239** (Aug 2026), whose photos are scrambled. PR #239
fixed new imports and shipped `scripts/reorder_polarsteps_memory_photos.py`
to repair old ones, but that script was never run on production and the issue
was left open.

Investigating it found that the #239 design — a photo's intended position is
a **list index** into `photos_json`, with `null` placeholders for slots not yet
filled — still loses or misplaces photos (memories and journal alike):

1. **Overwrite.** `_write_memory_photo` (`api/memories.py:569-571`) does
   `photos[order] = uuid` without checking the slot. A manual upload appended
   while an import is still downloading lands at index *k*; the download for
   order *k* then overwrites it. The overwritten photo vanishes from the list
   and its files stay on disk, still counted against the owner's quota.
2. **Shift.** `delete_photo` uses `photos.remove()` (`api/memories.py:684`),
   moving every later entry down one; downloads still in flight then land one
   slot off and overwrite a neighbour.
3. **Unvalidated `order`** (`api/memories.py:607`). `-1` overwrites the last
   photo; on an empty list it raises `IndexError` in the background task after
   the files are written (orphaned, counted); a huge value allocates a huge
   list of `null`.
4. **Stale re-import downloads.** `_clear_memory_photos` (`:541`) runs outside
   `photo_lock`, and nothing ties a background download to the import that
   queued it. A download from an earlier import that finishes after a
   re-import's clear lands in the fresh list (appended, or overwriting a slot).

`api/journal.py` has the same `_write_journal_photo` overwrite (`:184-186`),
the same `remove` shift (`:493`) and the same missing validation (`:247`).

## Current state

- `src/utils/photo_store.py` `_thumbnail` / `write_photo_files`: the one path
  every memory and journal photo takes (upload, replace, from-url, trip
  archive import #469). It stores the raw bytes as they came and writes a
  400×400 JPEG thumbnail. JPEGs are decoded at reduced scale (draft) inside
  `img.thumbnail(...)`.
- `src/utils/photo_privacy.py:112` already uses `ImageOps.exif_transpose`
  for the share copy — the idiom to follow.
- Checked during planning: after `img.thumbnail()` on a draft-decoded JPEG,
  `img.getexif()` still carries the orientation tag, and
  `ImageOps.exif_transpose` on the 400px result yields the upright size. So the
  transpose costs nothing extra.
- `DBMemory.photos_json` / `DBJournalEntry.photos_json`
  (`models/project_db.py:416, 459`): JSON array of UUID strings, display order,
  may contain `null`. Every reader filters falsy entries
  (`src/project/repo_row_mappers.py:33, 93`, `api/projects.py:396`).
- `api/photo_locks.py` `photo_lock(kind, id)`: in-process per-entity lock
  around every `photos_json` read-modify-write in `api/memories.py` and
  `api/journal.py`.
- Clients: the Polarsteps import notifier sends `order` = index within the
  step and fires all of a step's downloads concurrently
  (`flutter_client/lib/src/projects/polarsteps_import_notifier.dart:342-354`).
  Manual uploads are sequential appends with no `order`. No client calls the
  journal `from-url` route. There is no reorder UI.
- `scripts/reorder_polarsteps_memory_photos.py` (+ `tests/test_reorder_polarsteps_memory_photos.py`):
  dry-run-by-default repair of pre-#239 imports by content-hashing
  re-downloaded Polarsteps photos against the stored originals. Writes
  `photos_json` directly through `sqlite3`.
- Tests: `tests/test_photo_store.py`, `tests/test_memories_photo_order.py`
  (covers both memories and journal), `tests/test_photo_replace_api.py`.

## Decisions

**D1 — Transpose the thumbnail, not the stored original.** Apply
`ImageOps.exif_transpose` to the thumbnail image in `photo_store._thumbnail`
after `thumbnail()` and before returning. The original stays byte-for-byte as
uploaded: `reorder_polarsteps_memory_photos.py` depends on that, and the share
copy already handles orientation. *Rules out:* rotating the stored original
(breaks content-hash recovery, changes storage accounting for every upload);
transposing before `thumbnail()` (forces a full-size decode of every JPEG).

**D2 — Existing thumbnails are fixed by an operator script.** A new
`scripts/backfill_thumbnail_orientation.py`, dry-run by default, regenerates
the thumbnail of every memory and journal photo whose original carries an
orientation tag other than 1, and corrects the owner's counted storage by the
size difference. *Rules out:* a startup job (work on every boot of every env)
and no backfill (old trips stay sideways for good). Avatars are **not** in
scope (owner decision); the script's selection keeps them out.

**D3 — Order is a rank, not an index.** `photos_json` stays what every reader
and the export format already consume: the display-ordered UUID list. It
becomes **dense** — no `null` placeholder is ever written again. A new column
`photo_order_json` on `memory` and `journalentry` holds the placement state:

```json
{"epoch": 0, "ranks": {"<uuid>": 3, ...}}
```

- A photo that arrives with `order` *k* gets rank *k* and is **inserted**
  before the first photo whose rank is greater than *k*, or before the first
  unranked photo, whichever comes first. Equal ranks go after each other.
  Nothing is ever overwritten.
- A photo with no `order` (manual upload) is appended with no rank. Unranked
  photos sort after every ranked one for placement purposes, so a manual
  photo uploaded during an import stays after the imported set.
- Delete removes the UUID from the list and from `ranks`; nothing shifts in a
  way that matters, because placement never uses positions.
- Replace puts the new UUID at the old one's position and gives it the old
  rank.
- `ranks` entries for UUIDs no longer in `photos_json` are ignored (writers
  that are not rank-aware, like archive import, may leave them stale).

*Rules out:* patching the positional list (keep `null` gaps, refuse
overwrites, delete-to-`null`) — owner chose the rank model; and putting
`{uuid, rank}` objects into `photos_json` itself — that changes every reader,
the export format and the API contract for no benefit.

**D4 — Re-import invalidates in-flight downloads with an epoch.**
`_clear_memory_photos` empties the list and ranks and increments `epoch`.
Its caller `_adopt_and_refresh` holds `photo_lock` from before the clear
until after its `sess.commit()`, so no placement can interleave between the
clear and its commit (R1-1); `_clear_memory_photos` itself does not take the
lock (`photo_lock` is not re-entrant). The `from-url` route reads the epoch
when it queues a download and passes it to the background task; the writer
drops a download whose epoch no longer matches, deleting its files through
`unlink_and_record`. *Rules out:* leaving stale downloads to land (the
current behaviour) and per-download tokens (more state for the same effect).

**D4c — Memory and journal entry ids are never reused.** Without
AUTOINCREMENT SQLite gives a new row `max(id) + 1`, so deleting the newest
memory hands its id to the next one created, and any photo write still in
flight for the deleted one (a `from-url` download, an upload in its
thumbnail step, a replace) lands in the new one (R2-2, R3-2). U2 rebuilds
`memory` and `journalentry` with `sqlite_autoincrement`, exactly as
`alembic/versions/6abe17b5d61f_userinfo_autoincrement.py` did for
`userinfo` (#429). After that, "the row is gone" is a sufficient check on
every path, memories and journal alike. *Rules out:* threading `public_id`
through each writer (memory only — journal entries have none — and every
new writer must remember it); a nonce column on `journalentry` (a schema
field that only works around id reuse). Residual, same as #429: an id
deleted *before* the migration that was above the then-max can be handed
out once; in-flight downloads do not survive the deploy restart, so nothing
can be waiting on it.

**D4b — A photo that cannot be placed never leaves files behind.** Every
path that writes photo files and then fails to put the UUID in the list
deletes those files (and their counted storage) before returning: a stale
epoch (D4), a memory or journal entry deleted while the download was in
flight (R1-2, R2-2), and a replace whose old photo — or whose whole memory
or journal entry — was deleted meanwhile, which also answers 404 (R1-3,
R2-1).

**D5 — `order` is bounded.** `PhotoFromUrlIn.order` gets `ge=0, le=9999` on
both routes. Shipped clients send step indices (well under 9999) so none of
them is refused (E5). Out-of-range values get the standard 422.

**D6 — Old scrambled memories are repaired by running the existing script.**
`reorder_polarsteps_memory_photos.py` is updated for D3 (it writes a dense
list and resets `ranks`, keeping `epoch`) and its runbook steps are added to
`docs/RELEASING.md`'s post-deploy notes for this release. Running it on
val and then prod is an **owner action**, after a DB copy, dry-run first.

**D7 — Journal parity.** Every D3–D5 rule applies to `api/journal.py`
identically, through the same helper module.

## Review envelope

REVIEW.md defaults apply, with these additions:

- **Concurrency.** `photos_json` and `photo_order_json` are written only by
  the API process (except the one-off reorder script, below) (E1: one process), under `photo_lock`. In scope: any
  interleaving of uploads, `from-url` background downloads, deletes,
  replaces and a re-import's clear on the same memory or journal entry.
  Check during review that no RQ job writes either column; if one does, that
  is a finding.
- **Archive import (#469) and export** rewrite `photos_json` without touching
  ranks. A background download landing during an archive import on the same
  memory is out of scope (no client does both at once).
- **The backfill script** runs by the admin (E3) **with the API stopped**
  (owner decision, review round 2), against a data directory our own code
  wrote. Races with live requests are out of scope; `--apply` refuses to run
  without an explicit `--api-stopped` flag. It must still survive an
  unreadable or missing original (skip and report, never crash mid-run).
- **The reorder script** (D6) writes `photos_json` / `photo_order_json`
  through raw `sqlite3`, outside `photo_lock`, with the API live (owner
  decision, review round 4). A clash with a live edit of the same pre-#239
  memory during the run is an accepted residual: those memories are old and
  rarely edited, and the run is a one-off by the admin.
- **Id reuse** is removed at the source by D4c (owner decision, review
  round 3); the envelope covers SQLite only (Postgres never reuses sequence
  values).
- **Known residual, not a defect of this plan:** when a client retries a
  Polarsteps step creation whose first attempt committed, the server returns
  the existing memory and the client re-queues every download. Before this
  plan that overwrote the earlier photos; after it, it adds duplicates. See
  Open decisions O1.

## Boundaries crossed

- **Schema.** New nullable `TEXT` column `photo_order_json` on `memory` and
  `journalentry` (Alembic, one revision on top of the single current head
  `87200bcb9342` — re-check `alembic heads` after rebasing). The same revision
  compacts `null` entries out of every existing `photos_json`. Readers already
  filter them, so nothing visible changes. A `NULL` `photo_order_json` reads
  as `{"epoch": 0, "ranks": {}}`; no data backfill is needed for it.
  The same revision rebuilds `memory` and `journalentry` with SQLite
  AUTOINCREMENT (D4c; batch copy-drop-rename as in #429): rows, indexes and
  ids are kept, and `sqlite_sequence` starts at each table's current max id.
  Other tables referencing these ids (`projectitem.memory_id`, comments,
  likes, translations) are untouched because ids do not change.
- **API contract.** `POST /api/memories/{id}/photos/from-url` and the journal
  equivalent: `order` now refuses values outside 0..9999 with 422. All shipped
  clients send in-range values. Response shapes unchanged. `photos` in every
  response is unchanged in form and never contained `null`.
- **Stored files.** Thumbnails of rotated photos are rewritten by the backfill
  script; their sizes change by a few KB, and counted storage is adjusted by
  the difference. Originals are untouched.
- **Export format.** None. Exports read `photos_json`, which keeps its form.
- **Downgrade.** The migration's downgrade drops the column and rebuilds both
  tables without AUTOINCREMENT. The `null` compaction is not reversed (not
  needed — `null` carried no data).

## Conventions

- All `photos_json` / `photo_order_json` mutation in `api/memories.py` and
  `api/journal.py` goes through the pure functions of the new
  `api/photo_order.py` (U3), called inside `photo_lock`. No handler indexes
  `photos_json` by an `order` value again.
- Pure functions take and return plain values (`list[str]`, `dict`); they do
  not touch the DB, the disk or the lock.
- Tests for the API behaviour use a file-backed SQLite engine when they run
  requests concurrently (see memory note on threaded tests: `StaticPool` +
  concurrent requests fails randomly).
- Commits that change user-visible behaviour carry a `Release-Note:` trailer
  (CLAUDE.md, `docs/RELEASING.md`).

## Open decisions

- **O1 — Duplicate photos on a retried Polarsteps step creation.** After D3,
  a retried step creation that had committed yields duplicate photos instead
  of overwritten ones. Fixing it properly needs the server to tell the client
  the memory already existed (or `from-url` to be idempotent per source URL).
  *Affects:* nothing in this plan. **Decided (review round 1):** file a
  follow-up issue when this package merges.
- **O2 — Full-size viewer orientation on Flutter.** Thumbnails will be upright
  after this plan; whether each client rotates the **full-size** original by
  its EXIF tag in the photo viewer is not verified (web/CanvasKit, Android,
  iOS). *Affects:* the definition of done's device checks only. *Decide by:*
  the owner's device check after val deploy; a sideways full-size viewer
  becomes its own issue.
- **O3 — When the reorder script runs.** Owner action after deploy (D6); needs
  `CREDENTIALS_ENCRYPTION_KEY` on the host and the owners' stored Polarsteps
  tokens to still be valid. *Decide by:* release day.

## Execution units

### Wave 1 (independent)

#### U1 — Upright thumbnails (#511)

- **Goal:** `write_photo_files` writes thumbnails rotated according to the
  original's EXIF orientation.
- **Scope:** `src/utils/photo_store.py`, `tests/test_photo_store.py`.
- **Context:** `src/utils/photo_store.py` `_thumbnail`; follow the
  `ImageOps.exif_transpose` use in `src/utils/photo_privacy.py:112`. Existing
  tests in `tests/test_photo_store.py` (e.g.
  `test_a_jpeg_is_decoded_at_reduced_scale`) show the fixture style.
- **Do:**
  1. In `_thumbnail`, apply `ImageOps.exif_transpose` to the thumbnail image
     on both branches (JPEG draft path and other formats), after
     `thumbnail()`, before the RGB return. Keep the decode-at-reduced-scale
     behaviour.
  2. Make sure the returned image still has `info` cleared by
     `write_photo_files` (exif_transpose returns a new image whose `info`
     carries EXIF minus orientation; the existing `info.clear()` handles it —
     keep it after the transpose).
  3. Update the module docstring line that says the thumbnail is generated
     "from them" to mention it is upright.
- **Acceptance:** new tests in `tests/test_photo_store.py`:
  `test_a_rotated_jpeg_gets_an_upright_thumbnail` (parametrised over
  orientations 3, 6, 8 at least, asserting thumbnail width/height swap for
  6/8 and a known corner pixel colour moved for 3);
  `test_an_upright_jpeg_thumbnail_is_unchanged` (orientation 1 and no EXIF);
  `test_a_rotated_jpeg_is_still_decoded_at_reduced_scale`;
  `test_a_rotated_png_gets_an_upright_thumbnail` (non-JPEG branch);
  `test_the_stored_original_is_unchanged` (bytes equal to the input).
  `pytest tests/test_photo_store.py tests/test_photo_upload_processing.py tests/test_share_photo_exif.py`
  passes.
- **Out of scope:** avatars (`api/people.py`), the backfill, rotating the
  stored original.
- **Latitude:** none.
- **Escalate if:** the transpose cannot keep the reduced-scale decode; a file
  outside Scope must change.
- **Depends on:** —

#### U2 — `photo_order_json` column, null compaction, AUTOINCREMENT ids

- **Goal:** add `photo_order_json` to `memory` and `journalentry`, remove
  `null` entries from every stored `photos_json`, and make both tables'
  ids never reused (D4c).
- **Scope:** `models/project_db.py`, one new file in `alembic/versions/`,
  `tests/test_migration_photo_order.py` (new).
- **Context:** an additive-column migration to follow:
  `alembic/versions/f6a7b8c9d0e1_add_lock_version_to_project.py`; the
  AUTOINCREMENT rebuild to follow, model and migration both:
  `models/user.py:39-43` (`__table_args__ = {"sqlite_autoincrement": True}`
  with its comment) and
  `alembic/versions/6abe17b5d61f_userinfo_autoincrement.py` (`_rebuild`,
  SQLite-only guard). Its test, if any (grep `6abe17b5d61f` / `autoincrement`
  in `tests/`), is the test example. Memory notes: always Alembic; check
  `alembic heads` is a single head.
- **Do:**
  1. Add `photo_order_json: Optional[str] = sqlmodel.Field(default=None)` to
     `DBMemory` and `DBJournalEntry`, with a comment naming its shape
     (`{"epoch": int, "ranks": {uuid: int}}`) and pointing to
     `api/photo_order.py`.
  2. New revision `down_revision = "87200bcb9342"` (or the current single
     head): `add_column` on both tables (nullable TEXT), then a data step that
     reads `id, photos_json` and rewrites rows whose list contains a falsy
     entry, keeping order. Batch-mode where SQLite needs it.
  3. Same revision: rebuild `memory` and `journalentry` with
     `sqlite_autoincrement=True` (SQLite only), and add
     `__table_args__ = {"sqlite_autoincrement": True}` with a short comment
     (id reuse, #237/#511 plan D4c) to `DBMemory` and `DBJournalEntry`. If
     either model already has `__table_args__`, merge into it.
  4. Downgrade drops both columns and rebuilds both tables without
     AUTOINCREMENT.
- **Acceptance:** `tests/test_migration_photo_order.py`: upgrading a SQLite DB
  at the previous head containing a memory `["a", null, "b"]`, a journal
  entry `[null]` and a clean memory yields `["a","b"]`, `[]` and the clean
  list unchanged, with the new column present and `NULL`, all ids and rows
  kept; after upgrade, deleting the newest memory and inserting a new one
  gives a **new** id (same for journal entries); a fresh `create_all` schema
  (the tests' path) also has AUTOINCREMENT on both tables; downgrade removes
  the column. `alembic heads` prints one head. `pytest tests/test_migration_photo_order.py`
  passes.
- **Out of scope:** any reader/writer change.
- **Latitude:** none.
- **Escalate if:** `alembic heads` shows more than one head; the head is not
  `87200bcb9342` (re-base on whatever it is, and say so).
- **Depends on:** —

#### U3 — Rank placement helpers

- **Goal:** pure functions implementing D3/D4 placement over
  `(photos: list[str], order_state: dict)`.
- **Scope:** `api/photo_order.py` (new), `tests/test_photo_order.py` (new).
- **Context:** D3 and D4 above. Shape: small pure module like
  `api/photo_locks.py` (module docstring explaining the issue, then
  functions). No DB, no disk.
- **Do:** implement and document:
  - `load_state(raw: str | None) -> dict` — `NULL`/invalid → `{"epoch": 0, "ranks": {}}`.
  - `dump_state(state) -> str`.
  - `place(photos, state, uuid, rank: int | None) -> (photos, state)` — append
    unranked; insert ranked before the first photo with a greater rank or no
    rank; ties after equal ranks; never drops an entry; ignores ranks of UUIDs
    not in `photos`.
  - `remove(photos, state, uuid) -> (photos, state)`.
  - `replace(photos, state, old, new) -> (photos, state) | None` — same
    position, same rank; `None` when `old` is not in `photos` (deleted
    meanwhile), so the caller can undo its file write (D4b, R1-3).
  - `clear(state) -> state` — empty ranks, `epoch + 1`.
  - Inputs are never mutated in place.
- **Acceptance:** `tests/test_photo_order.py` covers: every permutation of
  arrival order for ranks 0..4 ends in rank order (itertools.permutations);
  a manual append during an import ends after all ranked photos; equal ranks
  keep arrival order and both survive; remove then late arrival places
  correctly (the old shift bug); replace keeps position and rank; replace of
  an absent `old` returns `None` and changes nothing; stale rank
  entries for absent UUIDs are ignored; `load_state` on `None`, `""`, `"{}"`,
  garbage; `clear` bumps epoch and empties ranks; inputs unchanged after each
  call. `pytest tests/test_photo_order.py` passes.
- **Out of scope:** wiring into handlers; locking.
- **Latitude:** local design (function signatures may differ if every rule
  above holds; list each deviation).
- **Escalate if:** a D3 rule turns out contradictory for some arrival order.
- **Depends on:** —

### Wave 2

#### U4 — Memories use rank placement and epochs

- **Goal:** every `photos_json` writer in `api/memories.py` goes through
  `api/photo_order.py`; `order` is bounded; stale downloads are dropped.
- **Scope:** `api/memories.py`, `tests/test_memories_photo_order.py`,
  `tests/test_photo_replace_api.py`.
- **Context:** current `_write_memory_photo`, `_download_photo_from_url`,
  `PhotoFromUrlIn`, `queue_photo_from_url`, `delete_photo`, `replace_photo`,
  `_clear_memory_photos` (`api/memories.py:530-725`). Follow the existing
  tests in `tests/test_memories_photo_order.py` for app/engine setup.
- **Do:**
  1. `PhotoFromUrlIn.order`: `Field(None, ge=0, le=9999, ...)`.
  2. `queue_photo_from_url` reads the memory's current epoch (via
     `load_state`) in its session and passes it to the background task.
  3. `_write_memory_photo(memory_id, uuid, order=None, epoch=None)`: under
     `photo_lock`, load the row and state. If the row is gone (memory or trip
     deleted while the download or upload ran — R1-2; with U2's
     AUTOINCREMENT a deleted id is never a different memory — D4c), or
     `epoch` is given and differs from the stored epoch, delete the
     just-written files
     (`_delete_photo_files` with the owner dir, which uncounts them) and
     return without writing; else `place(...)` and write both columns.
     `_download_photo_from_url` passes owner dir/epoch through. The upload
     route's own call gets the same row-gone cleanup.
  4. `delete_photo` → `remove(...)`; `replace_photo` → `replace(...)`; both
     write both columns. In `replace_photo`'s locked block, when the memory
     row is gone (deleted after the route's unlocked check — R2-1) or
     `replace(...)` returns `None` (the old photo was deleted — R1-3), delete
     the new files and answer 404; never a 500. Nothing in that block
     dereferences a row without checking it.
  5. `_clear_memory_photos` does **not** take the lock; it applies
     `clear(...)`. Its caller `_adopt_and_refresh` (`api/memories.py:~270-289`)
     takes `photo_lock("memory", id)` before calling it and releases it only
     after its `sess.commit()` (R1-1). Check no path inside that window takes
     `photo_lock` again (`photo_lock` is a plain `threading.Lock`, not
     re-entrant).
  6. Update the `_write_memory_photo` docstring: placeholders are gone.
- **Acceptance:** in `tests/test_memories_photo_order.py`, rewrite the tests
  that assert `null` placeholders and add:
  `test_a_manual_upload_during_import_is_not_overwritten`,
  `test_a_delete_during_import_does_not_shift_later_downloads`,
  `test_a_download_from_before_a_reimport_is_dropped_and_its_files_removed`
  (files gone and counted storage back to the pre-download value),
  `test_a_reimport_clear_and_a_concurrent_placement_do_not_interleave`
  (a placement attempted while `_adopt_and_refresh` holds the lock lands
  after the clear's commit and is then handled by the epoch rule — never
  overwritten by, nor overwriting, the clear),
  `test_a_download_for_a_deleted_memory_leaves_no_files` (files gone,
  counted storage back to the pre-download value),
  `test_replacing_a_photo_deleted_meanwhile_answers_404_and_leaves_no_files`,
  `test_replacing_a_photo_of_a_memory_deleted_meanwhile_answers_404_and_leaves_no_files`,
  `test_a_download_for_a_deleted_memory_does_not_land_in_a_new_memory`
  and `test_an_upload_for_a_deleted_memory_does_not_land_in_a_new_memory`
  (R3-2: delete the newest memory while the download / the upload's file
  write is in flight, create a new memory — which, with U2, gets a new id —
  then let the write finish: the new memory has no photos and the files are
  gone),
  `test_order_out_of_range_is_refused` (−1, 10000 → 422; 0 and 9999 accepted),
  `test_concurrent_downloads_lose_nothing` (file-backed SQLite, N threads,
  shuffled ranks). Existing `tests/test_photo_replace_api.py` passes, plus a
  test that replace keeps rank. `pytest tests/test_memories_photo_order.py tests/test_photo_replace_api.py tests/test_memories_photo_poll.py tests/test_staged_photos.py`
  passes.
- **Out of scope:** journal (U5); the client; O1.
- **Latitude:** local design (how the epoch and owner dir reach the writer).
- **Escalate if:** `_clear_memory_photos`'s caller already holds a lock that
  would deadlock; a file outside Scope must change.
- **Depends on:** U2, U3.

#### U5 — Journal parity

- **Goal:** the U4 changes, applied to `api/journal.py`.
- **Scope:** `api/journal.py`, `tests/test_journal_photo_order.py` (new).
- **Context:** U4's diff once integrated is the example to follow;
  `_write_journal_photo` (`api/journal.py:~170-200`), `PhotoFromUrlIn`
  (`:245`), delete (`:493`), replace (`:~526`).
- **Do:** as U4 steps 1–4 and 6 for the journal routes, including the
  row-gone file cleanup (R1-2) and the replace-after-delete 404 for both a
  deleted photo and a deleted entry (R1-3, R2-1). Journal has no
  re-import clear: if there is no clear path, the epoch is still threaded
  through (always matches) so the two modules stay alike — or, if simpler,
  omitted; say which.
- **Acceptance:** `tests/test_journal_photo_order.py` mirrors U4's tests that
  apply (manual-during-import, delete-no-shift, out-of-range 422, concurrent
  lose-nothing, replace keeps rank, download-for-deleted-entry leaves no
  files, replace-after-delete 404 with no files for a deleted photo and for a
  deleted entry, an upload for a deleted newest entry does not land in a
  newly created entry). The journal tests already in
  `tests/test_memories_photo_order.py` keep passing (adjusted only where they
  assert placeholders). `pytest tests/test_journal_photo_order.py tests/test_journal.py tests/test_memories_photo_order.py`
  passes.
- **Out of scope:** memories; client.
- **Latitude:** local design.
- **Escalate if:** `tests/test_memories_photo_order.py` needs journal
  changes U4 did not make (coordinate rather than both editing it).
- **Depends on:** U2, U3, U4.

#### U6 — Thumbnail orientation backfill script

- **Goal:** an offline script that regenerates the thumbnails of already
  stored rotated memory and journal photos and corrects counted storage.
- **Scope:** `scripts/backfill_thumbnail_orientation.py` (new),
  `tests/test_backfill_thumbnail_orientation.py` (new), `.dockerignore`.
- **Context:** CLI shape, dry-run-by-default and `sys.path` convention of
  `scripts/reorder_polarsteps_memory_photos.py`; folder layout from
  `src/utils/photo_paths.py`; storage delta via `src/billing/usage.py`
  `record_delta`.
- **Do:**
  1. `--data-dir` (and whatever `record_delta` needs to reach the DB, matching
     how other scripts open it), `--apply`; dry-run prints what would change.
     `--apply` refuses to run (non-zero exit, message saying to stop the API)
     unless `--api-stopped` is also passed; the script is not safe against
     live requests (envelope).
  2. Walk `users/<id>/memories/*/` and `users/<id>/journal/*/` only (not
     `people`). For each original `<uuid>.jpg` with a matching `_thumb.jpg`,
     read the orientation from the header only (`Image.open(...).getexif()`,
     no decode). Skip 1/missing: these are the **candidates**.
  3. For each candidate, build the upright thumbnail in memory through
     `photo_store._thumbnail` (U1) — the same code path as new uploads — and
     compare it with the thumbnail on disk: if they have the same size and
     their mean absolute per-channel pixel difference is below a small
     threshold (a named constant; JPEG re-encode noise only), the thumbnail is
     already upright and is skipped. This is the idempotency check (R1-5): the
     original's tag never changes (D1), so the tag alone cannot tell a fixed
     thumbnail from a sideways one, and orientation 3 (180°) keeps the
     dimensions, so a size check alone cannot either.
  4. With `--apply`, for each thumbnail that differs: write the upright one
     atomically (temp file + replace) and `record_delta(user_id, new - old)`.
     A dry run reports them without writing.
  5. Unreadable originals are skipped and listed; the run never stops early.
  6. Summary line: scanned / candidates / already upright / rewritten / skipped.
  7. Whitelist this script **and** `scripts/reorder_polarsteps_memory_photos.py`
     in `.dockerignore` (`!scripts/...` lines after `scripts/`, extending the
     existing comment) so both run in the image like the other operator
     scripts (R3-1). U6 owns this file for the package; U7 does not touch it.
- **Acceptance:** `tests/test_backfill_thumbnail_orientation.py`: a tmp data
  dir with a rotated memory photo, a rotated journal photo, an upright photo,
  a rotated avatar under `people/`, and a corrupt original. Dry run changes
  nothing; `--apply` rewrites exactly the two rotated memory/journal
  thumbnails upright, leaves the avatar, upright photo and corrupt one alone,
  records the size delta per user, and a second `--apply` rewrites nothing
  (reports them as already upright). The rotated fixtures include
  orientation 3 as well as 6 or 8. `--apply` without `--api-stopped` exits
  non-zero and writes nothing. `.dockerignore` whitelists both scripts:
  checked by building the image context (or, if a test already covers the
  whitelist, by extending it) and listing `/app/scripts` — the report shows
  the output.
  `pytest tests/test_backfill_thumbnail_orientation.py` passes.
- **Out of scope:** avatars; share copies (already upright); running it on
  any server.
- **Latitude:** local design.
- **Escalate if:** `_thumbnail` is not importable without side effects;
  counted storage cannot be adjusted from a script.
- **Depends on:** U1.

#### U7 — Reorder script speaks the new columns

- **Goal:** `scripts/reorder_polarsteps_memory_photos.py` writes a dense
  `photos_json` and resets ranks (keeping epoch), and its runbook is written.
- **Scope:** `scripts/reorder_polarsteps_memory_photos.py`,
  `tests/test_reorder_polarsteps_memory_photos.py`, `docs/RELEASING.md`
  (a post-deploy step for this release only — or wherever release-specific
  owner actions are recorded; check the file).
- **Context:** the script's own docstring and tests; D3, D6.
- **Do:**
  1. Where it writes `photos_json`, write a list with no `null`s and set
     `photo_order_json` to `{"epoch": <unchanged>, "ranks": {}}`. Tolerate
     the column being absent (a DB not yet migrated) by refusing with a clear
     message.
  2. Confirm it ignores `null` entries when reading (pre-migration copies may
     still have them).
  3. Document the run with the **exact commands**, in the form
     `docs/DEPLOYMENT_VPS.md:~913-927` uses for operator scripts
     (`docker compose run --rm --entrypoint python traxjourney scripts/<name>.py ...`,
     with the DB and data-dir paths as mounted in that container): DB copy,
     dry run, `--apply`, val then prod, needs `CREDENTIALS_ENCRYPTION_KEY`.
     The DB copy is **never a raw `cp`** (WAL with `wal_autocheckpoint=0`,
     `models/db.py:93-96`): write out the sqlite3 online-backup one-liner
     (`s=sqlite3.connect(<db>); d=sqlite3.connect(<copy>); s.backup(d)`, as in
     `docs/DEPLOYMENT_VPS.md` §4 step 1, adapted to `/opt/traxjourney` and
     `traxjourney.db`), and next to it the restore command (stop the API,
     put the copy back in place of `traxjourney.db`, remove any stale
     `-wal`/`-shm`, start the API) (R4-1). The reorder script runs with the
     API live (owner decision, review round 4; see envelope).
     Then the thumbnail backfill: **stop the API container**, dry run, then
     `--apply --api-stopped`, start the API again. Both scripts are in the
     image via U6's `.dockerignore` change (R3-1).
- **Acceptance:** existing tests pass; new test that `--apply` on a memory
  with stale ranks leaves `ranks` empty, `epoch` unchanged and no `null` in
  `photos_json`; new test for the unmigrated-DB refusal.
  `pytest tests/test_reorder_polarsteps_memory_photos.py` passes.
- **Out of scope:** running the script anywhere; changing its matching logic;
  `.dockerignore` (U6 owns it).
- **Latitude:** none.
- **Escalate if:** the script's matching logic depends on `null` slots.
- **Depends on:** U2, U3.

### Wave 3 — integration (orchestrator)

Full `pytest` (in the py314 citest container, with the WSL bash probe caveat),
`alembic heads` single head, `graphify update .` from main after merge.
Commits carry Release-Note trailers: #511 ("Portrait photos now show upright
in thumbnails and posters…") and #237 ("Photos no longer get lost or move
when you add or delete photos while an import is still downloading…").
PR closes #511 and #237 (one `Closes #N` each); O1 filed as a follow-up.

## Definition of done

- A JPEG with orientation 6 or 8 uploaded to a memory or journal entry gets an
  upright thumbnail; the stored original is byte-identical to the upload.
- Running `backfill_thumbnail_orientation.py --apply` twice on a data dir
  rewrites every rotated memory/journal thumbnail once, touches nothing else,
  and leaves counted storage matching the files on disk.
- No code path writes `null` into `photos_json`; after migration no stored
  `photos_json` contains one.
- For any arrival order of `from-url` downloads interleaved with manual
  uploads, deletes and replaces, no photo is lost from the list and ranked
  photos end in rank order — for memories and journal entries.
- A download queued before a Polarsteps re-import does not appear in the
  re-imported memory, and its files are removed and uncounted.
- `order` outside 0..9999 gets 422 on both `from-url` routes.
- No photo-writing path leaves files on disk that no list points at: a
  download for a deleted memory/entry, a stale-epoch download and a replace
  of a photo or memory/entry deleted meanwhile (404) all remove their files
  and uncount them.
- Memory and journal entry ids are never reused (AUTOINCREMENT), so no
  download, upload or replace for a deleted memory or entry lands in a new
  one.
- Both operator scripts are in the production image and the runbook gives
  the exact `docker compose run` commands.
- `reorder_polarsteps_memory_photos.py` runs against a migrated DB and its
  runbook is in the docs; running it on val/prod is listed as an owner action.
- Full test suite passes; `alembic heads` shows one head.
- Owner device checks after val deploy: a portrait phone photo shows upright
  in the memory list, a share link and a poster; O2 checked on web and
  Android.
