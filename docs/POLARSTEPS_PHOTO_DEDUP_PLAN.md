# Polarsteps photo dedup — Plan for #566

Package 5 was #566 and #551. The owner moved #551 to its own plan on
2026-10-10, trip-independent activities: trips are independent, and an
activity in two trips should be two copies. That plan also closes the reset
bug and the shared-split issue found while investigating #551. This plan is
#566 only.

## Problem

Since #237, a photo downloaded into a memory is placed by rank and never
overwrites another. If a memory that already has its photos receives the same
step's downloads again, it ends up with a second copy of every photo. The
owner sees the duplicates in the memory and pays for them in storage.

The issue names the client's automatic 5xx retry (`_postWithRetry`) as the
route. **It is not one on its own.** The client queues photo downloads only
after it gets a memory id
(`flutter_client/lib/src/projects/polarsteps_import_notifier.dart:351-375`).
A retry after a create that committed but answered 502 therefore queues each
photo exactly once. The routes that do duplicate are:

- **Pressing Import again after a partly failed run.** The screen stays open
  on failure. `importSelected` never removes the steps that succeeded from the
  selection. `alreadyImportedIds` is refreshed only when a trip is selected
  (`polarsteps_import_notifier.dart:133-139, 245-305`). The second press
  re-posts the steps that succeeded. The server returns their existing
  memories (`api/memories.py:391-396`), and every photo is downloaded again.
- **Two clients importing the same step at once** (two devices, or two tabs).
  The loser of the unique-index race gets the winner's id
  (`api/memories.py:438-446`), and both clients queue the photos.

## Current state

- **Storage.** Photos have no table of their own. A memory's photos live in:
  - `memory.photos_json`: a dense list of UUIDs;
  - `memory.photo_order_json`: `{"epoch", "ranks"}`.

  Both are managed by the pure functions in `api/photo_order.py`, under
  `photo_lock` (`api/photo_locks.py`). Journal entries use the same module.
- **Other writers of the order state.** `scripts/reorder_polarsteps_memory_photos.py:186`
  calls `dump_state` with a dict it builds itself, and its tests assert the
  exact two-key state (`tests/test_reorder_polarsteps_memory_photos.py:371,386,506`).
  So does `tests/test_memories_photo_order.py:295`.
- **The download path.** `POST /api/memories/{id}/photos/from-url` queues
  `_download_photo_from_url` (`api/memories.py:681-705`) as a FastAPI
  background task. The task:
  1. fetches the bytes;
  2. checks the quota;
  3. writes the files;
  4. calls `_write_memory_photo` (`:640-678`), which places the photo under
     the lock, or drops it if the memory's epoch moved.
- **Originals are stored byte for byte** (`src/utils/photo_store.py`), so a
  photo's content hash can be computed from the stored file at any time.
  `scripts/reorder_polarsteps_memory_photos.py` already relies on this.
- **Memory create.** It looks up `(project_id, polarsteps_step_id)`
  (`api/memories.py:304-311`). If there is no match but a memory with the
  same name and date exists, it adopts that memory (`_adopt_and_refresh`,
  `:332-361`), which clears its photos and bumps the epoch.

## Decisions

1. **Idempotent per photo content, checked by the server** (owner,
   2026-10-10).
   - Each photo downloaded from a URL records the sha256 of its fetched bytes
     in `photo_order_json`, as `"hashes": {uuid: sha256hex}`.
   - A download whose hash is already in the memory is not stored.
   - *Rules out* "the server returns `created: false` and the client skips
     all downloads". A step whose create committed but failed client-side
     would be retried into a memory with no photos at all.
2. **The key is the content, not the URL** (owner, 2026-10-10, review
   envelope question R1).
   - Nothing shows what a Polarsteps photo URL looks like across two fetches.
     If the path itself carries a signature, a URL key would never match.
   - The content hash makes no assumption about Polarsteps' URL scheme. It is
     the same test the cleanup script uses.
   - The client sends nothing new, so installed builds are protected too (E5).
   - *Rules out* a URL-derived key and a client-sent key.
   - Accepted cost: a repeated import still fetches each photo once to hash
     it, but stores nothing.
