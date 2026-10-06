# Zero-knowledge encryption (issue #26)

TraxJourney can encrypt your **memories**, **journal entries** and **activity
tracks** so that even someone operating the server — an administrator with full
database and disk access — cannot read them. Encryption and decryption happen
entirely on your device; the server only ever stores ciphertext and wrapped
keys, and performs no cryptography itself.

Turn it on under **Settings → Encryption → Set up encryption**.

## What is encrypted

The list below was written from the code, not from intent, and two tests keep
it that way:

- `encryptedFieldsByResource` in
  `flutter_client/lib/src/crypto/encryption_migration.dart` is the pinned
  list. `flutter_client/test/crypto/encryption_coverage_test.dart` runs the
  migration and asserts its writes carry exactly those fields as ciphertext,
  and exactly `date`, `geo_mode`, `time`, `lat`, `lon` (memory, journal) as
  plaintext. An activity write sends no plaintext content at all: only the
  trip's name and `lock_version`, which make it a compare-and-swap and are not
  stored on the row.
- `tests/test_encryption_doc_coverage.py` (server CI, no Flutter needed)
  parses that constant out of the Dart source and fails unless every resource
  and field appears in the table below — and unless the table claims nothing
  the constant does not.

What the tests do **not** cover: that the per-write memory/journal paths
(`project_memory_crud_mixin.dart`, `project_journal_crud_mixin.dart`) leave
`date`, `time`, `geo_mode`, `lat` and `lon` alone.
`flutter_client/test/projects/encryption_write_gating_test.dart` does drive the
real `encryption` singleton, unlocked, and asserts that `name`/`description`
go out as envelopes under the owner/author rule (see "When it happens") — but
a change there that encrypted `lat`, say, would not fail any test today;
covering it is a follow-up. The "What stays plaintext" table further down is
hand-written from the schema and is not test-pinned.

