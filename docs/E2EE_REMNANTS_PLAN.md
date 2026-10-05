# E2EE plaintext remnants — Plan for #504, #505, #506, #366

## Problem

With end-to-end encryption on, content the user expects to be encrypted still
reaches the database in plaintext, or ends up encrypted under the wrong key.
`docs/ENCRYPTION.md` ("Known plaintext remnants") lists most of them; the
investigation for this plan found three more.

- **#504** — plaintext copies left behind or created after enabling:
  1. `stravacache.activities_json` keeps the last Strava list (names,
     endpoints, polylines) for good.
  2. `project.low_res_geo_json` keeps pre-encryption names and endpoints.
  3. `activity.split_base_name` keeps the pre-split plaintext name.
  4. Poster jobs store decrypted memory text in `posterjob.request_json` and
     render it into files nothing ever deletes.
  5. Activities (Strava, GPX, split, `.traxj`/ZIP import) and Polarsteps
     memories added after enabling stay plaintext; nothing re-runs the
     migration.
- **Found during investigation, same family:**
  - **Track edit and split on an encrypted activity** send decrypted points
    (the unlocked panel holds the decrypted track) and the server stores the
    result in plaintext (`_write_track_geometry`,
    `src/project/repo_activities.py:164-242`). Its "before" measurement also
    decodes the stored envelope as a polyline (`:202-205`), which raises or
    returns garbage.
  - **Reset on an activity edited before encryption was enabled wipes its
    track**: the shipped migration nulled `original_*`
    (`encryption_migration.dart:171-179`) and reset copies the nulls into the
    geometry (`repo_activities.py:383-394`). Reachable today on an unlocked
    device.
  - **A split tail of an encrypted head** is named `f"{head.name} (2)"`
    (`repo_activities.py:561`), i.e. `"v1.<a>.<b> (2)"`: still an envelope to
    the server's structural check, undecryptable by the client.
  - **The migration aborts on a companion-imported activity**:
    `PUT /api/activities/{id}` answers 404 for a row the caller does not own
    (`api/activities.py:1976`), `_migrateActivity` has no per-activity error
    handling, and `enable_encryption_screen.dart:133` swallows the failure.
- **#505** — an encrypted companion's memories in a plaintext owner's trip
  are encrypted under the companion's key; the owner and other companions see
  "Encrypted content unavailable". Separately, an invite created before the
  owner enabled encryption can still be accepted afterwards.
- **#506** — `EncryptionService.protect()` passes plaintext through when the
  device is locked; a device awaiting approval or a web session before
  recovery saves memories and journal entries in the clear, silently.
- **#366** — the #260 backfill skipped activities whose elevation profile is
  an envelope; they keep the pre-#260 inflated `total_elevation_gain`
  (and, via the later repairs, possibly the 0.0 dropout sentinel).

## Current state

- **Encryption flag:** `UserInfo.encryption_enabled` (`models/user.py:64`),
  set by `api/encryption.py:138`, exposed only through
  `GET /api/encryption/status`. There is no way to turn it off.
- **Client service:** `encryption_service.dart` holds only `SecretKey? _cmk`
  (`isUnlocked`, `:112-115`). `prepareForSession` (`:198-209`) fetches the
  status, registers a pending device, and throws the status away;
  `AuthNotifier._unlockEncryption` (`auth_notifier.dart:293-297`) ignores the
  result. No UI says "locked" or "awaiting approval".
- **Write paths:** memory create/update
  (`project_memory_crud_mixin.dart:89-91`, `:165-169`) and journal
  create/update (`project_journal_crud_mixin.dart:91-92`, `:161-164`) call
  `protect` unconditionally. Polarsteps import
  (`polarsteps_import_notifier.dart:311-331`) and sync import
  (`sync_import_notifier.dart:152-168`) do not call it. The server stores
  `name`/`description` as sent (`api/memories.py:305-457`,
  `api/journal.py:255-384`); `_adopt_and_refresh` (`api/memories.py:270-289`)
  overwrites them on Polarsteps re-import.
- **Scope of content:** memories are shared with the trip
  (`api/memories.py:221-229`, role check only, no author column on
  `DBMemory`); journal entries are private to their author
  (`api/journal.py:84-104`).
- **Ownership on the client:** use `role == 'owner'` from the server's
  `caller_role` (`project_notifier.dart:1117`), never `ProjectRef.isOwn`
  (false on your own trip when the list sends `owner_id`,
  `api/projects.py:142`).
- **Companion guards:** `create_invite` refuses when the owner is encrypted
  (`api/members.py:273-279`); `accept_invite` (`:566-616`) checks nothing;
  `enable` refuses while trips you own have members (`api/encryption.py:108-121`).
- **Migration:** `EncryptionMigration.run()`
  (`flutter_client/lib/src/crypto/encryption_migration.dart:49-79`) is
  idempotent per field (`_isPlain`, `:81-82`, `:141-146`; endpoints/elevation
  come back empty once enveloped), skips shared trips (`:55-60`), and is
  triggered only from `enable_encryption_screen.dart:128-133`.
  `ProjectNotifier._revealActivities` (`project_notifier.dart:3302`) already
  walks every loaded activity when unlocked.
- **Strava cache:** `DBStravaCache` (`models/project_db.py:798-810`), written
  by `_save_cache` (`api/strava.py:125-147`) from `strava_activities`,
  `strava_sync` and `sync_check` (`api/projects.py:978`); read by the picker
  (`api/strava.py:372-484`) and the sync banner (`api/projects.py:945-1017`).
  Useful only within `STRAVA_CACHE_TTL` (3600 s); `_invalidate_cache`
  (`:150`) has no callers.
- **`low_res_geo_json`:** written by `repo_core.py:288`, `:408` and
  `repo_transfer.py:833`; read by nothing in production (`api/geo.py:682-728`
  recomputes). Listed in `_PROJECT_INFRA_FIELDS`
  (`models/project_db.py:120`), checked by `_check_schema_contract` at
  startup.
- **`split_base_name`:** database-only, used by `_renumber_split_family`
  (`repo_activities.py:434-469`), which already returns early for an
  enveloped root name (`:449`).
- **Posters:** `DBPosterJob` (`models/project_db.py:594-622`) stores the full
  request; files under `data/users/<uid>/posters/<job_id>/`
  (`poster_job_runner.py:65-68`); only an account deletion removes them.
  The trip-video equivalent is `sweep_video_jobs`
  (`src/video/job_runner.py:347-402`, hourly at :40, `RETENTION_DAYS = 30`)
  and its consent flow (`docs/ENCRYPTION.md`, "Trip videos").
- **Elevation:** `elevation_gain` (`src/models/track_edit.py:599-657`) with
  the six `ELEV_*` constants (`:342-382`) and helpers `_noise_estimate`,
  `_run_stride`, `_smooth_elevations`, `_noise_threshold`. The backfill
  `alembic/versions/b7f1a3c9d204_*` selected `is_edited = 1 OR source = 'gpx'`
  and skipped envelopes; `c4a9e1f70b38_*` (0.0 dropout sentinel) also skipped
  them. Profile format `{"distances_km": [...], "elevations_m": [...]}`; for
  encrypted rows the low-res column holds the same envelope as the full one
  (`encryption_migration.dart:159-167`). The client has no gain code; the
  closest parity precedent is `flutter_client/test/elevation_codec_test.dart`
  (vectors generated in Python), and `tests/test_encryption_doc_coverage.py`
  (Python test parsing Dart source).
- **Track editing (server):** `PUT …/track` → `_write_track_geometry`
  (`repo_activities.py:163-242`) computes every metric from plaintext
  points with `recompute_track_metrics` (`src/models/track_edit.py:1000-1073`):
  haversine distance (R = 6371 km), gain, elev high/low, endpoints, and
  moving/elapsed time scaled by `new distance / old geometry's distance`;
  gain by `_apportion_gain` (`:98-109`). The first edit snapshots only
  `original_polyline`, `original_elevation_profile_json` and
  `original_total_elevation_gain`. Split (`:471-653`) allocates a negative
  53-bit tail id (`src/project/local_ids.py:65-79`), seeds the tail with the
  head's stored geometry and times, shifts the tail's start by the head's
  apportioned elapsed time, inserts its item after the head, and renames the
  family (skipped for an enveloped root, `:449`). Reset (`:347-431`) restores
  the original geometry verbatim and **recomputes** distance and endpoints
  from it, recovering times by a distance ratio; it has no body and no
  lock-version check (`api/activities.py:1695-1741`). `GET …/track`
  (`:1581-1610`) loads the whole project with heavy columns to return one
  activity.
