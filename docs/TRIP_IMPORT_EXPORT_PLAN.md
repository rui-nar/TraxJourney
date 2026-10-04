# Trip import and export — Plan for #492, #367, #365, #368 and #408

Package D also lists #469 and #484. They already have their own reviewed
plan, `docs/TRIP_ZIP_IMPORT_PLAN.md` (on `feat/469-zip-import`), and a delivery
that stopped partway on 2026-09-28. **That delivery is resumed and finished
first** (owner decision, 2026-10-03), under its own plan. This plan covers the
other five issues and starts once #469 is on main.

## Problem

- **#492 — Trip import skips the trip-length quota.** `POST /api/projects/import`
  checks only `ensure_project_quota` (`api/project_transfer.py:235`), and never
  `ensure_trip_days_quota`. A user on a plan with a trip-length limit can import
  a longer trip than the plan allows. The same will hold for `/import-zip` once
  #469 adds it.
- **#367 — A GPX export doesn't survive a round trip.** `export_project_gpx`
  (`api/project_transfer.py:279-419`) writes one `<trk>` holding one
  `<trkseg>` per activity, plus one per connecting segment. The importer joins
  a track's segments into one candidate (`src/gpx/importer.py:282`). Exporting a
  trip and importing it back gives one activity covering the whole trip and the
  gaps in between, with no warning.
- **#365 — An imported activity's time is assumed to be UTC.**
  `import_gpx_activity` (`api/activities.py:809-843`) stores
  `start_date = start_date_local = <UTC instant>` and `timezone="UTC"`. Typed
  times are also read as UTC (`_resolve_times`, 550-551).
  - For a trip in Japan, the review step shows UTC wall-clock times, and an
    activity typed as "09:00" is stored 9 hours off.
  - GPX activities and Strava activities on the same day can sort wrong.
- **#368 — The app doesn't appear in "Open in…".** On Android, a `.gpx` from
  Mail, a download or Komoot can't be opened in TraxJourney. The user has to
  save it, open the trip, then browse back to it from the overflow menu.
- **#408 — A screen reader announces an imported activity's origin first.** The
  GPX badge is in the `ListTile`'s `leading`, so the row's merged label begins
  with "Imported from a GPX file", before the activity name.

## Current state

- **Trip import:** `import_project` (`api/project_transfer.py:174-256`).
  - Create and copy call `_repo.import_project`, which ends in
    `ingest_project`'s `sess.commit()` (`src/project/repo_transfer.py:208`).
  - Replace calls `_repo.replace_project`, which commits at 335.
  - After #469 lands, these functions also take staged photos and return a
    placement list, and `/import-zip` calls the same ingest.
- **Trip-length quota:** `ensure_trip_days_quota(sess, project_id, owner_id,
  *extra_dates)` (`src/billing/entitlements.py:330-366`).
  - It measures `used` and `prospective` from the same DB state through
    `project_day_bounds` (`src/billing/trip_days.py:72-113`). That function
    covers trip dates, memory and journal dates, activity `start_date_local`
    and segment dates.
  - It refuses only when `prospective > limit and prospective > used`.
  - The 402 body is built in `api/router.py:266-286`.
  - The precedent for checking inside a write is `add_activities`'s retry
    callback (`api/activities.py:411`).
- **GPX export:** a single `GPXTrack(name=project.name)`.
  - Activities are drawn from `summary_polyline`. Only the first point gets a
    time, and that time is `start_date_local`: the local clock, written as if
    it were UTC.
  - Connecting segments are 2-point great-circle arcs, written as segments of
    the same track.
  - Memories and day notes are `<wpt>`s.
  - An encrypted activity makes the whole export a 409.
- **GPX import:**
  - `candidates()` (`src/gpx/importer.py:268-303`) gives one candidate per
    `<trk>`, with segments joined. The type comes from
    `map_activity_type(track.type)`.
  - `POST /{name}/activities/gpx/inspect` (`api/activities.py:574-619`)
    returns the candidates.
  - `POST /{name}/activities/import-gpx` (720-902) imports one candidate,
    chosen by `track_index`. It refuses a duplicate with 409 (`source_id`
    fingerprint) and checks the trip-days quota (874).
  - The client dialog already has a one-track picker
    (`flutter_client/lib/src/projects/gpx_import_dialog.dart:521-548`) and
    already picks with `FileType.any` (161-176). That fixes the iOS
    greyed-out-file problem #368 mentions.
- **Timezones:** nothing in the repo maps coordinates to a zone.
  - The `Activity.timezone` column (`models/project_db.py:259`) is a plain
    string. Strava stores `"(GMT+01:00) Europe/Lisbon"`.
  - **Nothing reads the column** besides copying it through
    (`src/project/repo_activities.py:559,792,944,1047`,
    `src/models/activity.py:113,176`). No client code reads it.