| Resource | Encrypted field (API key) | Database column | Encrypted where |
|---|---|---|---|
| Memory | `name` (title) | `memory.name` | on every create/update (`project_memory_crud_mixin.dart`) and by the enable-time migration |
| Memory | `description` (notes) | `memory.description` | same |
| Journal entry | `description` (entry body) | `journalentry.description` | on every create/update (`project_journal_crud_mixin.dart`) and by the migration |
| Activity | `name` | `activity.name` | **by the migration and the catch-up after each load of an owned trip** (`encryption_migration.dart`, see "When it happens") |
| Activity | `summary_polyline` (the GPS track) | `activity.summary_polyline` | same |
| Activity | `start_latlng_json`, `end_latlng_json` (track endpoints) | `activity.start_latlng_json`, `activity.end_latlng_json` | same |
| Activity | `elevation_profile_json`, `elevation_profile_low_res_json` (elevation profile, full and downsampled — both hold the full profile's envelope) | same-named columns | same |
| Activity | `original_polyline`, `original_elevation_profile_json`, `original_start_latlng_json`, `original_end_latlng_json` (the edit snapshots Reset restores) | same-named columns | same |

Each value is stored as an opaque `v1.<b64>.<b64>` envelope
(`EncryptedField` in `e2ee_crypto.dart`). Running raw SQL against the database
shows only these blobs for the columns above.

Two more things are encrypted, under **different keys**:

- **Share links** (issue #28): when you generate a share link for a trip with
  encrypted memories, the client re-encrypts each encrypted memory's `name` and
  `description` under a fresh per-share key and uploads them
  (`share_memory_content.name_ciphertext` / `description_ciphertext`). The key
  lives only in the link's URL fragment (`#key=...`), which browsers never send
  to the server. Nothing else in a share link is share-encrypted: activity
  tracks that are E2EE-encrypted are simply **absent** from the shared map, and
  the server strips the raw envelopes from shared memory fields so a viewer
  without the fragment key sees "unavailable" rather than ciphertext.
  Everything else a share link carries — plaintext memories, photos, dates,
  memory coordinates, unencrypted tracks, connecting segments — is readable by
  **anyone holding the link**, by definition.
- **Key material**: the Content Master Key wrapped to each device and to your
  recovery method (`device_key.wrapped_cmk`, `recovery_wrap.wrapped_cmk`).

## When it happens

Issues #504, #505, #506 and #366 changed this section: content no longer stays
plaintext for good once it has arrived after you enabled encryption.

**Memories and journal entries** are encrypted at the moment you save them,
once encryption is unlocked on the device (`EncryptionService.protect` in the
two CRUD mixins), under the key of whoever the text belongs to:

- A trip's **memories** are shared with everyone on the trip, so they belong
  under the **trip owner's** key. The client encrypts memory `name` and
  `description` only on a trip you own (`_memoriesUseOwnKey` in
  `project_memory_crud_mixin.dart`: `role == 'owner'`). A memory you write on
  someone else's trip goes out as plaintext, which the owner and the other
  travellers can read. Polarsteps imports follow the same rule
  (`polarsteps_import_notifier.dart`, `sync_import_notifier.dart`).
- **Journal entries** are private to their author, so they are encrypted with
  the **author's** key everywhere, including on a trip you do not own
  (`project_journal_crud_mixin.dart`).

**A device that cannot use the key writes nothing in the clear.**
`EncryptionService` keeps the account's state as `EncryptionState`
(`encryption_service.dart`): `disabled`, `unlocked`, `locked` (encryption is
on, this device is approved, the key is not unwrapped) or `awaitingApproval`.
It is set from the server's status by `prepareForSession`/`unlock`. While the
state is `locked` or `awaitingApproval`:

- `EncryptionLockedBanner` (`encryption_locked_banner.dart`) on the trip screen
  says so and links to Manage devices and Recover access;
- the memory editor (on trips you own) and the journal editor disable Save with
  the same message (`EncryptionService.writeBlockedMessage`);
- Polarsteps import, and sync import when it carries Polarsteps steps, refuse
  to start.

Nothing is queued for later: the web client has no persistent outbox. When the
status cannot be read at all, the state stays `disabled` (unknown) and the
server's refusals below stand in for the client's gate.

**The server refuses the wrong key as a backstop**, which also stops older app
builds. On create and update, for each non-empty `name`/`description`
(`encryption_guard_code` and `raise_if_encryption_mismatch` in
`api/memories.py`; `_raise_if_author_mismatch` in `api/journal.py`):

- the key holder is encrypted and the value is not an envelope → 409
  `encryption_locked`;
- the key holder is not encrypted and the value is an envelope → 409
  `encryption_not_shared`.

The key holder is the **trip owner** for a memory and the **author** for a
journal entry. A memory create that adopts an existing Polarsteps memory is
checked first too. The price: a locked device cannot edit even the date of a
legacy plaintext memory on an encrypted trip, because the update resends the
stored text. Memory and journal updates now also advance the trip's
`lock_version` (`advance_lock_version` in `api/memories.py`), so other devices
see them as changes.

**Companions.** `POST .../invite` (`api/members.py`) refuses to create an invite
while the trip **owner** has encryption enabled, and accepting one refuses too
(`_refuse_if_owner_encrypted`, now also called by `accept_invite`), so an invite
made before the owner enabled encryption cannot be accepted afterwards.
`POST /api/encryption/enable` (`api/encryption.py`) refuses to turn encryption
on while any trip you **own** has companions. An encrypted account can still be
a companion on a plaintext owner's trip: its memories go out as plaintext
(above) and its journal entries under its own key.

**Activities are encrypted by the catch-up, from your device.** The server
creates activities in plaintext — a Strava sync, a GPX import, a split, a
`.traxj`/ZIP import (`ActivityMixin._upsert_activity`, `src/gpx/importer.py`) —
because it has no key. Instead, after every trip load on an unlocked device of
an encrypted account, `ProjectNotifier._catchUpAfterLoad` runs
`EncryptionMigration.encryptTrip` for that trip (one pass per trip at a time).
Opening a trip is enough; `load()`, the refresh path and `_applyDetails` all
call the same hook. The enable-time `EncryptionMigration.run()`, reached from
`enable_encryption_screen.dart`, runs the same per-trip function over every
trip you own. The pass:

- reads the trip payload **as received**, before the load decrypts it in
  place (`CatchUpPayload.of`), so it can tell envelopes from plaintext;
- takes activities from `plain_fields`, never from the payload's own columns.
  The server lists, per activity, which of the ten E2EE columns hold a
  non-empty non-envelope value (`_plain_fields_mask` in
  `src/project/repo_core.py`, computed in SQL with a case-sensitive envelope
  test and emitted by `ProjectIO.to_dict`, not by the `.traxj` export). For
  each activity with a non-empty list the client fetches
  `GET /api/projects/{name}/activities/{id}/track` — full track, full profile
  and the edit snapshots — and encrypts the listed fields from that
  (`_encryptActivity`). The trip payload is never used for geometry: its `/meta`
  form has no polyline and only the downsampled profile. The snapshots are
  encrypted, never nulled, so Reset keeps working on edited rows; one profile
  envelope is written to both profile columns;
- encrypts plaintext memories and journal entries from the payload on a trip
  you own. On a trip you do **not** own it encrypts your own plaintext journal
  entries, and **repairs** your earlier memories there: any memory whose
  envelope decrypts under your own key was written under the wrong key before
  #505, so it is re-saved as plaintext (successful decryption is the proof of
  authorship, as memories carry no author column) — `_restoreMemory`. It also
  repairs your own activity envelopes there (see "Remaining plaintext" case 5):
  an activity in that trip that is enveloped under your key is decrypted back,
  because that trip is another user's. This is the one activity write the
  non-owner pass makes, in `encryption_migration.dart`; it never encrypts your
  activity rows on such a trip. Its writes are `PUT /api/activities/{id}` with
  `project` (the trip's name), `owner` (the trip owner's id) and `lock_version`,
  so they are a compare-and-swap on that trip, which you may edit as a member
  (editor or above; any other caller gets 404). Envelopes under another key are
  left alone. A checked row is remembered only for the current app session, per
  user and activity, against the row's `/meta` signature (the static
  `_sharedChecked` map): a changed row is read again, and so is every row after
  a restart, including one with no polyline or profile, which is read once per
  app session. A write a friend's trip refuses (404) is remembered the same way,
  for that trip;
