# Polarsteps photo dedup — Plan for #566

Package 5 was #566 and #551. The owner moved #551 to its own plan on
2026-10-10, because trips are meant to be independent and an activity in two
trips should be two copies. That plan, trip-independent activities, also
closes the reset bug and the shared-split issue found while investigating
#551. This plan covers #566 only.

## Problem

Since #237, a photo downloaded into a memory is placed by rank and never
overwrites another. A memory that already has its photos and receives the same
step's downloads again therefore ends up with a second copy of every photo. The
owner sees duplicates in the memory and pays for them in storage.

The issue names the client's automatic 5xx retry (`_postWithRetry`) as the
route. **It is not one on its own.** The client queues photo downloads only
after it gets a memory id
(`flutter_client/lib/src/projects/polarsteps_import_notifier.dart:351-375`).
So a retry after a create that committed but answered 502 queues each photo
exactly once. The routes that do duplicate are:

- **Pressing Import again after a partly failed run.** The screen stays open on
  failure. `importSelected` never removes the steps that succeeded from the
  selection, and `alreadyImportedIds` is refreshed only when a trip is selected
  (`polarsteps_import_notifier.dart:133-139, 245-305`). The second press
  re-posts the succeeded steps. The server returns their existing memories
  (`api/memories.py:391-396`), and every photo is downloaded again.
- **Two clients importing the same step at once** (two devices, or two tabs).
  The loser of the unique-index race gets the winner's id
  (`api/memories.py:438-446`), and both clients queue the photos.

## Current state

- **How photos are stored.** Photos have no table. A memory's photos live in
  two columns, both managed by the pure functions in `api/photo_order.py`
  under `photo_lock` (`api/photo_locks.py`):
  - `memory.photos_json`: a dense list of UUIDs;
  - `memory.photo_order_json`: `{"epoch", "ranks"}`.

  Journal entries use the same module.
- **How a download works.** `POST /api/memories/{id}/photos/from-url` queues
  `_download_photo_from_url` (`api/memories.py:681-705`) as a FastAPI
  background task. The task:
  1. fetches the URL;
  2. checks the storage quota;
  3. writes the files;
  4. hands over to `_write_memory_photo` (`:640-678`), which places the photo
     under the lock, or drops it if the memory's epoch moved.

  The source URL is not stored anywhere.
- **Originals are stored byte for byte** (`src/utils/photo_store.py`), so two
  downloads of one source are byte-identical files.
  `scripts/reorder_polarsteps_memory_photos.py` already relies on this.
- **Memory create already deduplicates by step.** It looks up
  `(project_id, polarsteps_step_id)` (`api/memories.py:304-311`).
  - An unmatched memory with the same name and date is adopted instead
    (`_adopt_and_refresh`, `:332-361`). Adoption clears its photos and bumps
    the epoch, so the re-uploads that follow land in a clean memory.

## Decisions

1. **Idempotent per photo source, checked by the server** (owner,
   2026-10-10). Each photo downloaded from a URL records its source key in
   `photo_order_json` as `"sources": {uuid: key}`. A download whose source is
   already in the memory is skipped.
   - *Rules out* the "server returns `created: false`, client skips all
     downloads" option. A step whose create committed but failed client-side
     would be retried into a memory with no photos at all.
2. **The server derives the source key from the URL; the client sends nothing
   new.**
   - The key is the lower-cased scheme and host plus the path, without the
     query or fragment. A query string on a CDN URL is typically a signature
     or a resize parameter, so two fetches of one Polarsteps photo match.
   - *Rules out* a client-sent key. It would protect only builds that send it,
     and installed APKs cannot be forced to update (E5).
   - This refines the owner's choice ("client sends a source key") without
     changing what it protects. See O1.
3. **Checked twice.**
   - Before the fetch: a source that is already present costs no download and
     no quota check.
   - Again at placement, under `photo_lock`. Only this check is
     authoritative: two concurrent downloads of one source both pass the
     first. The second one to place has its files deleted and uncounted.
   - *Rules out* checking only at queue time, which races the same way.
4. **Sources follow the photo.**
   - `place` records the source, and `remove` drops it.
   - `clear` empties the sources, so the adopt path's refresh repopulates a
     clean set.
   - `replace` gives the replacement the old photo's source. A re-import
     therefore does not bring back an original the owner replaced.
   - A photo the owner deleted is downloaded again if its step is imported
     again.
   - *Rules out* tombstones for deleted photos. The import screen already
     blocks re-selecting imported steps, so tombstones would cost more than
     they save.
