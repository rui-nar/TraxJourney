# Data integrity — Plan for #566 and #551

Package 5. Two server-side bugs that leave bad data in the database, each with
a cleanup of the data already affected, plus a third bug found while
investigating #551 (owner decision, 2026-10-10: in scope).

## Problem

**#566 — duplicate Polarsteps photos.** Since #237, a photo downloaded into a
memory is placed by rank and never overwrites another. A memory that already
has its photos and receives the same step's downloads again therefore ends up
with a second copy of every photo. The owner sees duplicates in the memory and
pays for them in storage.

The issue names the client's automatic 5xx retry (`_postWithRetry`) as the
route. **It is not one on its own:** the client queues photo downloads only
after it gets a memory id (`flutter_client/lib/src/projects/polarsteps_import_notifier.dart:351-375`),
so a retry after a committed-but-502 create queues each photo exactly once.
The routes that do duplicate are:

- **Pressing Import again after a partly failed run.** The screen stays open on
  failure, `importSelected` never removes the steps that succeeded from the
  selection, and `alreadyImportedIds` is refreshed only when a trip is selected
  (`polarsteps_import_notifier.dart:133-139, 245-305`). The second press
  re-posts the succeeded steps; the server returns their existing memories
  (`api/memories.py:391-396`) and every photo is downloaded again.
- **Two clients importing the same step at once** (two devices, two tabs). The
  loser of the unique-index race gets the winner's id (`api/memories.py:438-446`)
  and both queue the photos.

**#551 — a local activity row in two trips.** A split piece (a "tail") is a
local row with a negative id. The split code assumes a local row belongs to one
trip: `delete_local_activity` refuses to delete one referenced elsewhere, and
`_remove_split_descendants` deletes pieces outright. Owner decision
(2026-10-03): a split piece belongs to exactly one trip. Two routes break that:

- `POST /api/projects/{name}/activities` keeps any of the caller's own ids,
  negative ones included (`own_activities_only`,
  `src/project/repo_activities.py:1301-1314`). The app's pickers only send
  positive Strava ids, so no user reaches this today; it is a hole, not a path.
- **Importing your own `.traxj`/ZIP export** as "Keep both" or with "Replace"
  keeps every id the importer already owns (`_as_importers_activities`,
  `src/project/repo_transfer.py:102-150`). The new or replaced trip then names
  the same local rows as the original trip. Users reach this.

**Reset deletes another trip's pieces (found during #551).** A Strava activity
legitimately sits in trips A and B. The owner splits it in A, so its tails are
in A only. Resetting the track from B runs `_remove_split_descendants`
(`src/project/repo_activities.py:439-479`), which unlinks the pieces' items in
B only and then deletes the pieces' rows outright. A keeps items that point at
nothing, and nothing tells anyone. No misuse is needed.

## Current state

- Photos have no table. A memory's photos are `memory.photos_json` (a dense
  list of UUIDs) and `memory.photo_order_json` (`{"epoch", "ranks"}`), managed
  by the pure functions in `api/photo_order.py` under `photo_lock`
  (`api/photo_locks.py`). Journal entries use the same module.
- `POST /api/memories/{id}/photos/from-url` queues `_download_photo_from_url`
  (`api/memories.py:681-705`) as a FastAPI background task; it fetches, checks
  quota, writes files, then `_write_memory_photo` (`:640-678`) places the photo
  under the lock and drops it if the memory's epoch moved. The source URL is
  not stored anywhere.
- Full-resolution originals are stored byte for byte (`src/utils/photo_store.py`),
  so two downloads of one source are byte-identical files.
  `scripts/reorder_polarsteps_memory_photos.py` already relies on this.
- A split family: root (keeps its own id, positive or negative,
  `split_root_id IS NULL`), tails (negative ids, `split_root_id` and
  `split_parent_id` set). `_split_descendants` walks `split_parent_id`.
