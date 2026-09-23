# Zero-knowledge encryption (issue #26)

TraxJourney can encrypt your **memories**, **journal entries** and **activity
tracks** so that even someone operating the server — an administrator with full
database and disk access — cannot read them. Encryption and decryption happen
entirely on your device; the server only ever stores ciphertext and wrapped
keys, and performs no cryptography itself.

Turn it on under **Settings → Encryption → Set up encryption**.

## What is encrypted

The list below is generated from the code, not from intent. It is pinned by
`encryptedFieldsByResource` in
`flutter_client/lib/src/crypto/encryption_migration.dart` and by
`flutter_client/test/crypto/encryption_coverage_test.dart`, which asserts that
the migration encrypts exactly those fields and re-sends exactly the plaintext
fields listed in the next section. Change one, and the test fails until the
other two are updated.

| Resource | Encrypted field (API key) | Database column | Encrypted where |
|---|---|---|---|
| Memory | `name` (title) | `memory.name` | on every create/update (`project_memory_crud_mixin.dart`) and by the enable-time migration |
| Memory | `description` (notes) | `memory.description` | same |
| Journal entry | `description` (entry body) | `journalentry.description` | on every create/update (`project_journal_crud_mixin.dart`) and by the migration |
| Activity | `name` | `activity.name` | **migration only** (see "When it happens") |
| Activity | `summary_polyline` (the GPS track) | `activity.summary_polyline` | migration only |
| Activity | `start_latlng_json`, `end_latlng_json` (track endpoints) | `activity.start_latlng_json`, `activity.end_latlng_json` | migration only |
| Activity | `elevation_profile_json`, `elevation_profile_low_res_json` (elevation profile, full and downsampled) | same-named columns | migration only |

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
- **Key material**: the Content Master Key wrapped to each device and to your
  recovery method (`device_key.wrapped_cmk`, `recovery_wrap.wrapped_cmk`).

## When it happens — and the gap

- **Memories and journal entries** are encrypted at the moment you save them,
  every time, once encryption is unlocked on the device
  (`EncryptionService.protect` in the two CRUD mixins).
- **Activities** are encrypted exactly once, by `EncryptionMigration.run()`,
  which is triggered only from the enable screen
  (`enable_encryption_screen.dart`). It walks every trip you own and encrypts
  the fields above for the activities that exist *at that moment*.
- Activities that arrive **after** you enabled encryption — a Strava sync, a
  GPX import, a split — are created server-side (`ActivityMixin._upsert_activity`,
  `src/gpx/importer.py`) in **plaintext**, and nothing re-runs the migration.
  They stay plaintext until encryption is re-enabled on a fresh account. This
  is a known gap, not a design choice.
- Memories imported from **Polarsteps** are posted by the import screen
  (`polarsteps_import_notifier.dart`) without going through `protect`, so they
  too land in plaintext even with encryption on. Editing such a memory once in
  the app re-saves it encrypted.
- Trips shared **with** you by another user are never migrated: only your own
  trips are, under your own key (issue #106).

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
| Activity | `type`, `distance`, `moving_time`, `elapsed_time`, `total_elevation_gain`, `start_date`/`start_date_local`, `timezone`, all Strava counters and flags (kudos, comments, photos, trainer, commute, private, …), `average_speed`/`max_speed`, heart-rate averages, `elev_high`/`elev_low`, `gear_id`, `source`/`source_id`, split/edit bookkeeping (`split_root_id`, `split_parent_id`, `split_base_name`, `is_edited`, `original_total_elevation_gain`) |
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
| `activity_geo_prepared` (zoom-level track cache, issue #369) | decoded track coordinates | **deleted** when the polyline becomes ciphertext (`store_prepared_geometry`); the backfill sweep skips `v1.` rows and its store is guarded against re-inserting plaintext |
| `project.stats_json` | totals, per-type counts, best-day **dates**, distance per tag/mode, sleeping counts, ride time series | numbers and dates only — no names, no coordinates; recomputed after migration |
| `memory_translation` | machine translations of memory `name`/`description` | **purged** when the memory becomes ciphertext; the server refuses to translate an encrypted memory (409) |
| Full-res geo response cache | GeoJSON built from plaintext tracks | busted for every trip the activity is in |
| GPX export, poster tracks | built from `summary_polyline` | GPX export refuses (409) any trip with an encrypted activity; the geo builders skip encrypted tracks |

## Known plaintext remnants (not yet fixed)

These were found while writing this page from the code (issue #433). They are
documented here so nobody relies on the doc over the database; each needs its
own fix.

1. **`stravacache.activities_json`** — the raw Strava activity list, one row
   per user, refreshed on every Strava sync (`api/strava.py::_save_cache`). It
   holds every synced activity's **name, start/end coordinates, summary
   polyline and dates** exactly as Strava returned them, regardless of
   encryption, and the migration does not touch it. Anyone with the database
   can read the last-synced tracks of an encrypted account from this column.
2. **`project.low_res_geo_json`** — a straight-line-per-activity GeoJSON with
   each activity's `name` and start/end coordinates. It is only rewritten by a
   full `save_project`, so after the migration it keeps the **pre-encryption
   names and endpoints** until the next full save. (No endpoint reads it any
   more — `api/geo.py` recomputes — but the column is still in the DB and in
   backups.)
3. **`activity.split_base_name`** — the plaintext pre-split name, captured the
   first time an activity is split. The migration encrypts `name` but neither
   scrubs nor encrypts this column, so an activity split before enabling
   encryption keeps its original title readable.
4. **Poster jobs** — the poster request (`posterjob.request_json`) is built by
   the client from its **decrypted** in-memory items (`posterMemoryJson` in
   `poster_job_notifier.dart`), so generating a poster sends memory titles,
   notes, dates and coordinates to the server in plaintext, stores them in the
   job row, and renders them into the poster PNG/PDF kept on the server for
   download. Nothing deletes job rows or files afterwards.
5. **Activities and Polarsteps memories added after enabling** stay plaintext
   (see "When it happens").
6. **Database backups** — `src/backup/backup_service.py` keeps the last 30
   daily SQLite copies. A backup taken before you enabled encryption holds the
   plaintext for up to 30 days after.
7. **On-device cache (native apps only)** — `project_cache_store_native.dart`
   gzips the fetched trip payload asynchronously, while `ProjectNotifier`
   decrypts the same maps in place; the SQLite cache on the device can
   therefore hold decrypted content. It is protected by the device only (the
   web client has no on-disk cache).

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
  and refusal), `api/share.py` (strip from shared views, refuse translation),
  `api/project_shares.py` (share-content upload accepted only for encrypted
  memories).

`PUT /api/activities/{id}` (`ActivityFieldsUpdate` in `api/activities.py`)
accepts only the six activity fields above plus the two `original_*` snapshot
columns the migration nulls out.

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

- **Scope**: memory and journal text, plus activity name, track, endpoints and
  elevation for activities present when encryption was enabled. See the gap
  and remnant lists above.
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