5. **The cleanup of existing duplicates is an owner-run script, for Polarsteps
   memories only** (owner, 2026-10-10).
   - It looks at memories with `polarsteps_step_id IS NOT NULL`. Photos whose
     full-resolution files have the same sha256 are duplicates.
   - The first in `photos_json` order is kept. The rest are deleted, with
     storage accounting and share-copy removal.
   - It is dry-run by default. `--apply` requires `--api-stopped`, as
     `scripts/backfill_thumbnail_orientation.py` does, because it writes usage
     counters the live API also writes.
   - *Rules out* a migration: the cleanup needs file I/O, and the precedent
     keeps file repairs in `scripts/`.
   - *Rules out* deduping manual memories, where a user may have uploaded the
     same photo twice on purpose.

## Review envelope

REVIEW.md §2 defaults apply, with these additions:

- **Concurrency on memory photos.** Many `from-url` background downloads for
  one memory run at once, alongside manual uploads, replaces and deletes. All
  of them are serialised by the in-process `photo_lock`. There is one API
  process (E1), so the lock is sufficient.
- **The cleanup script runs with the API stopped.** Concurrency with the live
  API is out of scope for it, and its flags refuse it.
- **Old clients (E5).** They keep sending `from-url` unchanged, and get the
  dedup without any change on their side.
- **Scale.** Memories of up to a few hundred photos.

## Boundaries crossed

- **API contract:** none. `from-url` still answers 202. A skipped duplicate is
  not an error.
- **Stored data:**
  - `photo_order_json` gains a `sources` key. `load_state` tolerates its
    absence, which is every existing row. It drops malformed entries one by
    one, as it does for `ranks`.
  - Memories imported before this ships have no sources, so they are not
    protected. The cleanup script handles the duplicates they already have.
- **Schema / migration:** none.
- **Export format:** none. `photo_order_json` is not exported.

## Conventions

- Server tests run in the py314 citest container. Threaded tests use
  file-backed SQLite (memory `feedback_threaded_tests_sqlite`).
- Every user-visible commit carries a `Release-Note:` trailer
  (docs/RELEASING.md).

## Open decisions

- **O1 — the server derives the key (Decision 2), in place of a client-sent
  key.** The owner should confirm during review. It affects U2 only.

## Execution units

### Wave 1

#### U1 — Photo sources in the order state
- **Goal:** `api/photo_order.py` carries a per-photo source key through every
  operation.
- **Scope:** `api/photo_order.py`, `tests/test_photo_order.py`.
- **Context:**
  - Read the module docstring and every function in `api/photo_order.py`.
  - Example to follow: how `ranks` is parsed in `load_state` (each entry is
    validated on its own) and carried by `place`, `remove` and `replace`.
  - Plan Decision 4.
- **Do:**
  1. Add `"sources": {uuid: str}` to the state, and give `_empty` an empty
     `{}` for it.
  2. `load_state` keeps only `str → non-empty str` entries. A malformed
     `sources` value never resets `epoch` or `ranks`.
  3. `dump_state` writes the sources.
  4. `place(photos, state, uuid, rank, source=None)` records the source when
     one is given.
  5. `remove` drops it. `replace` moves the old photo's source to the new
     UUID. `clear` empties it.
  6. Add `has_source(state, photos, key) -> bool`. It is true only when a UUID
     currently in `photos` carries `key`; a stale entry for a removed UUID does
     not count.
  7. Update the docstring.
- **Acceptance:**
  - `pytest tests/test_photo_order.py tests/test_memories_photo_order.py tests/test_journal_photo_order.py`
    passes.
  - New tests cover:
    - the round-trip of sources;
    - a state with no `sources`;
    - malformed `sources` alongside a valid epoch;
    - `place` with a source;
    - `remove` dropping it;
    - `replace` carrying it over;
    - `clear` emptying it;
    - `has_source` ignoring stale entries.
- **Out of scope:** every caller in `api/memories.py` or `api/journal.py`.
- **Latitude:** none.
- **Escalate if:**
  - an existing caller relies on `dump_state` writing exactly two keys;
  - a file outside Scope must change (X3).
- **Depends on:** —

#### U3 — Owner script: remove duplicate photos from Polarsteps memories
- **Goal:** `scripts/dedupe_memory_photos.py` removes byte-identical duplicate
  photos from Polarsteps-imported memories.