- Trips reference activities only through `projectitem` rows
  (`project_id`, `item_type='activity'`, `activity_id`). The only other table
  keyed by activity is `activitygeoprepared` (`models/project_db.py:371`).
- New local ids come from `allocate_local_activity_id`
  (`src/project/local_ids.py:65`): a random negative 53-bit id checked against
  the `activity` table.

## Decisions

1. **#566: idempotent per photo source, checked by the server** (owner,
   2026-10-10). Each photo downloaded from a URL records its source key in
   `photo_order_json` as `"sources": {uuid: key}`. A download whose source is
   already in the memory is skipped. *Rules out* the "server returns
   `created: false`, client skips all downloads" option: a step whose create
   committed but failed client-side would be retried into a memory with no
   photos at all.
2. **The server derives the source key from the URL; the client sends nothing
   new.** Key = lower-cased scheme and host plus the path, without query or
   fragment. A query string on a CDN URL is typically a signature or a resize
   parameter, so two fetches of one Polarsteps photo match. *Rules out* a
   client-sent key: it would protect only builds that send it, and installed
   APKs cannot be forced to update (E5). This refines the owner's choice
   ("client sends a source key") without changing what it protects; confirm in
   review.
3. **Checked twice.** Before the fetch (an already-present source costs no
   download and no quota check) and again at placement under `photo_lock`.
   Only the second is authoritative: two concurrent downloads of one source
   both pass the first, and the second to place has its files deleted and
   uncounted. *Rules out* checking at queue time alone, which races the same
   way.
4. **Sources follow the photo.** `place` records it, `remove` drops it, `clear`
   empties it (the adopt path's refresh repopulates a clean set), and `replace`
   gives the replacement the old photo's source, so re-importing a step does not
   bring back an original the owner replaced. A photo the owner deleted is
   downloaded again if its step is imported again. *Rules out* tombstones:
   the import screen already blocks re-selecting imported steps, so this would
   cost more than it saves.
5. **#566 cleanup is an owner-run script, Polarsteps memories only** (owner,
   2026-10-10). In a memory with `polarsteps_step_id IS NOT NULL`, photos whose
   full-resolution files have the same sha256 are duplicates. The first in
   `photos_json` order is kept and the rest are deleted with storage accounting
   and share-copy removal. It is dry-run by default, and `--apply` requires
   `--api-stopped`, as `scripts/backfill_thumbnail_orientation.py` does: it
   writes usage counters the live API also writes. *Rules out* a migration,
   since this needs file I/O and the precedent keeps file repairs in
   `scripts/`. It also rules out deduping manual memories, where a user may
   have uploaded the same photo twice on purpose.
6. **Every local (negative-id) activity row belongs to one trip, not only split
   pieces.** `delete_local_activity` already assumes this of every local row,
   and a plain GPX row shared by two trips becomes a shared split root the
   moment it is split in one of them. *Rules out* checking split columns only,
   which would let that state come back through a later split. A positive
   (Strava) id stays shareable, as `test_an_account_can_add_its_own_activity_to_several_trips`
   pins. **Open decision O1** asks the owner to confirm this widening.
7. **`add_activities` refuses with 409 `local_activity_in_other_trip`**
   rather than dropping silently as it does for other accounts' ids. No shipped
   client sends a negative id, so a refusal can only come from a hand-made
   request, and a clear error beats a quiet `added: 0`. An id that is already
   in *this* trip is not a refusal; `Project.add_activities` skips it as
   today.
8. **Import copies, it does not refuse** (owner, 2026-10-10). An own local id
   the file carries is given a fresh local id when it is not held by the trip
   being written and some trip already references it. That covers "Keep both"
   of your own export, and "Replace" of trip B with trip A's export. The copy
   is a plain local row: `_upsert_activity` never writes split columns on
   insert. Restoring a trip from its own export ("Replace" on the same trip)
   keeps every id, because the trip holds them (`held`). An item that names an
   own local id the file does not carry, and that another trip references, is
   dropped: there is no content to copy. *Rules out* refusing the import, which
   would break "Keep both" for any trip with a split.