3. **Checked twice, both after the fetch.**
   - First, before any file is written or quota checked: if the memory
     already holds the hash, the download stops there.
   - Then again at placement, under `photo_lock`. Only this check is
     authoritative: two concurrent downloads of one photo both pass the first,
     and the second one to place has its files deleted and uncounted.
   - *Rules out* checking only at queue time, which cannot know the content.
4. **Hashes follow the photo.**
   - `place` records the hash, `remove` drops it, and `clear` empties it (so
     the adopt path's refresh repopulates a clean set).
   - `replace` gives the replacement the old photo's hash. That hash names the
     download the photo stands for, so a re-import does not bring back an
     original the owner replaced.
   - A photo the owner deleted is downloaded again if its step is imported
     again.
   - Only `from-url` downloads record or check hashes. A manual upload records
     none and is never refused.
   - *Rules out* tombstones: the import screen already blocks re-selecting
     imported steps.
5. **Other writers keep the hashes.** The reorder script moves photos without
   changing their UUIDs, so it carries the row's existing hashes over
   (review R1-1). `dump_state` writes all three keys, and every caller passes
   a full state.
6. **The cleanup of existing duplicates is an owner-run script, Polarsteps
   memories only** (owner, 2026-10-10).
   - It covers memories with `polarsteps_step_id IS NOT NULL`. Photos whose
     full-resolution files have the same sha256 are duplicates.
   - It keeps the first in `photos_json` order and deletes the rest, with
     storage accounting and share-copy removal.
   - **For each memory it writes the row first, then deletes the files**
     (review R1-2, owner override to Fix now). An interrupted run then leaves
     at most files that no row lists, which nothing displays. A re-run reports
     them. It never leaves a listed photo without a file.
   - It is dry-run by default. `--apply` requires `--api-stopped`, as
     `scripts/backfill_thumbnail_orientation.py` does.
   - The script also records the hash of every photo it keeps. Those memories
     then get the dedup protection that new imports have.
   - *Rules out* a migration, because the cleanup needs file I/O. It also
     rules out deduping manual memories, where a user may have uploaded the
     same photo twice on purpose.

## Review envelope

REVIEW.md §2 defaults apply, with these additions:

- **Concurrency on memory photos.** Many `from-url` background downloads for
  one memory run at once, along with manual uploads, replaces and deletes. All
  of them are serialised by the in-process `photo_lock`. There is one API
  process (E1), so that lock is sufficient.
- **The cleanup script runs with the API stopped.** Concurrency with the live
  API is out of scope for it, and its flags refuse it.
- **Old clients (E5).** They keep sending `from-url` unchanged and get the
  dedup for free.
- **Scale.** Memories of up to a few hundred photos.

## Boundaries crossed

- **API contract:** none. `from-url` still answers 202, and a skipped
  duplicate is not an error.
- **Stored data:**
  - `photo_order_json` gains a `hashes` key. `load_state` tolerates its
    absence, which is every existing row. It drops malformed entries one by
    one, as it already does for `ranks`.
  - Memories imported before this ships get hashes only when the cleanup
    script runs over them (Decision 6).
- **Schema / migration:** none.
- **Export format:** none. `photo_order_json` is not exported.

## Conventions

- Server tests run in the py314 citest container. Threaded tests use
  file-backed SQLite (memory `feedback_threaded_tests_sqlite`).
- Every user-visible commit carries a `Release-Note:` trailer
  (docs/RELEASING.md).

## Open decisions

None.

## Execution units

### Wave 1

#### U1 — Photo hashes in the order state
- **Goal:** `api/photo_order.py` carries a per-photo content hash through
  every operation, and every other writer of the state keeps it.
- **Scope:**
  - `api/photo_order.py`
  - `tests/test_photo_order.py`
  - `tests/test_memories_photo_order.py`: the exact-state assertion at ~:295
    only.
  - `scripts/reorder_polarsteps_memory_photos.py`: `_write_reorder` only.
  - `tests/test_reorder_polarsteps_memory_photos.py`
- **Context:**
  - Read the module docstring and every function of `api/photo_order.py`.
  - Example to follow: how `ranks` is parsed in `load_state` (each entry is
    validated on its own) and carried by `place`, `remove` and `replace`.
  - Plan Decisions 4 and 5; review ledger R1-1.
- **Do:**
  1. Add `"hashes": {uuid: str}` to the state; `_empty` gets `{}`.
  2. `load_state` keeps only `str → 64-char lower-case hex` entries. A
     malformed `hashes` value never resets `epoch` or `ranks`.
  3. `dump_state` writes all three keys.
  4. `place(photos, state, uuid, rank, content_hash=None)` records the hash
     when one is given.
  5. `remove` drops the hash, `replace` moves the old photo's hash to the new
     UUID, and `clear` empties it.
  6. Add `has_hash(state, photos, h) -> bool`. It is true only when a UUID
     currently in `photos` carries `h`; a stale entry for a removed UUID does
     not count.
  7. `_write_reorder` in the reorder script builds the state from the row's
     loaded state, with `ranks` emptied and `epoch` and `hashes` kept.
  8. Update the exact-state assertions to the three-key shape.
  9. Update the docstring.
- **Acceptance:**
  - `pytest tests/test_photo_order.py tests/test_memories_photo_order.py tests/test_journal_photo_order.py tests/test_reorder_polarsteps_memory_photos.py`
    passes.
  - New tests cover:
    - the round-trip of hashes;
    - a state with no `hashes`;
    - malformed `hashes` next to a valid epoch;
    - `place` with a hash;
    - `remove` dropping it;
    - `replace` carrying it;
    - `clear` emptying it;
    - `has_hash` ignoring stale entries;
    - a reorder `--apply` keeping a memory's hashes.
- **Out of scope:** every caller in `api/memories.py` and `api/journal.py`.
- **Latitude:** none.
- **Escalate if:**
  - another writer of `photo_order_json` turns up that builds the state
    itself;
  - a file outside Scope must change (X3).
- **Depends on:** —

#### U3 — Owner script: remove duplicate photos from Polarsteps memories
- **Goal:** `scripts/dedupe_memory_photos.py` removes byte-identical duplicate
  photos from Polarsteps-imported memories, and records the hashes of those it
  keeps.
- **Scope:**
  - `scripts/dedupe_memory_photos.py` (new)
  - `tests/test_dedupe_memory_photos.py` (new)
  - `docs/RELEASING.md`: a new subsection under "Post-deploy owner actions".
- **Context:**
  - Example to follow, for the CLI, the `--apply`/`--api-stopped` guard, the
    use of the app's DB connection and storage accounting:
    `scripts/backfill_thumbnail_orientation.py` and
    `tests/test_backfill_thumbnail_orientation.py`.
  - Hashing stored originals: `scripts/reorder_polarsteps_memory_photos.py:95-147`.
  - How a photo is deleted: `_delete_photo_files` and `delete_photo` in
    `api/memories.py`.
  - Plan Decision 6; review ledger R1-2.
- **Do:**
  1. Select memories with `polarsteps_step_id IS NOT NULL`. `--project ID`
     (repeatable) narrows the selection.
  2. In each memory, sha256 every listed photo's full-resolution file. A
     listed photo whose file is missing is reported and is never a duplicate.
  3. Keep the first photo of each hash, in `photos_json` order. Build the new
     state:
     - drop the others with `photo_order.remove`;
     - record each kept photo's hash, unless it already carries one.
  4. **Write and commit the row first.** Then, for each dropped photo, unlink
     its files with `unlink_and_record` and call `remove_share_copy`.
  5. Also report the files in a selected memory's folder that its row does not
     list (left over by an interrupted run). Report only, never delete.
  6. Dry-run by default: print, per memory, what would be removed and the
     bytes freed. `--apply` without `--api-stopped` is refused.
  7. Print a summary: memories touched, photos removed, bytes freed, hashes
     recorded, unlisted files.
  8. Add the owner-action subsection: take a DB copy, dry-run, apply with the
     API stopped, on val first, then prod.
- **Acceptance:**
  - `pytest tests/test_dedupe_memory_photos.py` passes.
  - Its tests cover:
    - duplicates are removed and the first is kept;
    - kept photos get their hashes;
    - storage usage drops by the removed files' size;
    - the share copy is removed;
    - non-Polarsteps memories are untouched;
    - a missing file is never a duplicate;
    - a dry run writes nothing;
    - `--apply` without `--api-stopped` exits non-zero;
    - a second run is a no-op;
    - a simulated interruption between the row write and the unlink leaves no
      listed photo without a file, and a re-run reports the leftover files.
- **Out of scope:**
  - journal entries;
  - deleting unlisted files;
  - re-downloading from Polarsteps.
- **Latitude:** local design.
- **Escalate if:**
  - usage accounting cannot be reached from a script without starting the
    app;
  - a file outside Scope must change (X3).
- **Depends on:** U1 (it writes the three-key state).

### Wave 2

#### U2 — `from-url` does not store a photo the memory already has
- **Goal:** a memory never gets a second copy of a photo downloaded from a URL.
- **Scope:** `api/memories.py` (the from-url path and `_write_memory_photo`),
  `tests/test_memories_photo_order.py`.
- **Context:**
  - `api/memories.py:640-772`.
  - U1's `place(..., content_hash=)` and `has_hash`.
  - Example to follow, for dropping a photo at placement and deleting its
    files: the epoch check in `_write_memory_photo`.
  - For concurrent downloads: the existing concurrency tests in
    `tests/test_memories_photo_order.py`.
  - Plan Decisions 1-4.
- **Do:**
  1. In `_download_photo_from_url`, after the fetch, take the sha256 of the
     bytes.
  2. Read the memory's state without the lock. If `has_hash` finds the hash,
     log at info level and return. That skips the quota check and the file
     writes.
  3. `_write_memory_photo` gains `content_hash=None`. Under the lock:
     - if the hash is present, delete the just-written files the way the
       epoch path does, and return False;
     - otherwise, place the photo with the hash.
  4. A manual upload and a replace pass no hash. A replace keeps the old
     photo's hash through U1.
- **Acceptance:**
  - New tests cover:
    - the same bytes queued twice into one memory, one after the other → one
      photo;
    - the same bytes queued twice concurrently → one photo, and the loser's
      files are deleted and uncounted;
    - the same bytes from two different URLs → one photo;
    - two different photos → two photos, in rank order;
    - after the adopt path's clear, the same photos download again;
    - deleting a photo and re-queuing it → it downloads again;
    - a skipped duplicate writes no file and makes no quota check;
    - a manual upload of bytes the memory already holds is still stored.
  - `pytest tests/test_memories_photo_order.py tests/test_polarsteps_dedup.py`
    passes.
- **Out of scope:**
  - the journal `from-url` twin (no client calls it);
  - client changes;
  - the import screen re-posting the steps that succeeded.
- **Latitude:** none.
- **Escalate if:** a file outside Scope must change (X3).
- **Depends on:** U1.

## Definition of done

- A memory never holds two photos downloaded from URLs with the same content,
  whether the downloads ran one after the other or concurrently. Different
  photos are all placed, in rank order.
- Pressing Import twice on the same Polarsteps steps leaves each memory with
  one copy of each photo, on any client build.
- The reorder script keeps a memory's hashes.
- `scripts/dedupe_memory_photos.py` dry-runs and applies as specified, and
  never leaves a listed photo without a file. `docs/RELEASING.md` lists it as
  a post-deploy owner action.
- The full pytest suite passes.