- **Scope:**
  - `scripts/dedupe_memory_photos.py` (new);
  - `tests/test_dedupe_memory_photos.py` (new);
  - `docs/RELEASING.md`: a new subsection under "Post-deploy owner actions".
- **Context:**
  - Example to follow, for the CLI, the `--apply`/`--api-stopped` guard, the
    use of the app's DB connection and storage accounting:
    `scripts/backfill_thumbnail_orientation.py` and its test
    `tests/test_backfill_thumbnail_orientation.py`.
  - For hashing the stored originals:
    `scripts/reorder_polarsteps_memory_photos.py:95-147`.
  - For how a photo is deleted: `_delete_photo_files` and `delete_photo` in
    `api/memories.py`.
  - Plan Decision 5.
- **Do:**
  1. Select memories with `polarsteps_step_id IS NOT NULL`. `--project ID`
     (repeatable) narrows the selection.
  2. In each memory, sha256 every photo's full-resolution file. A photo whose
     file is missing is reported and is never a duplicate.
  3. Keep the first photo of each hash, in `photos_json` order. For every
     other photo with that hash:
     - unlink its files with `unlink_and_record`, and call
       `remove_share_copy`;
     - drop it with `photo_order.remove`;
     - write `photos_json` and `photo_order_json`.
  4. Dry-run by default: per memory, print what would be removed and the bytes
     it would free. `--apply` without `--api-stopped` is refused.
  5. At the end, print a summary: memories touched, photos removed, bytes
     freed.
  6. Add the owner-action subsection to `docs/RELEASING.md`:
     - take a DB copy;
     - dry-run;
     - apply with the API stopped;
     - on val first, then prod.
- **Acceptance:**
  - `pytest tests/test_dedupe_memory_photos.py` passes.
  - Its tests cover:
    - duplicates are removed and the first is kept;
    - storage usage drops by the removed files' size;
    - the share copy is removed;
    - non-Polarsteps memories are untouched;
    - a missing file is never a duplicate;
    - a dry run writes nothing;
    - `--apply` without `--api-stopped` exits non-zero;
    - a second run is a no-op.
- **Out of scope:**
  - journal entries;
  - writing `sources` for existing photos (their URLs were never stored);
  - re-downloading from Polarsteps.
- **Latitude:** local design.
- **Escalate if:**
  - usage accounting cannot be reached from a script without starting the
    app;
  - a file outside Scope must change (X3).
- **Depends on:** —

### Wave 2

#### U2 — `from-url` skips a photo whose source the memory already has
- **Goal:** a memory never gets a second copy of a photo from the same source
  URL.
- **Scope:** `api/memories.py` (the from-url path and `_write_memory_photo`),
  `tests/test_memories_photo_order.py`.
- **Context:**
  - `api/memories.py:640-772`.
  - U1's `place(..., source=)` and `has_source`.
  - Example to follow, for dropping a photo at placement and deleting its
    files: the epoch check in `_write_memory_photo`.
  - For concurrent downloads: the existing concurrency tests in
    `tests/test_memories_photo_order.py`.
  - Plan Decisions 1-4.
- **Do:**
  1. Add `photo_source_key(url) -> str`: the lower-cased scheme and host plus
     the path, without query or fragment.
  2. In `_download_photo_from_url`, before the fetch, read the memory's state.
     If `has_source` finds the key, log at info level and return: no fetch and
     no quota check.
  3. Give `_write_memory_photo` a `source=None` parameter. Under the lock:
     - if the source is already present, delete the just-written files the way
       the epoch path does, and return False;
     - otherwise, place the photo with its source.
  4. A manual upload passes no source. A replace passes none either; it keeps
     the old photo's source through U1.
- **Acceptance:**
  - New tests cover:
    - the same URL queued twice into one memory, one after the other → one
      photo;
    - the same URL queued twice concurrently → one photo, with the loser's
      files deleted and uncounted;
    - the same path with a different query string → one photo;
    - two different URLs → two photos, in rank order;
    - after the adopt path's clear, the same URLs download again;
    - deleting a photo and re-queuing its URL → it downloads again;
    - a skipped download makes no HTTP fetch.
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

- Queuing a photo URL that a memory already holds leaves one copy, whether the
  two requests come one after the other or concurrently. Different photos are
  all placed, in rank order.
- Pressing Import twice on the same Polarsteps steps leaves each memory with
  one copy of each photo, on any client build.
- `scripts/dedupe_memory_photos.py` dry-runs and applies as specified.
  `docs/RELEASING.md` lists it as a post-deploy owner action.
- The full pytest suite passes.