- **Incoming files:**
  - There is no share-receiving plugin.
  - `AndroidManifest.xml` has only the launcher filter and an App Links filter
    for `/share`, `/join` and `/verify-email`, with
    `flutter_deeplinking_enabled=true` and `launchMode="singleTop"`.
  - `ios/Runner/Info.plist` has no document types.
  - iOS is not built by CI and does not ship.
  - `GpxImportDialog` needs a `ProjectNotifier` in context, so it opens only
    from the trip screen (`app_screen.dart:645-700`).
  - There is no trip-picker widget. The trips list is in `projects_screen.dart`.
- **GPX badge:** `_ActivityIconBox` (`flutter_client/lib/src/projects/activity_panel.dart:103-174`).
  - It is `Tooltip` → `Semantics(label: importedLabel)` → `Stack`, used as
    the row `ListTile`'s `leading` (1682+).
  - The trailing part has the Edit track and Delete local activity
    `IconButton`s.
  - The tests are in `flutter_client/test/activity_panel_gpx_badge_test.dart`.
  - Precedent for an authored label: `ScrollingSelectableText`
    (`lib/src/core/scrolling_selectable_text.dart:162-165`), which uses
    `Semantics(label:)` + `ExcludeSemantics`.

## Decisions

1. **#469 first.** The paused #469 delivery is rebased onto main and finished
   (U1–U5 of its plan, its integrated review, merge) before this plan starts.
   - Why: U8 must guard `/import-zip` as well as `/import`, and touches the
     same ingest functions #469's U2 rewrites. U3 builds on #469's U1, which
     changed how the GPX export is sent.
   - Rules out: delivering both at once in the same files.
2. **#492: measure the span after the write, before the commit.**
   - The import writes its rows as today. Just before the commit, the trip's
     span is measured with `project_day_bounds` on the written but uncommitted
     rows: the same definition every other trip-length check uses.
   - `used` is the trip's span before the import: measured before writing on
     Replace, and 0 for create and copy.
   - Over the limit and longer than `used` → `QuotaExceeded` (402, `resource:
     "trip_days"`), the transaction rolls back, and nothing is created or
     changed.
   - The comparison is factored out of `ensure_trip_days_quota` into one shared
     function, so the rule exists once.
   - It applies to `/import` and `/import-zip`, in every mode.
   - Why: on Replace, matched activities keep their stored dates
     (`repo_activities.py:892-910`), and settings the file doesn't carry keep
     the trip's value. Only the written state gives the true span.
   - Rules out: predicting the span from the parsed file.
3. **#367: one `<trk>` per activity, plus "import all tracks".**
   (Owner decision, 2026-10-03.)
   - **Export.** Each activity becomes its own `<trk>`:
     - `<name>` is the activity name and `<type>` its type, written so it
       reads back as the same type. The importer's type table covers Strava's
       sport types, not only run, ride, hike and walk (R1-5).
     - **Every point is timed** (R1-3). Times run from `start_date` (the true
       UTC instant) to `start_date + elapsed_time`, spread by cumulative
       distance, so other tools get a timed track.
     - The track's `<extensions>` carry `moving_time` and `distance` in a
       TraxJourney namespace. The import uses them when present, so distance,
       moving time and average speed round-trip exactly (R1-3, R2-3). A
       Strava activity's `summary_polyline` is simplified, so a length
       recomputed from it falls short.
     - **A row whose instant is unknown gets no `<time>` at all** (R1-4).
       That is a GPX row labelled `timezone = "UTC"`, which only pre-release
       rows carry (Decision 5). Re-importing such a track asks for its times instead of
       guessing. With such a track in the file, "Import all" refuses the file
       and points the user to one-at-a-time import (owner decision,
       2026-10-03, round-2 envelope question).
   - **Connecting segments.** Each is its own `<trk>` with
     `<type>traxjourney-connection</type>`, so other tools still draw the
     whole journey.
   - **Unchanged:** waypoints, the file name, `gpx.name`, `creator`, and the
     409 for encrypted activities.
   - **Import.** A new endpoint imports every importable, non-connection track
     of one file as separate activities, in one transaction. Today's one-track
     picker stays.
   - **Third-party files** with one track of several segments are still one
     candidate, as today (#367 acceptance criterion 3).
   - Rules out: recognising our own creator string, which preserves a broken
     shape and helps only our own files.
4. **#365: a per-activity zone from the track's start coordinate, looked up
   with `tzfpy`.**
   - The zone is resolved offline from the first point.
   - `tzfpy` was chosen over `timezonefinder`: its 3 MB Linux wheel contains
     its boundary data. The `timezonefinder` 9 wheel is 144 KB, so its data
     has to come from elsewhere.
   - The zone is stored as an IANA name (`"Asia/Tokyo"`). At sea, `tzfpy`
     returns an `Etc/GMT±N` zone. When the lookup fails, or gives UTC, the
     zone is `"Etc/UTC"`. The bare `"UTC"` label stays reserved for
     pre-release rows, whose instant is unknown (Decision 5).
   - **When the file has timestamps:** `start_date` is the true UTC instant,
     and `start_date_local` is the wall clock at that instant in the zone,
     stored in the same format Strava sync stores.
   - **When the user types times for a stamped track, and the request says
     they are local** (`times_local=true`, sent only by the new client): they
     are wall-clock times in that zone, converted to UTC for `start_date`.
     Without the field, typed times for a stamped track are read as UTC, as
     today, because that is what installed clients display and send (R1-1).
   - **A typed time that equals the file's own** (date and minute, after
     conversion) keeps the file's exact instant, so a start in a repeated DST
     hour isn't re-derived as the other occurrence (R2-2).
   - **An untimed track (no stamps): typed times are always wall clock in
     the track's zone, with or without `times_local`** (R2-1, R3-1).
     - Installed clients prefill nothing for such a track. The user picks a
       raw wall clock and the client sends it verbatim, the same bytes the
       new client sends.
     - The server tells the two cases apart by the file itself (the
       candidate has no time span), not by the client.
     - `start_date_local` keeps the typed time, so every client shows what
       was typed, as today. `start_date` becomes the true instant.
   - **The review step** (`inspect`) keeps `started_at`/`ended_at` as UTC
     instants, which is what installed clients expect. It adds `timezone` and
     naive wall-clock `start_local`/`end_local`. The new client prefills from
     those and shows the zone (R1-2); Dart has no zone database, so the
     server does the conversion.
   - Each track resolves its own zone, so a trip crossing zones is handled per
     activity.
   - Rules out: a trip-level zone, which is wrong for any trip that crosses
     zones; and asking the user.