9. **Reset refuses with 409 `split_pieces_in_other_trip`** when any piece it
   would delete is referenced by a trip other than the one resetting. The error
   says to reset it from the trip that holds the pieces. It is checked before
   the lock_version bump, so a refused reset writes nothing. The editor shows
   the message, as it does for `nothing_to_restore`. *Rules out* deleting only
   this trip's pieces: the restored full track would then overlap the other
   trip's pieces in that trip.
10. **#551 repair is an Alembic data migration, which copies and does not
    delete.** For every local row referenced by more than one trip, the
    original stays with the trip whose reference is oldest (lowest
    `projectitem.id`, which is where it was made). Every other trip gets its
    own copy: a fresh local id, every column copied, split columns NULL, the
    `activitygeoprepared` row copied too, that trip's items repointed, and its
    `lock_version` bumped so clients refetch. Nobody loses a track. *Rules out*
    a report-only audit, because the bad state keeps destroying data (Decision
    9 and `delete_local_activity` both trip on it). It also rules out deleting
    the extra references, which would lose the second trip's activity.
11. **Items already dangling are reported, not repaired.** Items left pointing
    at deleted pieces by past resets cannot be restored, because the rows are
    gone. The existing `scripts/audit_activity_ownership.py`
    (`find_dangling_activity_refs`) lists them, and running it is an owner
    action.

## Review envelope

REVIEW.md §2 defaults apply, with these additions:

- **Concurrency on memory photos.** Many `from-url` background downloads for
  one memory run at once, plus manual uploads, replaces and deletes, all
  serialised by the in-process `photo_lock`. One API process (E1), so the lock
  is sufficient.
- **The cleanup script runs with the API stopped.** Concurrency with the live
  API is out of scope for it, and refused by its flags.
- **The repair migration runs at startup** (`alembic upgrade head` in the
  lifespan), before any request is served.
- **Old clients (E5).** They keep sending `from-url` unchanged and get the
  dedup for free. An old editor shows a refused reset as its generic
  "Reset failed: …" snackbar, which is accepted.
- **Scale.** Memories of up to a few hundred photos. Shared local rows are
  expected to be rare or absent, so the migration may be row-by-row.

## Boundaries crossed

- **API contract:**
  - Two new 409 codes: `local_activity_in_other_trip` on
    `POST …/activities`, and `split_pieces_in_other_trip` on
    `POST …/activities/{id}/reset`. No shipped client sends the request that
    gets the first one.
  - The import endpoints copy activities that they used to share. The response
    shape is unchanged.
  - `from-url` still answers 202. A skipped duplicate is not an error.
- **Stored data:**
  - `photo_order_json` gains a `sources` key. `load_state` tolerates its
    absence (every existing row) and drops malformed entries one by one, as it
    does for `ranks`. No schema change.
  - Memories imported before this ships have no sources, so they are not
    protected. The cleanup script handles the duplicates they already have.
- **Schema:** none. **Migration:** one data-only migration (Decision 10). Its
  downgrade is a no-op: the copies are valid data.
- **Export format:** none. `photo_order_json` is not exported.

## Conventions

- Server tests run in the py314 citest container, and threaded tests use
  file-backed SQLite (memory `feedback_threaded_tests_sqlite`).
- After any rebase, `alembic heads` must show one head (memory
  `feedback_parallel_migration_heads`). Tests must not pin their own head.
- Error bodies follow `_conflict(code, message, **extra)` in
  `api/activities.py`.
- Every user-visible commit carries a `Release-Note:` trailer, and the
  migration's commit carries one too (docs/RELEASING.md).

## Open decisions

- **O1 — the scope of "belongs to one trip" (Decision 6).** All local rows
  (the default) or split-family rows only. This affects U4, U5 and U8, and
  must be decided before delivery starts.
