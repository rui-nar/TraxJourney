# Trip ZIP import — Plan for #469 (and #484)

## Problem

A `.traxj` file lists each memory's and journal entry's photos by name, but
does not contain the photo files. On import (create, Keep both, and new
memories on Replace), those names are stored as they are. The importer's trip
then refers to photos that don't exist: the app shows broken images, and
everything that walks the photo lists (export, share tiles, storage
accounting) meets missing files. This was already true for memories before
#452; since #452 journal entries are imported too, with the same flaw.

The ZIP export already contains the memory photos, but:

- it cannot be imported;
- its trip file is a cut-down document: no people, groups, day notes,
  sleeping options or settings;
- it leaves out journal photos;
- it streams in newline-split chunks (#484), because the ZIP is built in a
  `BytesIO` and handed to `StreamingResponse`, which iterates it line by line.

Users moving a trip between accounts, or restoring one, lose every photo.

## Current state

- **ZIP export:** `GET /api/projects/{name}/export-zip`, `export_project_zip`
  (`api/project_transfer.py:448-514`).
  - The trip file holds only `version`, `name`, `trip_start`, `filter_state`,
    `items` and `activities`. `photo_refs` are added to memories (473-476).
  - Photos go in as `photos/{memory.id}/{uuid}.jpg`, read from the trip
    owner's folder (500-507); a missing file is skipped.
  - Neither journal photos nor avatars are included.
  - The whole ZIP is built in memory and streamed by lines. `.traxj` (439)
    and GPX (408) are streamed the same way.
- **`.traxj` export:** `export_project_traxj` (417-443) =
  `ProjectIO.to_dict` + `to_strava_dict` activities. This is the full format.
- **Import:** `POST /api/projects/import` (167-263).
  - `_CappedUploadRoute` enforces `MAX_IMPORT_BYTES = 50 MB`.
  - Only `.traxj` files are accepted, read wholly into memory.
  - `on_conflict`: absent → 409 `name_conflict`; `copy`; `replace`.
  - Only `ensure_project_quota` is checked, and not on Replace. There is no
    storage check, because "nothing lands on disk".
- **Ingest:** `src/project/repo_transfer.py`.
  - `import_project` (150-179) retries on a name or public_id clash.
  - `replace_project` (210-336) matches memories by `public_id` and the
    owner's journal entries by the exported `id`, and updates them in place.
  - `_write_content` (338-553) creates new rows with
    `photos_json=json.dumps(mem.photos)` (488, 511). The photo files are
    never touched.
  - `_update_memory`/`_update_journal` (579-609) return the dropped names,
    for deletion after commit.
- **Photo files:** `data/users/<uid>/{memories|journal|people}/<id>/<uuid>.jpg`
  plus `<uuid>_thumb.jpg` (`src/utils/photo_paths.py`; `photo_file()`
  enforces containment, #471).
  - Writing a photo means: decode with PIL (422 if invalid), write the raw
    bytes, then write a 400×400 JPEG q85 thumbnail and `record_written`.
  - That is `_save_photo_files`, duplicated in `api/memories.py:507-527` and
    `api/journal.py:159-177`.
  - Memory photos live under the trip **owner**; journal photos under the
    entry's **author**. There is a 25 MB per-photo upload limit
    (`api/memories.py:78`).
- **Pillow bomb limit:** `src/poster/tile_stitcher.py:38` sets
  `Image.MAX_IMAGE_PIXELS = None` for the whole process, so Pillow's
  decompression-bomb check is off for every decode, uploads included.
- **Client:**
  - `ProjectsNotifier.pickProjectFile` (`projects_notifier.dart:118-146`)
    accepts `.traxj` only and reads the whole file into memory.
  - `_uploadBytes` (152-210) handles 409 and 413.
  - The export menu is in `app_screen.dart:338-420`.
- **No ZIP reading exists on the server**, and so no archive safeguards.

## Decisions

1. **A separate endpoint, `POST /api/projects/import-zip`.**
   - It has the same `on_conflict` query and the same 409 `name_conflict`
     body as `/import`, and the same response (`ImportedOut`).
   - Why: the upload cap is enforced by the route class before the body is
     read, so it can't depend on the file type. A separate route keeps
     `.traxj` at 50 MB and gives ZIPs their own cap.
   - Rules out: sniffing the type inside `/import` after the body is read,
     which would mean the larger cap for everything.
2. **The ZIP upload cap is 1 GB (`MAX_ZIP_IMPORT_BYTES`).**
   - The upload is never held in memory. Starlette spools it to a temp file,
     and entries are streamed out of it one at a time.
   - The trip file inside is still parsed in memory, so it keeps
     `MAX_IMPORT_BYTES` (50 MB, uncompressed) as its own limit. The memory
     budget in the `MAX_IMPORT_BYTES` comment is unchanged.
   - (Owner decision, 2026-09-28.)
3. **Storage quota: refuse the whole import.**
   - Before any row is written, the bytes the photos will occupy (full files
     plus generated thumbnails, as staged) are checked with
     `ensure_storage_quota` against the user the photos will belong to.
     Over the limit → the usual `QuotaExceeded` 402, and nothing is created.
   - The check is made for Replace too.
   - Photos already on disk for a kept memory or journal entry (Replace) are
     not counted again (R1-6).
     - Before ingest, `already_present(sess, owner, name, project)` returns
       the uuids that are already in place. It uses Replace's matching:
       memories by `public_id`, the owner's journal entries by id.
     - Those uuids' staged bytes are subtracted from the total. The same set
       is what placement skips (Decision 8).
   - (Owner decision, 2026-09-28.)
4. **The ZIP's trip file is the full `.traxj` document.** It is what
   `export_project_traxj` writes, plus `photo_refs` on memories and journal
   entries. `ProjectIO.from_dict` already reads it, and a user who unzips the
   export gets a normal `.traxj`.
5. **The ZIP export adds the exporter's journal photos** under
   `journal/{entry.id}/{uuid}.jpg`, with `photo_refs`. Only the caller's own
   entries, which is what the export already contains.
   - Avatars are not added (owner decision, 2026-09-28). Import keeps today's
     avatar handling: new people get none; on Replace a kept person keeps an
     avatar whose file already exists.
   - Memory photos keep `photos/{memory.id}/{uuid}.jpg`, so ZIPs exported
     before this change stay importable.
6. **A photo is identified by where the file says it is, never by the name
   of an entry the archive happens to contain.**
   - For each memory and journal entry, the importer looks for exactly
     `photos/{file id}/{uuid}.jpg` (memories) or `journal/{file id}/{uuid}.jpg`
     (journal), where `file id` is the item's `id` in the trip file and
     `uuid` is one of its `photos`.
   - `photo_refs` are informational and never used as paths.
   - Every other archive entry is ignored and never extracted. Entry names
     are matched against these patterns as strings, so no archive path is
     ever joined to a filesystem path.
7. **Every imported photo goes through the same processing as an upload.**
   - That is: the 25 MB per-photo limit, a PIL decode (an invalid image
     refuses the import with 400 naming the entry), the raw file written as
     is, and a regenerated thumbnail. Thumbnails in the archive, if any, are
     ignored.
   - The duplicated `_save_photo_files` moves into one shared helper in
     `src/utils/photo_store.py`, used by memories, journal and the import.
   - The helper also enforces an explicit pixel limit before decoding,
     because the process-wide Pillow limit is off.
   - Rules out: trusting archive thumbnails, or writing files without a
     decode.
8. **Staging, then ingest, then placement after commit.** (R1-1, R1-2,
   R1-10)
   1. **Staging.** All photos are processed into a per-request staging
      directory, `<data_dir>/tmp/import-<uuid4>/`.
      - It is on the same volume as `users/`, so placement is a rename. It is
        outside `users/`, so it is never counted as anyone's storage.
      - Invalid images, the name conflict and quotas are all checked before
        any row is written.
   2. **Ingest.** The DB ingest runs with its existing commit and retry loop,
      and touches no photo file.
      - Each attempt computes the stored names (Decision 9) and a
        **placement list**: `(kind, row id, uuid, staged paths)` for every
        staged photo that the committed rows will reference.
      - Only the attempt that commits returns its list. Nothing is moved
        during an attempt, so a retry finds every staged file where it was.
   3. **Placement after commit.** The staged files are renamed into
      `photo_folder(data_dir, importer, kind, row id)` via `photo_file()`,
      and `record_written` runs for them.
      - **A photo already in place is skipped.** On Replace, a kept row's uuid
        whose file already exists in its folder is not in the placement list.
        It is not overwritten, not counted again, and never removed.
      - If a rename fails after commit (a disk error), that uuid is removed
        from its row's `photos_json` in a follow-up update and logged at
        ERROR, so no name is left dangling.
   4. **A failed import writes no file.** An import that fails before commit
      has moved nothing.
   5. **Cleanup.** The staging directory is always removed. Staging
      directories older than a day, left by a crash, are removed at the start
      of the next import.
   - This keeps #434's rule: a failed import leaves no file behind.
9. **Stored photo names come from the trip file's list, filtered, never from a
   folder listing.** (R1-3)
   - For each memory or journal entry, `photos_json` is the file's own
     `photos` list, in order, keeping a uuid only if:
     - it is staged for that item (ZIP), or
     - the row is kept on Replace and the uuid's file already exists in that
       row's folder.
   - Names the file no longer lists are not in its list, so they stay
     dropped, and `_update_memory`/`_update_journal` still return them for
     deletion.
   - For a `.traxj`, a new row therefore stores no names. A kept row keeps
     the names that are on disk.
   - The same rule applies to a ZIP: a name whose photo is missing from the
     archive is dropped, not stored dangling.
10. **Photo owner.** Imported memory photos go under the importer's folder,
    since the importer owns the new trip. Journal photos also go under the
    importer's folder, because an imported entry's author is the importer
    (#452).
11. **Exports stream from a spooled temp file in 64 KiB chunks (#484).**
    - The ZIP is written to a `SpooledTemporaryFile` (in memory up to a
      small threshold, then on disk) and streamed with a chunked iterator.
    - `.traxj` and GPX responses are bytes already built in memory; they are
      sent as a plain `Response`/`bytes` body instead of line iteration.

12. **One import at a time.** (Owner decision, 2026-09-28, round-1 envelope
    question.)
    - `/import` and `/import-zip` share one process-wide non-blocking
      `threading.BoundedSemaphore(1)`. It is shaped like `_thumb_semaphore`
      in `api/memories.py`, and the API runs as a single uvicorn process.
    - A request that finds it taken gets 503 "Another trip import is in
      progress. Try again in a minute." at once, before its trip file or
      archive is read.
    - The memory budget is then one import (a trip file of at most 50 MB plus
      one photo decode at a time) beside the running process.
    - Rules out: queueing imports, which would hold uploads of up to 1 GB
      open while they wait.

## Review envelope

What this plan adds to REVIEW.md §2's defaults:

- **New trust boundary: an untrusted archive of up to 1 GB.** Any
  authenticated user can upload one, including a crafted one. In scope:
  - zip-slip and absolute or odd entry names;
  - duplicate entry names;
  - entry-count and uncompressed-size bombs, and a compression ratio that
    lies about sizes;
  - a truncated or corrupt central directory;
  - ZIP64;
  - decompression-bomb images (Pillow's own check is disabled
    process-wide);
  - non-image files named `.jpg`;
  - a trip file over 50 MB uncompressed.
- **Resources.**
  - The API container has a 768 MB memory limit (`docker-compose.yml.example`);
    peak memory must stay bounded regardless of archive size.
  - Disk use of up to about 2× the upload per request: the spooled upload
    plus the staged photos, the latter on the data volume.
  - One import runs at a time, `.traxj` or ZIP (Decision 12). Other requests
    (uploads, map loads) run beside it, so its peak must leave room for
    them.
- **Concurrency.** A trip being edited while it is replaced is covered by the
  existing `lock_version` bump.
- **E2EE.** Photos are not encrypted (`docs/ENCRYPTION.md`). Text envelopes in
  the trip file round-trip unchanged, as for `.traxj` (#466).

## Boundaries crossed

- **Export format:** the ZIP's trip file grows from the cut-down document to
  the full `.traxj`.
  - It gains `journal/{id}/{uuid}.jpg` entries and `photo_refs` on journal
    entries.
  - No consumer reads the old ZIP besides humans; the ZIP was not importable.
  - Old ZIPs stay importable: missing keys already take defaults in
    `from_dict`, and `photos/{id}/…` is unchanged.
- **API:**
  - The new endpoint `POST /api/projects/import-zip`.
  - `/import` keeps its request and response shapes. It can now also answer
    503 while another import runs (Decision 12). Old clients show that as a
    generic error; U5 gives it a readable message. Stored photo names are
    filtered (Decision 9).
  - The export endpoints keep their URLs, media types and headers.
- **Stored data:**
  - New imports no longer store dangling photo names.
  - Trips imported earlier keep theirs. A clean-up of existing dangling
    names is out of scope; see Open decisions.
- **Schema:** none. No migration.

## Conventions

- **No archive path ever reaches the filesystem.** Files are written only
  under `photo_folder(...)` via `photo_file()`, from uuids validated by
  `is_photo_name`.
- **Errors the uploader caused** (bad archive, bad image, missing trip file,
  too large) are 400 or 413 with a readable `detail`. Anything else stays a
  500 (#451).
- **Size limits are module constants with a comment giving the reason**, like
  `MAX_IMPORT_BYTES`.

## Open decisions

- **Limits.** Entry-count limit (proposed 20,000) and pixel limit per photo
  (proposed 100 megapixels, above any phone camera). Decided at delivery,
  when U3 is routed; both are constants that are easy to change later.
- **Pillow limit for uploads.** Decided (owner, 2026-09-28): the explicit
  pixel limit in the shared helper applies to ordinary uploads too
  (Decision 7). Uploads above it get the existing 422.
- **Existing trips with dangling photo names from earlier imports.** A
  one-off clean-up could be a follow-up issue. Not decided here.

## Execution units

### Wave 1 — disjoint files

**U1 — Full-format ZIP export with journal photos, streamed (#484)**
- **Goal:** the ZIP export carries the full `.traxj` document plus memory and
  journal photos, and every export streams in fixed-size chunks.
- **Scope:** `api/project_transfer.py` (the export functions only:
  `export_project_zip`, `export_project_traxj`, `export_project_gpx` and
  their helpers); `tests/test_export_zip_full_format.py` (new);
  `tests/test_export_streaming.py` (new); and existing export tests whose
  assertions encode the cut-down document:
  `tests/test_import_historical_exports.py:164-172`,
  `tests/test_export_compact.py`, `tests/test_project_file_contract.py`.
- **Context:** `export_project_traxj` (417-443) is the document to reuse.
  `export_project_zip` (448-514) is the one to change. Read `photo_folder` and
  `photo_file` in `src/utils/photo_paths.py`.
- **Do:**
  1. Build the ZIP's trip document exactly as `export_project_traxj` does,
     plus `photo_refs` on memories (unchanged paths) and journal entries
     (`journal/{id}/{uuid}.jpg`).
  2. Add journal photo files from the caller's own journal folder.
  3. Write the ZIP to a `SpooledTemporaryFile` and stream it in 64 KiB
     chunks.
  4. Send `.traxj` and GPX as complete bodies, not line iteration.
- **Acceptance:**
  - A new test round-trips a trip with people, groups, day notes, sleeping
    options, settings, a memory with photos and a journal entry with photos
    through the ZIP export. The trip file inside equals the `.traxj` export
    apart from `photo_refs`, and every referenced photo is in the archive.
  - A streaming test shows that no chunk of a ZIP export with a photo
    exceeds 64 KiB, and that `.traxj` is not split on newlines.
  - The pytest CI command passes.
- **Out of scope:** the import side; avatars in the ZIP; changing the photo
  path layout for memories.
- **Latitude:** local design.
- **Escalate if:** a companion's export would include files from a folder
  that isn't the caller's or the owner's; needing a file outside Scope (X3).
- **Depends on:** —

**U2 — Ingest stores only photo names that have a file, and places staged
photos**
- **Goal:** `import_project`/`replace_project` accept optional staged photos,
  store names per Decision 9, and return a placement list from the attempt
  that commits. A separate function places the files after commit
  (Decision 8).
- **Scope:** `src/project/repo_transfer.py`; `src/project/photo_placement.py`
  (new: placement after commit, and `already_present`);
  `tests/test_import_photo_names.py` (new); `tests/test_import_replace.py`
  (fixture only, R1-7).
- **Context:** `_write_content` (338-553), `replace_project` (210-336), the
  retry loop in `import_project` (150-179), `_update_memory`/`_update_journal`
  (579-609), `PhotoRemoval`, `src/utils/photo_paths.py`, and
  `record_written` in `src/billing/usage.py`. Follow the `PhotoRemoval`
  pattern: the function returns what the caller must do after commit.
- **Do:**
  1. Add a `StagedPhotos` input: a mapping
     `(kind, file item id) → {uuid: StagedPhoto(full, thumb, bytes)}`.
  2. In `_write_content`, compute each memory's and journal entry's
     `photos_json` by Decision 9: the file's list, filtered, never a folder
     listing.
     - Add to the attempt's placement list each staged uuid the row will
       reference, unless the row is kept and that uuid's file already exists
       in its folder.
     - Move or write no file.
  3. Return the placement list only from the attempt that commits.
     - `import_project`'s retry recomputes the list on each attempt from the
       untouched staging directory.
     - The existing return values (name, removals) are kept, so the list is
       added beside them.
  4. `place_photos(data_dir, importer, placements)`: rename each staged file
     into `photo_folder(...)` via `photo_file()`, then `record_written`.
     - On a rename failure, remove that uuid from the row's `photos_json` in
       a follow-up commit and log at ERROR.
  5. `already_present(sess, owner, name, project)`: the set of `(kind, file
     item id, uuid)` whose file is already in the matching kept row's folder.
     It uses Replace's matching: memories by `public_id`, the owner's journal
     entries by id. It returns the empty set when the trip doesn't exist.
     U4 uses it for the quota (Decision 3).
  6. Fix the alps fixture in `tests/test_import_replace.py`: write the photo
     files before the import, so the stored name is real. Keep every
     assertion (R1-7).
- **Acceptance:**
  - `.traxj` imports store no dangling name (memory and journal; create,
    copy and replace).
  - Replace keeps the names that exist on disk. It does not re-store names
    the file dropped: a test asserts `photos_json` after a Replace with fewer
    photos, not only the deleted file.
  - Replace does not put an already-present uuid in the placement list.
  - A forced retry (an IntegrityError on the first commit) still returns
    every staged photo in the final placement list, and no file moved during
    the failed attempt.
  - `place_photos` puts files under the new row id and records usage. A
    failed rename leaves the row without that name.
  - `already_present` matches Replace's matching.
  - The existing `tests/test_import_*.py` pass with their assertions
    unchanged.
- **Out of scope:** reading archives; the quota check itself; HTTP.
- **Latitude:** local design.
- **Escalate if:** a companion's journal entries on Replace would need
  photos moved; any change to how rows are matched on Replace; changing an
  existing test assertion; X3.
- **Depends on:** —

**U3 — Shared photo processing and a safe ZIP stager**
- **Goal:**
  - one `save_photo_files` helper in `src/utils/photo_store.py`, used by
    memories and journal uploads;
  - a new `src/project/zip_import.py` that reads an uploaded ZIP safely
    into a trip plus staged photos in a temp dir.
- **Scope:** `src/utils/photo_store.py` (new); `src/project/zip_import.py`
  (new); `api/memories.py` and `api/journal.py` (only to call the shared
  helper); `tests/test_zip_import_reader.py` (new);
  `tests/test_photo_store.py` (new).
- **Context:** `_save_photo_files` in `api/memories.py:507-527` and
  `api/journal.py:159-177`; `ProjectIO.from_bytes`; `is_photo_name` in
  `src/utils/photo_paths.py`; the size-limit comment on `MAX_IMPORT_BYTES`.
- **Do:**
  1. Move the helper to `photo_store.py`, with the same behaviour plus an
     explicit pixel limit checked on the header before a full decode.
     Uploads keep their 422 for an invalid image.
  2. `read_trip_zip(fileobj, staging_dir)`:
     - **Before constructing `ZipFile`**, read the end-of-central-directory
       record, ZIP64 included. Refuse when its entry total is above the
       entry-count limit, or its central-directory size is above a
       constant sized for that many entries (R1-4). `ZipFile()` loads the
       whole directory into memory before any count check could run.
     - Open with `zipfile`, and check the entry count again against the real
       list.
     - Find exactly one root-level `*.traxj`, at most `MAX_IMPORT_BYTES`
       uncompressed, reading with a byte counter rather than trusting
       `file_size`.
     - Parse it with `ProjectIO.from_bytes`.
     - For each memory and journal item's `(id, photos)`, look up the exact
       entry names in Decision 6. Read each with a 25 MB counted limit,
       decode, and write the full file and thumbnail into `staging_dir`.
       One photo is decoded at a time.
     - Return the `Project` and the `StagedPhotos` mapping, with each photo's
       bytes (full plus thumbnail), so the caller can total them minus
       `already_present`.
     - Every uploader fault raises one `InvalidTripArchive(message)`.
- **Acceptance:** tests for:
  - a valid export round-trip, generated by U1's code shape (build the
    archive in the test);
  - `../` and absolute entry names, which are ignored and never written;
  - duplicate entry names;
  - too many entries, refused from the end-of-central-directory record
    without constructing `ZipFile` (patch `ZipFile` to fail the test if it
    is called);
  - an oversize central directory;
  - an entry that inflates past its limit;
  - no trip file, or two;
  - an oversize trip file;
  - a non-image `.jpg`;
  - an image over the pixel limit;
  - a corrupt archive.
  Each fault gives `InvalidTripArchive`, with nothing left in `staging_dir`
  on failure. The existing photo upload tests pass unchanged.
- **Out of scope:** DB writes; HTTP; quota.
- **Latitude:** local design.
- **Escalate if:** the pixel limit would change an existing upload test's
  outcome; X3.
- **Depends on:** —

### Wave 2 — endpoint and client (disjoint)

**U4 — `POST /api/projects/import-zip`**
- **Goal:** import a trip ZIP with its photos, under the same name and
  conflict rules as `/import`, all-or-nothing, within the storage quota, one
  import at a time.
- **Scope:** `api/project_transfer.py` (the import section: a new route, a
  capped route class parameterised by limit, the shared import guard,
  constants); `tests/test_import_zip.py` (new); `tests/test_import_guard.py`
  (new).
- **Context:** `import_project` (167-263) and `_CappedUploadRoute` (99-132)
  are the model to follow. `_thumb_semaphore` in `api/memories.py` is the
  example for the guard. Use U2's and U3's APIs, `ensure_storage_quota`
  (`src/billing/entitlements.py:169`), and `project_shared._DATA_DIR`.
- **Do:**
  1. Parameterise the capped route by limit, with 1 GB for ZIPs.
  2. Accept `.zip` only.
  3. **The guard (Decision 12):** a module-level
     `threading.BoundedSemaphore(1)`, taken non-blocking at the start of both
     `/import` and `/import-zip`. When it is taken, answer 503 with a
     readable `detail`. Release it in `finally`.
  4. Name from the file name, as `/import` does. **Before reading the
     archive (R1-9):** the name conflict (409) and `ensure_project_quota`
     (not on Replace).
  5. Remove stale staging directories (over a day old) under
     `<data_dir>/tmp/`. Create this request's staging directory there, never
     in the system temp dir (R1-10).
  6. Run `read_trip_zip` in a threadpool on the spooled file, never
     `file.read()`.
  7. `ensure_storage_quota(importer, staged bytes − already_present bytes)`,
     also on Replace (Decision 3, R1-6).
  8. Ingest with the staged photos, then `place_photos` after the commit
     (Decision 8).
  9. Always remove the staging directory.
  10. Queue the same refreshes and cache bust as `/import`.
- **Acceptance:** tests for:
  - create, copy and replace with photos, where the files exist, thumbnails
    are regenerated and storage usage increases by the placed bytes only;
  - Replace with the trip's own ZIP: existing photo files are untouched
    (same inode or mtime), usage is unchanged, and a user at their storage
    limit is not refused;
  - a 409 without `on_conflict`, returned without reading the archive
    (patch `read_trip_zip` to fail the test if called);
  - a 402 over the storage quota, with no trip, no rows and no files left;
  - a 400 on a bad archive or image;
  - a 413 over the cap, declared and chunked;
  - a failed commit leaves no photo file anywhere and no staging directory;
  - a second import, `.traxj` or ZIP, while one holds the guard gets 503, and
    the guard is released after success and after every failure;
  - the staging directory is under `<data_dir>/tmp/`;
  - a 1 GB-cap test does not allocate 1 GB (use a patched limit).
  The pytest CI command passes.
- **Out of scope:** the client; changes to `/import` other than the guard.
- **Latitude:** local design.
- **Escalate if:** peak memory can't be kept independent of archive size; X3.
- **Depends on:** U1 (same file, so a later wave), U2, U3.

**U5 — Client: import a ZIP**
- **Goal:** the import picker accepts `.traxj` and `.zip`, and sends a ZIP to
  `/import-zip` with the same Keep both / Replace flow and readable 402, 413,
  400 and 503 messages.
- **Scope:** `flutter_client/lib/src/projects/projects_notifier.dart`,
  `flutter_client/lib/src/projects/project_file.dart`,
  `flutter_client/lib/src/projects/projects_screen.dart` (only if the picker
  text or flow needs it); `flutter_client/test/projects/project_import_zip_test.dart`
  (new).
- **Context:** `pickProjectFile` and `_uploadBytes`
  (`projects_notifier.dart:118-243`); the existing tests
  `project_import_conflict_test.dart` and `project_import_too_large_test.dart`
  are the examples to follow.
- **Do:**
  1. Allow both extensions.
  2. Route by extension.
  3. Keep the multipart filename's extension.
  4. Show the server's 402 message (storage), a 413 message that names the
     right cap, and the 503 "another import is in progress" message, for
     both file types.
  5. The name prompt strips either extension.
- **Acceptance:**
  - widget or unit tests: ZIP goes to `/import-zip`, `.traxj` to `/import`;
    the 409 dialog works for ZIP; the 402, 413 and 503 messages show;
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** export UI changes; progress bars; reading the ZIP on the
  client.
- **Latitude:** local design.
- **Escalate if:** web cannot upload a large file without reading it wholly
  into memory (report the limit rather than work around it); X3.
- **Depends on:** U4's contract (can run alongside U4: disjoint files).

## Definition of done

- A trip with people, groups, day notes, sleeping options, settings,
  memories with photos and journal entries with photos, exported as a ZIP
  and imported by another account, has all of them, and every photo opens
  (full size and thumbnail).
- No import (`.traxj` or ZIP; create, copy or replace) stores a photo name
  whose file does not exist.
- A ZIP import over the importer's storage quota is refused with 402, and no
  trip, row or file is created.
- A crafted archive (traversal names, bombs, non-images, oversized trip
  file) is refused with 400 or 413 and leaves no file behind.
- Restoring a trip from its own ZIP with Replace leaves its existing photo
  files and storage usage untouched.
- A failed or retried import leaves no photo file behind and loses no staged
  photo.
- Only one import runs at a time; a second gets 503 at once.
- Exports stream in 64 KiB chunks (#484).
- `.traxj` import behaviour and its 50 MB cap are unchanged apart from the
  dangling names.
- The pytest CI command, `flutter analyze` and `flutter test` pass.