- writes every row as a compare-and-swap on the trip's `lock_version`:
  `PUT /api/activities/{id}`, `PUT /api/memories/{id}` and
  `PUT /api/journal/{id}` take an optional `lock_version` (and, for activities,
  the trip's `project` name), answer 409 `stale_write` on a mismatch and return
  the new version. The pass chains the returned value. A `stale_write`, or a
  `GET …/track` answered at another version, ends the pass and the next load
  retries from fresh data; any other failed write skips that row only. The pass
  also ends as soon as the session it started in is over (signed out, locked,
  another account's token);
- leaves alone, and counts, any activity a trip owned by another user also
  holds (the payload's `shared_with_others`, see "Remaining plaintext" case 5);
  a row of that kind an earlier pass had already encrypted is decrypted back to
  plaintext when your key opens its envelopes (`encryption_migration.dart`);
- recomputes elevation gain for legacy encrypted rows (see "Editing an
  encrypted track").

**The window.** Activities created server-side are plaintext in the database
until an unlocked device of the owner next opens the trip. That window is
inherent: only a device with the key can close it. A trip nobody opens on an
unlocked device stays as it is.

**Legacy Polarsteps memories** and any other plaintext memory on a trip you own
are caught by the same pass; editing such a memory in the app also re-saves it
encrypted.

Trips shared **with** you by another user are not touched by the enable-time
`run()` (issue #106); their journal entries and memory repair are handled by
the catch-up when you load them. Activities are the exception: on a trip you
do not own the catch-up never encrypts your own activity rows (it only decrypts
back your own envelopes, "Remaining plaintext" case 5), and rows you may
not encrypt, or that another user's trip also holds, stay plaintext too — see
"Remaining plaintext".

## Editing an encrypted track

Track edit, split and reset of an encrypted activity work on an unlocked device
and the server **never receives the decrypted track**. The client has a Dart
port of the server's track maths (`flutter_client/lib/src/track_metrics/`:
haversine, point alignment, the elevation profile, the dropout-sentinel
detection, `elevationGain`, `recomputeTrackMetrics` and `apportionGain`; the
polyline encoder lives there too). Python is the source of truth, pinned two
ways from server CI by `tests/test_track_metrics_parity.py`: the vectors in
`flutter_client/test/fixtures/track_metrics_vectors.json` must equal what
`scripts/gen_track_metrics_vectors.py` produces from today's Python (the Dart
tests in `flutter_client/test/track_metrics/` replay them), and the port's
constants in `elevation_gain.dart` must equal `src/models/track_edit.py`'s.

- **Open.** On an encrypted track the editor always fetches
  `GET …/track` and decrypts its envelopes (`EncryptedTrackEdit.open` in
  `project_notifier.dart`), never the panel's in-memory copy, so it holds the
  stored geometry and figures the server will compare against. A locked device
  gets a message instead of the editor (`activity_panel.dart`).
- **Save and split.** The client computes what the server's
  `_write_track_geometry`/`split_activity` would compute (distance, time
  apportioned against the opened geometry, gain by `apportionGain`, endpoints,
  elevation extremes), encrypts the four geometry values and sends them with
  the figures to `PUT /api/projects/{name}/activities/{id}/track/encrypted`
  (`edit_activity_track_encrypted`) or
  `POST …/split/encrypted` (`split_activity_encrypted`, both pieces plus the
  tail's name, encrypted from the decrypted head name as `"<name> (2)"`). The
  server keeps the bookkeeping: the tail's local id, `split_root_id`/
  `split_parent_id`, the tail's start shifted by the head's elapsed time,
  inserting the tail's item after the head. Because the server cannot read the
  names, an encrypted family is not renumbered `(i/N)`
  (`_renumber_split_family` skips an enveloped root).
- **What the server checks** on both routes: `lock_version` is required (409
  `stale_write`); the stored polyline must be an envelope (409 `not_encrypted`);
  the caller needs the same permission as the plaintext routes (editor on the
  trip, and the row owned by the trip's owner or a current member —
  `activity_rewritable_by_trip`); every geometry value must be well formed under
  the strict `is_well_formed_envelope` (`src/utils/encryption_check.py`), not
  the loose structural check; the figures must be finite, non-negative and
  within `src/models/value_bounds.py`; elevation profile, `elev_high` and
  `elev_low` are null together or not at all, and `elev_low` ≤ `elev_high`
  (422 otherwise). Trip editors may edit any track in the trip, as on the
  plaintext routes. The figures are the client's own measure of its own track:
  the server bounds them, it cannot verify them.
- **Snapshots carry the scalars.** The first edit — plaintext or encrypted path
  — also snapshots `distance`, `moving_time`, `elapsed_time`, `average_speed`,
  `elev_high`, `elev_low`, the endpoints and the gain into `original_*` columns
  (`_snapshot_current` in `src/project/repo_activities.py`; a split tail's
  snapshot is its own post-split values). The Alembic migration
  `c4e2a9f1b7d3_e2ee_remnants.py` backfilled them for rows already edited whose
  originals were plaintext.
- **Reset is server-only and needs no geometry** (`reset_activity_track`).
  Geometry comes back verbatim — an enveloped original profile goes to both
  profile columns — and, when the scalar snapshot exists (`original_distance`
  set), every scalar is restored exactly from it. A plaintext row without a
  snapshot keeps the old recompute. It answers 409 `nothing_to_restore`, and
  changes nothing, for an edited row whose `original_polyline` is null (the
  first encryption migration nulled the snapshots, so such a row can no longer
  be reset) or whose originals are envelopes with no scalar snapshot. It takes
  an optional `lock_version` body field (409 `stale_write`).
- **Old app builds.** `PUT …/track` and `POST …/split` answer 409
  `encrypted_edit_on_device` for a row whose stored polyline or profile is an
  envelope (`EncryptedEditOnDevice`), so a build that sends decrypted points
  gets an error instead of storing plaintext. Reset needs no points and keeps
  working for them.

**Elevation gain (#366).** Older versions of the server corrected the stored
gain of GPX imports and of edited activities on plaintext rows (the #260 and
later repairs) but had to skip rows whose profile is an envelope. The client
now does it for encrypted rows with the Dart port, in the catch-up
(`_recomputeGain` in `encryption_migration.dart`), for **legacy rows only**:
`source == 'gpx'`, or an edited activity with no gain snapshot
(`has_gain_snapshot` false: `original_total_elevation_gain` is null, so the
edit predates the gain scaling of #386 and its stored gain is the old raw sum).
Edits made since, on-device ones included, keep their Strava-scaled gain, as on
plaintext accounts. The pass decrypts the profile, repairs the 0.0 dropout
sentinel if it is present (writing the repaired profile back, re-encrypted, to
both profile columns), measures the gain, and posts it only when it differs
from the stored one by more than 0.5 m, so a second pass writes nothing. The
value is the absolute smoothed gain of the current profile: a pre-#386 edit's
original Strava figure cannot be recovered from the profile. It goes through
`PUT /api/activities/{id}/elevation-gain` (`update_activity_elevation_gain`):
body `total_elevation_gain` (finite, 0 to 50 000 m, `ENCRYPTED_GAIN_MAX_M`),
the same caller rule and optional `project`/`lock_version` as
`PUT /api/activities/{id}`, 409 `not_encrypted` unless the stored
`elevation_profile_json` is an envelope. `PUT /api/activities/{id}` itself
still never writes the gain.

## What stays plaintext

Zero-knowledge encryption hides **content**, not the **shape** of the data.
Everything below is readable by the server operator, from the database or a
backup, whether or not encryption is on.

| Area | Plaintext fields |
|---|---|
| Trip | name, trip start/end dates, filter state, colours and styles, translation languages, sleeping options, counters, share tokens, timestamps |
| Days (`project.day_meta_json`) | difficulty, sleeping, weather, tags, counter values, and the per-day free-text `journal` note edited in `day_meta_editor.dart` — this legacy note is **not** the encrypted journal entry |
| Memory | `date`, `time`, `geo_mode`, `lat`/`lon`, photo list, `public_id` (used in share deep links), Polarsteps step id, comment and like counts |
| Journal entry | `date`, `time`, `geo_mode`, `lat`/`lon`, photo list, author |
| Activity | `type`, `distance`, `moving_time`, `elapsed_time`, `total_elevation_gain`, `start_date`/`start_date_local`, `timezone`, all Strava counters and flags (kudos, comments, photos, trainer, commute, private, …), `average_speed`/`max_speed`, `elev_high`/`elev_low`, `gear_id`, `source`/`source_id`, split/edit bookkeeping (`split_root_id`, `split_parent_id`, `is_edited`, `original_total_elevation_gain`, and the scalar edit snapshots `original_distance`, `original_moving_time`, `original_elapsed_time`, `original_average_speed`, `original_elev_high`, `original_elev_low`) |
| Connecting segments (`projectitem.segment_json`) | type, label (e.g. "Basel → Paris"), start/end coordinates, date, train number, and the resolved route polyline — **rail/ferry/bus route geometry is never encrypted** |
| People, groups, encounters | every field: names, e-mail, phone, socials, nationalities, residence, notes, avatar, encounter date/place/description. Never exposed on share links, but readable by the operator |
| Photos | full-resolution files and server-generated thumbnails on disk (`api/memories.py`, `api/journal.py`, person avatars). Not encrypted. Immich photos are proxied, never stored |
| Comments and likes on memories | comment text, commenter/liker display names |
| Metadata | which rows exist, their sizes, ordering, ownership, project membership, device labels, recovery method, timestamps |

Because dates, coordinates and distances stay plaintext, the operator can
still see **where and when** you were and how far you went, even when every
memory, journal entry and track is encrypted.

## Server-side derived data

The server keeps derived copies of some of the above. What happens to them at
encryption time, from the code:

| Derived data | Contents | On encrypt |
|---|---|---|
| `activity_geo_prepared` (zoom-level track cache, issue #369) | decoded track coordinates | **deleted** when the polyline becomes ciphertext (`store_prepared_geometry`) — by the catch-up's write, by an encrypted edit or split (`_apply_encrypted_piece`) and by a reset to an enveloped original; the backfill sweep skips `v1.` rows and its store is guarded against re-inserting plaintext |
| `project.stats_json` | totals, per-type counts, best-day **dates**, distance per tag/mode, sleeping counts, ride time series | numbers and dates only — no names, no coordinates; recomputed after each activity write (the catch-up's included) |
| `memory_translation` | machine translations of memory `name`/`description` | **purged** when the memory becomes ciphertext; the server refuses to translate an encrypted memory (409) |
| Full-res geo response cache | GeoJSON built from plaintext tracks | busted for every trip the activity is in |
| GPX export, poster tracks | built from `summary_polyline` | GPX export refuses (409) any trip with an encrypted activity; the geo builders skip encrypted tracks |
| `.traxj` / ZIP export (`api/project_transfer.py`) | the project as JSON (ZIP adds the photos) | no check: encrypted fields are written into the file **as their envelopes** (`name`, `map.summary_polyline`, and the endpoints/elevation under `start_latlng_enc`/`end_latlng_enc`/`elevation_profile_enc`), so the export stays unreadable without your key. **Re-importing it keeps them** (issue #466): an activity row the import creates (another account's copy, or your own activity whose row is gone) takes the envelopes into `start_latlng_json`, `end_latlng_json`, `elevation_profile_json` and the low-res copy, exactly as the migration writes them; only a well-formed `v1.<b64>.<b64>` is accepted there. Your own key decrypts the result; another account cannot, and its app shows "Encrypted content unavailable" for such text. A plaintext activity the import creates is encrypted by the next catch-up |
| `stravacache.activities_json` (raw Strava list, issue #504) | every synced activity's name, endpoints, summary polyline and dates, as Strava returned them | **not written to disk for an encrypted account**: `_save_cache`/`_load_cache` in `api/strava.py` keep it in an in-process dict (`_memory_cache`, same `STRAVA_CACHE_TTL`, lost on restart at the price of one Strava refetch — it assumes a single API process). `_save_cache` deletes any row left in the table, `POST /api/encryption/enable` deletes the user's row, and the Alembic migration `c4e2a9f1b7d3_e2ee_remnants.py` deleted the existing rows of encrypted users |
| `project.low_res_geo_json` | straight-line GeoJSON per activity with name and endpoints | **column dropped** by the same migration; nothing read it. `api/geo.py` computes the low-res response from the live project on each request (`_compute_low_res_geo`) |
| `activity.split_base_name` | the plaintext pre-split name | **nulled** when `PUT /api/activities/{id}` stores an envelope `name` (`update_activity_fields`); the migration nulled it for rows whose name was already an envelope |

## Trip videos — plaintext by consent, for one render

A trip video is rendered on the server (docs/TRIP_VIDEO_PLAN.md, D1), which
cannot read an encrypted track. So for an encrypted trip the server asks, and
the user decides, per video:

1. An activity counts as encrypted when its `summary_polyline` **or** either
   endpoint (`start_latlng_json`/`end_latlng_json`) is an envelope
   (`is_encrypted_activity` in `src/video/legs.py`, the one definition the
   routes and the timeline share).
2. `POST /api/projects/{name}/video/plan` and `POST …/video` answer **409
   `consent_required`** with the ids of every encrypted activity the request
   brought no line for — before building the timeline, so a trip made only of
   encrypted activities still reaches the question instead of a 422.
3. After the user agrees, the client decrypts locally and resends with
   `decrypted_geometry`: activity id → Google-encoded polyline (for an
   activity with no track, its decrypted 2-point start–end line). The server
   accepts it only for activities that are in that trip **and** encrypted
   (422 otherwise) and never asks for keys.
4. The plaintext is written to one file, `data/users/<uid>/videos/<job_id>/geometry.json`
   (mode 0600), only after the job row commits. It is never put in
   `videojob.request_json`, Redis, a log line, an exception message, an
   `error_message` or an email: errors name activity ids only, the runner
   logs failures by exception type and traceback without the message, and
   `error_message` holds fixed reason strings.
5. It is deleted (`delete_job_geometry`) when the job ends in any way — done,
   failed, killed work-horse, the video worker's startup sweep, an enqueue
   failure — and by the API's hourly sweep for any job already terminal. The
   hourly sweep never deletes the file of a job still pending or running: that
   job would render without the tracks it was given. A job that stays pending
   24 h or running past its time limit is failed first, which deletes it.
6. `/video/plan` receives the same plaintext to count clips, holds it only in
   memory for that request, and writes nothing.

What stays after the job: the **MP4 itself** is a picture of the decrypted
route, kept on the server for 30 days under `data/users/<uid>/videos/` and
downloadable by anyone holding the email link, then deleted by the hourly
sweep. The job row keeps the trip, the length and the resolution, no geometry.
Database backups never hold the geometry (it is not in the database).

## Posters — plaintext by consent, for one job

A poster is rendered on the server from a request the client builds from its
**decrypted** in-memory items (`posterMemoryJson` in `poster_job_notifier.dart`),
so on an encrypted trip the memory text would reach the server in plaintext.
The server asks first, per poster (issue #504):

1. `POST /api/projects/{name}/poster` (`create_poster_job`, `_require_consent`
   in `api/poster.py`) answers **409 `consent_required`** when the trip
   **owner** is encrypted, the request carries any memory `name` or
   `description`, and the body's `plaintext_consent` is not true. The detail
   names memory ids, never their text.
2. The client (`createPosterJobWithConsent`, `poster_consent_dialog.dart`)
   asks the user, who can send the text (resend with `plaintext_consent: true`),
   send the poster **without** memory text (names and descriptions removed;
   ids, places, dates and photos stay), or cancel.
3. The preview endpoint, `POST …/poster/preview`, asks nothing: it renders and
   stores nothing, so the plaintext it receives is received by design.
4. When a job ends — done or failed — `request_json` is replaced by a scrubbed
   copy (`scrub_request` in `src/poster/poster_job_runner.py`, an allow-list:
   layout choices, memory ids and photo ids; no title, region, memory text,
   dates or positions).
5. An hourly sweep (`sweep_poster_jobs`, scheduled in `api/router.py`) deletes
   the files and then the row of every finished job 30 days (`RETENTION_S`)
   after `completed_at`, for every user. It never touches a pending or running
   job; `sweep_orphaned_poster_jobs` fails jobs a dead process left behind at
   startup.

What stays until then: the **poster PNG/PDF itself** is a picture of the
decrypted memory text (if you agreed to send it), kept on the server for 30
days and downloadable with its link token.

## Remaining plaintext (and unreadable) cases

The list found while writing this page from the code (issue #433) had nine
entries; the remnant package for #504, #505, #506 and #366 fixed all but the
backups and the on-device cache, and added the cases below. The list is
complete as far as the code shows; each case names the code to check. Cases 3
and 5 are the two ways an activity row stays plaintext on an otherwise
encrypted account, so that every traveller on the trip can read it.

1. **Database backups** — `src/backup/backup_service.py` keeps the last 30
   daily SQLite copies. A backup taken before you enabled encryption holds the
   plaintext for up to 30 days after, and one taken before a catch-up pass
   holds what that pass had not yet encrypted.
2. **On-device cache (native apps only)** — `project_cache_store_native.dart`
   gzips the fetched trip payload asynchronously, while `ProjectNotifier`
   decrypts the same maps in place; the SQLite cache on the device can
   therefore hold decrypted content. It is protected by the device only (the
   web client has no on-disk cache).
3. **Activity rows you may not encrypt.** `PUT /api/activities/{id}` and the
   elevation-gain route accept the caller only when they own the row, or when
   the row is local (negative id: a GPX import or a split piece), at least one
   trip holds it, and every trip holding it is theirs
   (`activity_e2ee_writable_by` in `src/project/repo_activities.py`, enforced by
   `_require_e2ee_writable` in `api/activities.py`, which answers 404). A Strava
   row (positive id) imported by another user — for instance a former
   companion's — stays refused even when only your trips hold it: its owner's
   next Strava sync reuses the row, and it would come back to them encrypted
   under a key they do not have. A local row that is also in another user's
   trip is refused for the same reason. Such rows keep their name, track,
   endpoints and profile in plaintext. The catch-up counts them
   (`CatchUpResult.unencryptable`, a 404 on the write), the trip screen says
   "N activities are also used by another traveller and stay unencrypted"
   (one wording for this case and case 5; `_UnencryptableNotice` in `encryption_locked_banner.dart`, fed by
   `ProjectNotifier.unencryptableActivityCount`), and the enable screen reports
   the count after the migration (`migrationNotice` in
   `enable_encryption_screen.dart`).
4. **A companion's memories on a trip whose owner enabled encryption later.**
   This arises only from legacy state: before the invite checks above, a
   companion with an encrypted account could write memories into a plaintext
   owner's trip, encrypted under the **companion's** key, which the owner
   cannot read; the companion's own catch-up repairs these by re-saving them as
   plaintext — but if the owner has since enabled encryption, the server
   refuses that plaintext (409 `encryption_locked`, `encryption_guard_code` in
   `api/memories.py`), the repair is skipped (`_restoreMemory`), and the
   memories stay under the companion's key, unreadable to the owner ("Encrypted
   content unavailable"), until key sharing between travellers exists (#108).
   No new case can arise: companions cannot join an encrypted owner's trip, and
   the owner cannot enable encryption while the trip has companions.
5. **An activity row that another user's trip also holds.** A row that a trip
   owned by another user references is never stored encrypted, even when you
   own the row and even when it is also in your own trip: an envelope under
   your key would make the track, name, endpoints and profile unreadable in
   that traveller's trip (issue #106). The server enforces it. Any write that
   stores an envelope on such a row — `PUT /api/activities/{id}`, the encrypted
   track and split routes, the elevation-gain route — is refused with 409
   `shared_with_other_trip` (`_refuse_shared` in `api/activities.py`, backed by
   `activity_shared_with_others` in `src/project/repo_activities.py` and the
   SQL `referenced_by_others` in `src/project/repo_core.py`). A plaintext write
   from a caller who may write the row is still accepted, so its owner can
   decrypt it back. The trip payload carries `shared_with_others` per activity
   (computed in the project-load query by `src/project/repo_core.py`, emitted
   by `ProjectIO.to_dict`, absent from the `.traxj` export), which tells the
   app. The owner's catch-up (`encryption_migration.dart`) skips such rows and
   counts them with the rows of case 3, which the trip screen reports in a
   notice (`encryption_locked_banner.dart`). A row the earlier migration or
   catch-up had already encrypted is decrypted back to plaintext, all its
   encrypted fields included, when the pass can open its envelopes with your
   key; envelopes under another key are left as they are. The ride therefore
   stays readable in the other traveller's trip, and stays plaintext on the
   server for as long as that trip holds it. Once no other user's trip holds
   the row, the next catch-up encrypts it. The enable-time `run()` skips trips
   shared with you (`isSharedWithMe`); on those trips the catch-up does not
   encrypt your own activity rows.

   The repair also runs when you open a trip you do **not** own. A row you own
   can sit in a friend's trip that holds it, with envelopes under your key that
   the friend cannot read, and the owner's catch-up never runs on that trip. So
   the non-owner pass (`encryption_migration.dart`) decrypts those envelopes
   back to plaintext in the same way, writing with the compare-and-swap of the
   friend's trip: `PUT /api/activities/{id}` with `owner`, `project` and
   `lock_version` (`ActivityFieldsUpdate`, `update_activity_fields` in
   `api/activities.py`; `owner` is accepted only beside `project`, else 422).
   After such a write the server refreshes the cache of each trip holding the
   row under that trip's own owner, not under the caller.

## What the server checks

The server never decrypts. It recognises an envelope structurally
(`src/utils/encryption_check.py::is_encrypted_envelope`: three dot-separated
parts starting with `v1`) and uses that check in exactly these places, which
is therefore the list of columns the server *expects* may be ciphertext:

- `activity.name`, `start_latlng_json`, `end_latlng_json`, `summary_polyline`,
  `elevation_profile_json`, `elevation_profile_low_res_json` —
  `src/project/repo_activities.py` (never overwritten by a Strava re-sync,
  force refresh or enrichment once encrypted; parsed as absent and surfaced via
  `*_enc` keys on read), `src/models/prepared_geo.py`, `api/geo.py`,
  `api/project_transfer.py`.
- `memory.name`, `memory.description` — `api/memories.py` (translation purge
  and refusal, and the key guard of "When it happens"), `api/share.py` (strip
  from shared views, refuse translation), `api/project_shares.py` (share-content
  upload accepted only for encrypted memories). `journalentry.description` —
  the same key guard, keyed on the author (`api/journal.py`).

Where the server stores a client-computed value it can never read back, it
uses the strict `is_well_formed_envelope` instead (version `v1`, a wrapped key
and ciphertext of the exact lengths `EncryptedField.isWellFormed` checks,
standard base64): the geometry of the encrypted track and split routes, so
text a user typed, such as "v1.2.3", cannot get there.

The activity routes, in one place:

- `PUT /api/activities/{id}` (`ActivityFieldsUpdate` in `api/activities.py`)
  accepts only the six activity fields above plus the four `original_*`
  geometry snapshot columns (`original_polyline`,
  `original_elevation_profile_json`, `original_start_latlng_json`,
  `original_end_latlng_json`), and optional `project` + `lock_version` (given
  together) for a compare-and-swap. It is not a general editor.
- `PUT /api/activities/{id}/elevation-gain` — the encrypted activity's gain,
  see "Editing an encrypted track".
- `PUT /api/projects/{name}/activities/{id}/track/encrypted` and
  `POST …/split/encrypted` — a track edited or split on the device.
- `PUT …/track` and `POST …/split` — the plaintext routes; 409
  `encrypted_edit_on_device` for an encrypted row.
- `POST …/reset` — restores the snapshots; 409 `nothing_to_restore` when it
  has none to restore from.
- `GET …/track` — one activity's editor payload from a single-row read; for an
  edited row it also returns the four geometry snapshots, to callers with the
  editor role or above on a trip that may still rewrite the row
  (`activity_rewritable_by_trip`) or who may write the row's E2EE fields.

## How it works

- A random **Content Master Key (CMK)** encrypts your data (via per-item keys,
  XChaCha20-Poly1305). The server never sees the CMK.
- The CMK is **wrapped** (encrypted) to:
  - **each trusted device** — an X25519 key kept in the OS keystore, so daily
    use is automatic and passwordless; and
  - **a recovery method** you choose (below).
- **New device**: sign in, and it registers itself; approve it from an already
  trusted device (which re-wraps the CMK to it). No password typing.
- **Lost all devices**: use your recovery method to unlock and re-trust a device.

## Recovery — you pick the security level

The recovery method *is* the security level, because how you can get back in
determines who else can. Set at enable time:

| Level | Method | Zero-knowledge? | Notes |
|------|--------|-----------------|-------|
| **High** | Recovery key **or** passphrase | ✅ Yes, strongest | A key/passphrase only you hold. Lose it *and* all devices ⇒ data is unrecoverable **by design**. |
| **Medium** | Security questions | ⚠️ Yes, but weaker | Answers are low-entropy; someone with the server database can attempt an offline brute-force (slowed by Argon2id, not made strong). |
| **Low** | Email reset | ❌ **No** | *Not yet available.* Would let the server recover your key — meaning the admin **could** read your data. Encryption *at rest*, not *from the operator*. |

The recovery key is shown **once** at setup — save it (password manager / print).
The app keeps only the *encrypted* copy, never the plaintext key.

## Status / limitations (pre-release)

- **Scope**: memory and journal text, plus activity name, track, endpoints,
  elevation and edit snapshots, for everything an unlocked device of the owner
  has opened since it arrived. See "The window" and the remaining cases above.
- **A compromised device** — if your unlocked device or its OS keystore is
  compromised, the attacker can read what you can.
- **Low tier** (email recovery) is presented but disabled — it needs a server
  key-escrow + email subsystem (tracked separately).
- The recovery-key is currently rendered as grouped hex; a BIP39 word phrase is
  the intended final format.
- **Not yet validated on real Android/iOS hardware**: Argon2id timing, native↔web
  ciphertext interop with the production KDF, web secure-storage clear-data
  behaviour, and `flutter build web --wasm` bundle size. These must be confirmed
  before shipping to users.