5. **#365 backfill: existing GPX rows are left alone.** (Owner decision,
   2026-10-03, reversing the first choice of a zone relabel after R1-4.)
   - Old rows can't say whether their time was a true instant (stamped file)
     or a typed wall clock, so moving the times would be a guess.
   - Their `timezone = 'UTC'` label is kept on purpose. It is the only marker
     of a row whose instant is unknown, and the GPX export uses it to leave
     out times it can't vouch for (Decision 3).
   - Rules out: relabelling the zone. That would erase the marker, and a
     re-import would move typed times by the zone offset to another day.
   - No migration. The choice is recorded on #365.
6. **#368: Android handler, iOS declarations, one Dart flow.** (Owner decision,
   2026-10-03.)
   - **Android:** intent filters for `ACTION_VIEW` and `ACTION_SEND` with GPX
     MIME types.
   - **iOS:** a `CFBundleDocumentTypes` entry and a
     `UTImportedTypeDeclarations` entry for `com.topografix.gpx`, the type
     used for GPX, conforming to `public.xml`, extension `gpx`.
   - **The shared Dart flow:**
     1. A received file is held as a pending GPX import.
     2. If signed out, the user signs in and then continues.
     3. A trip picker lists the user's trips.
     4. The chosen trip's screen opens the existing `GpxImportDialog` with the
        file already loaded, so it goes straight to inspection.
   - Only Android is verified. iOS stays unverified until iOS ships.
   - Rules out: a separate import flow for shared files. The dialog, review
     and server path stay the single path.
