# Immich album photo upgrade — Plan for #33

A project-level action: the user picks the Immich album that holds a trip's
original photos. The server compares every photo of the trip against it,
proposes one original per photo, and replaces the ones the user approves.
The plan also fixes five defects found while designing it (F1–F5, Decision 10).

## Problem

Polarsteps imports thumbnail-quality photos: `large_thumbnail_path`,
[src/api/polarsteps_client.py:182-188](../../src/api/polarsteps_client.py#L182-L188).
Today the only way to upgrade them is the per-memory dialog
([photo_upgrade_screen.dart](../../flutter_client/lib/src/photos/photo_upgrade_screen.dart)),
opened one memory at a time and limited to memories. For a trip of several
hundred photos, that means hundreds of dialogs. The owner keeps the
originals of each trip in one dedicated Immich album and wants to upgrade a
whole trip in one pass, journal entries included.

The design work also found five defects in the code that exists:

- **F1.** `POST /api/immich/search` reads only the first page of Immich's
  results ([api/immich.py:367](../../api/immich.py#L367)). On a busy day the
  per-memory dialog silently misses photos.
- **F2.** The per-memory dialog puts Immich photos on the wrong day for trips
  far from UTC. It buckets Immich's `fileCreatedAt`, a true UTC instant, as
  if it were wall-clock time
  ([photo_upgrade_screen.dart:156-179](../../flutter_client/lib/src/photos/photo_upgrade_screen.dart#L156-L179)).
  A photo taken at 08:00 in Tokyo lands on the previous day.
- **F3.** A replace is checked against the quota by the size of the new file
  alone. The bytes of the file it deletes are not credited
  ([api/memories.py:847](../../api/memories.py#L847),
  [api/journal.py:572](../../api/journal.py#L572)). Near the limit, a
  replacement that fits is refused.
- **F4.** HEIC photos cannot be stored at all, because Pillow cannot decode
  them ([src/utils/photo_store.py:54-90](../../src/utils/photo_store.py#L54-L90)).
  Every stored original is also served as `image/jpeg`, whatever its real
  format: `api/memories.py:895,967`, `api/journal.py:622,644`,
  `api/people.py:461,482`.
- **F5.** A Polarsteps re-import that adopts a memory created before step ids
  existed deletes all of that memory's photos, upgraded ones included
  ([api/memories.py:333-360](../../api/memories.py#L333-L360)).

## Current state

- **Immich proxy:** [api/immich.py](../../api/immich.py). Per-user server URL
  and API key, stored encrypted (`ImmichToken`). Every call goes through the
  guarded fetch in `src/utils/safe_fetch.py` (public addresses only, unless
  the host is listed in `IMMICH_ALLOWED_HOSTS`). The HTTP plumbing
  (`_GuardedHttp`, `_http`) lives in the route module, so a background job
  cannot reuse it. Tests patch `api.immich._http`.
- **Per-memory dialog:** [photo_upgrade_screen.dart](../../flutter_client/lib/src/photos/photo_upgrade_screen.dart)
  with [immich_source.dart](../../flutter_client/lib/src/photos/immich_source.dart)
  and [photo_match.dart](../../flutter_client/lib/src/photos/photo_match.dart).
  It hashes in the app, with a single 64-bit aHash
  ([perceptual_hash.dart](../../flutter_client/lib/src/photos/perceptual_hash.dart)).
  Its thresholds were never calibrated (`kDefaultMaxHammingDistance`, "adjust
  empirically").
- **Where photos live:**
  - memories: `DBMemory.photos_json`, files under the project owner's tree;
  - journal entries: `DBJournalEntry.photos_json`, files under the author's
    tree. An entry is visible to its author only (`_get_owned_journal`,
    [api/journal.py:95](../../api/journal.py#L95));
  - person avatars: `DBPerson.avatar_photo`. Not in scope.
- **Placement state:** [api/photo_order.py](../../api/photo_order.py), with
  `{"epoch", "ranks", "hashes"}`. `replace` gives the old photo's rank and
  sha256 to the new UUID, so a re-import does not bring back a photo the user
  replaced.
- **Replace endpoints:** `PUT /api/memories/{id}/photos/{old}/replace`
  ([api/memories.py:823](../../api/memories.py#L823)) and its journal twin
  ([api/journal.py:549](../../api/journal.py#L549)). The two are near
  duplicates. Both write the new files, swap the UUID under `photo_lock`,
  then delete the old files.
- **Single write path:** [src/utils/photo_store.py](../../src/utils/photo_store.py).
  It stores the raw bytes as they came, writes a 400×400 upright JPEG
  thumbnail, and counts the bytes against storage.
- **Photos are not end-to-end encrypted:**
  [docs/ENCRYPTION.md:319](../../docs/ENCRYPTION.md#L319) records this, and
  Immich bytes already pass through the server.
- **Jobs:** RQ queues in [src/jobs/queue.py](../../src/jobs/queue.py).
  Default timeout 600 s; jobs run inline when there is no broker. The worker
  topology is fixed by `docker-compose.yml.example` and enforced by
  `tests/test_worker_topology.py`. The pattern for job rows and sweeps is
  `DBPosterJob` and `DBVideoJob` with `src/poster/poster_job_runner.py` and
  `src/video/job_runner.py`.
- **Storage limits:** [src/billing/plans.py:102-105](../../src/billing/plans.py#L102-L105).
  Free 500 MB, Tier 1 5 GB, Tier 2 20 GB, Tier 3 50 GB.

## Decisions

**D1 — The scan and the replacements run on the server** (owner decision).
An RQ job reads the album, hashes both sides and stores suggestions. A
second job pulls each approved original from Immich straight into the photo
store. *Rules out* matching in the app, as #59 did. In a browser that is
about 2,300 image decodes on the main thread for a typical trip and album,
and every original would travel Immich → app → server, 2.5 GB for 500
photos. The end-to-end encryption reason for #59's choice does not apply to
photos (Current state).

**D2 — Confident matches are ticked by default** (owner decision). The user
sees the whole grid and confirms with one button. Rows to check are
unticked. *Rules out* confirming each row separately.

**D3 — Who may scan what** (owner decision).
- Memory photos: the project owner only. The replacements spend the owner's
  storage.
- Journal photos: anyone with editor access or more, for their own entries
  only. A NULL-author entry belongs to the owner, as `_get_owned_journal`
  already rules.
- Companions can therefore scan only their own journal entries.
- The existing single-photo replace endpoints keep their editor rule; this
  plan does not change them.
- *Rules out* letting editors bulk-replace memory photos.

**D4 — HEIC becomes JPEG on every ingest path** (owner decision, fixes F4).
- `photo_store` opens HEIC and HEIF through `pillow-heif`. It stores them as
  JPEG at quality 92, upright, keeping EXIF (orientation reset to 1) and the
  ICC profile.
- Because `photo_store` is the single write path, uploads, replacements,
  from-URL downloads, archive imports and avatars all convert.
- RAW originals: the apply job uses Immich's JPEG rendition when one exists
  (OD3). Otherwise the row fails with "unsupported format".
- Stored originals are served with the media type sniffed from their first
  bytes, not a fixed `image/jpeg`.
- *Rules out* storing HEIC as it came: browsers other than Safari cannot
  show it, and the thumbnailer cannot read it.

**D5 — The old file is deleted immediately** (owner decision). This is what
replace does today. The undo is "Choose another": an applied row stays
editable and can be replaced again from Immich, which still holds every
original. *Rules out* a soft-delete window, which would need its own storage
accounting.

**D6 — Each photo records where it came from.** `photo_order_json` gains
`"sources": {uuid: "immich:<asset id>"}`.
- The source is set by the apply job and removed with the photo. Unlike a
  hash, it is not handed to a replacement.
- A scan reports a photo with a source as "already original" and takes its
  asset out of the pool.
- When the photo being replaced has no sha256 yet, the apply hashes the old
  full-resolution file before deleting it and gives that hash to the new
  UUID. A later Polarsteps re-import of the same thumbnail then finds the
  hash and stores nothing.
- *Rules out* a separate provenance table: the order state is already the
  per-photo record every writer carries.

**D7 — How matching works** (pure module, `src/photo_upgrade/matching.py`).
- **Candidates:** album photos whose local capture date (Immich
  `localDateTime`) is within ±1 day of the memory or entry date. This is a
  hard filter. Adjacent days get a small score penalty.
- **Appearance:** 64-bit aHash plus 64-bit dHash, both computed on upright
  images. The score is their summed Hamming distance (0–128).
- **Aspect ratio:** a hard gate. Upright width/height must agree within 3%.
- **Location:** a tie-break only, never a filter. A memory's location is
  often its start of day, and on travel days that is far from where the
  photo was taken. This deliberately differs from the per-memory dialog,
  where location is a filter.
- **Assignment:** one-to-one and global. Pairs are taken in ascending score
  order (ties broken by ids), and each asset is used at most once.
- **Confidence:** "low" when a rival asset scores within the ambiguity
  margin for the same photo, or the asset's second-best photo does.
- **No upgrade:** a matched asset with no more pixels than the current photo
  is reported as "already original", not proposed.
- **Thresholds:** the maximum score and the margin come from the U1 probe on
  the owner's real album (OD2).
- *Rules out:*
  - reusing the Dart aHash alone, which cannot separate burst shots;
  - Hungarian assignment, since greedy matching with a flag on near-ties
    gives the same results on well-separated data and says when it is
    unsure.

**D8 — Scans and suggestions are stored.** Two tables, `photoupgradescan`
and `photoupgradesuggestion` (U6).
- A review can span sessions and devices.
- Each (user, project) has one scan. Starting a new scan deletes the
  previous one, after carrying over its rejected pairs so they are not
  proposed again.
- An hourly sweep deletes scans older than 30 days.
- Suggestion rows hold ids, dates, sizes and dimensions only, never image
  data or text.
- *Rules out* keeping results in the job's return value, which cannot hold
  decisions per row.

**D9 — A new `photos` queue, one at a time, on the poster worker.**
- The queue runs one job at a time and is consumed by `worker-poster` after
  `poster`: `poster photos default resolve`.
- A scan gets a 30-minute job timeout.
- Apply runs in batches of at most 25 photos per RQ job. Each batch enqueues
  the next. Without a broker, one in-process run loops over the batches.
- A poster therefore waits behind at most one batch.
- An API sweep fails any scan or apply still `pending` after 15 minutes,
  with a message that no worker is taking photo jobs. This catches a VPS
  compose file that was not updated.
- *Rules out:*
  - `default`, where minutes-long jobs would delay share tiles and stats;
  - a sixth worker service, given the memory budget of the 4 GB host
    (`docker-compose.yml.example:56-62`).

**D10 — The five defects are fixed in this plan.**
- **F1:** the Immich client follows `nextPage`, up to 10 pages of 1,000
  (U2).
- **F2:** the search response gains `local_taken_at`, taken from Immich's
  `localDateTime`, and the dialog buckets on it (U2, U7).
- **F3:** a replacement is checked against storage by the size of the new
  file minus the size of the file it frees, never below 0 (U8).
- **F4:** see D4 (U4, U8).
- **F5:** an adopt-and-refresh keeps photos that have a source, with their
  ranks, hashes and sources. It clears the rest and bumps the epoch (U3,
  U8).

**D11 — Quota is checked on the incoming bytes before conversion.** A HEIC
upload converts after the check, so its stored JPEG can be larger than what
was checked. The counter records the bytes actually written, and the nightly
reconcile corrects drift. *Rules out* restructuring the five callers of
`photo_store` around a separate prepare step for a gap the counter closes
anyway.

**D12 — Limits.**
- A scan considers at most 10,000 album photos whose dates fall in the trip,
  and at most 5,000 trip photos in scope. Beyond that it fails with a
  message.
- An original over 25 MB (`MAX_PHOTO_BYTES`) fails its row with "too large".
- Videos, trashed assets and archived assets are never candidates.

## Review envelope

REVIEW.md §2 defaults apply, with these additions:

- **SQLite only** (owner decision, 2026-09-28). Postgres-only findings are
  D1.
- **New volume to a user-chosen host.**
  - A scan fetches up to 10,000 small thumbnails from the user's Immich
    server.
  - An apply fetches up to 5,000 originals of up to 25 MB each.
  - All of it goes through the existing guarded fetch: per-request byte and
    time caps, address policy re-checked per request. Immich responses are
    untrusted (E2).
- **Credentials in the worker.** The worker decrypts the runner's
  `ImmichToken`, as the API does. It never logs it.
- **Concurrency.** While a scan or apply runs, the user and companions may
  delete, reorder, replace or re-import the same photos.
  - Every write goes through `photo_lock` and the existing stale checks: a
    photo gone since the scan makes its row `stale`.
  - Each (user, project) has at most one scan and one apply at a time.
- **Memory.** HEIC decode is bounded by `MAX_PHOTO_PIXELS`, as PNG decode
  already is. It runs in the API container (768 MB) for uploads and in
  `worker-poster` (1280 MB) for applies. Each job handles one photo at a
  time.
- **No new end-to-end encryption surface.** Photos are documented as
  unencrypted. Scans read no memory or journal text.

## Boundaries crossed

- **API, all additive.** Old clients are unaffected (E5).
  - `GET /api/immich/albums` is new.
  - `POST /api/immich/search` candidates gain `local_taken_at` (string or
    null). Clients that ignore it keep today's behaviour.
  - New `/api/photo-upgrade/...` and
    `/api/projects/{name}/photo-upgrade/...` routes (U10).
  - Stored originals now carry their real `Content-Type`. Bytes are
    unchanged.
- **Schema.** Two new tables in one Alembic migration on top of the current
  head `e5c3a7b91d24`. After rebasing on main there must still be a single
  head.
- **Stored data.**
  - `photo_order_json` gains a `sources` key. `load_state` reads its absence
    as `{}`, and every writer goes through `dump_state`.
  - HEIC uploads, refused until now, are stored as JPEG.
  - Applies delete the old files, as replace does today.
  - No migration of existing data.
- **Export format.** `photo_order_json` is not exported
  (`api/project_transfer.py`). ZIP exports of upgraded trips are larger
  because the photos are.
- **Infrastructure.**
  - The new `photos` queue changes `worker-poster`'s command.
  - The VPS compose file must be updated by the owner (U13 adds the
    post-deploy action). Until then, D9's sweep fails jobs visibly instead
    of leaving them pending.
- **Dependency.** `pillow-heif`, with its bundled libheif. Add it to
  `pyproject.toml` and `uv.lock`, and check that its licences are compatible
  with AGPL-3 (U4).

## Conventions

- **States.**
  - Scan `status`: `pending | scanning | ready | failed`.
  - Scan `apply_status`: `idle | pending | running | done | stopped`.
  - Suggestion `state`: `proposed | no_match | already_original | rejected |
    applying | applied | failed | stale`.
  - Every state transition is a compare-and-set update in SQL, like `_cas`
    in `src/video/job_runner.py`.
- **Error codes on a failed row:** `too_large`, `unsupported_format`,
  `not_found_in_immich`, `immich_unreachable`, `photo_gone`,
  `storage_full`, `invalid_image`. The app maps each to a sentence.
- **Writes.** Every photo write goes through the replace core in
  `api/photo_replace.py` (U8). No unit writes `photos_json` or
  `photo_order_json` itself.
- **Logging.**
  - Never log the Immich key, the server URL with its credentials, or image
    bytes.
  - Ids and counts are fine.
  - Follow `src/utils/logging.get_logger`.
- **Names.** User-facing copy says "Upgrade photos from Immich" and
  "original". It does not say "asset", "pHash" or "scan job".

## Open decisions

All come out of the U1 probe, which the owner runs against their own album.
They must be settled before U2 and U5 start; U3, U4, U6 and U7 do not wait
for them.

- **OD1 — Immich API shape.**
  - Questions:
    - Does `POST /api/search/metadata` accept `albumIds`, or must album
      photos be listed through `GET /api/albums/{id}`?
    - Its page size and `nextPage` format.
    - Is `localDateTime` present and wall-clock?
    - The formats served by `thumbnail?size=thumbnail`.
    - The minimum Immich version to support.
  - Affects: U2, and U7's fallback.
- **OD2 — Thresholds.** The maximum score, the ambiguity margin and the
  adjacent-day penalty, read off the probe's contact sheet. Affects: U5.
- **OD3 — RAW.** Does `thumbnail?size=fullsize` return a full-resolution
  JPEG for a RAW or HEIC asset on the owner's server? If not, RAW rows fail
  with `unsupported_format`. Affects: U2, U11.
- **OD4 — EXIF on Polarsteps imports.** Do stored Polarsteps photos keep
  `DateTimeOriginal`? If they do, exact capture time is a far stronger
  signal than appearance, and D7 gains it as a plan amendment before U5.
  Affects: U5.
- **OD5 — API key permissions.** Which Immich key permissions the feature
  needs (album read, asset read, view and download). Affects: U2's error
  message, U13's README.

## Execution units

### Wave 0

#### U1 — Immich probe and calibration script
- **Goal:** a read-only script the owner runs on val. It answers OD1–OD5 and
  writes an HTML contact sheet to calibrate D7's thresholds.
- **Scope:**
  - `scripts/immich_upgrade_probe.py` (new)
  - `tests/test_immich_upgrade_probe.py` (new)
- **Context:**
  - Example to follow for the CLI, the app's DB connection and its
    read-only stance: `scripts/audit_photo_names.py`.
  - Immich calls: `api/immich.py` (`_http`, `_headers`, `_require_token`).
  - Decrypting the stored token: `src/auth/credentials_crypto.py`, as
    `ImmichToken` is read in `api/immich.py`.
  - Decisions D7 and D12, and the OD list.
- **Do:**
  1. Arguments: `--project-id`, `--album-id` (omit to list albums), `--out`
     for the HTML file, `--limit-days`. The user is the project's owner.
  2. Report:
     - the Immich version (`GET /api/server/about`);
     - whether `search/metadata` with `albumIds` works, its page size and
       `nextPage` shape;
     - whether `localDateTime` is present, with five sample pairs of
       `fileCreatedAt` and `localDateTime`;
     - the content type and size of `thumbnail?size=thumbnail` and
       `size=fullsize` for one JPEG, one HEIC and one RAW asset if the album
       has them;
     - which calls answer 403 (permissions);
     - for 20 stored Polarsteps photos, whether `DateTimeOriginal` is
       present.
  3. Calibration:
     - For each trip photo in `--limit-days`, compute aHash and dHash on
       the 400 px thumbnail, and the same on the candidates' Immich
       thumbnails within ±1 day.
     - Write the HTML sheet: each photo with its 3 best candidates, showing
       both distances, the aspect ratios, and the day offset.
     - Hash with a local copy of the functions U5 will own. U5 replaces it
       with an import.
  4. Write nothing to the DB or the data directory.
- **Acceptance:**
  - `pytest tests/test_immich_upgrade_probe.py` passes, with Immich faked
    by patching `api.immich._http`.
  - The tests prove:
    - nothing is written to the DB;
    - the report flags a 403;
    - the HTML lists 3 candidates per photo.
  - The owner runs it on val. The answers to OD1–OD5 and the thresholds are
    written into this plan's Open decisions before Wave 1 units U2 and U5
    start.
- **Out of scope:** any change to `api/immich.py`; any matching beyond
  listing the nearest candidates.
- **Latitude:** local design.
- **Escalate if:**
  - decrypting the token needs code outside Scope;
  - a file outside Scope must change (X3).
- **Depends on:** —

### Wave 1

#### U2 — Immich client module, album listing, search fixes (F1, F2 server side)
- **Goal:** the Immich HTTP plumbing moves to `src/immich/client.py`, where
  jobs can use it. Albums can be listed. Search pages through results and
  returns local capture time.
- **Scope:**
  - `src/immich/__init__.py` (new)
  - `src/immich/client.py` (new)
  - `api/immich.py`
  - `tests/test_immich_client.py` (new)
  - `tests/test_immich_api.py`
  - `tests/test_no_real_hosts.py`, only if it lists modules
- **Context:**
  - `api/immich.py` entirely; `src/utils/safe_fetch.py`.
  - Example to follow for a service module used by both routes and jobs:
    `src/api/polarsteps_client.py`.
  - OD1, OD3 and OD5 answers; Decisions D10 and D12.
- **Do:**
  1. Move `_GuardedHttp`, `_Response`, `_coordinate`, the allowed-hosts
     reading and `ImmichAddressNotAllowed` into `src/immich/client.py`.
     Keep `api.immich._http` as a name bound to the client's instance, so
     existing tests that patch it still work.
  2. Add:
     - `ImmichClient(server_url, api_key)`;
     - `list_albums() -> list[Album]`, owned and shared;
     - `iter_album_assets(album_id, taken_after=None, taken_before=None)`.
       It yields `Asset(id, type, local_date_time, file_created_at, lat,
       lon, mime, file_size, original_file_name, is_trashed, is_archived)`
       and follows `nextPage` up to 10 pages of 1,000, using the method OD1
       chose;
     - `search_assets(taken_after, taken_before)` with the same paging;
     - `thumbnail_bytes(asset_id, size)`;
     - `open_original(asset_id)` and `open_fullsize(asset_id)` (OD3), both
       streamed.
  3. Malformed items are dropped one by one, as today.
  4. Errors are typed: `ImmichUnreachable`, `ImmichRejected(status)` with
     403 named, `ImmichAddressNotAllowed`.
  5. `GET /api/immich/albums` returns
     `{albums: [{id, name, asset_count, start_date, end_date, thumb_url,
     shared}]}`. A 403 from Immich becomes a 502 whose message names the
     missing permissions (OD5).
  6. `POST /api/immich/search` uses `search_assets`, so it pages (F1).
     Each candidate gains `local_taken_at` from `localDateTime`, or null
     (F2).
- **Acceptance:**
  - `pytest tests/test_immich_client.py tests/test_immich_api.py
    tests/test_safe_fetch.py tests/test_no_real_hosts.py` passes.
  - New tests cover:
    - paging across 3 pages;
    - the 10-page cap;
    - `local_taken_at` present and absent;
    - album listing;
    - a 403 naming the permissions;
    - trashed and archived assets kept in the iterator output but flagged
      (U9 filters them);
    - the address policy applying to every new call.
- **Out of scope:** the scan job; any Flutter change; caching.
- **Latitude:** local design.
- **Escalate if:**
  - OD1 says neither album-listing method works on the minimum version;
  - a file outside Scope must change (X3).
- **Depends on:** U1 (OD answers).

#### U3 — Sources in the photo order state, and `retain`
- **Goal:** `api/photo_order.py` carries a `sources` map, and gains
  `retain`, which keeps chosen photos' state and clears the rest.
- **Scope:**
  - `api/photo_order.py`
  - `tests/test_photo_order.py`
- **Context:**
  - The module docstring and how `hashes` were added. Example to follow:
    U1 of `docs/plans/POLARSTEPS_PHOTO_DEDUP_PLAN.md`, which did the same
    for hashes.
  - Decisions D6 and D10 (F5).
- **Do:**
  1. The state gets `"sources": {uuid: str}`. `_empty` has it as `{}`.
     `load_state` keeps only `str → "immich:<id>"` entries, where `<id>`
     matches `[A-Za-z0-9-]{1,64}`, read independently of the other fields.
     `dump_state` writes four keys.
  2. Source handling per function:
     - `place(..., source=None)` records a source when one is given;
     - `remove` drops it;
     - `replace(..., source=None)` sets the new UUID's source to the given
       value, or to none. It never carries the old photo's source; ranks
       and hashes are carried as today;
     - `clear` empties it.
  3. `retain(photos, state, keep) -> (photos, state)`:
     - keeps the photos in `keep`, in their order, with their ranks,
       hashes and sources;
     - drops everything else;
     - bumps the epoch.
  4. `source_of(state, photos, uuid) -> Optional[str]` ignores stale
     entries.
  5. Update the docstring.
- **Acceptance:**
  - `pytest tests/test_photo_order.py tests/test_memories_photo_order.py
    tests/test_journal_photo_order.py tests/test_dedupe_memory_photos.py
    tests/test_reorder_polarsteps_memory_photos.py` passes.
  - New tests cover:
    - the round-trip of sources;
    - a state without sources;
    - a malformed source next to a valid epoch;
    - place, remove, replace and clear with sources;
    - replace not carrying the old source;
    - retain keeping order, ranks, hashes and sources, and bumping the
      epoch;
    - `source_of` ignoring stale entries.
- **Out of scope:** every caller in `api/memories.py` and `api/journal.py`.
- **Latitude:** none.
- **Escalate if:**
  - a writer of `photo_order_json` turns up that builds the state without
    `load_state`;
  - a file outside Scope must change (X3).
- **Depends on:** —

#### U4 — HEIC to JPEG on ingest, and sniffed media types (F4)
- **Goal:** `photo_store` converts HEIC and HEIF to JPEG before writing, and
  offers a helper that names a stored file's real media type. Avatars use
  it.
- **Scope:**
  - `pyproject.toml`
  - `uv.lock`
  - `src/utils/photo_store.py`
  - `api/people.py`, only the `FileResponse(..., media_type="image/jpeg")`
    lines at 461 and 482
  - `tests/test_photo_upload_processing.py`
  - `tests/test_photo_heic.py` (new)
- **Context:**
  - `src/utils/photo_store.py` entirely. Its #511 orientation handling is
    what the conversion must preserve.
  - Example to follow for format-specific tests:
    `tests/test_photo_upload_processing.py`.
  - Decisions D4 and D11.
  - Memory notes: `uv run python -m` on this machine; run the suite in the
    py314 test container if the local venv is stale.
- **Do:**
  1. Add `pillow-heif` and register its opener once, at import of
     `photo_store`.
  2. In `write_photo_files`, when the decoded format is HEIF:
     - convert to RGB and make it upright;
     - encode JPEG at quality 92 with the original EXIF (orientation set to
       1) and the ICC profile;
     - store those bytes, and build the thumbnail from the upright image.
     Every other format keeps today's behaviour exactly.
  3. `MAX_PHOTO_PIXELS` is checked before decoding, as today.
  4. Add `photo_media_type(path) -> str`. It reads the first 16 bytes and
     returns JPEG, PNG, WebP or GIF, defaulting to `image/jpeg`.
  5. `api/people.py` serves avatars with it.
  6. Check the licences of `pillow-heif` and its bundled libraries for
     AGPL-3 compatibility. Put the finding in the unit report.
- **Acceptance:**
  - `pytest tests/test_photo_heic.py tests/test_photo_upload_processing.py
    tests/test_quota_enforcement.py` passes.
  - New tests cover:
    - a HEIC fixture, generated in the test with `pillow_heif`, stored as
      a JPEG that keeps its EXIF date;
    - a portrait HEIC with a rotation stored upright, its thumbnail upright
      too;
    - a HEIC over the pixel cap refused before decoding;
    - a JPEG stored byte-for-byte as before;
    - `photo_media_type` for each of the four signatures and for unknown
      bytes.
  - `docker build` of the API image succeeds. This is checked in CI, not
    locally, per the Docker memory note.
- **Out of scope:**
  - converting PNG or WebP;
  - RAW support;
  - the memory and journal serve routes (U8);
  - backfilling existing files.
- **Latitude:** local design.
- **Escalate if:**
  - `pillow-heif` has no wheel for the image's platform;
  - a licence is incompatible;
  - orientation cannot be made upright without changing JPEG behaviour;
  - a file outside Scope must change (X3).
- **Depends on:** —

#### U5 — Matching engine
- **Goal:** a pure module that turns trip photos and album photos into
  one-to-one suggestions with confidence, per D7.
- **Scope:**
  - `src/photo_upgrade/__init__.py` (new)
  - `src/photo_upgrade/hashing.py` (new)
  - `src/photo_upgrade/matching.py` (new)
  - `tests/test_photo_upgrade_matching.py` (new)
  - `tests/test_photo_upgrade_hashing.py` (new)
  - `scripts/immich_upgrade_probe.py`, only to import `hashing` instead of
    its local copy
- **Context:**
  - Decision D7 and the OD2 and OD4 answers.
  - Rules to mirror where D7 does not differ: `pairCandidatesWithThumbnails`
    in `flutter_client/lib/src/photos/photo_match.dart`.
  - Example to follow for a pure module with plain-value inputs:
    `api/photo_order.py`.
- **Do:**
  1. `hashing.py`:
     - `ahash(img) -> int` and `dhash(img) -> int`, each 64 bits, on an
       already upright PIL image;
     - `fingerprint(raw_bytes) -> Fingerprint(ahash, dhash, width, height)`.
       It makes the image upright by EXIF and returns None for bytes it
       cannot decode.
  2. `matching.py`:
     - inputs `TripPhoto(key, date, lat, lon, fp, pixels, source)` and
       `AlbumPhoto(id, local_date, lat, lon, fp, pixels)`;
     - output `Suggestion(key, state, asset_id, score, confidence,
       alternatives[≤5])`, where `state` is `proposed`, `no_match` or
       `already_original`;
     - the constants from OD2 live at module top.
  3. Implement D7 in order:
     - photos with a source become `already_original`, and their asset ids
       leave the pool;
     - candidates within ±1 day, with the adjacent-day penalty;
     - the aspect gate;
     - the score;
     - global greedy assignment, deterministic on ties;
     - confidence;
     - the "no more pixels" rule;
     - geo distance as a tie-break.
  4. `excluded_pairs` is an input set of (key, asset_id) that are never
     proposed. Rejections from a previous scan arrive through it.
- **Acceptance:**
  - `pytest tests/test_photo_upgrade_matching.py
    tests/test_photo_upgrade_hashing.py` passes.
  - Tests cover:
    - an exact match;
    - a burst of 3 near-identical frames giving low confidence with 2
      alternatives;
    - a wrong aspect ratio excluded;
    - a photo dated D matched by an asset dated D+1 only when no D asset
      scores better;
    - an asset never used twice;
    - `already_original` by source and by pixels;
    - excluded pairs never proposed;
    - deterministic output when the input order is shuffled;
    - 5,000 photos against 10,000 assets spread over 30 days in under 10
      s.
- **Out of scope:** any I/O, any DB access, Immich calls.
- **Latitude:** local design, inside D7.
- **Escalate if:**
  - OD4 says capture times exist, and D7 has not been amended yet;
  - a file outside Scope must change (X3).
- **Depends on:** U1 (OD2, OD4).

#### U6 — Scan and suggestion tables
- **Goal:** the two tables of D8 exist, with their migration, and go with
  their project and their account.
- **Scope:**
  - `models/project_db.py`
  - `alembic/versions/<new>_add_photo_upgrade_tables.py` (new)
  - `src/project/repo_core.py`, only `delete_project`
  - `src/auth/account_deletion.py`
  - `tests/test_photo_upgrade_tables.py` (new)
- **Context:**
  - Example to follow for a job table: `DBVideoJob` in
    `models/project_db.py`, and its migration.
  - Account deletion: `_delete_all` in `src/auth/account_deletion.py`;
    `tests/test_account_deletion_owned_tables.py` finds every owner column
    on its own.
  - Memory notes: always Alembic; check `alembic heads` after rebasing.
- **Do:**
  1. `DBPhotoUpgradeScan` (`photoupgradescan`):
     - `id`;
     - `project_id` (FK, index) and `user_info_id` (FK, index): the runner;
     - `album_id` and `album_name`;
     - `include_memories` and `include_journal`;
     - `status`, `stage`, `progress_done`, `progress_total`,
       `error_message`;
     - `apply_status`, `apply_stopped_reason`;
     - `created_at`, `completed_at`.
     A unique index on (`project_id`, `user_info_id`).
  2. `DBPhotoUpgradeSuggestion` (`photoupgradesuggestion`):
     - `id` and `scan_id` (FK, index);
     - `artifact_kind` (`memory` or `journal`), `artifact_id`,
       `photo_uuid`, `date`;
     - `state`, `confidence`, `selected`;
     - `asset_id`, `score`, `alternatives_json`;
     - `asset_bytes`, `asset_width`, `asset_height`, `asset_mime`;
     - `current_bytes`, `current_width`, `current_height`;
     - `error_code`, `applied_uuid`.
     An index on (`scan_id`, `state`).
  3. Write the migration, down-revising from the current head. The upgrade
     and downgrade both run on SQLite.
  4. `delete_project` deletes the project's scans and their suggestions.
     Account deletion deletes the account's scans and their suggestions.
- **Acceptance:**
  - `pytest tests/test_photo_upgrade_tables.py
    tests/test_account_deletion_owned_tables.py
    tests/test_account_deletion.py` passes.
  - `alembic upgrade head` and `alembic downgrade -1` work on a copy of a
    migrated DB, and `alembic heads` prints one head.
  - Tests cover:
    - deleting a project removes its scans and suggestions;
    - deleting an account removes its scans in other people's projects;
    - the unique index refuses a second scan for the same (project, user).
- **Out of scope:** any job or route.
- **Latitude:** none.
- **Escalate if:**
  - main gains a migration before merge (rebase and re-point);
  - a file outside Scope must change (X3).
- **Depends on:** —

#### U7 — Per-memory dialog uses local capture day (F2 app side)
- **Goal:** the per-memory dialog buckets Immich photos by local capture
  time, so trips far from UTC see the right day's photos.
- **Scope:**
  - `flutter_client/lib/src/photos/immich_source.dart`
  - `flutter_client/lib/src/photos/photo_upgrade_screen.dart`
  - `flutter_client/test/photos/immich_source_test.dart`
  - `flutter_client/test/photos/photo_upgrade_screen_test.dart`
- **Context:**
  - The `_fetchImmichCandidatesForDay` comment on the "EXIF-as-UTC
    convention" (`photo_upgrade_screen.dart:156`); `classifyDayGeoMatch`.
  - Example to follow for the tests: `immich_source_test.dart`'s
    MockClient tests.
  - Decision D10 and the U2 contract (`local_taken_at`, string or null).
  - Memory notes: run the tests in the `traxjourney-flutter-3.47.1-tests`
    container; never pumpAndSettle on a spinner.
- **Do:**
  1. `ImmichCandidate` gains `localTakenAt`, nullable, parsed from
     `local_taken_at`.
  2. The day comparison is done on a `PhotoCandidate` whose `capturedAt` is
     `localTakenAt` when present (wall clock as UTC, offset zero), else
     `takenAt` as today. That covers `filterImmichCandidatesForDay` and
     `_applyImmichCandidate`'s `classifyDayGeoMatch`.
  3. The dialog asks the search for the UTC window from 14 h before the
     local day to 14 h after it, so no timezone's photos fall outside it.
     The local-day filter then narrows the results.
- **Acceptance:**
  - In the Flutter test container, `flutter test
    test/photos/immich_source_test.dart
    test/photos/photo_upgrade_screen_test.dart` passes.
  - New tests cover:
    - a photo at 08:00 local in UTC+9 lands on its local day;
    - a candidate without `local_taken_at` behaves as today;
    - the widened search window is what gets requested.
- **Out of scope:** any other change to the dialog; the new project-level
  screen.
- **Latitude:** none.
- **Escalate if:**
  - a test relies on the old window;
  - a file outside Scope must change (X3).
- **Depends on:** U2's contract only. This unit can merge before U2,
  because the field is optional.

### Wave 2

#### U8 — Shared replace core, net-delta quota, adopt guard, served media types (F3, F4, F5)
- **Goal:** one function replaces a memory or journal photo. Both endpoints
  and the apply job use it. It charges storage by the net change, records a
  source and carries a hash. An adopt-and-refresh keeps upgraded photos.
  Originals are served with their real media type.
- **Scope:**
  - `api/photo_replace.py` (new)
  - `api/memories.py`
  - `api/journal.py`
  - `tests/test_photo_replace_core.py` (new)
  - `tests/test_photo_replace_api.py`
  - `tests/test_memories_photo_order.py`
  - `tests/test_polarsteps_dedup.py`
- **Context:**
  - `replace_photo` in both route files (the near-duplicate this unit
    removes); `_adopt_and_refresh` and `_clear_memory_photos` in
    `api/memories.py`.
  - `src/billing/usage.py` (`bytes_of`, `unlink_and_record`);
    `ensure_storage_quota`.
  - Example to follow for a lock-held DB write with file cleanup:
    `_write_memory_photo` in `api/memories.py`.
  - Decisions D3, D5, D6 and D10; U3's `replace(..., source=)` and
    `retain`; U4's `photo_media_type`.
- **Do:**
  1. Write `replace_photo_bytes(kind, artifact_id, old_uuid, raw, *,
     owner_dir, source=None) -> str`, where `kind` is `memory` or
     `journal`. It raises `PhotoGone`, `QuotaExceeded` or `InvalidPhoto`.
     It does, in order:
     - check storage against `max(0, len(raw) − bytes_of(old full file))`
       (F3);
     - write the new files;
     - under `photo_lock`, re-read the row and, if the old photo has no
       hash, sha256 its full file (D6);
     - `replace_in_order(..., source=source)`, then commit, bust the
       caches, and unlink the old files (with the share copy for memories)
       while still under the lock;
     - on a vanished row or photo, delete the new files and raise
       `PhotoGone`.
  2. Both `PUT …/replace` routes call it. Their status codes, bodies and
     role rules do not change.
  3. `_adopt_and_refresh` uses `retain`. It keeps photos that have a source
     and deletes the files of the rest (F5).
  4. The memory and journal serve routes for originals, and their thumbnail
     fallbacks to originals, use `photo_media_type`.
- **Acceptance:**
  - `pytest tests/test_photo_replace_core.py tests/test_photo_replace_api.py
    tests/test_memories_photo_order.py tests/test_journal_photo_order.py
    tests/test_polarsteps_dedup.py tests/test_quota_enforcement.py
    tests/test_memories_thumb_concurrency.py
    tests/test_photo_path_containment.py` passes.
  - New tests cover:
    - a replace that fits only by net delta is accepted (F3);
    - a replace of a hashless photo hands the old file's sha256 to the new
      UUID;
    - a source is recorded;
    - a photo deleted mid-replace leaves no files and raises `PhotoGone`;
    - an adopt-and-refresh keeps a sourced photo with its rank and hash
      and drops the others;
    - a re-import download whose sha256 matches the carried hash is
      skipped;
    - a PNG original is served as `image/png`.
- **Out of scope:**
  - the single-photo endpoints' role rules (D3);
  - avatars (done in U4);
  - the share-link routes, whose copies are already JPEG.
- **Latitude:** local design inside `api/photo_replace.py`; none in the
  route files.
- **Escalate if:**
  - a route's behaviour must change to share the core;
  - the share copy of a journal photo turns out to exist;
  - a file outside Scope must change (X3).
- **Depends on:** U3, U4.

#### U9 — Scan job and the `photos` queue
- **Goal:** an RQ job that turns an album and a trip into stored
  suggestions, on a new `photos` queue that runs one job at a time.
- **Scope:**
  - `src/photo_upgrade/scan_job.py` (new)
  - `src/jobs/queue.py`
  - `src/jobs/worker.py`, only its startup sweep
  - `docker-compose.yml.example`
  - `tests/test_worker_topology.py`
  - `tests/test_photo_upgrade_scan_job.py` (new)
- **Context:**
  - Example to follow for the job-row lifecycle, compare-and-set updates,
    the interrupted-job sweep and progress stages:
    `src/video/job_runner.py`.
  - `src/immich/client.py` (U2); `src/photo_upgrade/` (U5); U6's tables.
  - Reading a photo's thumbnail path: `photo_folder` and `photo_file` in
    `src/utils/photo_paths.py`.
  - Decisions D3, D7, D8, D9 and D12.
  - Memory note: threaded tests need a file-backed SQLite, not StaticPool.
- **Do:**
  1. Add `QUEUE_PHOTOS = "photos"` with concurrency 1. `worker-poster`'s
     command becomes `poster photos default resolve`; update the topology
     test and the compose comment block.
  2. `run_scan(scan_id)`:
     - compare-and-set `pending → scanning`;
     - load the runner's token;
     - list album photos with dates within the trip's dates ±1 day,
       dropping videos, trashed and archived assets. Over 10,000 fails the
       scan with a message;
     - collect trip photos in scope per D3, re-checking roles. Over 5,000
       fails the scan;
     - fingerprint each trip photo from its `_thumb.jpg` on disk, and read
       its current bytes and upright dimensions from the full file's
       header;
     - fetch Immich thumbnails 4 at a time and fingerprint them. A failed
       fetch skips that asset and is counted;
     - read the rejected pairs of the previous scan, run the matcher, and
       write the suggestion rows. `selected` is true for high-confidence
       proposals (D2);
     - delete the previous scan's rows, then set `ready`;
     - update the stage and progress about every 50 items.
  3. An unreachable Immich or a 403 fails the scan with a sentence the app
     can show.
  4. The worker's startup sweep fails scans left `scanning`. The job
     timeout is 30 minutes.
- **Acceptance:**
  - `pytest tests/test_photo_upgrade_scan_job.py
    tests/test_worker_topology.py` passes.
  - Tests use a faked Immich client and real files in a temp data
    directory, and cover:
    - suggestions written with `selected` per confidence;
    - a companion's scan covering only their own journal entries;
    - rejected pairs carried over;
    - the previous scan replaced;
    - the caps;
    - Immich unreachable;
    - a 403;
    - a skipped thumbnail counted;
    - a project deleted mid-scan ending `failed`, with no rows left;
    - the startup sweep.
- **Out of scope:** routes; apply.
- **Latitude:** local design.
- **Escalate if:**
  - the trip's date span is not available without loading the whole
    project;
  - a file outside Scope must change (X3).
- **Depends on:** U2, U5, U6.

### Wave 3

#### U10 — Photo-upgrade API
- **Goal:** routes to start a scan, read it, review suggestions page by page,
  change decisions, list a day's candidates, and start an apply.
- **Scope:**
  - `api/photo_upgrade.py` (new)
  - `api/router.py`, to include the router and register the hourly sweep
  - `tests/test_photo_upgrade_api.py` (new)
- **Context:**
  - Example to follow for project-scoped routes with `?owner=` and roles:
    `api/video.py`, including `resolve_project` and the quota responses.
  - Storage figures for the summary: `src/billing/entitlements.py`.
  - Decisions D2, D3, D8, D9 and D12; Conventions.
- **Do:**
  1. `POST /api/projects/{name}/photo-upgrade/scans` with body `{album_id,
     include_memories, include_journal}`:
     - `include_memories` needs the owner role, otherwise 403;
     - refuses with 409 while an apply runs;
     - creates or replaces the caller's scan row and enqueues `run_scan`;
     - answers 202 with the scan.
  2. `GET /api/projects/{name}/photo-upgrade/scans/latest` returns the
     caller's scan or null, and a summary: counts per state and
     confidence, the selected count, the sum of
     `asset_bytes − current_bytes` over selected rows, and the caller's
     storage used and limit.
  3. Scan routes, under `/api/photo-upgrade/scans/{id}`, all checked to
     belong to the caller:
     - `GET …/suggestions?filter=&cursor=` pages 100 at a time, ordered by
       date, kind, artifact and position;
     - `PATCH …/suggestions/{sid}` with `{selected?, asset_id?,
       rejected?}`. A chosen `asset_id` must be the row's alternative or an
       album photo of that day (re-checked through the client). On an
       applied row it targets `applied_uuid` and puts the row back to
       `proposed` (D5);
     - `POST …/select` with `{which: "confident" | "all_proposed" |
       "none"}`;
     - `GET …/candidates?suggestion_id=` returns that day's album photos,
       ±1 day, ranked for the chooser;
     - `POST …/apply` sets `apply_status=pending` and enqueues U11's
       `start_apply(scan_id)`;
     - `DELETE …` discards the scan, refused while an apply runs.
  4. The hourly sweep in `api/router.py`:
     - deletes scans older than 30 days;
     - fails scans and applies `pending` for more than 15 minutes, with
       the message "No worker is processing photo jobs" (D9).
- **Acceptance:**
  - `pytest tests/test_photo_upgrade_api.py` passes, with the enqueue
    faked.
  - Tests cover:
    - an editor refused `include_memories` but allowed journal only;
    - a viewer refused;
    - another user's scan returning 404;
    - the summary's numbers;
    - paging;
    - PATCH with an asset from another day refused;
    - PATCH on an applied row;
    - select-confident;
    - 409 on a scan or delete during an apply;
    - both sweeps.
- **Out of scope:** the apply job's internals (U11); Flutter.
- **Latitude:** local design for response shapes, listed in the report and
  consumed by U12.
- **Escalate if:**
  - `?owner=` resolution cannot express "owner only" the way
    `api/video.py` does;
  - a file outside Scope must change (X3).
- **Depends on:** U6, U9.

#### U11 — Apply job
- **Goal:** apply the selected suggestions in batches, from Immich straight
  into the photo store, with a reason on every row that does not apply.
- **Scope:**
  - `src/photo_upgrade/apply_job.py` (new)
  - `tests/test_photo_upgrade_apply_job.py` (new)
- **Context:**
  - `api/photo_replace.py` (U8); `src/immich/client.py` (U2); U6's tables.
  - Example to follow for compare-and-set lifecycle and sweeps:
    `src/video/job_runner.py`.
  - Decisions D3, D4, D5, D9 and D12; Conventions (error codes); OD3.
- **Do:**
  1. `start_apply(scan_id)` and `run_apply_batch(scan_id)`.
  2. A batch takes up to 25 rows that are `proposed` and selected. For
     each row:
     - compare-and-set `proposed → applying`;
     - re-check the role (owner for memory rows) and that the photo is
       still listed;
     - download the original: through the RAW rendition per OD3 when the
       mime is RAW; rows over 25 MB fail with `too_large`;
     - call `replace_photo_bytes(..., source="immich:<id>")`;
     - mark the row `applied` with `applied_uuid`, or `failed` with an
       error code, or `stale` on `PhotoGone`.
  3. On `QuotaExceeded`, put the row back to `proposed` and stop with
     `apply_status=stopped` and `apply_stopped_reason=storage_full`.
  4. After a batch, enqueue the next one, or set `done`. Without a broker,
     loop in process.
  5. Recovery: a row found `applying` at batch start becomes `applied` if
     the photo list holds a photo sourced from its asset, otherwise
     `proposed`.
- **Acceptance:**
  - `pytest tests/test_photo_upgrade_apply_job.py` passes.
  - Tests cover:
    - 60 rows in 3 batches;
    - each error code;
    - the storage-full stop leaving rows to retry;
    - a photo deleted during the apply becoming `stale`;
    - recovery from `applying` both ways;
    - a role removed between scan and apply;
    - a HEIC original stored as JPEG;
    - the inline loop without a broker.
- **Out of scope:** routes; notifications; retrying failed rows
  automatically.
- **Latitude:** local design.
- **Escalate if:**
  - OD3 leaves RAW rows with no sensible behaviour;
  - a file outside Scope must change (X3).
- **Depends on:** U8, U9.

### Wave 4

#### U12 — Flutter: Upgrade photos from Immich
- **Goal:** the project-level screen: choose an album, scan, review, apply,
  per D2 and D5.
- **Scope:**
  - `flutter_client/lib/src/photos/album_upgrade_service.dart` (new)
  - `flutter_client/lib/src/photos/album_upgrade_notifier.dart` (new)
  - `flutter_client/lib/src/photos/album_upgrade_screen.dart` (new)
  - `flutter_client/lib/src/photos/album_upgrade_widgets.dart` (new)
  - `flutter_client/lib/src/core/app_router.dart`, one route
  - `flutter_client/lib/src/projects/app_screen.dart`, one menu item in the
    narrow and wide layouts
  - `flutter_client/test/photos/album_upgrade_*_test.dart` (new)
- **Context:**
  - Example to follow for a project-scoped import screen with its notifier
    and route: `polarsteps_import_screen.dart` and
    `polarsteps_import_notifier.dart`, with `/polarsteps-import` in
    `app_router.dart`.
  - Row visuals to reuse: `_rowTile` in `photo_upgrade_screen.dart`.
  - Design tokens: `core/design_tokens.dart`.
  - U10's response shapes, as reported.
  - Memory notes: widget tests in the Flutter test container; swap the
    `api` global; pump narrowly.
- **Do:**
  1. The menu item "Upgrade photos from Immich…" sits after "Import steps
     from Polarsteps". It opens `/photo-upgrade?project=&owner=`.
  2. Not connected: a card explaining Immich, with a button to Settings.
  3. No scan yet, or "New scan":
     - the album list, with albums overlapping the trip dates first and
       the last-used album preselected;
     - checkboxes for Memories (owner only) and My journal entries, with
       their counts;
     - one line saying nothing changes until you approve;
     - a Scan button.
  4. Scanning: the stage and a progress bar, polled every 2 s while
     visible, stopped when the page is left.
  5. Review:
     - the summary chips (To check, Confident, No match, Already original,
       Rejected, Failed) double as filters;
     - the list is grouped by day, then by memory or entry;
     - each row shows current → proposed, sizes and dimensions, the
       confidence, a tick box, "Choose another" (a sheet of that day's
       album photos), "Not this one" (reject), and a tap-to-compare view
       side by side with zoom;
     - "No match" rows offer "Choose from album";
     - the sticky bar reads "Replace N photos · +X of Y free", blocked with
       an upgrade link when over storage;
     - "Select all confident" and "Clear".
  6. Applying: per-row results arrive as rows flip. A failed row shows its
     reason. A storage-full stop explains itself.
  7. Applied rows keep "Choose another".
  8. The ProjectNotifier reloads the project once at the end, so the new
     photos show.
- **Acceptance:**
  - In the Flutter test container, `flutter test
    test/photos/album_upgrade_*_test.dart` passes.
  - Tests cover:
    - the not-connected card;
    - a companion seeing no Memories checkbox;
    - confident rows ticked by default;
    - filters;
    - choose-another calling PATCH;
    - the bar's numbers and its storage block;
    - polling stopping when the page is left;
    - the applied, failed and storage-full states.
  - `flutter analyze` is clean for the new files.
- **Out of scope:**
  - push or email notifications;
  - changes to the per-memory dialog (U7 did those);
  - offline support.
- **Latitude:** local design for layout, inside the design-system rules.
- **Escalate if:**
  - U10's shapes are missing data a screen needs;
  - a file outside Scope must change (X3).
- **Depends on:** U10, U11.

#### U13 — Docs, release notes, owner actions
- **Goal:** the feature, its permissions and its deploy step are written
  down.
- **Scope:**
  - `README.md`, the Immich section
  - `docs/ENCRYPTION.md`, the Photos row at line 319
  - `docs/RELEASING.md`, under "Post-deploy owner actions"
  - `docs/DEPLOYMENT_VPS.md`, the worker commands
  - `docs/plans/README.md`
  - `scripts/release_notes.py`, only if a new area is needed
- **Context:**
  - The existing Immich README text from PR #234.
  - Decisions D4 and D9; the OD5 answer.
  - The release-note rule in `CLAUDE.md` and `docs/RELEASING.md`.
- **Do:**
  1. README:
     - the project-level upgrade;
     - the Immich key permissions it needs (OD5);
     - the 25 MB limit;
     - HEIC converted, RAW per OD3.
  2. ENCRYPTION.md: Immich thumbnails are read on the server to compare
     them and are not kept; applied originals are stored like any photo.
  3. RELEASING.md owner action: change `worker-poster`'s command on val and
     prod to `poster photos default resolve`, then `docker compose up -d`.
     Check with a scan on val.
  4. The deployment doc gets the same command.
  5. The plans index gets this plan's row.
- **Acceptance:**
  - `pytest tests/test_release_notes.py tests/test_worker_topology.py`
    passes.
  - Every command in the new owner action matches
    `docker-compose.yml.example`.
- **Out of scope:** the privacy policy. Nothing new is collected or kept.
- **Latitude:** none.
- **Escalate if:**
  - the privacy policy turns out to describe Immich processing in a way
    this changes;
  - a file outside Scope must change (X3).
- **Depends on:** U9, U11.

## Definition of done

- An owner with Immich connected opens "Upgrade photos from Immich…" on a
  trip, picks an album, and gets suggestions for every memory photo and
  their own journal photos. Confident ones are ticked.
- Applying replaces the ticked photos with their originals, in place, with
  the same position, from Immich to the server. The trip shows them after
  one reload.
- A companion can scan and apply only their own journal entries; the API
  refuses memory scope with 403.
- A second scan of the same album proposes nothing already applied and
  nothing rejected.
- A HEIC original is stored and shown as JPEG, upright. A RAW one follows
  OD3. An original over 25 MB fails with "too large".
- Running out of storage stops the apply cleanly, with the remaining rows
  still ticked.
- A Polarsteps re-import that adopts a memory keeps its upgraded photos and
  does not bring the thumbnails back.
- F1: `/api/immich/search` returns every page, up to the cap.
- F2: the per-memory dialog shows a UTC+9 morning photo on its local day.
- F3: a replace that fits by net size is accepted.
- F4: HEIC uploads are accepted, and originals carry their real media type.
- F5: as the Polarsteps re-import item above.
- `alembic heads` shows one head. The worker topology test passes. The val
  compose file runs the `photos` queue.
- Every unit's acceptance tests pass in CI, and `graphify update .` has been
  run from main after merge.