- **Track editing (client):** `track_edit_model.dart` (186 lines) and
  `track_editor_controller.dart` edit points; nothing computes distance,
  time or gain on the device. The editor opens from the panel's in-memory
  copy when it has a polyline (`activity_panel.dart:812`), otherwise from
  `GET …/track`, and blocks envelopes (`:839-849`). A Dart polyline encoder
  exists in the video module (`video_job_notifier.dart:34-55`); haversine
  with R = 6371 exists (`photos/photo_match.dart:150`).
- **`PUT /api/activities/{id}`** (`api/activities.py:1955`,
  `ActivityFieldsUpdate` `:1929-1952`): six E2EE columns plus two `original_*`
  snapshots; bumps the lock version, re-prepares geometry and queues a stats
  refresh (`:1988-2011`). It is the pattern to copy, and it is not widened.

## Decisions

1. **The trip owner's key encrypts a trip's memories; the author's key
   encrypts their journal entries (#505, owner decision).** The client calls
   `protect` on memory `name`/`description` only when `role == 'owner'`, and
   on journal `description` whenever the caller's account is encrypted
   (journal entries are author-private, so the author's key is correct
   anywhere). This matches the migration, which already skips shared trips,
   and stays correct when #108 key sharing lands (#108 would make the owner's
   key reachable to companions). Rules out refusing encrypted users as
   companions, which locks them out of companionship until #108.
2. **Repair of #505 damage by the companion's own client.** Memories have no
   author column, so the server cannot find them. On loading a trip it does
   not own, an unlocked client re-saves as plaintext every memory whose
   envelope decrypts under its own key: successful decryption is the proof
   of authorship. Rules out an Alembic author backfill (no data to backfill
   from).
3. **Server backstop on memory and journal writes (#505 + #506, owner
   decision).** On create/update (and `_adopt_and_refresh`), for each of
   `name` and `description` that is non-empty:
   - **memory**: the trip owner is encrypted and the value is not an
     envelope → 409 `encryption_locked`; the trip owner is not encrypted and
     the value is an envelope → 409 `encryption_not_shared`.
   - **journal**: the same two rules keyed on the author (the caller).
   
   This stops old installed builds (E5) too. Accepted cost: a locked device
   cannot edit a legacy plaintext memory on an encrypted trip at all, even
   its date, since `PUT` resends the stored text. Rules out a client-only
   gate.
4. **`accept_invite` refuses when the trip owner is now encrypted** (409, same
   message as `create_invite`). Closes the "invite made before enabling"
   gap; one check.