7. **#408: the row's spoken label is authored.**
   - The badge's semantics leave the `leading`.
   - The title gets an explicit label: name, then distance and duration, then
     "Imported from a GPX file" last.
   - The trailing buttons keep their own semantics nodes, so they stay
     reachable.
   - The tooltip stays, for sighted pointer users.
   - Rules out: `hint`, which VoiceOver users can turn off (rejected in the
     PR #406 review); and sort keys on every part of the row.

## Review envelope

What this plan adds to REVIEW.md §2's defaults:

- **Shared files are untrusted input (E2)** from any app on the phone: any
  size, any MIME type, any name, possibly not GPX. The client must not read a
  file larger than the server's GPX upload cap into memory. The server's GPX
  parsing and limits stay the only validation.
- **`tzfpy` is trusted code with trusted data,** used offline. Its input
  coordinates come from untrusted GPX files, already range-checked by the GPX
  parse layer.
- **The quota check inside a transaction** (Decision 2) adds no new
  concurrency. Imports run one at a time once #469 is in (its Decision 12).

## Boundaries crossed

- **Export format (GPX):** one `<trk>` per activity and per connecting
  segment, instead of one `<trk>` of segments. Each activity track has a
  `<type>`, correct UTC times on every point (none for rows whose instant is
  unknown), and a TraxJourney extension carrying `moving_time` and `distance`
  (R3-3).
  - Tools that read the old shape get more tracks than before. A GPX export is
    an interchange file; `.traxj` is the archive.
  - Old exports stay importable as today (one joined candidate).
- **API, `inspect` (existing):** additive only. Each candidate gains
  `timezone`, `start_local`, `end_local` and `is_connection`. `started_at` and
  `ended_at` keep their meaning (UTC instants), so installed clients (E5)
  behave exactly as today.
- **API, `import-gpx` (existing):** a new optional form field,
  `times_local`.
  - For a stamped track, only with `times_local=true` are typed `date`,
    `start_time` and `end_time` read as wall-clock times in the track's zone.
    Without it they are UTC, as installed clients send them (R1-1).
  - For an untimed track, typed times are wall clock in the zone either way.
    That is what every client sends there, so an installed client still
    shows the time it typed (R3-1).
  - Untouched times come from the file's stamps, now with the right local
    time and zone.
- **API, new:** `POST /api/projects/{name}/activities/import-gpx-tracks`.
- **API, `/import` and `/import-zip`:** can now answer 402 `trip_days`. The
  body is shaped like every other quota refusal; installed clients show the
  `detail`.
- **Stored data:**
  - New GPX imports store real UTC instants, local times and an IANA zone,
    whichever client sent them.
  - Existing GPX rows are untouched (Decision 5).
- **Schema:** none. No migration.
- **Dependencies:** `tzfpy` is added to `requirements.txt`, which grows the
  image by about 3 MB. Its licence is permissive and compatible with the
  project's.
- **Platform manifests:** new Android intent filters; new iOS document-type
  and UTI declarations.

## Conventions

- **One zone helper:** every GPX time conversion goes through
  `src/gpx/timezone.py` (U1). No other module calls `tzfpy` or builds a
  `ZoneInfo` for GPX.
- **Times on activities:** for every GPX row written from now on,
  `start_date` is the true UTC instant and `start_date_local` the wall clock
  in `timezone`, formatted like Strava's. The one exception is a GPX row
  labelled `"UTC"`: a pre-release row whose instant is unknown (Decision 5).
  New code never writes that label. A resolved zone that is UTC is written
  `"Etc/UTC"`.
- **The quota comparison exists once,** in `src/billing/entitlements.py`
  (Decision 2).

## Open decisions

- **Android MIME types (U4, decided at delivery with on-device evidence).**
  Gmail and some file managers hand a `.gpx` over as
  `application/octet-stream`. A filter on that type puts TraxJourney in the
  share sheet for every binary file. `pathPattern` doesn't work on
  `content://` URIs.
  - U4 tests Gmail, Files and Chrome downloads on a device or emulator with
    only `application/gpx+xml`, `application/gpx` and `text/xml`.
  - It reports which sources still miss before `application/octet-stream` is
    added. The owner decides on that report.
- **Closing #368.** Close it with a new iOS-verification issue once Android is
  verified, or leave it open until iOS ships. Decide at merge.
- **Share-receiving plugin (U4).** Which plugin (`receive_sharing_intent`,
  `share_handler`, or a small platform channel) is a design decision local to
  U4. It must keep `flutter_deeplinking_enabled` App Links working, and must
  not let go_router try to route a `content://` URI.

## Execution units

**Precondition:** the #469 delivery is merged to main (Decision 1). Every unit
branches from that main.

### Wave 1 — disjoint, four units

**U1 — Per-activity timezone for GPX imports (#365)**
- **Goal:** GPX imports store the true UTC instant, the wall clock in the zone
  of the track's start, and an IANA zone name; the review step shows local
  times.
- **Scope:**
  - `src/gpx/timezone.py` (new) and `requirements.txt`;
  - `api/activities.py`: `_resolve_times`, `inspect_gpx_file`,
    `GPXInspectOut` and `import_gpx_activity` only;
  - `tests/test_gpx_timezone.py` (new);
  - `tests/test_gpx_import_api.py` and `tests/test_gpx_inspect_api.py`, only
    where an assertion encodes UTC-as-local, and each such change listed in the
    report.
- **Context:**
  - Read #365 and `_resolve_times` (`api/activities.py:513-561`);
  - the Activity construction (809-843);
  - `GpxCandidate` start and end times (`src/gpx/importer.py:100-180`);
  - how Strava sync formats `start_date_local` (`api/strava.py`, and
    `src/models/activity.py`);
  - `_zone()` in `src/services/hafas_service.py:477-483`, the example for
    zone handling.
- **Do:**
  1. Add `tzfpy` to `requirements.txt`.
  2. Write `src/gpx/timezone.py`:
     - `zone_at(lat, lon) -> str`, an IANA name. It returns `"Etc/UTC"` when
       the coordinate is missing, the lookup fails, or the zone is UTC. It
       never returns the bare `"UTC"` (Decision 5);
     - `to_local(instant_utc, zone) -> datetime`;
     - `local_to_utc(naive_wall_clock, zone) -> datetime`. DST: an ambiguous
       time takes `fold=0`; a time that doesn't exist moves forward by the gap.
       Both are documented in the docstring.
  3. `inspect`: resolve each candidate's zone from its first point. Keep
     `started_at`/`ended_at` as they are, as UTC instants. Add `timezone`,
     and naive wall-clock `start_local`/`end_local` in that zone (leave
     `is_connection` to U5).
  4. `import-gpx`:
     - stamped times: real UTC for `start_date`, local for
       `start_date_local`;
     - stamped track, typed times with `times_local=true`: wall clock in the
       zone, converted to UTC;
     - stamped track, typed times without it: UTC, exactly as today
       (installed clients, R1-1);
     - a typed time equal to the file's own (date and minute) keeps the
       file's exact instant (R2-2);
     - untimed track (the candidate has no time span): typed times are wall
       clock in the zone, with or without `times_local` (R3-1);
     - the duplicate fingerprint of an untimed track (`_import_fingerprint`,
       `api/activities.py:505-510`) stays based on the typed wall clock
       labelled UTC, the bytes old and new clients both send, never on the
       new true instant. Re-imports across the release still match (R4-1);
     - `timezone`: the zone;
     - crossing midnight, duplicates and the trip-days quota keep working on
       the local date.
- **Acceptance:**
  - `tests/test_gpx_timezone.py`: Tokyo, Lisbon in summer and in winter, a
    point at sea, a missing coordinate, a DST gap and a DST fold.
  - API tests:
    - a stamped Tokyo track gives `start_date` 00:00Z,
      `start_date_local` 09:00 and `timezone` `"Asia/Tokyo"`;
    - a typed "09:00" in Tokyo with `times_local=true` gives `start_date`
      00:00Z;
    - **the installed-client request:** typed "00:00" without `times_local`
      gives `start_date` 00:00Z and `start_date_local` 09:00, the same instant
      that client showed (R1-1);
    - a trip with one Lisbon track and one Tokyo track stores each with its
      own zone, and the trip-days check sees local dates;
    - `inspect` keeps `started_at` byte-identical to today's output, and adds
      `timezone`, `start_local` and `end_local`;
    - an untimed Tokyo track with typed 20:00 stores `start_date` 11:00Z,
      `start_date_local` 20:00 on the typed day and `timezone`
      `"Asia/Tokyo"`, identically with and without `times_local` (R2-1,
      R3-1);
    - a stamped Lisbon track starting in the second occurrence of the
      repeated October hour, re-sent with only the end time changed and
      `times_local=true`, keeps its exact start instant (R2-2);
    - an untimed route imported the pre-release way (row fingerprinted on
      typed 20:00 as UTC), then re-imported with the same typed times, gets
      409 (R4-1);
    - no import writes the bare `"UTC"` label.
  - The pytest CI command passes.
  - Report the Docker image size before and after.
- **Out of scope:** existing rows (Decision 5); the GPX export (U3); batch
  import (U5); the client (U7).
- **Latitude:** local design.
- **Escalate if:**
  - `tzfpy` has no wheel for the image's Python and platform;
  - the image grows by more than 10 MB;
  - something besides copying reads `Activity.timezone` or relies on GPX
    `start_date_local` being UTC;
  - X3.
- **Depends on:** —

**U2 — Spoken order of an imported activity row (#408)**
- **Goal:** a screen reader announces an imported activity's name first, its
  stats next, and "Imported from a GPX file" last; the row's buttons stay
  reachable.
- **Scope:** `flutter_client/lib/src/projects/activity_panel.dart`
  (`_ActivityIconBox` and the activity row's `ListTile` only);
  `flutter_client/test/activity_panel_gpx_badge_test.dart`.
- **Context:** #408. `ScrollingSelectableText`
  (`lib/src/core/scrolling_selectable_text.dart:162-165`) is the example to
  follow: `Semantics(label:)` around an `ExcludeSemantics` child.
- **Do:**
  1. In `_ActivityIconBox`, put the badge (Tooltip included) inside
     `ExcludeSemantics`, so `leading` adds nothing to the row's label. Keep
     the Tooltip and the `gpx_source_badge` key.
  2. Wrap the row's `title` Column in `Semantics(label: <name>, <stats>[,
     Imported from a GPX file], excludeSemantics: true)`. Use
     `_ActivityIconBox.importedLabel`, and add the phrase only when `source ==
     'gpx'`.
  3. Leave `trailing` untouched.
- **Acceptance:** widget tests with `tester.ensureSemantics()`:
  - the row's label starts with the activity name and ends with "Imported from
    a GPX file" for a GPX activity;
  - a synced activity's label has no such phrase;
  - "Edit track" and "Delete local activity" are still found by semantics
    label;
  - the existing badge-visibility and tooltip tests pass;
  - `flutter analyze` and `flutter test` pass (in the Flutter test container).
- **Out of scope:** the activity editor's AppBar badge; any other row.
- **Latitude:** none.
- **Escalate if:** selecting text in the title, or tapping the row, stops
  working; X3.
- **Depends on:** —

**U3 — GPX export: one track per activity (#367, export half)**
- **Goal:** the GPX export writes one `<trk>` per activity, with name, type
  and correct UTC times, and one `<trk>` per connecting segment, typed
  `traxjourney-connection`.
- **Scope:**
  - `api/project_transfer.py` (`export_project_gpx` and its helpers only);
  - `src/gpx/export_format.py` (new: `CONNECTION_TRACK_TYPE`, the extension
    namespace and the per-point time spreading, also used by U5);
  - `src/gpx/importer.py` (only `_TYPE_ALIASES`/`map_activity_type`, and
    exposing the extension's `moving_time` and `distance` on the candidate,
    as new optional fields);
  - `tests/test_gpx_export_tracks.py` (new).
- **Context:**
  - `export_project_gpx` as #469's U1 left it;
  - `candidates()`, `GpxCandidate.moving_seconds` (`importer.py:100-191`)
    and `map_activity_type` (75-88, 342-354): the export must read back
    through them;
  - how `import_gpx_activity` turns the candidate into moving time and
    average speed (`api/activities.py:780-807`), read only.
- **Do:**
  1. Write the export as Decision 3 says. Activities without geometry are
     left out, as today.
  2. Spread per-point times by cumulative distance over the elapsed time.
  3. Add the `moving_time` and `distance` extension, from the activity's
     stored values.
  4. Leave out every `<time>` for a GPX row labelled `"UTC"` (R1-4).
  5. Widen the type table to Strava's sport types (R1-5), so every type the
     app stores reads back as itself. Unknown types still map to `None`.
  6. Expose the extension's values on the candidate. Using them in the
     import endpoints is U5's job (`api/activities.py` belongs to U1 in this
     wave).
- **Acceptance:** `tests/test_gpx_export_tracks.py`:
  - a trip of 3 activities and 2 connecting segments exports 5 tracks;
  - the 3 activity tracks read back through `candidates()` as 3 candidates
    with the original names, types, and UTC start and end times. The carried
    moving time and distance equal the stored values, including for a Strava
    activity whose polyline is simplified (R1-3, R2-3). Local dates are
    checked in U5, which has U1's zone helper (R2-4);
  - every activity point carries a time, increasing along the track;
  - a Kayaking, a Swim and an AlpineSki activity read back as those types
    (R1-5);
  - a GPX row labelled `"UTC"` exports with no `<time>`, and reads back as a
    candidate without times (R1-4);
  - the connection tracks carry the type constant;
  - waypoints are unchanged;
  - an encrypted activity still gives 409;
  - the existing GPX import tests pass unchanged;
  - the pytest CI command passes.
- **Out of scope:** import endpoints (U5); `.traxj` and ZIP export.
- **Latitude:** local design.
- **Escalate if:**
  - an activity's `start_date` is missing or isn't UTC in stored data you meet
    in tests or fixtures;
  - widening the type table changes an existing import test's outcome;
  - X3.
- **Depends on:** —

**U4 — Open a shared `.gpx` in the app (#368)**
- **Goal:** on Android, "Open in" and "Share" a `.gpx` lists TraxJourney.
  Choosing it leads (after sign-in if needed) to a trip picker, then to that
  trip's GPX import dialog with the file already loaded. iOS gets the same
  declarations and Dart flow, unverified.
- **Scope:**
  - `flutter_client/pubspec.yaml`, if a plugin is chosen;
  - `flutter_client/android/app/src/main/AndroidManifest.xml`;
  - `flutter_client/ios/Runner/Info.plist`;
  - `flutter_client/lib/src/core/app_router.dart`, for a new route and the
    sign-in return;
  - `flutter_client/lib/src/projects/incoming_gpx.dart` (new: receiving and
    holding the pending file);
  - `flutter_client/lib/src/projects/trip_picker_screen.dart` (new);
  - `flutter_client/lib/src/projects/gpx_import_dialog.dart`, only to accept
    an initial file (name plus bytes) that skips the pick step;
  - `flutter_client/lib/src/projects/app_screen.dart`, only to open the
    dialog with a pending file once the trip is loaded;
  - the tests under `flutter_client/test/projects/`.
- **Context:**
  - #368;
  - `_openGpxImportDialog` (`app_screen.dart:645-700`);
  - `_pickFile`/`_inspect` (`gpx_import_dialog.dart:161-226`);
  - `authRedirectTarget` and `safeReturnTo` (`app_router.dart:66-143`), the
    example for resuming after sign-in;
  - `projects_screen.dart` for listing trips;
  - `isNativeMobile` (`lib/src/core/platform.dart:12`).
- **Do:**
  1. Choose how files are received (Open decisions).
  2. Add the Android filters with the narrow MIME set, and the iOS
     declarations from Decision 6.
  3. Hold the received file. Refuse it with a message, without reading it
     into memory, when it is larger than the server's GPX upload cap.
  4. Route to the picker, after sign-in if needed.
  5. Open the chosen trip, and its dialog with the file, at the inspect step.
  6. Cold start and warm start (`singleTop`) both work.
  7. Web is unaffected.
- **Acceptance:**
  - widget tests: the dialog given an initial file skips the pick step and
    inspects it; the picker lists trips and opens the chosen one; a pending
    file survives the sign-in redirect; an oversized file is refused without
    being read;
  - the existing `gpx_import_dialog_test.dart` passes;
  - `flutter analyze`, `flutter test` and `flutter build apk --debug` pass;
  - the report gives the on-device or emulator result for Gmail, Files and a
    Chrome download, and which of them need `application/octet-stream`.
- **Out of scope:**
  - importing `.traxj` or `.zip` from the share sheet;
  - several files in one share;
  - iOS device testing;
  - changes to the dialog beyond the initial file.
- **Latitude:** local design.
- **Escalate if:**
  - the App Links filter (`/share`, `/join`, `/verify-email`) stops working;
  - go_router receives a `content://` location;
  - the plugin needs a minimum SDK change or an iOS share extension target;
  - X3.
- **Depends on:** —

### Wave 2 — after wave 1 is integrated

**U5 — Import every track of a GPX file (#367, import half)**
- **Goal:** one request imports every importable, non-connection track of a
  file as separate activities, all or nothing.
- **Scope:** `api/activities.py`: a new route
  `POST /{name}/activities/import-gpx-tracks`; in `inspect`'s candidates,
  `is_connection`, and `distance_m`/`moving_seconds` preferring the carried
  values (R3-2); and in `import_gpx_activity`, only the distance, moving time
  and average speed lines, so they prefer the candidate's carried values
  (R2-3). `src/gpx/importer.py` (mark connection candidates only);
  `tests/test_gpx_import_all_tracks.py` (new).
- **Context:**
  - `import_gpx_activity` as U1 left it, which is the model;
  - `CONNECTION_TRACK_TYPE` from U3;
  - `_existing_import` (564-571) for duplicates;
  - `ensure_trip_days_quota` (874).
- **Do:**
  1. Mark the candidates whose track `<type>` is the connection type.
  2. The new route takes the file and nothing else. Every importable
     non-connection candidate needs times; if any lacks them, answer 400
     naming the tracks, so the user imports them one at a time.
  3. For each track:
     - names, types and times come from the file;
     - distance and moving time come from the carried values when present,
       as in `import-gpx`, and average speed is computed from them;
     - its zone comes from U1's helper;
     - a duplicate of an activity already in the trip is skipped and listed.
  4. Check the trip-days quota once, over every date.
  5. Insert in one transaction.
  6. Respond `{"imported": [{"activity_id", "name"}], "skipped":
     [{"track_index", "name", "duplicate_of"}]}`.
  7. Queue the same refreshes as `import-gpx`.
- **Acceptance:** `tests/test_gpx_import_all_tracks.py`:
  - U3's export of a 3-activity trip, imported into an empty trip, gives
    3 activities with the original names, types, local dates, times,
    distance, moving time and average speed, one of them a Strava activity
    with a simplified polyline (#367 acceptance 1, R1-3, R2-3, R2-4);
  - the single-track `import-gpx` of one of those tracks gives the same
    distance, moving time and average speed;
  - `inspect` of that export reports, for the Strava track, the same
    `distance_m` and `moving_seconds` the import stores (R3-2);
  - a file with an untimed track (a pre-release row's export) gives 400
    naming it, and imports nothing (owner decision, round 2);
  - the connection tracks are not imported;
  - re-importing skips all 3 as duplicates;
  - over the trip-days limit gives 402 and imports nothing;
  - tracks in two zones each get their own zone;
  - the pytest CI command passes.
- **Out of scope:** recreating connecting segments from connection tracks; the
  client (U7).
- **Latitude:** local design.
- **Escalate if:** sharing code with `import_gpx_activity` would change its
  behaviour beyond preferring the carried values; X3.
- **Depends on:** U1, U3.

*U6 — removed after review round 1 (R1-4): existing GPX rows are left alone
(Decision 5), so there is no relabel migration.*

**U7 — Client: local times and "Import all tracks" (#365, #367)**
- **Goal:**
  - the review step shows and prefills the wall clock at the track's start,
    names the zone, and sends typed times as local (R1-2);
  - when a file holds several importable non-connection tracks, the choose
    step offers "Import all N tracks" beside the per-track list.
- **Scope:** `flutter_client/lib/src/projects/gpx_import_model.dart`;
  `flutter_client/lib/src/projects/gpx_import_dialog.dart`;
  `flutter_client/lib/src/projects/app_screen.dart` (only the result handling
  in `_openGpxImportDialog`);
  `flutter_client/test/projects/gpx_import_dialog_test.dart` and the model's
  tests.
- **Context:**
  - `_parseTime` and its `.toUtc()` (`gpx_import_model.dart:91-101`);
  - the prefill (`gpx_import_dialog.dart:239-245`) and `_submit` (339-375),
    which are the example;
  - U1's and U5's response contracts.
- **Do:**
  1. Prefill date and times from `start_local`/`end_local`, as naive wall
     clocks, never converted through the device zone. Show `timezone` beside
     the times.
  2. Send `times_local=true` with every import that sends typed times.
  3. Fall back to today's behaviour when the server gives no `start_local`.
  4. Show connection candidates labelled as connections, not in the
     import-all count.
  5. "Import all" posts to `import-gpx-tracks`.
  6. The result reports how many were imported and how many skipped as
     duplicates.
  7. Undo removes every imported activity.
  8. Show 400 and 402 `detail`s as the single-track path does.
- **Acceptance:**
  - widget tests:
    - a Tokyo candidate (`started_at` 00:00Z, `start_local` 09:00) prefills
      09:00 and shows "Asia/Tokyo", whatever the test's device zone;
    - editing the end time sends 09:00 with `times_local=true`;
    - the "Import all" button appears only with 2 or more importable tracks;
      it posts to the right endpoint; skipped duplicates are reported;
      connection tracks are labelled;
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** editing names or types per track before import-all.
- **Latitude:** local design.
- **Escalate if:** X3.
- **Depends on:** U4 (same dialog file), U1's and U5's contracts.

**U8 — Trip import checks the trip-length quota (#492)**
- **Goal:** `/import` and `/import-zip` refuse with 402 `trip_days`, before
  committing anything, an import that would make the trip longer than the plan
  allows and longer than it already was.
- **Scope:**
  - `src/billing/entitlements.py` (factor the comparison out of
    `ensure_trip_days_quota`; no other change);
  - `src/project/repo_transfer.py` (a check just before the commit in ingest
    and in Replace);
  - `api/project_transfer.py` (the import routes only: pass the owner and the
    check);
  - `tests/test_import_trip_days_quota.py` (new).
- **Context:**
  - Decision 2;
  - the retry callback in `add_activities` (`api/activities.py:411`), the
    example of a quota checked inside a write;
  - `tests/test_trip_days_quota.py` and `tests/test_import_replace.py:362`
    for test setup;
  - #469's ingest and placement order: the check must fail before commit, so
    no photo is placed.
- **Do:**
  1. Write `ensure_trip_span_quota(sess, owner_id, used, prospective)`, and
     have `ensure_trip_days_quota` call it.
  2. On Replace, measure `used` before writing.
  3. Just before each commit, measure `prospective` with
     `trip_days_used(sess, project_id)` on the flushed rows, and call the
     shared function.
  4. A refusal rolls back, and `/import-zip` removes its staging directory as
     on any failure.
- **Acceptance:** `tests/test_import_trip_days_quota.py`, for `/import` and
  `/import-zip`:
  - create, copy and replace over the limit give 402 `trip_days`, with no new
    trip, no changed row and no placed photo;
  - a trip already over the limit (a downgrade) replaced by its own export is
    allowed;
  - a Replace that shortens the trip is allowed;
  - quotas off allows everything;
  - the existing quota and import tests pass unchanged;
  - the pytest CI command passes.
- **Out of scope:** the client (it shows `detail`); other quotas on import.
- **Latitude:** local design.
- **Escalate if:** the ingest's retry loop would run the check on an attempt
  that doesn't commit, and refuse wrongly; any existing test assertion would
  change; X3.
- **Depends on:** #469 merged.

W1/W4 check:
- **Wave 1** scopes are disjoint:
  - U1: `api/activities.py`, `src/gpx/timezone.py`, `requirements.txt`;
  - U2: `activity_panel.dart`;
  - U3: the export in `api/project_transfer.py`, `src/gpx/export_format.py`,
    and the type table and extension in `src/gpx/importer.py`;
  - U4: the dialog's initial file, the router and the app screen.
- **Wave 2** scopes are disjoint:
  - U5: `api/activities.py` and the connection mark in `importer.py`;
  - U7: `gpx_import_model.dart`, the dialog, and the app screen's result
    handling;
  - U8: the import section of `project_transfer.py`, `repo_transfer.py` and
    `entitlements.py`.
- No unit adds a migration.

## Definition of done

- Importing a trip (`.traxj` or ZIP, any mode) that would make it longer than
  the plan allows, and longer than it was, is refused with 402 `trip_days`,
  and nothing is created or changed (#492).
- A trip with 3 activities, exported as GPX and imported back with "Import all
  tracks", gives 3 activities with their names, types, dates, times,
  distance, moving time and average speed, Strava activities included (#367). A third-party file of one track with several
  segments still imports as one activity.
- A GPX recorded in Tokyo shows and stores the Tokyo wall clock, stores the
  true UTC instant and `timezone = "Asia/Tokyo"`. In the new client, a typed
  time is the local wall clock. An installed client's import stores the
  instant it showed for a stamped track, and the wall clock it typed for an
  untimed one. No import writes the bare `"UTC"` label. Existing GPX rows are unchanged; when exported, those
  rows carry no times (#365).
- On an Android device, sharing a `.gpx` from Gmail, Files or a Chrome
  download offers TraxJourney. Choosing it leads to a trip picker, then to the
  import review step with the file loaded. The iOS declarations are in place,
  unverified (#368).
- A screen reader announces an imported activity's name first and "Imported
  from a GPX file" last; the row's buttons are still reachable (#408).
- The pytest CI command, `flutter analyze`,
  `flutter test` and `flutter build apk --debug` pass.
- #365 records the backfill choice. Each issue is closed by the PR that fixes
  it.