- **O2 — a split of a shared Strava root still changes the root row for every
  trip.** Trip B sees the cut-down head and the "(1/N)" name. This is the
  design issue underneath the reset bug, and fixing it needs per-trip copies of
  a root. It is out of scope here; file it as a follow-up issue when this
  package merges.

## Execution units

### Wave 1

#### U1 — Photo sources in the order state
- **Goal:** `api/photo_order.py` carries a per-photo source key through every
  operation.
- **Scope:** `api/photo_order.py`, `tests/test_photo_order.py`.
- **Context:**
  - Read the module docstring and every function in `api/photo_order.py`.
  - Example to follow: how `ranks` is parsed in `load_state` (each entry
    validated on its own) and carried by `place`, `remove` and `replace`.
  - Plan Decision 4.
- **Do:**
  1. Add `"sources": {uuid: str}` to the state. `_empty` gets `{}`.
     `load_state` keeps only `str → non-empty str` entries, and a malformed
     `sources` value never resets `epoch` or `ranks`. `dump_state` writes it.
  2. `place(photos, state, uuid, rank, source=None)` records the source when
     one is given.
  3. `remove` drops it. `replace` moves the old photo's source to the new
     UUID. `clear` empties it.
  4. Add `has_source(state, photos, key) -> bool`. It is true only when a UUID
     currently in `photos` carries `key`; a stale entry for a removed UUID does
     not count.
  5. Update the docstring.
- **Acceptance:** `pytest tests/test_photo_order.py tests/test_memories_photo_order.py tests/test_journal_photo_order.py`
  passes, with new tests for each of:
  - the round-trip of sources;
  - a state without `sources`;
  - malformed `sources` alongside a valid epoch;
  - `place` with a source;
  - `remove` dropping the source;
  - `replace` carrying it over;
  - `clear` emptying it;
  - `has_source` ignoring stale entries.
- **Out of scope:** any caller in `api/memories.py` or `api/journal.py`.
- **Latitude:** none.
- **Escalate if:** an existing caller relies on `dump_state` writing exactly
  two keys, or a file outside Scope must change (X3).
- **Depends on:** —

#### U3 — Owner script: remove duplicate photos from Polarsteps memories
- **Goal:** `scripts/dedupe_memory_photos.py` removes byte-identical duplicate
  photos from Polarsteps-imported memories.
- **Scope:**
  - `scripts/dedupe_memory_photos.py` (new).
  - `tests/test_dedupe_memory_photos.py` (new).
  - `docs/RELEASING.md`: a new subsection under "Post-deploy owner actions".
- **Context:**
  - Example to follow, for the CLI, the `--apply`/`--api-stopped` guard, the
    use of the app's DB connection and storage accounting:
    `scripts/backfill_thumbnail_orientation.py` and its test
    `tests/test_backfill_thumbnail_orientation.py`.
  - For hashing the stored originals: `scripts/reorder_polarsteps_memory_photos.py:95-147`.
  - For how a photo is deleted: `api/memories.py` `_delete_photo_files` and
    `delete_photo`.
  - Plan Decision 5.
- **Do:**
  1. Select memories with `polarsteps_step_id IS NOT NULL`. Optionally narrow
     with `--project ID` (repeatable).
  2. In each memory, sha256 every photo's full-resolution file. A photo whose
     file is missing is reported and never treated as a duplicate.
  3. Keep the first photo of each hash in `photos_json` order. For each other
     photo:
     - unlink its files with `unlink_and_record` and call `remove_share_copy`;
     - drop it with `photo_order.remove`;
     - write `photos_json` and `photo_order_json`;
     - bump nothing else.
  4. Dry-run by default: print, per memory, what would be removed and the bytes
     it would free. `--apply` without `--api-stopped` is refused.
  5. Print a summary at the end: memories touched, photos removed, bytes freed.
  6. Add the owner-action subsection: take a DB copy, dry-run, apply with the
     API stopped, on val first then prod.