5. **The client knows its encryption state (#506).** `EncryptionService`
   keeps the last status as a state: `disabled`, `unlocked`, `locked`
   (enabled, device approved but key not unwrapped), `awaitingApproval`.
   When encrypted and not unlocked:
   - a banner on the trip screen says so and links to Manage devices /
     Recover;
   - memory and journal dialogs disable Save with the same message;
   - Polarsteps import and sync-import refuse to start.
   
   Rules out queueing writes until unlock (needs a persistent outbox the web
   client does not have).
6. **Catch-up encryption on load, not only at enable time (#504.5).** After
   an owned trip loads on an unlocked device of an encrypted account, the
   client runs the migration's per-trip step for that trip: plaintext
   activity fields, memories and journal entries are encrypted and written
   back. This covers Strava/GPX/split/`.traxj` arrivals and legacy Polarsteps
   memories, wherever they came from. The migration and the catch-up share
   one per-trip function, and:
   - it reads the trip's payload **as received, before reveal**: the loaded
     maps are decrypted in place, so feeding them would re-encrypt every row
     on every load and hide which memories were envelopes (R1-9);
   - **activities are never encrypted from the trip payload.** Ordinary
     loads receive `/meta`, which has no polyline and only the ~300-point
     profile; encrypting that would leave the track plaintext and write the
     downsample over the full profile (R3-1). Instead the server lists, per
     activity, `plain_fields`: every E2EE column (name, polyline, endpoints,
     both profiles, and the four geometry snapshots `original_polyline`,
     `original_elevation_profile_json`, `original_start_latlng_json`,
     `original_end_latlng_json`) holding a non-null, non-envelope value.
     The envelope test is case-sensitive, like the Python and Dart ones
     (R5-5). It is computed from the row on
     every payload, `/meta` included. For each activity with a non-empty
     list, the pass fetches `GET …/track` — full track, full profile and the
     edit snapshots — and encrypts from that. Memories and journal entries
     are complete in `/meta` and are encrypted from the payload. `plain_fields`
     is computed in the query, without loading the deferred heavy columns
     (R4-6);
   - plaintext `original_polyline`/`original_elevation_profile_json` are
     **encrypted, never nulled**, so Reset keeps working on edited rows
     (R1-1). `GET …/track` returns them only to callers with the editor
     role or above, the role Reset requires (R2-4, R3-4);
   - on a trip the user does not own, it encrypts the user's own plaintext
     journal entries (they are author-key, decision 1) and runs the memory
     repair of decision 2 (R1-4);
   - the pass holds **one** expected lock version: the payload's, advanced
     only by its own successful writes. Every write carries it (decision
     13); a `GET …/track` response whose `lock_version` differs from it means
     someone else wrote, and ends the pass like a 409 (R4-1). A failed write
     skips that row only, a stale write (409 `stale_write`) ends the pass,
     and the next load retries from fresh data (R2-9);
   - it runs from one hook shared by every place that applies a loaded trip
     (`load()`, `_applyRefreshedProject`, `_applyDetails`), so opening a
     trip is enough (R2-7);
   - an activity row the server refuses to let the user encrypt
     (decision 14) is counted and shown on the trip as "N activities were
     imported by another traveller and stay unencrypted". Polarsteps
   import also calls `protect` directly so new memories never arrive
   plaintext from an unlocked device. Rules out server-side encryption (the
   server has no key, E6) and a one-shot "re-run migration" button (users
   would not know to press it).
7. **Track edit, split and reset of an encrypted activity are computed on
   the device (owner decision, after review round 4).** The server never
   receives a decrypted track. Rules out editing on the server with
   plaintext in transit (rounds 1–4 found 12 defects in it: R1-1, R1-3,
   R2-4–R2-6, R3-2–R3-4, R4-2–R4-4, R4-9) and blocking editing (loses the
   feature).
   - **Port.** The client gets a Dart port of the server's track maths
     (`track_metrics/`): haversine (R = 6371 km), `align_points`,
     `interpolate_elevation_gaps`, `points_to_elevation_profile`,
     the dropout-sentinel detection rule (`_sentinel_mask` of migration
     `c4a9e1f70b38`, needed by U8),
     `elevation_gain` with its helpers and constants (decision 12),
     `recompute_track_metrics` with its time apportioning, and
     `_apportion_gain`; the polyline encoder moves out of the video module
     into it. Python is the source of truth, pinned by vectors (decision 12).
     Before and after are measured by the same port, so edit→reset
     round-trips exactly (R2-6, R4-3 no longer arise).
   - **Open.** On an encrypted activity the editor always fetches
     `GET …/track` and decrypts its envelopes, never the panel's in-memory
     copy, so it holds the current full geometry and stored metrics (R3-2).
   - **Save.** The client computes what `_write_track_geometry` computes:
     "before" = metrics of the opened geometry, new metrics with times
     apportioned against it, gain by `_apportion_gain` from the stored gain.
     It encrypts polyline, profile (one envelope written to both profile
     columns) and endpoints, and sends them with the scalars (`distance`,
     `moving_time`, `elapsed_time`, `average_speed`, `total_elevation_gain`,
     `elev_high`, `elev_low`) to `PUT /{name}/activities/{id}/track/encrypted`.
   - **Split.** The client computes head and tail as `split_activity` does
     (both pieces apportioned against the opened geometry and its stored
     times) and sends both pieces, plus the tail's name encrypted from the
     decrypted head name, to `POST /{name}/activities/{id}/split/encrypted`.
     The server keeps the bookkeeping: tail id, `split_root_id`/
     `split_parent_id`, item insertion, the tail's start shifted by the
     head's elapsed time (plaintext dates), snapshots, family renaming
     (still skipped for an enveloped root).
   - **Server checks on both routes:** `lock_version` required (CAS); the
     stored polyline must be an envelope (409 `not_encrypted`); the caller
     passes the same edit permission as today's routes; every geometry value
     is a well-formed envelope under a **strict** check mirroring
     `EncryptedField.isWellFormed` (version `v1`, two non-empty parts in
     **standard** base64 with padding, as `EncryptedField.encode` writes
     them — R5-2), not the loose `is_encrypted_envelope` — so R1-10 does not
     reach these fields; scalars finite, non-negative, within
     `implausible_track`'s bounds and `value_bounds`; `elev_low ≤ elev_high`.
     A track with no elevations sends `elevation_profile`, `elev_high` and
     `elev_low` all null, as the server stores them for such a track today;
     they are null together or not at all (R5-4).
   - **Snapshots carry the scalars.** The first edit — plaintext or
     encrypted path — also snapshots `distance`, `moving_time`,
     `elapsed_time`, `average_speed`, `elev_high`, `elev_low`,
     `start_latlng_json`, `end_latlng_json` into new `original_*` columns. A
     split tail's snapshot is its own post-split values, as #131 does for
     geometry. The migration backfills them for rows already edited whose
     originals are plaintext, computing them from the original geometry the
     way reset does today.
   - **Reset is server-only and needs no geometry.** Geometry columns get
     the `original_*` values verbatim — an enveloped original profile goes
     to **both** profile columns, since the low-res copy cannot be derived
     from ciphertext (R5-3); when the scalar snapshot exists, the
     scalars are restored from it (exact, both paths); a plaintext row
     without it keeps today's recompute. An edited row whose
     `original_polyline` is null (the shipped migration nulled it, R4-2), or
     whose originals are envelopes with no scalar snapshot, answers 409
     `nothing_to_restore` and is left unchanged. Reset accepts an optional
     `lock_version` (CAS, R4-9).
   - **Old routes refuse envelopes.** `PUT …/track` and `POST …/split`
     answer 409 `encrypted_edit_on_device` for a row whose stored polyline or
     profile is an envelope, so an old app build, which would send decrypted
     points, gets an error instead of storing plaintext (R4-4 no longer
     arises).
8. **Strava cache: in memory for encrypted accounts.** For a user with
   encryption on, `_save_cache`/`_load_cache` use an in-process dict keyed by
   user id with the same TTL, and never write `DBStravaCache`; enabling
   encryption deletes the user's row; the migration deletes existing rows of
   encrypted users. One API process (E1) makes the in-process cache
   sufficient. Rules out scrubbing names/polylines from the cache (the picker
   shows names) and deleting after each use (repeat Strava calls inside the
   hour, rate limits).
9. **Drop `project.low_res_geo_json`.** No reader; removing it removes the
   remnant for every user. Its writers and the `_PROJECT_INFRA_FIELDS` entry
   go. Rules out nulling it on encryption (a dead column kept only to be
   kept clean).
10. **`split_base_name` is nulled once the name is an envelope.**
    `PUT /api/activities/{id}` nulls it when it stores an envelope `name`;
    the migration nulls it on rows whose name is already an envelope.
    Renumbering already ignores it for enveloped roots.
11. **Posters: consent and retention (owner decision).**
    - `POST` poster create answers 409 `consent_required` when the trip owner
      is encrypted, the request carries any memory `name`/`description`
      text, and `plaintext_consent` is not true. The client asks the user,
      then resends with `plaintext_consent: true`.
    - The preview endpoint asks no consent: it renders and stores nothing,
      so its plaintext is received by design (R1-7).
    - When a job ends (done or failed), `request_json` is replaced with a
      scrubbed copy (no memory text, no coordinates).
    - An hourly sweep deletes poster job rows and files 30 days after
      `completed_at`, for every user, shaped like `sweep_video_jobs`.
    Rules out excluding memory text from encrypted posters.
12. **#366: a dedicated narrow endpoint and a Dart port pinned by Python.**
    The port is the `track_metrics/` library of decision 7; its vectors also
    cover `align_points`, `interpolate_elevation_gaps`,
    `points_to_elevation_profile`, `recompute_track_metrics` (with original
    distance and times), `_apportion_gain` and polyline encoding.
    - `PUT /api/activities/{id}/elevation-gain`, body
      `{"total_elevation_gain": float}`: accepted only when the caller owns
      the row (same check as `PUT /api/activities/{id}`) and its
      `elevation_profile_json` is an envelope; the value must be finite,
      ≥ 0 and ≤ 50 000. It bumps the lock version and queues the stats
      refresh like the existing PUT. `PUT /api/activities/{id}` is not
      widened.
    - With that port, the client recomputes, during the
      decision-6 pass, for encrypted activities with `is_edited` or
      `source == 'gpx'` (the backfill's selection), writes the repaired
      profile back (re-encrypted, through the existing PUT) when the sentinel
      was present, and posts the gain only when it differs by more than
      0.5 m (idempotent, no marker column).
    - Parity ("shared, not duplicated", #366 AC2): the Python constants are
      the source of truth. A pytest parses the Dart constants and fails
      unless they equal `track_edit.py`'s; a Python script writes
      `flutter_client/test/fixtures/track_metrics_vectors.json` (series →
      expected gain, including noisy, stepped and sentinel cases from
      `tests/elevation_bench/`), a pytest fails when the committed file no
      longer matches Python's output, and a Dart test asserts the port
      reproduces every vector. Rules out a shared JSON params file read by
      both (the server image does not ship `flutter_client/`) and a server
      config endpoint (an extra request for six constants).
    - The value written is the absolute smoothed gain of the current
      profile, as the plaintext backfill wrote. For edited Strava activities
      that differs from #386's apportioned figure; for encrypted rows the
      original Strava figure is not recoverable from the profile, so absolute
      is the only consistent choice.
13. **Catch-up writes are compare-and-swap on the trip's lock version
    (R1-2).** `PUT /api/activities/{id}`, `PUT /api/memories/{id}`,
    `PUT /api/journal/{id}`, `PUT /api/activities/{id}/elevation-gain` and
    track reset accept an optional `lock_version` (and, for the activity routes, the trip
    `project` it refers to). When present, the write uses
    `check_and_bump_lock_version` on that trip instead of
    `bump_lock_version`, answers 409 `stale_write` on mismatch, and returns
    the new `lock_version`. The catch-up sends it on every write and chains
    the returned value; a 409 ends the pass, and the next load retries from
    fresh data. Absent = no check, but the write still advances the lock
    version: memory and journal updates do not today, which breaks the
    #173 rule and would leave a plain save invisible to the catch-up's check
    (R2-1). Old clients are unaffected.
    Precedent: `SplitRequest.lock_version`. Rules out a per-field "if
    unchanged" hash (protects the encrypted fields but not a concurrent date
    or geometry change carried by the same `PUT`).
14. **An owner may encrypt a local activity row every trip of which is
    theirs (R1-5, R2-2, R2-3).** `PUT /api/activities/{id}` (and the
    elevation-gain route) accept the caller when they own the row, **or**
    when the row is local (negative id: GPX import, split piece), at least
    one project references it, and every project that references it is
    owned by the caller. A Strava row (positive id) owned by another user
    stays refused even when only the caller's trips reference it: its owner's
    next Strava sync reuses the row, and it would come back to them encrypted
    under someone else's key. Refused rows are counted and shown (decision
    6), and `ENCRYPTION.md` lists them as a remaining plaintext case. The
    non-empty requirement stops an unreferenced row from being written by
    anyone. Rules out encrypting Strava rows under the owner's key and
    deleting or detaching them on companion removal (a data change outside
    this package's intent).

## Review envelope

REVIEW.md defaults apply, with:

- **Database: SQLite only** (owner decision 2026-09-28).
- **E6 restated for this package:** plaintext the server receives *by
  design* — Strava's own API responses, poster text sent after consent — is
  out of scope as a leak. A track edit of an encrypted activity is not in
  that list: the server must never receive its decrypted points.
  What is in scope: anything the server **stores** in plaintext beyond the
  window between that receipt and the client's catch-up write, and anything
  stored under the wrong key.
- **Catch-up window:** content created server-side (Strava sync, GPX, split,
  `.traxj` import) is plaintext until an unlocked owner's device next loads
  the trip. That window is accepted; a finding that only shows the window
  exists is D1. A finding that shows the catch-up never closes it (a path it
  misses, a row it can't write) is in scope.
- **Concurrency:** the catch-up pass can run on two devices of the same user
  at once, and race a companion's or the server's write to the same row.
  Catch-up writes are compare-and-swap on the trip's lock version
  (decision 13); a refused write is retried on the next load. A race that
  reverts another write, leaves a row plaintext *permanently* or stores a
  mixed (half-encrypted) row is in scope.
- **Startup/scheduled work:** the poster sweep runs hourly in the single API
  process (E1), must be safe to interrupt and re-run, and must never delete a
  pending or running job's files.
- **Trust:** a companion is trusted as a trip member, but the server must not
  let one store content under a key the owner cannot use (decision 3).
- **Client-supplied metrics** (decision 7's encrypted routes, decision 12's
  gain) are untrusted input (E2): bounds-checked, and accepted only for rows
  the caller may already edit. A user misstating their own activity's
  figures is out of scope; a value that breaks a server invariant (export
  bounds, stats, another user's data) is in scope.

## Boundaries crossed

- **API contract:**
  - New 409s: memory/journal create and update (`encryption_locked`,
    `encryption_not_shared`), `_adopt_and_refresh` via memory create,
    `accept_invite`, `PUT …/track` and `POST …/split` on an enveloped row
    (`encrypted_edit_on_device`), reset with nothing to restore
    (`nothing_to_restore`), poster create (`consent_required`). Old installed builds show their generic error for
    these (E5): on a locked device of an encrypted account, on a companion
    write from an encrypted account, and for posters with memory text on an
    encrypted trip — each of which is the leak this package closes.
  - New endpoints `PUT /{name}/activities/{id}/track/encrypted`,
    `POST /{name}/activities/{id}/split/encrypted` and
    `PUT /api/activities/{id}/elevation-gain`.
  - Old installed builds can no longer edit or split an encrypted activity
    (409; today they store it in plaintext); reset keeps working for them.
  - `PUT /api/activities/{id}` also accepts `original_start_latlng_json` and
    `original_end_latlng_json` (two more E2EE snapshot columns, same rules
    as the existing two).
  - Optional `lock_version` (and `project` for activities) on
    `PUT /api/activities/{id}`, `PUT /api/memories/{id}`,
    `PUT /api/journal/{id}`, with a new 409 `stale_write` and the new
    `lock_version` in the response; absent keeps today's behaviour.
  - `PUT /api/activities/{id}` accepts a caller who owns every trip using the
    row (decision 14) where it used to answer 404.
  - `GET …/track` additionally returns the four geometry `original_*`
    columns for edited rows to editors and above; the trip payload (`/meta`
    and full) gains `plain_fields` on activities, emitted by
    `ProjectIO.to_dict`, not `to_strava_dict`, so the `.traxj` export is
    unchanged.
  - Memory and journal updates now advance the trip's lock version, so
    other devices' caches see them as changes.
  - Track reset accepts an optional `lock_version` body field.
  - Poster create body gains optional `plaintext_consent: bool` (absent =
    false).
  - `GET /api/encryption/status` is unchanged; the client reads more of it.
- **Schema:** one Alembic migration: drop `project.low_res_geo_json`; null
  `activity.split_base_name` where `activity.name` is an envelope; delete
  `stravacache` rows of users with `encryption_enabled`; add eight nullable
  `activity.original_*` scalar snapshot columns and backfill them for edited
  rows with plaintext originals. The column drop is
  irreversible in data but the column has no reader; downgrade re-adds it
  empty.
- **Stored data:**
  - Clients rewrite existing rows: plaintext → envelope on owned trips
    (catch-up, edit snapshots included), envelope → plaintext for a
    companion's own memories in a plaintext trip (repair), corrected gain and
    repaired profile on encrypted activities.
  - The encrypted field list grows: `original_polyline`,
    `original_elevation_profile_json`, `original_start_latlng_json`,
    `original_end_latlng_json` become encrypted columns (they were nulled).
  - Poster job rows and files older than 30 days are deleted; finished jobs'
    `request_json` is scrubbed.
- **Export formats:** none. `.traxj`/ZIP export never included
  `low_res_geo_json`; to be confirmed by U4 (escalate if it does). The
  function `_compute_low_res_geo` stays: `api/geo.py:728` computes the
  low-res response from it on every request (and `project_repo.py:35`
  re-exports it); only the column and its three stored writes go (R1-6).

## Conventions

- Every new test is proved to fail on the code before the change; the unit's
  report says how.
- Server tests that need concurrency use a file-backed SQLite in `tmp_path`
  (`tests/test_strava_disconnect.py`), never `StaticPool`.
- Every write to a project's rows advances `lock_version` in SQL before item
  rows, as `PUT /api/activities/{id}` does.
- Client ownership checks use `role == 'owner'`, never `ProjectRef.isOwn`.
- Client tests that need the real `encryption` singleton follow
  `test/projects/encrypted_dialog_fields_test.dart` (mock secure storage,
  `encryption.enable(...)`, `lock()` in tearDown); service-level tests use
  the `FakeDeviceKeyStore`/`FakeEncryptionApi` fakes of
  `test/crypto/encryption_service_test.dart`.
- Server error details follow the video consent 409's shape
  (`api/video.py`): a fixed `code` string plus ids, never content.
- No graphify output is committed from a unit's worktree.
- `docs/ENCRYPTION.md` is edited only by U10, except the "What is
  encrypted" table rows, which U7 updates with the field-list change so
  `tests/test_encryption_doc_coverage.py` stays green.
- Commits carry `Closes #N` for the issue a unit finishes (U10 closes all
  four) and a `Release-Note:` trailer in user vocabulary.

## Open decisions

- **Poster retention period** — 30 days to match videos. Affects U5. Decide
  before U5 starts; default 30.
- **Upper bound for the client-written gain** — 50 000 m. Affects U3. Default
  stands unless the owner objects.
- **Follow-up issues to file after delivery** (not part of this package):
  #108 key sharing (would let companions encrypt into an encrypted owner's
  trip).

## Execution units

### Wave 1 — server

#### U1 — Memory, journal and invite guards

- **Goal:** the server refuses plaintext into an encrypted space and envelopes
  into a plaintext owner's trip, and refuses accepting an invite to a now
  encrypted owner.
- **Scope:** `api/memories.py`, `api/journal.py`, `api/members.py`,
  `tests/test_memory_encryption_guard.py` (new),
  `tests/test_journal_encryption_guard.py` (new),
  `tests/test_project_members.py`.
- **Context:** decisions 3 and 4; `is_encrypted_envelope`
  (`src/utils/encryption_check.py:18-28`, already imported in
  `api/memories.py:65`); follow the existing owner-encryption check in
  `create_invite` (`api/members.py:273-279`) for how the owner's
  `UserInfo` is loaded; tests shaped like
  `tests/test_project_members.py:505-529`.
- **Do:**
  1. Add one helper (in `api/memories.py`, reused by `api/journal.py`) that,
     given the encryption flag of the key holder and the two text values,
     returns the 409 code or None. Empty/None values are exempt.
  2. Memory create, update and the `_adopt_and_refresh` path: key holder =
     trip owner (`project_row.user_info_id`).
  3. Journal create and update: key holder = caller.
  4. `accept_invite`: 409 when the trip owner's `encryption_enabled` is true,
     same message as `create_invite`.
  5. 409 detail: `{"code": "encryption_locked" | "encryption_not_shared"}`.
  6. Decision 13 for `PUT /api/memories/{id}` and `PUT /api/journal/{id}`:
     optional `lock_version`; when present, `check_and_bump_lock_version` on
     the row's project, 409 `{"code": "stale_write"}` on mismatch; when
     absent, `bump_lock_version` (neither update bumps today — R2-1). The
     response carries the new `lock_version` either way.
- **Acceptance:** new tests for each of: owner-encrypted memory create with
  plaintext → 409 locked; with envelope → 200; plaintext-owner memory create
  with envelope by a companion → 409 not_shared; plaintext → 200; update
  variants of both; empty name/description on an encrypted trip → 200;
  journal by an encrypted author with plaintext → 409, envelope → 200;
  journal by a plaintext author with envelope → 409; Polarsteps re-import
  adopting plaintext into an encrypted trip → 409; `accept_invite` after the
  owner enabled → 409; memory and journal update with the current
  `lock_version` → 200 and the returned version is one higher, with a stale
  one → 409 `stale_write` and the row unchanged, without one → 200 and the
  project's version advanced by one. `pytest tests/test_memory_encryption_guard.py
  tests/test_journal_encryption_guard.py tests/test_project_members.py
  tests/test_memories*.py tests/test_journal*.py` passes.
- **Out of scope:** activities; client handling of the 409s (U6).
- **Latitude:** local design.
- **Escalate if:** an existing test creates plaintext memories on an
  encrypted trip and would need its intent changed rather than its setup;
  a file outside Scope is needed.
- **Depends on:** —

#### U2 — Activity routes: refusals, reset fixes, CAS, ownership, /track

- **Goal:** the server stops taking decrypted tracks for encrypted rows,
  reset never wipes a track and can be compare-and-swapped,
  `PUT /api/activities/{id}` supports the catch-up's CAS and the
  owner-of-every-trip rule, and `GET …/track` is a cheap single-row read
  that gives editors the edit snapshots.
- **Scope:** `api/activities.py` (the `/track` GET and PUT, split, reset and
  `update_activity_fields` handlers, and the ownership helper they share),
  `src/project/repo_activities.py` (reset and the activity-write helpers
  only), `tests/test_activity_encrypted_refusals.py` (new),
  `tests/test_activity_fields_update_cas.py` (new),
  `tests/test_activity_reset.py` (new or existing reset tests),
  `tests/test_activity_track_get.py` (new).
- **Context:** decisions 7 (last two bullets), 10, 13, 14; reset
  (`repo_activities.py:347-431`); CAS precedent `check_and_bump_lock_version`
  as split uses it (`:530-533`); single-row load precedent in
  `edit_activity_track` (`api/activities.py:1655-1660`, containment via
  `_require_rewritable_by_trip`); role check `resolve_project(...,
  min_role=...)` as reset uses it (`:1712`).
- **Do:**
  1. `PUT …/track` and `POST …/split`: 409
     `{"code": "encrypted_edit_on_device"}` when the stored polyline or
     profile is an envelope, before any decoding.
  2. Reset: optional body `{lock_version}` → CAS (409 `stale_write`); an
     edited row whose `original_polyline` is null → 409
     `{"code": "nothing_to_restore"}`, row unchanged.
  3. `PUT /api/activities/{id}`: null `split_base_name` when the stored
     `name` becomes an envelope (decision 10); optional `project` and
     `lock_version` per decision 13 (row must be in that project, else 404;
     CAS on it; other projects containing the row bumped as today; response
     carries the new `lock_version`).
  4. Decision 14: one helper "caller may write this activity's E2EE fields"
     — owns the row, or the row is local (negative id), referenced by at
     least one project, and every referencing project is the caller's —
     used by `update_activity_fields` (and by U12's gain route).
  5. `GET …/track`: load only the one activity's row (containment checked
     without loading the project's heavy columns, R4-7); return
     `original_polyline` and `original_elevation_profile_json` as stored
     when the row `is_edited` and the caller's role is editor or above
     (R3-4); `lock_version` as today. The two latlng snapshots, whose
     columns U4 adds in this wave, are added to the response by U12 (R5-1).
- **Acceptance:** tests: `PUT …/track` and split on a row with an envelope
  polyline (and on one with only an envelope profile) → 409 and the polyline
  decoder never called (patch it to raise), row unchanged; plaintext rows
  unchanged in behaviour; reset with current `lock_version` → 200, stale →
  409 and row unchanged, absent → 200 as today; reset of an edited row with
  null `original_polyline` → 409 and geometry unchanged; PUT with an
  envelope name nulls `split_base_name`; PUT CAS current/stale/wrong
  project → 200 version+1 / 409 unchanged / 404; decision 14 cases: local
  row of another user referenced only by the caller's trips → 200,
  positive-id row of another user referenced only by the caller's trips →
  404, local row referenced by no project → 404, row also in another user's
  trip → 404; `GET …/track` returns originals to an editor, not to a
  viewer, none for an unedited row, and issues no query loading other
  activities' polylines (assert on statements or with a heavy-column
  sentinel). Existing `tests/test_activity_*` pass.
- **Out of scope:** the encrypted edit/split routes, snapshot scalars and
  `plain_fields` (U12); client changes.
- **Latitude:** local design.
- **Escalate if:** a plaintext-row behaviour has to change to satisfy a
  check; a file outside Scope is needed.
- **Depends on:** —

#### U4 — Strava cache, low-res geo column, split base name migration

- **Goal:** no plaintext Strava list is persisted for encrypted accounts, the
  dead `low_res_geo_json` column is gone, stale `split_base_name`s on
  encrypted rows are cleared, and the eight scalar snapshot columns exist and
  are backfilled.
- **Scope:** `alembic/versions/<new>_e2ee_remnants.py`,
  `models/project_db.py`, `src/project/repo_core.py`,
  `src/project/repo_transfer.py`, `api/strava.py`, `api/projects.py`
  (`sync_check` only), `api/encryption.py` (`enable` only), `tests/` files
  that reference `low_res_geo_json`
  (`tests/test_import_encrypted_activity.py` and any other found),
  `tests/test_strava_cache_encrypted.py` (new),
  `tests/test_migration_e2ee_remnants.py` (new).
- **Context:** decisions 7 (snapshots), 8, 9, 10; backfill computed the way
  reset recomputes today (`repo_activities.py:383-424`: `align_points` on the
  original geometry, metrics from it, times by the distance ratio) and shaped
  like `alembic/versions/b7f1a3c9d204_*` (Python loop, imports app code,
  skips envelopes); migration shaped like
  `alembic/versions/a442d0e1f2b3_drop_heart_rate_data.py` (data scrub then
  `batch_alter_table` drop); `_check_schema_contract` and
  `_PROJECT_INFRA_FIELDS` (`models/project_db.py:120`); cache functions
  `api/strava.py:89-150`.
- **Do:**
  1. Migration: delete `stravacache` rows whose user has
     `encryption_enabled`; null `split_base_name` where `name LIKE 'v1.%'`
     and the value passes the envelope structure check (do it in Python, as
     the elevation backfill does); drop `project.low_res_geo_json`; add
     nullable `activity.original_distance`, `original_moving_time`,
     `original_elapsed_time`, `original_average_speed`, `original_elev_high`,
     `original_elev_low`, `original_start_latlng_json`,
     `original_end_latlng_json`, and backfill them for `is_edited` rows whose
     `original_polyline` is non-null and not an envelope; rows with null or
     enveloped originals stay null. Downgrade re-adds `low_res_geo_json`
     nullable and drops the eight columns.
  1b. Add the eight fields to `DBActivity` (no writers here; U12 writes them).
  2. Remove `low_res_geo_json` from the model and `_PROJECT_INFRA_FIELDS`,
     and remove its three stored writes (`repo_core.py:288`, `:408`,
     `repo_transfer.py:833`). Keep `_compute_low_res_geo` and its
     re-export: `api/geo.py:728` calls it on every request.
  3. `_load_cache`/`_save_cache`: for an encrypted user, a module-level dict
     keyed by user id with the same TTL; no DB write. Delete the user's DB
     row on the first such save.
  4. `enable`: delete the user's `DBStravaCache` row in the same transaction.
- **Acceptance:** tests: an encrypted user's picker request twice within the
  TTL hits Strava once and leaves no `stravacache` row; a plaintext user is
  unchanged; enabling deletes the row; migration upgrade on a seeded DB
  removes encrypted users' rows only, nulls only enveloped rows'
  `split_base_name`, drops the column, backfills the snapshot columns of a
  plaintext edited row to exactly what today's reset would restore and
  leaves null-original and enveloped-original rows null; downgrade works.
  `alembic heads`
  shows one head. Full `pytest` passes.
- **Out of scope:** changing the TTL or the picker's behaviour; the
  `.traxj` format.
- **Latitude:** local design.
- **Escalate if:** `low_res_geo_json` turns out to be read or exported
  anywhere; another migration head appears after rebase; a file outside
  Scope is needed.
- **Depends on:** —

#### U5 — Poster consent, scrub and retention

- **Goal:** poster jobs on encrypted trips need consent before carrying memory
  text, finished jobs keep no memory text, and poster jobs are deleted 30
  days after completion.
- **Scope:** `api/poster.py`, `src/poster/poster_job_runner.py`,
  `api/router.py` (scheduler registration only),
  `tests/test_poster_consent.py` (new), `tests/test_poster_sweep.py` (new).
- **Context:** decision 11; video consent in `api/video.py` and
  `docs/ENCRYPTION.md` "Trip videos"; sweep shaped like `sweep_video_jobs`
  (`src/video/job_runner.py:347-402`) and its registration in
  `api/router.py:183`; job model `models/project_db.py:594-622`.
- **Do:**
  1. Request model gains `plaintext_consent: bool = False`. Create answers
     409 `{"code": "consent_required"}` when the trip owner is encrypted, any
     memory in the request has non-empty `name`/`description`, and consent is
     false. The preview endpoint is unchanged (decision 11).
  2. On job end (done or failed, including the startup orphan sweep),
     replace `request_json` with a copy without memory `name`,
     `description`, coordinates and `title_text`.
  3. `sweep_poster_jobs`: hourly at a minute no other sweep uses; deletes
     rows and files of jobs with `completed_at` older than 30 days; never
     touches pending or running jobs; safe to re-run.
- **Acceptance:** tests: encrypted-owner request with memory text and no
  consent → 409, with consent → 202/200; plaintext owner → unchanged;
  request with no memory text on an encrypted trip → accepted without
  consent; preview on an encrypted trip with memory text and no consent →
  200 as today; after a job completes (and after one fails), `request_json` holds
  no memory text; sweep deletes a 31-day-old job's row and files, keeps a
  29-day-old one and a running one, and a second run is a no-op.
- **Out of scope:** client consent dialog (U9); video jobs.
- **Latitude:** local design.
- **Escalate if:** the runner needs the request after completion (e.g.
  re-render); a file outside Scope is needed.
- **Depends on:** —

### Wave 2 — server edit routes, parity artefacts, client state

#### U3 — Python parity artefacts for the Dart track maths

- **Goal:** Python publishes the vectors and constant checks the Dart
  `track_metrics/` port is pinned to.
- **Scope:** `scripts/gen_track_metrics_vectors.py` (new),
  `flutter_client/test/fixtures/track_metrics_vectors.json` (new,
  generated), `tests/test_track_metrics_parity.py` (new).
- **Context:** decisions 7 and 12; the functions in
  `src/models/track_edit.py` (`align_points` `:50-106`,
  `points_to_elevation_profile` `:116-155`, `interpolate_elevation_gaps`
  `:158-190`, gain pipeline `:332-657`, `recompute_track_metrics`
  `:971-1073`), `_apportion_gain` (`src/project/repo_activities.py:98-109`),
  the Python `polyline` encoder; Dart-parsing test precedent
  `tests/test_encryption_doc_coverage.py:32-44`; series from
  `tests/elevation_bench/`; vector precedent
  `flutter_client/test/elevation_codec_test.dart`.
- **Do:**
  1. Script writes the vectors JSON, one section per function, covering:
     noisy, stepped, flat and sentinel elevation series; tracks with missing
     elevations, leading/trailing gaps, two points, coincident points;
     `recompute_track_metrics` with and without original distance/times
     (trim, extend); `_apportion_gain` with None/zero/positive before;
     polyline encoding of negative, half-way-rounding and antimeridian
     coordinates.
  2. Parity test: regenerates in memory and fails when the committed JSON
     differs; parses `flutter_client/lib/src/track_metrics/elevation_gain.dart`
     (path fixed here, file created by U13) for the six `ELEV_*` constants
     and the noise constants and fails when they differ from Python's —
     skips with a clear message only while that file does not exist.
- **Acceptance:** `pytest tests/test_track_metrics_parity.py` passes (with
  the skip); changing any constant in `track_edit.py` makes it fail.
- **Out of scope:** the Dart port (U13); changing the server algorithm.
- **Latitude:** local design.
- **Escalate if:** a function's output depends on state the vectors cannot
  carry; a file outside Scope is needed.
- **Depends on:** —

#### U12 — Encrypted edit and split routes, scalar snapshots, `plain_fields`, gain route

- **Goal:** the server accepts device-computed edits and splits of encrypted
  activities, snapshots and restores scalars on every edit, tells the client
  which activity fields are still plaintext, and accepts the #366 gain.
- **Scope:** `api/activities.py` (new handlers, the reset handler's restore,
  the `GET …/track` originals and the `ActivityFieldsUpdate` model only),
  `src/project/repo_activities.py`, `src/project/repo_core.py` (the
  project-load query only), `src/models/activity.py`,
  `src/project/project_io.py`, `src/utils/encryption_check.py` (a strict
  envelope check beside the existing one), tests
  `tests/test_activity_encrypted_edit.py`,
  `tests/test_activity_encrypted_split.py`,
  `tests/test_activity_snapshot_reset.py`, `tests/test_plain_fields.py`,
  `tests/test_activity_elevation_gain_api.py` (all new).
- **Context:** decisions 6, 7, 12, 13, 14; `edit_activity_track` /
  `_write_track_geometry` and `split_activity` (`repo_activities.py:163-270`,
  `:471-653`) — the encrypted paths share their bookkeeping and replace only
  the geometry computation; reset (`:347-431`) as changed by U2; the
  client's strict shape `EncryptedField.isWellFormed`
  (`flutter_client/lib/src/crypto/e2ee_crypto.dart:243-284`); deferred
  columns on the light path (`repo_core.py:533-541`); `update_activity_fields`
  for the gain route's shape (`api/activities.py:1955-2011`); U2's helper.
- **Do:**
  1. Strict envelope check `is_well_formed_envelope`, mirroring
     `EncryptedField.isWellFormed`: version `v1`, two non-empty parts that
     decode as **standard** base64 with padding, of the lengths it checks
     (R5-2).
  2. `PUT /{name}/activities/{id}/track/encrypted` and
     `POST /{name}/activities/{id}/split/encrypted` per decision 7: required
     `lock_version`; same permission checks as the plaintext routes; 409
     `not_encrypted` unless the stored polyline is an envelope; 422 on any
     non-strict envelope or out-of-bounds scalar. Split takes `head` and
     `tail` pieces (geometry envelopes + scalars) and `tail_name` (envelope);
     the server computes the tail's start dates from the head's
     `elapsed_time`.
  3. First-edit snapshot writes the eight scalar `original_*` columns on the
     plaintext and the encrypted path; a split tail's snapshot is its own
     post-split values.
  4. Reset restores the scalars from the snapshot when present; enveloped
     originals with no scalar snapshot → 409 `nothing_to_restore`.
  5. `ActivityFieldsUpdate` accepts `original_start_latlng_json` and
     `original_end_latlng_json` with the rules of the existing two
     `original_*` fields, and `GET …/track` returns them beside U2's two,
     under U2's role rule (R5-1).
  6. `plain_fields`: computed in the project-load query as SQL expressions
     over the E2EE columns (non-null and not an envelope, tested
     case-sensitively — `substr(col, 1, 3) = 'v1.'` plus the dot structure,
     never `LIKE`, which SQLite matches case-insensitively, R5-5), so the
     light path never loads the deferred columns; set on
     `Activity`, emitted by `ProjectIO.to_dict`, not `to_strava_dict`.
  7. `PUT /api/activities/{id}/elevation-gain` per decision 12 (U2's
     helper, decision 13 CAS, 409 `not_encrypted`, 422 out of bounds).
- **Acceptance:** tests: encrypted edit stores exactly the sent envelopes
  and scalars, snapshots the previous ones on first edit, bumps the version,
  queues stats/tiles and busts the geo cache like the plaintext route;
  stale version → 409; plaintext row → 409; malformed envelope (including
  "v1.2.3") → 422 while a real `EncryptedField.encode` envelope containing
  `+`, `/` and `=` → 200; bad scalars → 422; a track with no elevations
  (profile and elev_high/low all null) → 200, partially null → 422. Reset
  of an encrypted edited activity leaves the same envelope in both profile
  columns. Encrypted split creates the tail item
  after the head with the sent values, the tail's start = head start + head
  elapsed, tail snapshot = its own values, family name untouched. Plaintext
  edit now also writes the scalar snapshot; reset after a plaintext edit
  restores every scalar exactly; reset after an encrypted edit restores
  envelopes and scalars exactly. `plain_fields` lists exactly the
  non-envelope E2EE columns (the four geometry originals included, and a
  plaintext name "V1.0.1 ride" counted as plaintext) on `/meta` and full
  payloads (and `GET …/track` returns all four geometry originals to an
  editor, none to a viewer), loads no deferred column on the light path
  (statement assertion), and is absent from a `.traxj` export. Gain route:
  owner + envelope → 200, stats refresh queued; stale → 409; non-owner →
  404; plaintext profile → 409; bad value → 422; `PUT /api/activities/{id}`
  still rejects `total_elevation_gain`. Existing `tests/test_activity_*` pass.
- **Out of scope:** the Dart port and editor (U13, U14).
- **Latitude:** local design.
- **Escalate if:** sharing the split bookkeeping between the two paths
  needs a change to plaintext split behaviour; the query cannot express
  `plain_fields` without loading the columns; a file outside Scope is
  needed.
- **Depends on:** U2, U4.

#### U6 — Client encryption state, write gating and owner-only memory encryption

- **Goal:** the client knows when an encrypted account is locked, blocks
  memory/journal/Polarsteps writes then, and encrypts memories only on trips
  it owns.
- **Scope:** `flutter_client/lib/src/crypto/encryption_service.dart`,
  `flutter_client/lib/src/auth/auth_notifier.dart`,
  `flutter_client/lib/src/projects/project_memory_crud_mixin.dart`,
  `flutter_client/lib/src/projects/project_journal_crud_mixin.dart`,
  `flutter_client/lib/src/projects/memory_dialog.dart`,
  `flutter_client/lib/src/projects/journal_dialog.dart`,
  `flutter_client/lib/src/projects/polarsteps_import_notifier.dart`,
  `flutter_client/lib/src/projects/sync_import_notifier.dart`,
  `flutter_client/lib/src/app_screen.dart` (banner only), a new
  `flutter_client/lib/src/crypto/encryption_locked_banner.dart`, and tests
  under `flutter_client/test/crypto/` and `flutter_client/test/projects/`.
- **Context:** decisions 1, 3, 5, 6 (Polarsteps `protect`); status shape in
  `encryption_api_http.dart:17-31`; existing read-only/help-text pattern
  `memory_dialog.dart:61-62`, `:303`, `:389`; offline banner pattern
  `app_screen.dart:1055-1070`; test patterns per Conventions. Paths above
  are as reported by investigation; correct them in the report if they
  differ.
- **Do:**
  1. `EncryptionService` exposes an `EncryptionState` (`disabled`,
     `unlocked`, `locked`, `awaitingApproval`) as a `ValueListenable`,
     set from `prepareForSession`, `unlock`, recovery and `lock`.
  2. Banner on the trip screen when `locked`/`awaitingApproval`, linking to
     Manage devices / Recover. `app_screen.dart` mounts the banner widget
     unconditionally; the widget decides its own visibility from the
     service state, so U14 can add a notice to it without touching
     `app_screen.dart` (R2-8).
  3. Memory/journal dialogs: Save disabled with the banner's message in the
     same states. `updateMemory`/`updateJournal` surface failure to the
     dialog (return `bool` like the create paths).
  4. Memory mixin: `protect` only when the trip's `role == 'owner'`. Journal
     mixin: `protect` whenever the account is encrypted.
  5. Polarsteps import and sync-import: refuse to start when locked (same
     message); otherwise `protect` memory text when the trip is owned.
  6. Map server 409 `encryption_locked` / `encryption_not_shared` to a clear
     message.
- **Acceptance:** widget/unit tests: locked state → Save disabled with
  message and no request sent; awaitingApproval likewise; unlocked owner →
  memory create sends envelopes; unlocked companion (`role` editor) → memory
  create sends plaintext; journal by encrypted companion → envelope;
  Polarsteps import on an owned encrypted trip sends envelopes, refuses when
  locked; banner shows for locked and not for disabled/unlocked. Full
  `flutter test` passes in the test container.
- **Out of scope:** catch-up and repair (U7); posters (U9).
- **Latitude:** local design.
- **Escalate if:** the top-level `encryption` singleton's captured API
  (`crypto/encryption.dart:13-16`) blocks testing and needs a change
  outside Scope; a file outside Scope is needed.
- **Depends on:** U1 (409 codes).

### Wave 3 — Dart port, catch-up, poster consent

#### U13 — Dart `track_metrics/` port

- **Goal:** the client computes everything the server's track edit computes,
  bit-for-bit within float tolerance, pinned by U3's vectors.
- **Scope:** `flutter_client/lib/src/track_metrics/` (new: `haversine.dart`,
  `align.dart`, `elevation_profile.dart`, `elevation_gain.dart`,
  `track_metrics.dart`, `polyline_encoder.dart`),
  `flutter_client/lib/src/projects/video_job_notifier.dart` (import the moved
  encoder only), `flutter_client/test/track_metrics/` (new).
- **Context:** decisions 7 and 12; Python references listed in U3's Context;
  the existing encoder `video_job_notifier.dart:34-55`; port style from
  `flutter_client/lib/src/map/great_circle.dart` ("mirrors" header); vector
  tests shaped like `flutter_client/test/elevation_codec_test.dart`; vectors
  at `flutter_client/test/fixtures/track_metrics_vectors.json` (U3).
- **Do:** port each function named in decision 7 with the Python names of
  its constants (U3 parses them); move `encodePolyline` and make the video
  module import it; web-safe arithmetic (no 64-bit-only int tricks).
- **Acceptance:** `flutter test test/track_metrics/` reproduces every vector
  (floats within 1e-6, ints and strings exact); `pytest
  tests/test_track_metrics_parity.py` passes with the constants parsed (no
  skip); existing video tests pass.
- **Out of scope:** using the port in the editor (U14) or the catch-up (U8).
- **Latitude:** local design.
- **Escalate if:** any vector differs beyond tolerance after a faithful port;
  a file outside Scope is needed.
- **Depends on:** U3.

#### U7 — Catch-up encryption and companion repair on load

- **Goal:** every owned trip loaded on an unlocked device of an encrypted
  account ends with its activities, memories and journal entries encrypted;
  on others' trips the user's own journal entries are encrypted and their
  own encrypted memories restored to plaintext; no write can revert a
  concurrent change.
- **Scope:** `flutter_client/lib/src/crypto/encryption_migration.dart`,
  `flutter_client/lib/src/projects/project_notifier.dart` (keeping the
  pre-reveal payload, the post-load hook, and an
  `unencryptableActivityCount` listenable only),
  `flutter_client/lib/src/crypto/enable_encryption_screen.dart` (error
  surfacing only), `docs/ENCRYPTION.md` ("What is encrypted" table rows
  only), tests under `flutter_client/test/crypto/` (including
  `encryption_coverage_test.dart`).
- **Context:** decisions 2, 6, 13, 14; `EncryptionMigration`
  (`encryption_migration.dart:49-179`, including the `original_*` nulling at
  `:171-179` that must change) and its tests
  `test/crypto/encryption_migration*_test.dart`,
  `test/crypto/encryption_coverage_test.dart`; reveal loop and in-place
  decryption `project_notifier.dart:3257-3349`, `_applyDetails` order.
- **Do:**
  1. Extract the per-trip step of `EncryptionMigration` into a function that
     takes a trip's **pre-reveal** payload, the trip's role and its
     `lock_version`; `run()` loops over it. The copy is kept, and the pass
     started, from one hook called by all three places that reveal a loaded
     trip in place: `load()` (`project_notifier.dart:1121-1130`),
     `_applyRefreshedProject` (`:2900-2907`) and `_applyDetails`
     (`:3351-3407`) (R2-7).
  2. Activities: only those with a non-empty `plain_fields`; for each,
     fetch `GET …/track` and encrypt every listed field from that response
     (never from the trip payload). Plaintext originals — the four geometry
     `original_*` columns — are encrypted, not nulled: add them to
     `encryptedFieldsByResource`, update the coverage test's pins and the
     doc table rows.
  3. One expected `lock_version` per pass: the payload's, advanced only by
     the pass's own successful writes; every write sends it (and `project`
     for activities). A `GET …/track` response with a different
     `lock_version`, or a `stale_write`, ends the pass; other failures are
     logged and skipped. A 404 on an activity counts it in
     `unencryptableActivityCount`.
  4. After a load completes, when the account is encrypted and unlocked:
     owner → the per-trip step; non-owner → encrypt the user's own plaintext
     journal entries and, for each memory whose envelope decrypts under the
     user's key, `PUT` it back plaintext. In the background, at most one pass
     per trip at a time.
  5. After the writes, refresh the in-memory copy without triggering another
     pass (no loop through `_silentReload`).
  6. Enable screen: show a non-blocking notice when the initial run skipped
     items, instead of swallowing.
- **Acceptance:** tests, all fed the payload as the server sends it and run
  through the real post-load path, the first of them through `load()` with
  the `/meta` payload shape (not the step alone); no activity write ever
  carries geometry taken from the trip payload: a trip with one
  plaintext activity, one plaintext memory and one plaintext journal entry
  is fully encrypted after one load; a second load of the now-encrypted trip
  sends no writes; an activity's encrypted profile is the full one from
  `GET …/track`, not the `/meta` downsample; an edited activity listing its
  originals in `plain_fields` gets them written back encrypted, not null; a
  memory saved by another device between the load and the pass is not
  overwritten (the pass ends on the changed `lock_version` of its first
  `GET …/track`); a `stale_write` on the first write stops the pass and
  the next load completes it; a 404 on one activity does not stop the others
  and sets the count to 1; on a companion's trip, the user's plaintext
  journal entry is encrypted, a memory encrypted under the user's key → PUT
  plaintext, one under a foreign key → untouched, a plaintext memory →
  untouched; locked device → no pass; `encryption_coverage_test.dart` passes.
- **Out of scope:** elevation recompute (U8); server changes; showing the
  count (U14).
- **Latitude:** local design.
- **Escalate if:** the pass and `_silentReload` cannot be kept from looping
  without changes outside Scope; the payload lacks `lock_version` or what is
  needed to tell owned rows; a file outside Scope is needed.
- **Depends on:** U1, U2, U12 (server accepts the writes, the CAS and
  `plain_fields`).

#### U9 — Poster consent dialog

- **Goal:** generating a poster on an encrypted trip asks the user before
  sending memory text, then sends it with consent.
- **Scope** (corrected during delivery, X3): `flutter_client/lib/src/projects/poster_job_notifier.dart`
  (`PosterConsentRequired`, `plaintext_consent` passthrough),
  `flutter_client/lib/src/projects/poster_consent_dialog.dart` (new),
  `flutter_client/lib/src/projects/app_screen.dart` (`_startPosterJob`
  only), `flutter_client/test/poster_job_notifier_test.dart`,
  `flutter_client/test/poster_consent_dialog_test.dart` (new).
- **Context:** decision 11; the video consent dialog and its 409 handling in
  the video export client code (find via `consent_required` under
  `flutter_client/lib/`) is the example to follow.
- **Do:** on 409 `consent_required`, show the consent dialog with the same
  wording style as videos; on agree, resend with `plaintext_consent: true`;
  on decline, offer to generate without memory text.
- **Acceptance:** widget tests: 409 → dialog; agree → second request carries
  consent; decline → request without memory text; plaintext trip → no
  dialog.
- **Out of scope:** server changes.
- **Latitude:** local design.
- **Escalate if:** no reusable video consent dialog exists; a file outside
  Scope is needed.
- **Depends on:** U5.

### Wave 4 — catch-up gain recompute, device editing

#### U8 — Encrypted gain recompute in the catch-up

- **Goal:** the catch-up recomputes and stores corrected gain (and repaired
  profile) for legacy encrypted edited/GPX activities, with the shared Dart
  port.
- **Scope:** `flutter_client/lib/src/crypto/encryption_migration.dart`
  (recompute call in the per-trip step only),
  `flutter_client/test/crypto/encryption_gain_recompute_test.dart` (new).
- **Context:** decision 12; `track_metrics/` from U13; the per-trip step and
  its `lock_version` handling from U7; endpoint from U12.
- **Do:** for each activity with an enveloped stored profile and `is_edited`
  or `source == 'gpx'`: decrypt the full profile (from `GET …/track` when
  the pass already fetched it, else the payload's `elevation_profile_enc`,
  which for encrypted rows is the full envelope); apply the sentinel repair;
  if repaired, re-encrypt and `PUT` the profile (same envelope to both
  columns); compute gain; if it differs from the stored value by more than
  0.5 m, call the gain route. Both writes use the pass's `lock_version`.
- **Acceptance:** an inflated encrypted edited activity triggers exactly one
  gain call with the vector's value; a correct one triggers none; a
  Strava-unedited one is never touched; a sentinel profile is repaired and
  written once; a second pass writes nothing.
- **Out of scope:** the port itself (U13).
- **Latitude:** local design.
- **Escalate if:** the payload lacks `source`; a file outside Scope is
  needed.
- **Depends on:** U7, U12, U13.

#### U14 — Encrypted track editing on the device, and the "stays unencrypted" notice

- **Goal:** on an unlocked device, edit, split and reset of an encrypted
  activity work without the server seeing the track; the trip shows how
  many activities could not be encrypted.
- **Scope:** `flutter_client/lib/src/projects/activity_editor_page.dart`,
  `flutter_client/lib/src/projects/activity_panel.dart` (the open path and
  the envelope block only), `flutter_client/lib/src/projects/track_edit_model.dart`
  (alignment via `track_metrics/` only),
  `flutter_client/lib/src/projects/project_notifier.dart` (track edit, split
  and reset methods only), `flutter_client/lib/src/projects/project_service.dart`
  (track edit, split and reset requests only),
  `flutter_client/lib/src/crypto/encryption_locked_banner.dart` (the notice,
  inside the banner U6 mounts unconditionally), tests:
  `flutter_client/test/projects/activity_editor_encrypted_test.dart`,
  `flutter_client/test/projects/activity_panel_track_editor_encrypted_test.dart`,
  `flutter_client/test/crypto/encryption_locked_banner_test.dart` (R5-8).
- **Context:** decision 7; `track_metrics/` (U13); routes from U12 and U2;
  today's open path `activity_panel.dart:801-863` and
  `fetchActivityForEdit` (`project_notifier.dart:3007-3011`); request shapes
  `project_service.dart:408-501`; reset confirmation flow
  `activity_editor_page.dart:100-120`, `:252-299`; banner from U6;
  `unencryptableActivityCount` from U7.
- **Do:**
  1. Open: for an activity whose stored track is an envelope (seen in the
     `GET …/track` response), always open from that response, decrypted;
     locked device → keep today's block with the locked message.
  2. Save and split of such an activity: compute with `track_metrics/`
     exactly as decision 7 describes, encrypt, send to the encrypted routes
     with the response's `lock_version`; tail name = encrypted decrypted head
     name. Plaintext activities keep today's requests.
  3. Reset: send the editor's `lock_version`; show `nothing_to_restore` and
     `stale_write` with clear messages.
  4. Banner: when `unencryptableActivityCount > 0` on an unlocked encrypted
     account, show "N activities were imported by another traveller and stay
     unencrypted".
- **Acceptance:** widget/unit tests: opening an encrypted activity fetches
  `/track` even when the panel's copy has a polyline; save sends only
  envelopes and the scalars the port computes for the vector track (no
  plaintext coordinate anywhere in the request body — assert by scanning
  it); split sends two pieces whose times sum as the port computes and an
  encrypted tail name; plaintext activities' requests are unchanged; reset
  sends `lock_version`; 409 messages shown; notice shown for count 1, hidden
  for 0 and when locked.
- **Out of scope:** server changes; editing on a locked device.
- **Latitude:** local design.
- **Escalate if:** the editor's model cannot carry missing elevations the
  way `align_points` does without changes outside Scope; a file outside
  Scope is needed.
- **Depends on:** U6, U7, U12, U13.

### Wave 5 — docs

#### U10 — ENCRYPTION.md

- **Goal:** the doc describes the code after this package.
- **Scope:** `docs/ENCRYPTION.md`, `tests/test_encryption_doc_coverage.py`
  (only if the table it parses moves).
- **Context:** every decision above; the merged diffs of U1–U9 and U12–U14.
- **Do:** remove remnants 1–5, 8, 9 from "Known plaintext remnants";
  rewrite "When it happens" (catch-up on load, owner-key memories,
  author-key journal, locked-device refusal); describe on-device track
  editing and the scalar snapshots; add, as a remaining remnant, activity rows the owner may not
  encrypt (decision 14); add posters to the consent section and "Server-side derived
  data"; drop `low_res_geo_json` from "What the server checks" context;
  document the encrypted edit/split routes and the elevation-gain endpoint
  beside `PUT /api/activities/{id}`;
  remove the "per-write paths not exercised by tests" caveat if U6's tests
  now cover them.
- **Acceptance:** `pytest tests/test_encryption_doc_coverage.py` passes;
  every remaining remnant names a code location that still matches.
- **Out of scope:** any code.
- **Latitude:** none.
- **Escalate if:** a statement cannot be verified against the merged code.
- **Depends on:** U1–U9, U12–U14.

## Definition of done

- On an encrypted account, after one load of each owned trip on an unlocked
  device, raw SQL shows no plaintext `memory.name`/`description`,
  `journalentry.description`, or activity name/polyline/endpoints/profile in
  that trip, whatever the content's origin (Strava, GPX, split, track edit,
  Polarsteps, `.traxj`) — except Strava activity rows imported by another
  user, or local rows also used by another user's trip, which the app
  counts and shows. Their own journal entries on others'
  trips are encrypted too. A fully encrypted trip's load sends no writes, and
  no catch-up write can overwrite a change made after the pass loaded.
- No `stravacache` row exists for an encrypted user; `project.low_res_geo_json`
  no longer exists; no enveloped activity has a non-null `split_base_name`.
- A locked or unapproved device cannot save a memory or journal entry on an
  encrypted account: the app says why, and the server refuses an old build's
  attempt with 409.
- An encrypted companion's new memory in a plaintext trip is readable by the
  owner; their earlier encrypted ones become readable after their next
  unlocked load of that trip.
- No user can write the E2EE fields of an activity row they neither own
  nor hold every trip of; Strava rows of another user are never written.
- Memory and journal updates advance the trip's lock version.
- An invite to an owner who enabled encryption after creating it cannot be
  accepted.
- Track edit, split and reset of an encrypted activity work on an unlocked
  device and the server never receives its decrypted track: requests carry
  only envelopes and scalars, which match what the server would compute for
  the same track (parity vectors). Reset restores every scalar exactly on
  both paths, never wipes a track, and refuses (409) when there is nothing
  to restore. Old app builds get 409 instead of storing a decrypted track.
  A split tail shows the head's title.
- Poster jobs on encrypted trips with memory text require consent; finished
  jobs hold no memory text; jobs older than 30 days are gone with their files.
- Encrypted edited/GPX activities show the same smoothed gain as the server
  would compute for the same profile, written through
  `PUT /api/activities/{id}/elevation-gain`; `PUT /api/activities/{id}` is
  unchanged; parity tests fail if either side's constants or algorithm drift.
- `docs/ENCRYPTION.md` lists only remnants 6 (backups) and 7 (on-device
  cache) and activity rows imported by another user (shown in the app as
  "stay unencrypted").
- Full `pytest` and `flutter test` pass; `alembic heads` shows one head.
