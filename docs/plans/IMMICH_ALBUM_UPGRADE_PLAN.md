# Immich album photo upgrade — Plan for #33

A project-level action: the user picks the Immich album that holds a trip's
original photos. The server compares every photo of the trip against it,
proposes one original per photo, and replaces the ones the user approves.
The plan also fixes five defects found while designing it (F1–F5, Decision
10). Review ledger: [docs/reviews/IMMICH_ALBUM_UPGRADE_PLAN.md](../reviews/IMMICH_ALBUM_UPGRADE_PLAN.md).

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
  existed deletes all of that memory's photos, upgraded and hand-added ones
  included ([api/memories.py:333-360](../../api/memories.py#L333-L360)).

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
- **Writing a photo list.** Seven places read `photos_json` and
  `photo_order_json`, change them, and write them back with a plain ORM
  update:
  - in `api/memories.py`: the clear at 637, placement at 680, delete at 815
    and replace at 865;
  - in `api/journal.py`: placement at 214, delete at 541 and replace at 590.

  They are serialised only by `photo_lock`
  ([api/photo_locks.py](../../api/photo_locks.py)), a `threading.Lock` per
  row. That holds only because every writer runs in the API process.
  Project-level writes already use a cross-process compare-and-set instead:
  `bump_lock_version` in
  [src/project/repo_core.py:69](../../src/project/repo_core.py#L69).
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
- **Jobs:**
  - RQ queues are defined in [src/jobs/queue.py](../../src/jobs/queue.py),
    with a default timeout of 600 s; jobs run inline when there is no
    broker.
  - `queue_has_workers` tells whether anything consumes a queue.
  - The worker topology is fixed by `docker-compose.yml.example` and
    enforced by `tests/test_worker_topology.py`.
  - The pattern for job rows, killed-job handlers and sweeps is `DBVideoJob`
    with `src/video/job_runner.py`, `_INTERRUPT_HANDLERS` in
    `src/jobs/worker.py`, and `STALE_PENDING_S` (24 h). The enqueue-time
    worker check is at `api/video.py:200-203`.
- **Storage limits:** [src/billing/plans.py:102-105](../../src/billing/plans.py#L102-L105).
  Free 500 MB, Tier 1 5 GB, Tier 2 20 GB, Tier 3 50 GB.

## Decisions

**D1 — The scan and the replacements run on the server** (owner decision).
RQ jobs read the album, hash both sides and store suggestions. Other jobs
pull each approved original from Immich straight into the photo store.
*Rules out* matching in the app, as #59 did. In a browser that is about
2,300 image decodes on the main thread for a typical trip and album, and
every original would travel Immich → app → server, 2.5 GB for 500 photos.
The end-to-end encryption reason for #59's choice does not apply to photos
(Current state).

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
- When the photo being replaced has no sha256 yet, the replace hashes the
  old full-resolution file before deleting it and gives that hash to the new
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

**D8 — Scans, album photos and suggestions are stored** (revised for R1-2,
R1-7, R1-9). Three tables (U6):
- **`photoupgradescan`:** one row per scan.
- **`photoupgradeasset`:** the scan's album photos with their fingerprints.
  The chain of scan jobs keeps its progress there, and the "Choose another"
  chooser reads from it.
- **`photoupgradesuggestion`:** one row per trip photo, with its `position`
  in its memory or entry, so the review lists photos in trip order.

Scans per person:
- A person can have many scans of a trip, but only one active (`pending` or
  `scanning`), enforced by a partial unique index.
- "Latest" is decided by `created_at`.
- A new scan reads the rejected pairs of the person's earlier scans of that
  trip before it writes its own suggestions. Only once it is `ready` does it
  delete those earlier scans.
- A failed scan therefore leaves the previous review and its rejections in
  place.

Retention:
- An hourly sweep deletes scans older than 30 days.
- Rows hold ids, dates, sizes, dimensions and fingerprints only, never image
  data or text.

*Rules out:*
- a unique index per (trip, person), which cannot hold a new scan beside the
  one whose rejections it must read;
- keeping results in a job's return value, which cannot hold decisions per
  row.

**D9 — A `photos` queue, one job at a time, on the poster worker, with
short jobs only** (revised for R1-5, R1-6, R1-7).
- **Placement.** The queue runs one job at a time. `worker-poster` consumes
  it after `poster`: `poster photos default resolve`.
- **Short jobs.**
  - A scan is a chain: read the album, then fingerprint up to 250 album
    photos per job, then match.
  - An apply is a chain of batches of up to 25 photos.
  - Each job enqueues the next; without a broker, one in-process run loops.
  - Every job fits the default 600 s timeout, and a poster waits behind at
    most one of them, usually a minute or two.
- **No worker.** When a broker exists but no worker listens on `photos`,
  starting a scan or apply answers 503 at once, as `api/video.py:200-203`
  does. This catches a VPS compose file that was not updated.
- **Pending jobs.** A job still `pending` after 24 hours is failed with a
  neutral message, as `STALE_PENDING_S` does for video. Waiting in the
  queue is normal.
- **Killed jobs.** Every job updates a heartbeat.
  - A job whose work-horse is killed is caught by `_INTERRUPT_HANDLERS`.
    Running scans and applies are also caught by the worker's startup sweep,
    and by an hourly sweep when the heartbeat is older than 30 minutes.
  - An interrupted scan fails with "interrupted".
  - An interrupted apply stops with "interrupted". Its rows left `applying`
    are recovered (U11), and the user can resume it.
- *Rules out:*
  - `default`, where minutes-long jobs would delay share tiles and stats;
  - a sixth worker service, given the memory budget of the 4 GB host
    (`docker-compose.yml.example:56-62`);
  - one long scan job, which would hold posters for up to 30 minutes.

**D10 — The five defects are fixed in this plan.**
- **F1:** the Immich client pages through results. The dialog's search
  stops at 10 pages of 1,000 and logs a warning when it cuts results; the
  album iterator fails loudly instead (D12) (U2).
- **F2:** the search response gains `local_taken_at`, taken from Immich's
  `localDateTime`, and the dialog buckets on it (U2, U7).
- **F3:** a replacement is checked against storage by the size of the new
  file minus the size of the file it frees, never below 0 (U8).
- **F4:** see D4 (U4, U8).
- **F5** (revised for R1-3): an adopt-and-refresh deletes no photos at all.
  - Before the refresh, it records a sha256, from the file on disk, for
    every listed photo that has none (U3, U8).
  - The re-import's downloads of photos already there are then skipped by
    the existing hash check. Photos Polarsteps has that the memory lacks are
    added. Upgraded and hand-added photos stay.
  - The epoch is not bumped, since nothing is cleared.
  - Residual: an original that replaced a hashless thumbnail before this
    change can get that thumbnail back beside it. That is a visible
    duplicate, with nothing lost.

**D11 — Quota is checked on the incoming bytes before conversion.** A HEIC
upload converts after the check, so its stored JPEG can be larger than what
was checked. The counter records the bytes actually written, and the nightly
reconcile corrects drift. *Rules out* restructuring the five callers of
`photo_store` around a separate prepare step for a gap the counter closes
anyway.

**D12 — Limits** (revised for R1-4).
- A scan considers at most 10,000 album photos dated within the trip ±1 day,
  and at most 5,000 trip photos in scope.
- The album iterator asks Immich for images in the date window only. It
  raises `ImmichTooManyAssets` as soon as it would yield a 10,001st item,
  and the scan then fails with a message.
- An original over 25 MB (`MAX_PHOTO_BYTES`) fails its row with "too large".
- Videos, trashed assets and archived assets are never candidates.
- *Rules out* a page cap equal to the limit, which truncates without saying
  so.

**D13 — Every photo-list write is a compare-and-set** (new, for R1-1).
- The apply job writes photo lists from the worker process while the API
  writes the same lists, and `photo_lock` cannot serialise across
  processes.
- So each of the seven read-modify-write sites becomes an `UPDATE … WHERE
  id = :id AND photos_json = :read AND photo_order_json IS :read`, through
  one helper (U14). On a conflict, the writer re-reads and recomputes, up to
  5 times.
- Side effects that depend on the outcome (deleting files, busting caches)
  happen only after the update succeeds.
- `photo_lock` stays, so writers in one process queue instead of retrying.
- This is the cross-process pattern project writes already use
  (`bump_lock_version`).
- *Rules out:*
  - a Redis lock: it would make every photo write depend on the broker,
    and do nothing when there is no broker;
  - `BEGIN IMMEDIATE`: it is SQLite-only and would hold the database's
    write lock while files are hashed and unlinked.

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
- **Two processes write photo lists.** The API and `worker-poster` both do.
  - Every write is a compare-and-set (D13), so either side may lose a race
    and retry, but never overwrite the other.
  - While a scan or apply runs, the user and companions may delete, reorder,
    replace or re-import the same photos. A photo gone since the scan makes
    its row `stale`.
  - Each person has at most one active scan and one apply per trip.
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
  - The photo write routes keep their responses. Losing a write race 5
    times in a row answers 409, which today cannot happen.
- **Schema.** Three new tables in one Alembic migration on top of the
  current head `e5c3a7b91d24`. After rebasing on main there must still be a
  single head.
- **Stored data.**
  - `photo_order_json` gains a `sources` key. `load_state` reads its absence
    as `{}`, and every writer goes through `dump_state`.
  - HEIC uploads, refused until now, are stored as JPEG.
  - Applies delete the old files, as replace does today.
  - An adopt-and-refresh now keeps a memory's photos instead of deleting
    them (D10, F5).
  - No migration of existing data.
- **Export format.** `photo_order_json` is not exported
  (`api/project_transfer.py`). ZIP exports of upgraded trips are larger
  because the photos are.
- **Infrastructure.**
  - The new `photos` queue changes `worker-poster`'s command.
  - The VPS compose file must be updated by the owner (U13 adds the
    post-deploy action). Until then, scans and applies answer 503 (D9).
- **Dependency.** `pillow-heif`, with its bundled libheif. Add it to
  `pyproject.toml` and `uv.lock`, and check that its licences are compatible
  with AGPL-3 (U4).

## Conventions

- **States.**
  - Scan `status`: `pending | scanning | ready | failed`.
  - Scan `apply_status`: `idle | pending | running | done | stopped`.
  - `apply_stopped_reason`: `storage_full | interrupted`.
  - Suggestion `state`: `proposed | no_match | already_original | rejected |
    applying | applied | failed | stale`.
  - Every state transition is a compare-and-set update in SQL, like `_cas`
    in `src/video/job_runner.py`.
- **Error codes on a failed row:** `too_large`, `unsupported_format`,
  `not_found_in_immich`, `immich_unreachable`, `photo_gone`,
  `storage_full`, `invalid_image`. The app maps each to a sentence.
- **Writes.**
  - Every change to `photos_json` or `photo_order_json` goes through U14's
    helper.
  - Every photo replacement goes through the replace core in
    `api/photo_replace.py` (U8).
  - Scripts run with the API stopped, and archive import writes rows it has
    just created, so both are exempt.
- **Job contract.** The function names other units import are fixed here:
  - `src/photo_upgrade/scan_job.py`: `start_scan(scan_id: int) -> None` and
    `mark_scan_interrupted(scan_id: int, reason: str) -> None`.
  - `src/photo_upgrade/apply_job.py`: `start_apply(scan_id: int) -> None`,
    `recover_interrupted_apply(scan_id: int) -> None` and
    `mark_apply_interrupted(scan_id: int, reason: str) -> None`.
- **Logging.**
  - Never log the Immich key, the server URL with its credentials, or image
    bytes.
  - Ids and counts are fine.
  - Follow `src/utils/logging.get_logger`.
- **Names.** User-facing copy says "Upgrade photos from Immich" and
  "original". It does not say "asset", "pHash" or "scan job".

## Open decisions

All come out of the U1 probe, which the owner runs against their own album.
They must be settled before U2 and U5 start; the other Wave 1 units do not
wait for them.

- **OD1 — Immich API shape.**
  - Questions:
    - Does `POST /api/search/metadata` accept `albumIds` together with a
      date window and `type: IMAGE`, or must album photos be listed through
      `GET /api/albums/{id}` and filtered here?
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

| Wave | Units |
|---|---|
| 0 | U1 |
| 1 | U2, U3, U4, U5, U6, U7, U14 |
| 2 | U8, U9 |
| 3 | U11 |
| 4 | U10, U13 |
| 5 | U12 |

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
     - whether `search/metadata` with `albumIds`, a date window and `type:
       IMAGE` works, its page size and `nextPage` shape;
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
    written into this plan's Open decisions before U2 and U5 start.
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
  returns local capture time. The album iterator fails loudly past its
  limit.
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
  2. Add `ImmichClient(server_url, api_key)` and `list_albums() ->
     list[Album]`, owned and shared.
  3. Add `iter_album_assets(album_id, taken_after, taken_before, *,
     max_items=10_000)`:
     - it yields `Asset(id, type, local_date_time, file_created_at, lat,
       lon, mime, file_size, original_file_name, is_trashed, is_archived)`;
     - it asks for images in the window only, using the method OD1 chose,
       and follows `nextPage`;
     - it raises `ImmichTooManyAssets` instead of yielding item
       `max_items + 1`.
  4. Add `search_assets(taken_after, taken_before, *, max_pages=10)`. It
     returns `(assets, truncated)`.
  5. Add `thumbnail_bytes(asset_id, size)`, plus `open_original(asset_id)`
     and `open_fullsize(asset_id)` (OD3), both streamed.
  6. Malformed items are dropped one by one, as today.
  7. Errors are typed: `ImmichUnreachable`, `ImmichRejected(status)` with
     403 named, `ImmichAddressNotAllowed`, `ImmichTooManyAssets`.
  8. `GET /api/immich/albums` returns `{albums: [{id, name, asset_count,
     start_date, end_date, thumb_url, shared}]}`. A 403 from Immich becomes
     a 502 whose message names the missing permissions (OD5).
  9. `POST /api/immich/search` uses `search_assets`, so it pages (F1), and
     logs a warning when the result is truncated. Each candidate gains
     `local_taken_at` from `localDateTime`, or null (F2).
- **Acceptance:**
  - `pytest tests/test_immich_client.py tests/test_immich_api.py
    tests/test_safe_fetch.py tests/test_no_real_hosts.py` passes.
  - New tests cover:
    - paging across 3 pages;
    - `ImmichTooManyAssets` raised on item 10,001 and not at exactly
      10,000;
    - the search cap with its truncated flag and log line;
    - `local_taken_at` present and absent;
    - album listing;
    - a 403 naming the permissions;
    - trashed and archived assets flagged;
    - the address policy applying to every new call.
- **Out of scope:** the scan job; any Flutter change; caching.
- **Latitude:** local design.
- **Escalate if:**
  - OD1 says neither album-listing method works on the minimum version;
  - the date window cannot be applied on Immich's side, which would make the
    cap count photos outside the trip;
  - a file outside Scope must change (X3).
- **Depends on:** U1 (OD answers).

#### U3 — Sources in the photo order state, and hash backfill
- **Goal:** `api/photo_order.py` carries a `sources` map, and gains
  `fill_hashes`, which records hashes for listed photos that have none.
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
  3. `fill_hashes(photos, state, hashes) -> state`:
     - records `hashes[uuid]` for each listed UUID that has no hash yet;
     - never overwrites a hash;
     - ignores UUIDs not in `photos` and malformed values;
     - does not touch the epoch, ranks or sources.
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
    - `fill_hashes` filling only missing hashes and leaving everything else
      as it was;
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
       cannot decode;
     - `to_hex` and `from_hex` for storing a 64-bit hash as 16 hex
       characters, since SQLite integers are signed.
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
     proposed. Rejections from earlier scans arrive through it.
  5. `rank_candidates(photo, album_photos) -> list[(asset_id, score)]`
     serves the "Choose another" chooser.
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
    - the hex round-trip of a hash with the top bit set;
    - 5,000 photos against 10,000 assets spread over 30 days in under 10
      s.
- **Out of scope:** any I/O, any DB access, Immich calls.
- **Latitude:** local design, inside D7.
- **Escalate if:**
  - OD4 says capture times exist, and D7 has not been amended yet;
  - a file outside Scope must change (X3).
- **Depends on:** U1 (OD2, OD4).

#### U6 — Scan, album-photo and suggestion tables
- **Goal:** the three tables of D8 exist, with their migration, and go with
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
  - Partial unique index example: `uq_memory_project_polarsteps_step_id` on
    `DBMemory`.
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
       `error_message`, `heartbeat_at`;
     - `apply_status`, `apply_stopped_reason`, `apply_heartbeat_at`;
     - `created_at`, `completed_at`.

     Indexes:
     - (`project_id`, `user_info_id`, `created_at`);
     - a partial unique index on (`project_id`, `user_info_id`) where
       `status IN ('pending', 'scanning')`, for both SQLite and Postgres.
  2. `DBPhotoUpgradeAsset` (`photoupgradeasset`):
     - `id` and `scan_id` (FK, index);
     - `asset_id`, `local_date`, `lat`, `lon`;
     - `mime`, `file_bytes`, `width`, `height`;
     - `ahash` and `dhash`, 16-character hex or null;
     - `fingerprinted` and `fetch_failed`.

     Indexes: (`scan_id`, `local_date`) and (`scan_id`, `fingerprinted`).
  3. `DBPhotoUpgradeSuggestion` (`photoupgradesuggestion`):
     - `id` and `scan_id` (FK, index);
     - `artifact_kind` (`memory` or `journal`), `artifact_id`,
       `photo_uuid`, `position`, `date`;
     - `state`, `confidence`, `selected`;
     - `asset_id`, `score`, `alternatives_json`;
     - `asset_bytes`, `asset_width`, `asset_height`, `asset_mime`;
     - `current_bytes`, `current_width`, `current_height`;
     - `error_code`, `applied_uuid`.

     Indexes: (`scan_id`, `state`) and (`scan_id`, `date`,
     `artifact_kind`, `artifact_id`, `position`).
  4. Write the migration, down-revising from the current head. The upgrade
     and downgrade both run on SQLite.
  5. `delete_project` deletes the project's scans with their assets and
     suggestions. Account deletion deletes the account's scans with their
     assets and suggestions.
- **Acceptance:**
  - `pytest tests/test_photo_upgrade_tables.py
    tests/test_account_deletion_owned_tables.py
    tests/test_account_deletion.py` passes.
  - `alembic upgrade head` and `alembic downgrade -1` work on a copy of a
    migrated DB, and `alembic heads` prints one head.
  - Tests cover:
    - deleting a project removes its scans, assets and suggestions;
    - deleting an account removes its scans in other people's projects;
    - a second active scan for the same (project, user) is refused, while a
      second scan beside a `ready` or `failed` one is allowed.
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

#### U14 — Photo-list writes are compare-and-set (D13)
- **Goal:** every read-modify-write of `photos_json` and `photo_order_json`
  in the API succeeds only if neither column changed since it was read, and
  retries otherwise. A writer in another process can then never be
  overwritten.
- **Scope:**
  - `api/photo_write.py` (new)
  - `api/memories.py`, the seven sites listed in Current state and their
    callers' handling of the result (four of the seven are here)
  - `api/journal.py`, the three journal sites
  - `tests/test_photo_write_cas.py` (new)
- **Context:**
  - The seven sites: `api/memories.py` 637 (`_clear_memory_photos`), 680
    (`_write_memory_photo`), 815 (`delete_photo`), 865 (`replace_photo`);
    `api/journal.py` 214, 541 and 590.
  - Example to follow for a compare-and-set in SQL: `bump_lock_version` in
    `src/project/repo_core.py:69`, and `_cas` in `src/video/job_runner.py`.
  - Memory note: threaded and concurrent tests need a file-backed SQLite,
    not StaticPool.
  - Decision D13.
- **Do:**
  1. In `api/photo_write.py`, add `cas_photo_columns(sess, model, row_id, *,
     read_photos_json, read_order_json, photos, state) -> bool`:
     - it issues `UPDATE <table> SET photos_json, photo_order_json WHERE
       id = :id AND photos_json = :read_photos_json AND photo_order_json IS
       :read_order_json` (equality when the read value is not NULL);
     - it returns whether exactly one row changed;
     - it touches no other column.
  2. Add `mutate_photos(kind, row_id, compute, *, attempts=5)`:
     - it reads the row's two columns raw;
     - it calls `compute(photos, state, row)`, which returns the new
       `(photos, state)`, or `None` for no change;
     - it applies the result with `cas_photo_columns` in the same
       transaction as any other column `compute` set on the row;
     - it re-reads and recomputes on a conflict;
     - it raises `PhotoWriteConflict` after the last attempt.
  3. Convert each of the seven sites:
     - side effects that depend on the outcome (deleting the files of a
       removed or replaced photo, deleting the new files of a placement
       that was refused, busting caches) run only after the write
       succeeds;
     - the per-row `photo_lock` stays around the call;
     - a route that gets `PhotoWriteConflict` answers 409; a background
       placement logs it and deletes its new files.
  4. Behaviour seen through the routes is otherwise unchanged.
- **Acceptance:**
  - `pytest tests/test_photo_write_cas.py tests/test_memories_photo_order.py
    tests/test_journal_photo_order.py tests/test_photo_replace_api.py
    tests/test_memories_photo_poll.py tests/test_memories_thumb_concurrency.py
    tests/test_polarsteps_dedup.py tests/test_quota_enforcement.py` passes.
  - New tests use a file-backed SQLite and a second session that writes
    between the read and the write. They cover:
    - a delete racing a placement: both photos end correct;
    - a replace racing a delete of the same photo: the delete wins, the new
      files are removed, the route answers 404;
    - five straight conflicts answering 409;
    - a CAS update leaving the row's other columns as they were.
- **Out of scope:**
  - scripts, which run with the API stopped;
  - archive import, which writes rows it has just created;
  - `api/photo_order.py`;
  - any change to what the routes return when there is no race.
- **Latitude:** local design inside `api/photo_write.py`; none in the
  route files.
- **Escalate if:**
  - a site also writes a column whose race this cannot cover;
  - a writer of these columns turns up outside the seven;
  - a file outside Scope must change (X3).
- **Depends on:** —

### Wave 2

#### U8 — Shared replace core, net-delta quota, adopt keeps photos, served media types (F3, F4, F5)
- **Goal:** one function replaces a memory or journal photo. Both endpoints
  and the apply job use it. It charges storage by the net change, records a
  source and carries a hash. An adopt-and-refresh deletes no photos.
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
    removes), as U14 left them; `_adopt_and_refresh` and
    `_clear_memory_photos` in `api/memories.py`.
  - `src/billing/usage.py` (`bytes_of`, `unlink_and_record`);
    `ensure_storage_quota`.
  - `mutate_photos` (U14), and U3's `replace(..., source=)` and
    `fill_hashes`; U4's `photo_media_type`.
  - Example to follow for hashing stored originals:
    `scripts/dedupe_memory_photos.py`.
  - Decisions D3, D5, D6, D10 and D13.
- **Do:**
  1. Write `replace_photo_bytes(kind, artifact_id, old_uuid, raw, *,
     owner_dir, source=None) -> str`, where `kind` is `memory` or
     `journal`. It raises `PhotoGone`, `QuotaExceeded`, `InvalidPhoto` or
     `PhotoWriteConflict`. In order:
     - check storage against `max(0, len(raw) − bytes_of(old full file))`
       (F3);
     - if the old photo has no hash, sha256 its full file, outside any
       lock (D6);
     - write the new files;
     - `mutate_photos` with `replace_in_order(..., source=source)`, also
       giving the computed hash to the new UUID when it still has none;
     - on success, bust the caches and unlink the old files, with the
       share copy for memories;
     - on a vanished row or photo, delete the new files and raise
       `PhotoGone`.
  2. Both `PUT …/replace` routes call it. Their status codes, bodies and
     role rules do not change.
  3. `_adopt_and_refresh` no longer calls `_clear_memory_photos`:
     - it sha256s, outside the lock, the full file of every listed photo
       that has no hash;
     - it then applies `fill_hashes` through `mutate_photos`, in the same
       transaction as the refreshed scalar fields, without bumping the
       epoch (F5);
     - `_clear_memory_photos` is removed if nothing else calls it.
  4. The memory and journal serve routes for originals, and their thumbnail
     fallbacks to originals, use `photo_media_type`.
- **Acceptance:**
  - `pytest tests/test_photo_replace_core.py tests/test_photo_replace_api.py
    tests/test_photo_write_cas.py tests/test_memories_photo_order.py
    tests/test_journal_photo_order.py tests/test_polarsteps_dedup.py
    tests/test_quota_enforcement.py tests/test_memories_thumb_concurrency.py
    tests/test_photo_path_containment.py` passes.
  - New tests cover:
    - a replace that fits only by net delta is accepted (F3);
    - a replace of a hashless photo hands the old file's sha256 to the new
      UUID;
    - a source is recorded;
    - a photo deleted mid-replace leaves no files and raises `PhotoGone`;
    - an adopt-and-refresh keeps every photo, upgraded and hand-added ones
      included, fills the missing hashes and leaves the epoch as it was;
    - a re-import download whose sha256 matches a filled or carried hash is
      skipped;
    - a re-import download of a photo the memory lacks is placed;
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
  - a test relies on adopt clearing photos;
  - a file outside Scope must change (X3).
- **Depends on:** U3, U4, U14.

#### U9 — Scan job chain and the `photos` queue
- **Goal:** a chain of short RQ jobs that turns an album and a trip into
  stored suggestions, on a new `photos` queue that runs one job at a time,
  with recovery when a job is killed.
- **Scope:**
  - `src/photo_upgrade/scan_job.py` (new)
  - `src/jobs/queue.py`
  - `src/jobs/worker.py`: `_INTERRUPT_HANDLERS` and the startup sweep, scan
    entries only
  - `docker-compose.yml.example`
  - `tests/test_worker_topology.py`
  - `tests/test_photo_upgrade_scan_job.py` (new)
- **Context:**
  - Example to follow for the job-row lifecycle, compare-and-set updates,
    the interrupt handler, the startup sweep and progress stages:
    `src/video/job_runner.py` with its entries in `src/jobs/worker.py`.
  - `src/immich/client.py` (U2); `src/photo_upgrade/` (U5); U6's tables.
  - Reading a photo's thumbnail path: `photo_folder` and `photo_file` in
    `src/utils/photo_paths.py`.
  - Decisions D3, D7, D8, D9 and D12; Conventions, job contract.
  - Memory note: threaded tests need a file-backed SQLite, not StaticPool.
- **Do:**
  1. Add `QUEUE_PHOTOS = "photos"` with concurrency 1. `worker-poster`'s
     command becomes `poster photos default resolve`; update the topology
     test and the compose comment block.
  2. `start_scan(scan_id)` enqueues `run_scan_prepare`. Each job below
     first checks that its scan still exists and is `scanning` (or
     `pending` for the first), and stops quietly otherwise. Each updates
     `heartbeat_at`, `stage` and progress.
  3. `run_scan_prepare(scan_id)`:
     - compare-and-set `pending → scanning`;
     - load the runner's token;
     - iterate the album through `iter_album_assets` with the trip's dates
       ±1 day, and insert one asset row per image that is not trashed or
       archived. `ImmichTooManyAssets` fails the scan with a message;
     - count the trip photos in scope per D3, re-checking roles. Over 5,000
       fails the scan;
     - enqueue `run_scan_fingerprint`.
  4. `run_scan_fingerprint(scan_id)`:
     - take up to 250 asset rows not yet fingerprinted;
     - fetch their thumbnails 4 at a time and store their fingerprints;
     - mark a failed fetch `fetch_failed`;
     - enqueue itself again while rows remain, then `run_scan_match`.
  5. `run_scan_match(scan_id)`:
     - fingerprint each trip photo in scope from its `_thumb.jpg` on disk;
       read its current bytes and upright dimensions from the full file's
       header, and its `position` in its photo list;
     - read the rejected pairs from the person's earlier scans of this
       trip;
     - run the matcher and write the suggestion rows. `selected` is true
       for high-confidence proposals (D2);
     - set `ready`;
     - only then delete the person's earlier scans of this trip, with their
       rows.
  6. Without a broker, `start_scan` runs the chain in one in-process loop.
  7. An unreachable Immich or a 403 fails the scan with a sentence the app
     can show.
  8. Add `mark_scan_interrupted(scan_id, reason)`. Register it in
     `_INTERRUPT_HANDLERS` for the three job functions. The worker's
     startup sweep fails every scan left `scanning`.
- **Acceptance:**
  - `pytest tests/test_photo_upgrade_scan_job.py
    tests/test_worker_topology.py` passes.
  - Tests use a faked Immich client and real files in a temp data
    directory, and cover:
    - a 600-photo album taking three fingerprint jobs;
    - suggestions written with `selected` per confidence and `position` in
      trip order;
    - a companion's scan covering only their own journal entries;
    - rejected pairs carried over from an earlier scan;
    - earlier scans deleted only once the new one is `ready`;
    - a failed scan leaving the earlier scan and its rejections in place;
    - `ImmichTooManyAssets` and the 5,000 limit failing the scan;
    - Immich unreachable;
    - a 403;
    - a fetch failure counted;
    - a project deleted mid-chain stopping the next job quietly;
    - the interrupt handler and the startup sweep;
    - the in-process loop.
- **Out of scope:** routes; apply.
- **Latitude:** local design.
- **Escalate if:**
  - the trip's date span is not available without loading the whole
    project;
  - a file outside Scope must change (X3).
- **Depends on:** U2, U5, U6.

### Wave 3

#### U11 — Apply job chain
- **Goal:** apply the selected suggestions in batches, from Immich straight
  into the photo store, with a reason on every row that does not apply, and
  recovery when a batch is killed.
- **Scope:**
  - `src/photo_upgrade/apply_job.py` (new)
  - `src/jobs/worker.py`: `_INTERRUPT_HANDLERS` and the startup sweep, apply
    entries only
  - `tests/test_photo_upgrade_apply_job.py` (new)
- **Context:**
  - `api/photo_replace.py` (U8); `src/immich/client.py` (U2); U6's tables;
    U9's scan jobs, for the chaining and the worker entries.
  - Example to follow for compare-and-set lifecycle and sweeps:
    `src/video/job_runner.py`.
  - Decisions D3, D4, D5, D9 and D12; Conventions (error codes, job
    contract); OD3.
- **Do:**
  1. `start_apply(scan_id)` enqueues `run_apply_batch(scan_id)` on
     `photos`.
  2. `run_apply_batch` sets `apply_status` from `pending` to `running`,
     then takes up to 25 rows that are `proposed` and selected. For each
     row:
     - compare-and-set `proposed → applying`;
     - update `apply_heartbeat_at`;
     - re-check the role (owner for memory rows) and that the photo is
       still listed;
     - download the original: through the RAW rendition per OD3 when the
       mime is RAW; rows over 25 MB fail with `too_large`;
     - call `replace_photo_bytes(..., source="immich:<id>")`;
     - mark the row `applied` with `applied_uuid`, or `failed` with an
       error code, or `stale` on `PhotoGone`.

     A `PhotoWriteConflict` puts the row back to `proposed` for the next
     batch.
  3. On `QuotaExceeded`, put the row back to `proposed` and stop with
     `apply_status=stopped` and `apply_stopped_reason=storage_full`.
  4. After a batch, enqueue the next one, or set `done`. Without a broker,
     loop in process.
  5. `recover_interrupted_apply(scan_id)`. Each row left `applying` becomes:
     - `applied`, when the photo list holds a photo sourced from its asset;
     - otherwise `proposed`.
  6. `mark_apply_interrupted(scan_id, reason)`:
     - runs the recovery;
     - sets `stopped` with `interrupted`;
     - is registered in `_INTERRUPT_HANDLERS` for `run_apply_batch`.

     The worker's startup sweep applies it to every apply left `running`.
  7. Calling `start_apply` again on a stopped scan resumes it: the remaining
     selected `proposed` rows are taken.
- **Acceptance:**
  - `pytest tests/test_photo_upgrade_apply_job.py
    tests/test_worker_topology.py` passes.
  - Tests cover:
    - 60 rows in 3 batches;
    - each error code;
    - the storage-full stop, then a resume finishing the rest;
    - a photo deleted during the apply becoming `stale`;
    - a write conflict retried in the next batch;
    - recovery from `applying` both ways;
    - the interrupt handler and the startup sweep stopping the apply as
      `interrupted`;
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

#### U10 — Photo-upgrade API
- **Goal:** routes to start a scan, read it, review suggestions page by page,
  change decisions, list a day's candidates, and start or resume an apply,
  plus the hourly sweeps.
- **Scope:**
  - `api/photo_upgrade.py` (new)
  - `api/router.py`, to include the router and register the hourly sweep
  - `tests/test_photo_upgrade_api.py` (new)
- **Context:**
  - Example to follow for project-scoped routes with `?owner=` and roles,
    the enqueue-time worker check (`api/video.py:200-203`) and the
    stale-pending sweep: `api/video.py` and `sweep_video_jobs`.
  - Storage figures for the summary: `src/billing/entitlements.py`.
  - `start_scan`, `start_apply` and `recover_interrupted_apply`, per the
    Conventions' job contract (U9, U11).
  - `rank_candidates` (U5).
  - Decisions D2, D3, D8, D9 and D12; Conventions.
- **Do:**
  1. Starting a scan or an apply answers 503 with "No worker is processing
     photo jobs" when `queue_available()` is true but
     `queue_has_workers(QUEUE_PHOTOS)` is false.
  2. `POST /api/projects/{name}/photo-upgrade/scans` with body `{album_id,
     include_memories, include_journal}`:
     - `include_memories` needs the owner role, otherwise 403;
     - refuses with 409 while the caller has an active scan, or an apply
       `pending` or `running`, on this trip;
     - inserts a new scan row and calls `start_scan`;
     - answers 202 with the scan.
  3. `GET /api/projects/{name}/photo-upgrade/scans/latest` returns:
     - `scan`: the caller's newest `ready` scan, or null, with its summary.
       The summary has counts per state and confidence, the selected count,
       the sum of `asset_bytes − current_bytes` over selected rows, and the
       caller's storage used and limit;
     - `newer`: the caller's newest scan when it is newer and not `ready`,
       with its status, stage, progress and error, or null.
  4. Scan routes, under `/api/photo-upgrade/scans/{id}`, all checked to
     belong to the caller:
     - `GET …/suggestions?filter=&cursor=` pages 100 at a time, ordered by
       date, kind, artifact and position;
     - `PATCH …/suggestions/{sid}` with `{selected?, asset_id?, rejected?}`.
       A chosen `asset_id` must be one of the scan's asset rows within the
       row's date ±1 day. On an applied row it targets `applied_uuid` and
       puts the row back to `proposed` (D5);
     - `POST …/select` with `{which: "confident" | "all_proposed" |
       "none"}`;
     - `GET …/candidates?suggestion_id=` returns the scan's fingerprinted
       asset rows within ±1 day, ranked by `rank_candidates`, for the
       chooser;
     - `POST …/apply` sets `apply_status=pending` and calls `start_apply`.
       This is allowed when the apply is `idle`, `stopped` or `done`, and
       answers 409 while it is `pending` or `running`;
     - `DELETE …` discards the scan, refused while an apply is `pending` or
       `running`.
  5. The hourly sweep in `api/router.py`:
     - deletes scans older than 30 days;
     - fails scans, and stops applies, still `pending` after 24 hours, with
       a neutral message;
     - fails scans `scanning` with a heartbeat older than 30 minutes, as
       `interrupted`;
     - for applies `running` with a heartbeat older than 30 minutes, calls
       `recover_interrupted_apply` and stops them as `interrupted`.
- **Acceptance:**
  - `pytest tests/test_photo_upgrade_api.py` passes, with the job entry
    points faked.
  - Tests cover:
    - the 503 with no worker;
    - an editor refused `include_memories` but allowed journal only;
    - a viewer refused;
    - another user's scan returning 404;
    - `latest` with a newer failed scan beside a ready one;
    - the summary's numbers;
    - paging in trip order;
    - PATCH with an asset from another day refused;
    - PATCH on an applied row;
    - select-confident;
    - resume after `stopped`;
    - 409 on a scan, apply or delete during an apply;
    - each sweep branch.
- **Out of scope:** the job internals (U9, U11); Flutter.
- **Latitude:** local design for response shapes, listed in the report and
  consumed by U12.
- **Escalate if:**
  - `?owner=` resolution cannot express "owner only" the way
    `api/video.py` does;
  - a file outside Scope must change (X3).
- **Depends on:** U6, U9, U11.

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
  - Decisions D4, D9 and D10 (F5's changed adopt behaviour); the OD5
    answer.
  - The release-note rule in `CLAUDE.md` and `docs/RELEASING.md`.
- **Do:**
  1. README:
     - the project-level upgrade;
     - the Immich key permissions it needs (OD5);
     - the 25 MB limit;
     - HEIC converted, RAW per OD3.
  2. ENCRYPTION.md: Immich thumbnails are read on the server to compare
     them, and only their fingerprints are kept, until the scan is
     deleted. Applied originals are stored like any photo.
  3. RELEASING.md owner action: change `worker-poster`'s command on val and
     prod to `poster photos default resolve`, then `docker compose up -d`.
     Check with a scan on val. Until this is done, scans answer 503.
  4. The deployment doc gets the same command.
  5. The plans index gets this plan's row.
- **Acceptance:**
  - `pytest tests/test_release_notes.py tests/test_worker_topology.py`
    passes.
  - Every command in the new owner action matches
    `docker-compose.yml.example`.
- **Out of scope:** the privacy policy. Nothing new is collected, and the
  fingerprints are derived from the user's own photos and deleted with the
  scan.
- **Latitude:** none.
- **Escalate if:**
  - the privacy policy turns out to describe Immich processing in a way
    this changes;
  - a file outside Scope must change (X3).
- **Depends on:** U9, U11.

### Wave 5

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
  3. A 503 with no worker: a sentence saying photo upgrades are not
     available on this server right now.
  4. No scan yet, or "New scan":
     - the album list, with albums overlapping the trip dates first and
       the last-used album preselected;
     - checkboxes for Memories (owner only) and My journal entries, with
       their counts;
     - one line saying nothing changes until you approve;
     - a Scan button.
  5. Scanning: the stage and a progress bar, polled every 2 s while
     visible, stopped when the page is left. When the scan fails while an
     earlier review exists, show that review with a banner giving the
     failure and "Try again".
  6. Review:
     - the summary chips (To check, Confident, No match, Already original,
       Rejected, Failed) double as filters;
     - the list is grouped by day, then by memory or entry, in trip order;
     - each row shows current → proposed, sizes and dimensions, the
       confidence, a tick box, "Choose another" (a sheet of that day's
       album photos), "Not this one" (reject), and a tap-to-compare view
       side by side with zoom;
     - "No match" rows offer "Choose from album";
     - the sticky bar reads "Replace N photos · +X of Y free", blocked with
       an upgrade link when over storage;
     - "Select all confident" and "Clear".
  7. Applying:
     - per-row results arrive as rows flip, and a failed row shows its
       reason;
     - a stop explains itself: storage full, or interrupted, with
       "Resume".
  8. Applied rows keep "Choose another".
  9. The ProjectNotifier reloads the project once at the end, so the new
     photos show.
- **Acceptance:**
  - In the Flutter test container, `flutter test
    test/photos/album_upgrade_*_test.dart` passes.
  - Tests cover:
    - the not-connected card and the no-worker sentence;
    - a companion seeing no Memories checkbox;
    - confident rows ticked by default;
    - filters;
    - choose-another calling PATCH;
    - the bar's numbers and its storage block;
    - polling stopping when the page is left;
    - a failed newer scan shown over the earlier review;
    - the applied, failed, storage-full and interrupted-with-Resume states.
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

## Definition of done

- An owner with Immich connected opens "Upgrade photos from Immich…" on a
  trip, picks an album, and gets suggestions for every memory photo and
  their own journal photos, in trip order. Confident ones are ticked.
- Applying replaces the ticked photos with their originals, in place, with
  the same position, from Immich to the server. The trip shows them after
  one reload.
- A companion can scan and apply only their own journal entries; the API
  refuses memory scope with 403.
- A second scan of the same album proposes nothing already applied and
  nothing rejected. A failed second scan leaves the first review in place.
- Deleting or re-importing photos while an apply runs never loses a photo
  and never leaves a listed photo without its files (D13).
- A worker killed mid-scan or mid-apply leaves the scan failed or the apply
  stopped as interrupted, within 30 minutes at most. The apply can be
  resumed.
- With no worker on the `photos` queue, starting a scan or apply answers
  503 at once.
- No photo job holds the poster worker for more than one short batch.
- An album with more than 10,000 photos in the trip's dates fails the scan
  with a message, never a silent cut.
- A HEIC original is stored and shown as JPEG, upright. A RAW one follows
  OD3. An original over 25 MB fails with "too large".
- Running out of storage stops the apply cleanly, with the remaining rows
  still ticked; resuming after freeing space finishes them.
- F1: `/api/immich/search` returns every page up to its cap, and logs when
  it cuts.
- F2: the per-memory dialog shows a UTC+9 morning photo on its local day.
- F3: a replace that fits by net size is accepted.
- F4: HEIC uploads are accepted, and originals carry their real media type.
- F5: a Polarsteps re-import that adopts a memory deletes none of its
  photos and does not bring back thumbnails whose hashes it knows.
- `alembic heads` shows one head. The worker topology test passes. The val
  compose file runs the `photos` queue.
- Every unit's acceptance tests pass in CI, and `graphify update .` has been
  run from main after merge.