- **Acceptance:** `pytest tests/test_dedupe_memory_photos.py` passes. It must
  cover:
  - duplicates are removed and the first is kept;
  - storage usage drops by the removed files' size;
  - the share copy is removed;
  - non-Polarsteps memories are untouched;
  - a missing file is never a duplicate;
  - a dry run writes nothing;
  - `--apply` without `--api-stopped` exits non-zero;
  - a second run is a no-op.
- **Out of scope:**
  - Journal entries.
  - Writing `sources` for existing photos: the URLs are not stored.
  - Re-downloading from Polarsteps.
- **Latitude:** local design.
- **Escalate if:** usage accounting cannot be reached from a script without
  starting the app, or a file outside Scope must change (X3).
- **Depends on:** —

#### U4 — `add_activities` refuses a local row another trip holds
- **Goal:** `POST /api/projects/{name}/activities` answers 409
  `local_activity_in_other_trip` for an own negative id that another trip
  references, and a shared helper answers "which trips reference these
  activities".
- **Scope:**
  - `src/project/repo_activities.py`: a new helper, plus whatever
    `own_activities_only` needs.
  - `api/activities.py`: the `add_activities` route only.
  - `tests/test_activity_ownership.py`.
- **Context:**
  - `api/activities.py` `add_activities` (~:396-496).
  - `own_activities_only` (`repo_activities.py:1301-1314`).
  - Example to follow, for the 409 body: `_conflict` and its use for
    `nothing_to_restore` in the reset route (`api/activities.py` ~:1820).
  - Test helpers `env`, `_activity`, `_create_trip` and `_add` in
    `tests/test_activity_ownership.py`.
  - Plan Decisions 6 and 7.
- **Do:**
  1. Add `projects_referencing(sess, activity_ids) -> Dict[int, Set[int]]` to
     the repo: activity id to the set of project ids with an activity item
     naming it. It makes one query.
  2. In `add_activities`, after the project resolves, find the posted ids that
     are negative, owned by the caller and referenced by a project other than
     this one. If any exist, answer 409 `local_activity_in_other_trip` with
     those ids in the body, and write nothing.
  3. Ids already in this trip, and positive ids, behave as today.
- **Acceptance:** new tests in `tests/test_activity_ownership.py` pass:
  - adding a split tail from trip A to trip B → 409, and B is unchanged;
  - adding a plain local (GPX-like) row from A to B → 409;
  - re-posting a local row already in B → 200, nothing added;
  - a positive id in two trips still works (the existing test stays green).

  `pytest tests/test_activity_ownership.py tests/test_activity_split_api.py`
  passes.
- **Out of scope:** the import path (U5), reset (U6) and client changes.
- **Latitude:** none.
- **Escalate if:** O1 is decided as "split rows only", or a file outside Scope
  must change (X3).
- **Depends on:** —

#### U8 — Repair migration: copy local rows that several trips reference
- **Goal:** an Alembic data migration leaves every local activity row
  referenced by exactly one trip, copying it for the others.
- **Scope:**
  - `alembic/versions/<new>_copy_shared_local_activities.py` (new).
  - `tests/test_migration_copy_shared_local_activities.py` (new).
- **Context:**
  - Example to follow, for a data-only migration on these tables and its
    test: `alembic/versions/d5b1c0a2e3f4_prune_orphaned_split_tail_activities.py`
    and `tests/test_migration_prune_orphaned_tails.py`.
  - Id allocation rules: `src/project/local_ids.py`. A migration must not
    import app models, so re-implement the random negative 53-bit id with its
    collision check inline.
  - Tables: `activity`, `activitygeoprepared` (`models/project_db.py:371`),
    `projectitem`, and `project.lock_version`.
  - Plan Decisions 10 and 11.
- **Do:**
  1. `down_revision` = the current single head (`alembic heads`).
  2. Find local rows (`id < 0`) with activity items in more than one project.
  3. For each, keep the original in the project whose referencing
     `projectitem` has the lowest `id`.
  4. For each other project:
     - insert a copy of the `activity` row under a fresh id, copying every
       column read from the table at migration time with
       `split_root_id`/`split_parent_id`/`split_base_name` set NULL;
     - copy its `activitygeoprepared` row if one exists;
     - repoint that project's activity items to the copy;
     - bump that project's `lock_version` by one.
  5. Log how many rows were copied. It is idempotent: a second run is a no-op.
  6. Downgrade is a no-op, with the reason given.
- **Acceptance:** `pytest tests/test_migration_copy_shared_local_activities.py`
  passes. It must cover:
  - a tail in two trips: the oldest reference keeps the original and the other
    gets a copy with identical columns except id and split columns;
  - the geo prepared row is copied;
  - the copying trip's lock_version is bumped;
  - a positive id in two trips is untouched;
  - a local row in one trip is untouched;
  - a second upgrade changes nothing.

  `alembic heads` shows one head.
- **Out of scope:** dangling items (Decision 11), and positive ids.
- **Latitude:** local design.
- **Escalate if:**
  - `projectitem.id` is not monotonic in insertion order on SQLite or Postgres;
  - another table turns out to be keyed by activity id;
  - O1 is decided as "split rows only";
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
  1. Add `photo_source_key(url) -> str`: lower-cased scheme and host plus the
     path, without query or fragment.
  2. In `_download_photo_from_url`, before the fetch, read the memory's state.
     If `has_source` says the key is present, log at info level and return:
     no fetch and no quota check.
  3. `_write_memory_photo` gains `source=None`. Under the lock, if the source
     is present, delete the just-written files the way the epoch path does and
     return False. Otherwise place it with the source.
  4. A manual upload and a replace pass no source (replace carries the old one
     via U1).
- **Acceptance:** new tests pass:
  - the same URL queued twice into one memory, sequentially → one photo;
  - concurrently → one photo, and the loser's files are deleted and uncounted;
  - the same path with a different query string → one photo;
  - two different URLs → two photos, in rank order;
  - after the adopt path's clear, the same URLs download again;
  - deleting a photo and re-queuing its URL → downloaded again;
  - a skipped download makes no HTTP fetch.

  `pytest tests/test_memories_photo_order.py tests/test_polarsteps_dedup.py`
  passes.
- **Out of scope:** the journal `from-url` twin (no client calls it), client
  changes, and the import screen re-posting succeeded steps.
- **Latitude:** none.
- **Escalate if:** a file outside Scope must change (X3).
- **Depends on:** U1.

#### U5 — Import copies own local rows that another trip references
- **Goal:** importing a `.traxj`/ZIP never makes a trip reference a local row
  another trip already references.
- **Scope:** `src/project/repo_transfer.py` (`_as_importers_activities`),
  `tests/test_import_name_conflict.py`, `tests/test_activity_ownership.py`.
- **Context:**
  - `_as_importers_activities` (`repo_transfer.py:102-150`): its existing
    remap of other accounts' ids is the example to follow.
  - U4's `projects_referencing`.
  - Tests to extend: `test_an_import_of_the_importers_own_activity_reuses_it`
    (`tests/test_activity_ownership.py:209`) and the "Keep both" tests in
    `tests/test_import_name_conflict.py`.
  - Plan Decision 8.
- **Do:**
  1. A carried activity with a negative id, owned by the importer, not in
     `held`, and referenced by any project → remap it to a fresh local id, the
     same way as `others`.
  2. An activity item naming such an id that the file does not carry → drop it.
  3. Positive ids and `held` ids behave as today.
- **Acceptance:** new tests pass:
  - exporting trip A with a split, then importing it with "Keep both" → the new
    trip's pieces have different ids from A's, A is unchanged, and the new
    pieces have NULL split columns;
  - "Replace" of trip B with A's export → B gets copies;
  - "Replace" of A with its own export → A keeps its ids;
  - a positive root stays shared.

  `pytest tests/test_activity_ownership.py tests/test_import_name_conflict.py tests/test_replace_import_prunes_strava.py`
  passes.
- **Out of scope:** the content of a positive root the file overwrites
  (existing behaviour), and O2.
- **Latitude:** none.
- **Escalate if:** `held` does not contain exactly the target trip's
  activities on the replace path, or a file outside Scope must change (X3).
- **Depends on:** U4.

#### U6 — Reset refuses when another trip holds the pieces
- **Goal:** `POST …/activities/{id}/reset` answers 409
  `split_pieces_in_other_trip` instead of deleting pieces another trip
  references.
- **Scope:**
  - `src/project/repo_activities.py` (`reset_activity_track`, and a new
    exception beside `NothingToRestore`).
  - `api/activities.py` (the reset route only).
  - `tests/test_activity_split_api.py`.
- **Context:**
  - `reset_activity_track` and `_remove_split_descendants`
    (`repo_activities.py:439-560`).
  - Example to follow: how `NothingToRestore` is raised in the repo and mapped
    with `_conflict` in the route.
  - U4's `projects_referencing`.
  - `_seed_second_project` in `tests/test_activity_split_api.py:335`.
  - Plan Decision 9.
- **Do:**
  1. In `reset_activity_track`, after the existing checks and **before** the
     lock_version bump, compute the descendants. If any is referenced by a
     project other than `project_id`, raise `SplitPiecesHeldElsewhere`.
  2. The route maps it to 409 `split_pieces_in_other_trip` with a message
     telling the user to reset it from the trip that holds the pieces.
- **Acceptance:** new tests pass:
  - a Strava root in trips A and B, split in A, reset from B → 409; A's pieces,
    A's items and both lock_versions are unchanged;
  - reset from A still undoes the split;
  - resetting a piece with no children is unaffected.

  `pytest tests/test_activity_split_api.py tests/test_local_delete_scope.py`
  passes.
- **Out of scope:** O2 (the shared root's geometry), and the client (U7).
- **Latitude:** none.
- **Escalate if:** a file outside Scope must change (X3).
- **Depends on:** U4 (shared files, and the helper).

### Wave 3

#### U7 — The editor explains a refused reset
- **Goal:** the activity editor shows a clear message for 409
  `split_pieces_in_other_trip`.
- **Scope:** `flutter_client/lib/src/projects/activity_editor_page.dart`, and
  its existing reset test file under `flutter_client/test/`. Find it by
  grepping for `nothing_to_restore`.
- **Context:**
  - Example to follow: the `nothing_to_restore` case in
    `activity_editor_page.dart:336-346` and its test.
  - Plan Decision 9.
- **Do:** add the case, with the snackbar "This activity was split in another
  trip. Reset it from that trip."
- **Acceptance:**
  - a widget test proves a 409 with that code shows the message and leaves the
    editor open;
  - `flutter analyze` is clean;
  - the targeted test passes in the Flutter test container.
- **Out of scope:** naming the other trip, and any other error.
- **Latitude:** none.
- **Escalate if:** no existing reset widget test exists to extend, or a file
  outside Scope must change (X3).
- **Depends on:** U6.

## Definition of done

- Queuing a photo URL a memory already holds, sequentially or concurrently,
  leaves one copy. Different photos are all placed in rank order.
- Pressing Import twice on the same Polarsteps steps leaves each memory with
  one copy of each photo.
- `scripts/dedupe_memory_photos.py` dry-runs and applies as specified, and
  `docs/RELEASING.md` lists it as a post-deploy owner action.
- No server path can make a second trip reference a local activity row:
  - `add_activities` refuses with 409;
  - import copies;
  - existing shared rows are copied by the migration.
- Resetting from trip B never deletes pieces trip A references. It answers
  409, and the editor says why.
- The full pytest suite and the full Flutter suite pass, `alembic heads` shows
  one head, and `flutter analyze` is clean.
- Owner actions are listed in the PR: run the dedupe script and
  `scripts/audit_activity_ownership.py` on val, then prod. File O2 as an issue.
