# Client state and the map — Plan for #418, #294, #379, #278, #397, #478, #401

## Problem

Package C groups client-side defects and debt around the app-wide
`ProjectNotifier` and the map.

- **#418: another account's data survives a logout. This is the most urgent item.**
  - `ProjectNotifier` is created once for the app, and logout never clears it.
    Account B can be shown account A's whole trip, filters and selection. The
    filter half is confirmed by a widget test in the #415 review.
  - `User.id` is `''` after every session restore. As a result:
    - `last_opened_project` is shared by every account on a device;
    - `projectDataCache` keys everything to user `0`;
    - `resolveRoleFor` guesses every own trip as "editor".
  - The two forced-logout (401) paths do not lock E2EE.
  - A notifier that `AppScreen` reuses without reloading can write stale
    filters over state that view mode saved (issue #418, second comment).
- **#294: `ProjectNotifier` holds everything.** It is 3,437 lines, plus
  2,104 more in six mixins, with 122 `notifyListeners()` calls in total.
  - Every notify rebuilds every listener, the map included.
  - `map_panel.dart` holds about 30 `_lastX` identity guards in two copies
    to undo that.
  - Measurements (#296 comment, `docs/PERF_MAP_LOAD.md` Phase 3) show this
    is no longer a performance problem. The owner chose the full facet split
    (2026-10-04) as a maintainability change.
- **#379: an edit bypasses the zoom level of detail.** Three
  post-mutation paths assign full-resolution geometry to `geo`
  (`getGeo(bypassCache: true)`) and never update `_loadedZoomBucket` or
  `_loadedGeoBox`.
  - The map then keeps the full payload (≈180 MB of Dart heap on the
    219-activity trip) until the user crosses a zoom bucket.
  - It also writes that payload into the L1 cache.
- **#278: the map does not show a train route that resolves late.**
  - The client stops polling a resolve after 2 minutes and never polls
    again.
  - The server runs two resolve jobs at a time, so a third resolve queued
    behind them easily misses that window.
  - Separately, `reconcileSegmentOverlay` drops a pending patch as soon as
    the server geometry contains the segment's id, without checking whether
    the route in it is the resolved one. A refetch that started before the
    resolve finished can put the old great-circle line back.
- **#397: two devices revert each other's day notes.**
  - `PUT /day-meta` replaces the whole map with whatever the client loaded,
    with no check.
  - The settings screen sends its snapshot, taken when the screen opened,
    on every save, even a colour change.
- **#478: turning on auto-zoom ignores the current selection.** Auto-zoom
  only fires when the selection *changes*, so switching it on does nothing
  until the next tap.
- **#401: a session spends 10–11 s fetching geometry, and we cannot tell
  where it goes.**
  - The `fetch_geo_lod` span lumps queueing, server work, transfer and gunzip
    together.
  - The client discards every response header.
  - The numbers predate the server's cold-path fixes (#369).
  - A pinch that oscillates around an integer zoom fetches twice.

## Current state

- **Auth** (`flutter_client/lib/src/auth/auth_notifier.dart`)
  - `logout()` (272-288) locks E2EE, nulls the user, then clears
    `projectDataCache` and `photoThumbCache`. The clears come after the
    `finally` on purpose (#429).
  - The 401 paths in `init()` (135-139) and `_refreshMeInBackground()`
    (183-188) only call `_service.logout()` and null the user.
  - The `notifyListeners` override (79-85) calls
    `projectDataCache.setCurrentUser(int.tryParse(user?.id))`.
  - `User.fromMap` (37-46) reads `map['id']`. `User.restored` (50-56) has
    `id: ''`.
- **`GET /api/auth/me`** (`api/auth.py:377-391`) returns the JWT payload
  (`sub`, no `id`) with no response model. Login and register responses do
  carry `id` (`_token_response`, 152-167). `ApiClient.tokenUserId`
  (`client.dart:42-53`) already decodes `sub`.
- **App-wide `ProjectNotifier`** (`lib/main.dart:75`)
  - `clear()` (`project_notifier.dart:2168-2211`) is called only from
    tests. It misses `_heldStateKey`, `people`, `groups`, `selectedDays`,
    `selectedJournalId`, `pendingSync`, the segment overlay, the photo
    polling and degraded-route timers, the share tokens, the sync settings,
    and more (see U5).
  - `ProjectsNotifier` already follows auth through a proxy provider
    (`onAuthChanged`, `projects_notifier.dart:63-71`).
  - `ProjectService._inFlightFetches` (`project_service.dart:30`) has keys
    with no user in them.
- **`AppScreen` reuse** (`app_screen.dart:259-277`): when
  `notifier.ref == projectRef && !isLoading && error == null`, it skips
  `load()` and `_restoreUiState`. `ProjectRef ==` compares name, owner and
  role, never the account.
- **Notifier structure**
  - Mixins are separate libraries (`mixin X on ChangeNotifier`). They reach
    notifier state through abstract `@override` fields.
  - `ViewProjectNotifier` (`view_screen.dart:49`) and `SharedProjectNotifier`
    (`shared_project_screen.dart:166`) extend `ProjectNotifier`.
  - Listeners:
    - `app_screen.dart`: 5 `Consumer` and 5 `Selector`;
    - `view_screen.dart`: 2 `Consumer` and 3 `Selector`;
    - `ActivityPanel`: a direct listener plus 4 per-tile `Selector`s;
    - `DayCarousel`: a direct listener;
    - `context.watch` in `travel_companions_section.dart:296`,
      `project_stats_screen.dart:259` and `shared_project_screen.dart:421`.
- **Map guards** (`map_panel.dart`)
  - `_MapPanelState` (1337-1377) and `ManageMapPanelState` (2298-2338) each
    hold 14 main guards plus encounter guards. Uses are
    `selectionChanged` (1586), `styleChanged` (1592) and
    `geoOrStyleChanged` (1601).
  - A selection-only change restyles cached specs and never re-enters the
    spec builders. `test/map_panel_selection_cost_test.dart` pins that.
- **Geometry assignments** (`project_notifier.dart`)
  - LOD paths: `_refetchGeoForZoomInner` (1380-1436, which stamps the bucket
    and box) and the progressive load (1546, which stamps the bucket only).
  - Full-resolution post-mutation paths:
    - `_applyRefreshedProject` (2913-2915), after a Strava refresh;
    - `_silentReload` (3177-3184), after remove, track save/reset, split
      and local delete;
    - `_resyncOnConflict` (`project_segment_crud_mixin.dart:44`), after a
      segment 409.
  - The server's simplified endpoint is guarded by the same generation
    counter that `bust_geo_cache` bumps on every write (`api/geo.py:81`,
    206). A simplified fetch after a write therefore sees the write.
- **LOD mechanics**
  - `_bucketOf = zoom.ceil()` (1293).
  - `_geoIsStaleForCamera` (1299-1308).
  - A 700 ms debounce (1291).
  - The single-flight `_refetchInFlight` (1336-1356).
  - The self-healing disarm (1425-1428).
  - `fetchBoxFor(..., padFraction = 0.25)` (`geo_viewport.dart:132`).
- **Segment resolve**
  - `pollSegmentResolution` (`project_segment_crud_mixin.dart:299-352`)
    polls `/meta` every 3 s with a 2-minute deadline, one loop per segment.
    At the deadline it sets `error` and leaves the tile "pending" forever.
  - `reconcileSegmentOverlay` (591-603) matches on `segment_id` only.
- **Day-meta**
  - Stored in one JSON column, `day_meta_json`. It is plaintext, not E2EE
    (`docs/ENCRYPTION.md:99`).
  - `PUT /api/projects/{name}/day-meta` (`api/projects.py:745-784`, editor
    role) does a read-modify-write of the whole column.
    - The writer is wrapped in `_keep_days_the_caller_cannot_see` (#387) and
      `_merge_day_meta_preserve_counters`.
    - It runs a blind `bump_lock_version`, then busts caches and queues a
      stats refresh.
    - `_check_written_day_notes` (724-742) already diffs incoming against
      stored.
  - Client: one sender, `ProjectNotifier.saveDayMeta` (3072-3099).
    - It updates optimistically. On error it sets `error` and does no
      rollback.
    - Three callers send the whole map:
      - the day editor (`day_meta_editor.dart:361-373`);
      - bulk tags (`activity_panel.dart:2258-2271`);
      - the settings screen `_save` (`project_settings_screen.dart:256-436`),
        which sends a snapshot taken in `initState` (149-156) and prunes days
        past the trip end.
  - There is no forced upgrade for native builds
    (`core/version_gate.dart:52-54`).
- **Auto-zoom** (`map_panel.dart:1688-1694`, 2666-2672) fires only when
  `selectionChanged`. `didUpdateWidget` ignores `autoZoom` turning on.
  `_autoZoom` is screen-local (`app_screen.dart:106`, `view_screen.dart:176`).
- **Geometry fetch instrumentation**
  - `fetch_geo_lod` and `decode_geo_lod` are recorded in
    `project_service.dart:279-289` and in the share route
    (`shared_project_screen.dart:140,147`).
  - `perfSpanReport` prints n, total, p50, p90 and worst.
  - The `geo_lod` size note keeps only the last payload.
  - `ApiClient.getBytes` (`client.dart:78-86`) returns only the body.
  - The server logs `geo_simplified ... load= build= gzip= cache=`
    (`api/geo.py:1105`). A byte-cache HIT returns early at 1047-1052 without
    that line.

## Decisions

1. **#418: the signed-in account owns `ProjectNotifier`'s lifetime.**
   - `ProjectNotifier` follows `AuthNotifier` through the same proxy-provider
     pattern as `ProjectsNotifier.onAuthChanged`. Whenever the signed-in user
     id changes, including to or from null, it calls `clear()`.
   - Why: this covers logout, the 401 paths, account deletion and any future
     way of switching account in one place, instead of a call at each exit.
   - Rules out: calling `clear()` from each logout path, which is what
     missed the 401 paths this time. Also rules out comparing the account in
     `AppScreen`'s reuse check, which fixes the filter half only (#418, first
     comment).
2. **#418: `clear()` resets everything a load sets.**
   - That covers every field, timer and overlay a load can set, including
     what it misses today.
   - A behaviour test compares the state after `clear()` with a freshly
     constructed notifier, for the fields that exist today.
   - A source-scan test lists every instance field declared in
     `project_notifier.dart` and its six mixins. It fails unless each one is
     reset in `clear()` or appears on an allowlist next to the test with a
     one-line reason. A field added later and missing from both fails the
     suite. Flutter has no reflection, so this is the only check that can
     see a new field. (R1-4)
3. **#418: fix the empty id at the root, on both sides.**
   - `/me` adds `"id": int(sub)` (additive). Installed builds already read
     `map['id']`.
   - `User.fromMap` falls back to `sub`, and `User.restored` takes its id
     from `api.tokenUserId`, so an offline restore has a real id before
     `/me` answers.
   - Once the id is real, `last_opened_project` and `projectDataCache` are
     per account with no further change.
   - `resolveRoleFor` stops guessing "editor" for own trips.
4. **#418: the 401 paths end the session like a logout, except for the disk
   cache.**
   - They lock E2EE, null the user, clear `ProjectNotifier` (through
     Decision 1) and clear `ProjectService`'s in-flight fetches.
   - They do **not** wipe the on-device cache. With Decision 3 it is keyed
     by the real account, so an expired token no longer costs the same user
     their offline trips.
   - An explicit logout keeps wiping it, as #429 decided.
5. **#418: one-off clean-up of state the empty id wrote.** On the first run
   of this version, the client:
   - removes the `last_opened_project_` key with an empty suffix;
   - removes `projectDataCache` entries keyed to user `0`.
   Nobody can own either once ids are real, and either can leak a previous
   account's trip name or metadata.
6. **#418: a reused notifier re-reads its saved UI state.**
   - When `AppScreen` reuses the held notifier without reloading, it runs
     `_restoreUiState` for the current key, so a filter that view mode
     changed is not overwritten by the next tap (#418, second comment).
   - Rules out: always reloading on reuse, which throws away the remount
     fast path that `app_screen_remount_load_guard_test.dart` pins.
7. **#294: five facets, each a `Listenable` with a version counter, owned by
   `ProjectNotifier`.**
   - The facets:
     - `GeoFacet`: `geo` plus the LOD it holds.
     - `SelectionFacet`: the selection ids, `selectedDays`, filters and
       `showJournals`.
     - `StyleFacet`: track colours, width, alternation, colour-by-type,
       `typeStyles`, the elevation chart colour and line, and `languages`.
     - `ItemsFacet`: activities, items, people, groups, day-meta, trip dates,
       sleeping options and counters.
     - `ElevationFacet`: the full track, per-activity tracks and the totals.
   - `ProjectNotifier` keeps the loading, error, ref, sync, sharing and
     member state, and every write method. Mixins keep their methods.
   - Why composition, not separate providers built independently: every
     write still goes through the notifier and its supersession tokens. The
     facets only scope who gets told.
   - Rules out: splitting into independent notifiers, which would split the
     load/supersession logic that #332 and #358 made correct.
8. **#294: migrate in three steps, each behaviour-preserving until the
   last.**
   1. **Move state.** One unit per facet. The root re-notifies on every
      facet change ("bubbling"), so nothing changes for listeners. The root
      getters for moved fields are removed, so the compiler finds every
      reader.
   2. **Move subscriptions.** Widgets listen to the facets they read,
      through facet providers.
   3. **Cut the bubble.** The root notifies only for its own state.
   Each step passes the full suite. Step 1 passes it with only mechanical
   renames in the tests.
9. **#294: the `_lastX` guards become per-facet version keys, not
   nothing.**
   - `PERF_MAP_LOAD.md` Phase 3 shows the guards are load-bearing: they are
     why a selection change never re-enters the spec builders. Their
     function stays.
   - Their mechanism changes: ~30 identity fields per panel become a key of
     `(geo.version, style.version, items.version, showJournals)` for the
     specs, and `selection.version` for the restyle.
   - `map_panel_selection_cost_test.dart` stays as the scope test. The tests
     the issue cites by number (#256's
     `map_panel_activities_toggle_test.dart`; #268 and #271 turned out to be
     unrelated PRs) are kept and pass unchanged.
10. **#379: the geometry facet records what `geo` holds.**
    - `GeoFacet.replace(geo, lod)` takes a `GeoLod`, one of:
      - `level(bucket, box?)`;
      - `full`;
      - `lowRes`;
      - `none`.
    - It has no setter without one, so a geometry and its stamp can no
      longer disagree.
    - Post-mutation refreshes (`_silentReload`, `_applyRefreshedProject`,
      `_resyncOnConflict`) fetch **simplified geometry at the current bucket
      and box**, through the same fetch the zoom refetch uses, and stamp it.
      - This is safe because the simplified endpoint is guarded by the
        generation counter every write bumps (Current state).
      - They stop writing full resolution into L1.
    - E2EE trips, which have no LOD and fetch full resolution today, keep
      `full`.
    - Rules out: stamping `null` to force a refetch (#379's middle option).
      That keeps the transient 180 MB.
11. **#278: one resolve poller per trip, with no deadline, and a reconcile
    that checks content.**
    - The notifier keeps a set of pending segment ids, and one loop polls
      `/meta` for all of them.
      - It polls every 3 s for the first 2 minutes, then backs off to 15 s,
        then 60 s.
      - It stops only when the set is empty or the trip changes. "Trip
        changes" means name and owner, not role, because `ProjectRef ==`
        includes role.
    - `load()` seeds the set from segments the server reports as pending, so
      reopening a trip, or a resolve started on another device, resumes
      polling.
    - The deadline no longer sets `error`.
    - `reconcileSegmentOverlay` drops a pending patch only when the server
      feature for that segment matches the patch's route state, not merely
      when the id is present.
    - Rules out: a longer deadline, which only moves the cliff.
12. **#397: option C, a per-day patch.** (Owner decision, 2026-10-04.)
    - **The endpoint.** `PATCH /api/projects/{name}/day-meta` takes:
      - `days: {date: entry}`, the days to set;
      - `delete: [date]`;
      - the optional `sleeping_options`, `sleeping_option_groups` and
        `counters` with today's PUT semantics.
    - **The merge.** It is per day:
      - a day in `days` replaces that day, keeping the stored counters when
        the entry omits `counters`, as today;
      - a day in `delete` is removed, unless #387's guard keeps it;
      - every other day is untouched.
    - **Atomicity.** The read-modify-write is retried server-side on a
      `lock_version` compare-and-set, shaped like
      `save_project_with_retry`. Two concurrent PATCHes to different days
      both land, and an unrelated bump costs a retry, never a 409.
    - **The response.** It returns 200 with the merged `day_meta`, so the
      client converges on days other devices changed.
    - **The client.** It sends only what changed:
      - the day editor sends its day;
      - bulk tags send the selected days;
      - the settings screen sends a diff against its `initState` snapshot,
        and trip-end pruning goes into `delete`.
      - On failure the client reloads day-meta instead of keeping its
        optimistic copy.
    - **Old builds: the whole-map `PUT` is retired.** (Owner decision,
      2026-10-04, R1-1.)
      - An old build's `PUT` sends a stale whole map, and no server check
        can tell a stale revert from an intended edit. So the `PUT` answers
        **426 Upgrade Required** with the detail "This version of the app can
        no longer save day notes. Please update the app."
      - It writes nothing. Old builds show that detail as their error string
        and lose nothing.
      - Old builds' other settings still save, because they send day-meta
        in a separate, unawaited request.
      - Stale web tabs are already prompted to reload by `VersionGate`.
    - **Accepted.** When two devices edit the **same** day, the last writer
      wins on that day.
    - Rules out: (B) a version column, which detects a conflict but cannot
      stop a stale map reverting days the client never touched. Also rules
      out keeping the `PUT` for old builds, which keeps exactly that revert
      (R1-1).
    - **The client keeps its in-memory gap days.** The days that
      `_autoFillDaysToToday` adds after the last activity, up to today, exist
      only in memory. The client merges the response into its map: it adopts
      the server's value for every day the server returns, drops the days
      the server no longer has unless they are empty gap-fill days, then
      re-runs `_autoFillDaysToToday`. (R1-2)
13. **#478: switching auto-zoom on fits the current selection.** Both map
    panels handle `autoZoom` going from false to true in `didUpdateWidget`
    by running the same fit a selection change runs, when an activity, a
    segment or days are selected.
14. **#401: measure first, plus one change that is correct whatever the
    numbers say.**
    - **Server.** Both return paths of the simplified endpoints (project and
      share) send `Server-Timing` (`load`, `build`, `gzip`, `total`; a HIT
      sends `total` and `cache;desc=hit`) and keep `X-Cache`.
    - **Client.** `ApiClient` gains a bytes call that also returns headers.
      For each fetch, the perf report records:
      - `geo_lod_server`, the server total;
      - `geo_lod_wait`, the fetch minus the server time;
      - the payload bytes, as a distribution, not a last-value note;
      - the HIT count.
    - **Bucket hysteresis.** The loaded bucket stays fresh until the zoom
      moves a margin past the bucket's boundaries. The refetch requests
      `ceil(zoom)` as today. A property test proves that a refetch always
      leaves the state fresh (the #332 lesson).
    - **The owner records one device session** afterwards. The larger
      options (#401's padding and per-vertex levels) are decided from it and
      are not in this plan.
15. **A minimum client version, for every later contract change.** (Owner
    decision, 2026-10-04, R1-1.)
    - `GET /api/version` gains `min_client_version`, a server constant (an
      env override is allowed, like `_APP_VERSION`).
    - `VersionGate` compares `kClientVersion` with it on start and on resume,
      on every platform.
      - Below the minimum, a native build shows a blocking "Update required"
        screen with a link to its store page.
      - Below the minimum, a web build shows the existing reload bar, made
        non-dismissable.
      - `dev` and empty versions never trigger it, as `isClientStale` already
        does.
    - The initial value is `0.0.0`, which is off.
      - Builds that ship before this gate cannot read it, which is why the
        `PUT` is retired on the server and not gated (Decision 12).
      - The gate is for the next contract change.
      - `docs/RELEASING.md` says when and how to raise the value.
    - Rules out: a header on every request with a server-side 426 for old
      versions. That cannot reach builds that send no header either, and it
      turns every endpoint into a version check.

## Review envelope

What this plan adds to REVIEW.md §2's defaults:

- **Shared devices are in scope for #418.**
  - Two accounts signing in one after another on one browser or one
    installed app, without a page reload, is the threat.
  - A second account must not see, read from the cache, or write over any
    state of the first. That covers trip data, filters, selection, the last
    opened trip, cached metadata, in-flight responses and an unlocked E2EE
    key.
  - Out of scope: anyone with OS-level access to the device or the browser
    profile.
- **Concurrency on day-meta.**
  - Two PATCHes to one trip can run at once: the API is one process with a
    thread pool for sync endpoints.
  - A PATCH can also run alongside a structural `save_project` or a trip
    Replace import. The old `PUT` no longer writes (Decision 12).
  - None of them may lose another's change to a different day.
  - The same day edited twice is last-writer-wins by decision, and not a
    defect.
- **Client concurrency inside the notifier.** Facet moves must keep every
  existing supersession guard (`isCurrent(token, ref)`) in front of facet
  writes, exactly where the field writes were.
- **Old builds (E5).**
  - They gain `id` on `/me` and `min_client_version` on `/api/version`. They
    ignore both.
  - Their `PUT /day-meta` gets a 426 and writes nothing, by decision: they
    cannot save day notes until they update.
  - Nothing else they send changes meaning.
- **`Server-Timing` on share links** exposes server phase durations to
  holders of a share token. These are timings only, with no content, and
  are accepted.
- **Scale.** Trips up to the 227-activity / ~19k rendered point device trip,
  and the 219-activity / 1.46 M coordinate full-resolution case for #379.

## Boundaries crossed

- **API**
  - `GET /api/auth/me` gains `id` (int). It is additive, and old builds
    already read it.
  - New `PATCH /api/projects/{name}/day-meta`.
  - **`PUT /api/projects/{name}/day-meta` now answers 426 and writes
    nothing.** This is a deliberate contract break for installed builds
    (R1-1).
    - Their day-note, tag and counter saves fail with a readable "please
      update" message.
    - Their other settings still save.
  - `GET /api/version` gains `min_client_version` (additive).
  - `GET /api/geo/project/simplified` and `/api/share/{token}/geo/simplified`
    gain a `Server-Timing` header. The body is unchanged.
- **Stored data:** none on the server.
- **Device storage**
  - The one-off removal of the empty-suffix `last_opened_project_` key and of
    user-`0` cache entries (Decision 5).
  - After that, `projectDataCache` keys by the real account id, so an
    existing user's cached trips (keyed `0` today) are fetched again once.
- **Schema:** none. No migration.
- **Export formats:** none.

## Conventions

- **Facet state is written only by `ProjectNotifier` and its mixins.**
  Widgets get read-only access. U9 picks the mechanism (library privacy or a
  test that greps for mutator calls outside `lib/src/projects/project_*`)
  and every later facet unit follows it.
- **No test assertion changes value during the state moves (U10, U12-U15).**
  Only mechanical renames are allowed (`n.geo` → `n.geoFacet.geo`). A test
  that fails for any other reason is a regression, not a test to adjust
  (`feedback_dont_bend_tests`).
- **Flutter tests run in the `traxjourney-flutter-3.47.1-tests` container.**
  Smart App Control blocks `impellerc` on the host.
- **Every geometry write goes through `GeoFacet.replace` with a `GeoLod`**
  from U10 on.
- **Graphify output is never committed from a worktree.**

## Open decisions

- **Hysteresis margin.** Proposed 0.3 of a zoom level. Decided when U11 is
  routed; it is a constant.
- **When to raise `min_client_version`.** Off (`0.0.0`) in this plan. The
  owner raises it on a later release, following the note U21 adds to
  `docs/RELEASING.md`.
- **iOS store link.** U21 needs an App Store URL; if none exists yet, the
  update screen ships without a link on iOS.
- **Resolve backoff steps.** Proposed 3 s, then 15 s after 2 minutes, then
  60 s after 10 minutes. Decided when U8 is routed.
- **What #401 does next.** Decided by the owner from the device session once
  U3 and U6 are deployed (DoD). It may close #401 or become a new plan.
- **Poster and video pending-job keys** (`poster_job_pending`,
  `video_job_pending`) are not per account. The server answers 404 for
  another account's job and the key clears, so the risk is low. A follow-up
  issue, not this plan.
- **`resolveRoleFor` still guesses "editor" for other people's `?owner=`
  links.** The server corrects it on load. Unchanged here.

## Execution units

### Wave 1 — server sides and the auto-zoom fix (disjoint)

**U1 — `/me` returns the account id (#418)**
- **Goal:** `GET /api/auth/me` includes `"id": <int>`, equal to the JWT `sub`.
- **Scope:** `api/auth.py` (`me()` only); `tests/test_auth_me_id.py` (new).
- **Context:** `_token_response` (`api/auth.py:152-167`) is the shape to
  match: `id` is the `UserInfo` id as an int. `local_auth_id` is a different
  id; do not use it. `tests/test_email_verification.py:187` calls `/me`.
- **Do:** add `id` to the returned dict. Leave every other key as it is.
- **Acceptance:** the new test checks that `/me`'s `id` equals the login
  response's `user.id`, for a local and an admin user. The pytest CI command
  passes.
- **Out of scope:** a response model for `/me`; any change to the JWT.
- **Latitude:** none.
- **Escalate if:** `sub` is not the `UserInfo` id for some auth provider; X3.
- **Depends on:** —

**U2 — `PATCH /day-meta`, a per-day merge (#397)**
- **Goal:** the endpoint in Decision 12. It is atomic against concurrent
  writers, and the PUT is retired with 426.
- **Scope:**
  - `api/projects.py` (a new request model and handler; the PUT becomes a
    426 stub; its helpers are reused by the PATCH);
  - `tests/test_day_meta_patch.py` (new);
  - the existing tests that write day-meta through the PUT, moved to the
    PATCH, with their assertions about stored days unchanged:
    `tests/test_day_meta.py`, `test_day_meta_guard.py`,
    `test_companion_e2e.py`, `test_companion_roles.py`,
    `test_content_days.py`, `test_import_trip_settings.py`,
    `test_meta_cache.py`, `test_project_members.py`,
    `test_project_write_lost_update.py` and `test_request_value_rules.py`.
- **Context:**
  - The PUT handler (`api/projects.py:745-784`) and its helpers
    `_check_written_day_notes`, `_keep_days_the_caller_cannot_see` and
    `_merge_day_meta_preserve_counters`.
  - `check_and_bump_lock_version` (`src/project/repo_core.py:97-120`) and
    `save_project_with_retry` (`src/project/repo_retry.py:43-81`) are the
    compare-and-set and retry shape to follow.
  - `tests/test_project_write_lost_update.py` (the concurrent-bump
    monkeypatch at ~285-331) is the test to follow.
- **Do:**
  1. Request model: `days: Dict[str, Dict[str, Any]] = {}`,
     `delete: List[str] = []`, plus the optional `sleeping_options`,
     `sleeping_option_groups` and `counters`.
     - A date in both `days` and `delete` is 400.
     - Keys are not validated, as the PUT never validated them. Deleting an
       absent key is a no-op.
  2. In a retry loop:
     - read the row and its `lock_version`;
     - merge per Decision 12, running `_check_written_day_notes` on the
       touched days and the #387 guard on `delete` only;
     - write the column, then `check_and_bump_lock_version(expected)`;
     - on `StaleWriteError`, roll back and retry with backoff, as
       `save_project_with_retry` does (5 attempts, then 409).
  3. Then do exactly what the PUT does after writing: bust caches and queue
     the stats refresh. Return 200 `{"day_meta": <merged map>}`.
  4. Editor role and above, as for the PUT.
  5. The PUT answers 426 with Decision 12's detail and writes nothing.
- **Acceptance:** new tests show:
  - two PATCHes to different days, one injected between the other's read
    and its write, both persist;
  - a PATCH racing a structural `save_project_with_retry` loses neither's
    changes;
  - `delete` respects #387, and omitting `counters` keeps them;
  - a viewer gets 403, and overlapping keys get 400;
  - the response equals a following `GET /meta`'s `day_meta`;
  - a PUT gets 426 with the detail and leaves the column and `lock_version`
    untouched;
  - every moved test keeps its assertions about what ends up stored. Only
    the request changes (`feedback_dont_bend_tests`);
  - the pytest CI command passes.
- **Out of scope:** per-field merge inside a day; a migration; the client.
- **Latitude:** local design.
- **Escalate if:**
  - the compare-and-set cannot be made to include the column write in one
    transaction;
  - the merged map cannot be made equal to what `/meta` serves (null
    stripping);
  - X3.
- **Depends on:** —

**U3 — `Server-Timing` on the simplified geometry endpoints (#401)**
- **Goal:** every response from the project and share simplified endpoints
  carries `Server-Timing` per Decision 14.
- **Scope:** `api/geo.py` (the simplified handler's return paths); `api/share.py`
  (its simplified handler); `tests/test_geo_server_timing.py` (new).
- **Context:** the `geo_simplified` log line (`api/geo.py:1105`) already
  measures `load`, `build` and `gzip`. The HIT path returns early at
  1047-1052. `X-Cache` is set at 1051 and 1110.
- **Do:** emit `Server-Timing: load;dur=x, build;dur=y, gzip;dur=z, total;dur=t`
  in milliseconds on a MISS, and `cache;desc=hit, total;dur=t` on a HIT.
  Durations are numbers only.
- **Acceptance:** tests check the header parses on MISS and HIT for both
  endpoints and that the body bytes are unchanged; the pytest CI command
  passes.
- **Out of scope:** other endpoints; a global middleware.
- **Latitude:** none.
- **Escalate if:** CORS hides the header from the web client (check
  `expose_headers` in `api/router.py`); a change outside Scope is needed (X3).
- **Depends on:** —

**U4 — Switching auto-zoom on fits the selection (#478)**
- **Goal:** turning auto-zoom on with an activity, a segment or days
  selected animates the camera to that selection, in both map panels.
- **Scope:** `flutter_client/lib/src/projects/map_panel.dart`;
  `flutter_client/test/map_autozoom_toggle_test.dart` (new).
- **Context:**
  - The selection-change auto-zoom in `_MapPanelState` (1688-1694,
    1756-1778) and `ManageMapPanelState` (2666-2672, 2701-2723) is the code
    to reuse.
  - `test/map_panel_fit_bounds_test.dart` is the widget test to follow.
- **Do:**
  - In each panel's `didUpdateWidget`, when `!oldWidget.autoZoom &&
    widget.autoZoom` and there is a selection, queue the same fit.
  - Turning auto-zoom off does nothing.
- **Acceptance:** widget tests for both panels:
  - toggling on with a selected day fits to it;
  - toggling on with nothing selected does not move the camera;
  - toggling off does nothing;
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** auto-zoom on memory or journal selection; persisting the
  toggle.
- **Latitude:** none.
- **Escalate if:** X3.
- **Depends on:** —

### Wave 2 — #418 client, version and job-watch server (disjoint)

**U5 — The account owns the client session (#418)**
- **Goal:** Decisions 1-6. No state of one account survives into the next,
  and the user id is real after a restore.
- **Scope:**
  - `flutter_client/lib/src/auth/auth_notifier.dart`
  - `flutter_client/lib/main.dart`
  - `flutter_client/lib/src/projects/project_notifier.dart` (`clear()`, a
    new `onAuthChanged`)
  - the six `project_*_mixin.dart` files (only to expose their reset)
  - `flutter_client/lib/src/projects/project_service.dart`
    (`_inFlightFetches` reset only)
  - `flutter_client/lib/src/projects/app_screen.dart` (the reuse branch
    only)
  - `flutter_client/lib/src/core/last_opened_project.dart`
  - `flutter_client/lib/src/projects/project_data_cache.dart` (the user-`0`
    purge)
  - tests:
    - `flutter_client/test/auth/session_reset_test.dart` (new)
    - `flutter_client/test/projects/project_notifier_clear_complete_test.dart`
      (new)
    - `flutter_client/test/projects/project_notifier_clear_scan_test.dart`
      (new)
    - `flutter_client/test/app_screen_account_switch_test.dart` (new)
    - `flutter_client/test/auth/auth_notifier_test.dart`, only the
      assertion at 126, which encodes the bug
- **Context:**
  - `ProjectsNotifier.onAuthChanged` and its proxy provider (`main.dart:70-74`)
    are the pattern to follow.
  - `test/app_screen_remount_load_guard_test.dart` (real `AppScreen` and
    GoRouter, `loadCallCount`) is the base for the account-switch test.
  - `test/projects/stale_filter_restore_test.dart:526-585` and
    `test/helpers/signed_in.dart` (`signInAs`) show account scoping.
- **Do:**
  1. `User.fromMap`: `id = map['id'] ?? map['sub']`. `User.restored` takes
     its id from `api.tokenUserId`.
  2. Wire `ProjectNotifier` as a proxy of `AuthNotifier`. `onAuthChanged`
     calls `clear()` when the user id differs from the last one seen,
     including to or from null.
  3. Make `clear()` reset everything a load sets: every field the
     "Current state" list names, the segment overlay, and the photo-polling
     and degraded-route timers.
  4. The 401 paths (`auth_notifier.dart:135-139`, 183-188) lock E2EE and
     reset `ProjectService`'s in-flight fetches. An explicit `logout()` does
     the same, plus its existing cache wipes.
  5. The one-off purge (Decision 5), on startup, before the router's first
     redirect.
  6. `AppScreen` reuse branch: run `_restoreUiState` for the current key
     before `afterLoad()` (Decision 6).
- **Acceptance:**
  - `project_notifier_clear_complete_test`: after loading a trip with
    people, groups, multi-day selection, filters, share tokens and a
    pending segment patch, `clear()` leaves every public getter equal to a
    fresh notifier's, and no timer is active.
  - `project_notifier_clear_scan_test` (new): the source scan from
    Decision 2, with its allowlist. Removing a reset from `clear()` makes it
    fail. (R1-4)
  - `app_screen_account_switch_test`:
    - account A filters Japan to Hotel, then logs out; account B opens
      `/app?project=Japan`;
    - `loadCallCount` increases, and B has no filter;
    - the same holds for a 401 forced logout.
  - `session_reset_test`:
    - a restored session has a real id before and after `/me`;
    - a 401 path locks E2EE;
    - the empty-suffix key and user-`0` cache entries are gone after
      startup;
    - two accounts' `last_opened_project` are independent.
  - A view-mode filter change survives returning to the reused edit
    notifier (#418, second comment).
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** poster/video pending keys; `resolveRoleFor` for other
  people's links; any facet work.
- **Latitude:** local design.
- **Escalate if:**
  - `onAuthChanged` fires during a load in a way that clears a trip the
    same account is opening;
  - the purge cannot run before the first redirect;
  - X3.
- **Depends on:** U1 (the server half; the client fallback works without
  it).

**U20 — `min_client_version`, and a warning for stuck route jobs (Decision
15, R1-5)**
- **Goal:** `/api/version` serves `min_client_version`. An hourly job logs
  route jobs that have been pending or running too long.
- **Scope:** `api/router.py` (`app_version` and one scheduler entry);
  `src/jobs/route_jobs.py` (a new read-only `warn_stuck_route_jobs`);
  `tests/test_app_version.py` (new or existing);
  `tests/test_stuck_route_jobs.py` (new).
- **Context:**
  - `app_version` (`api/router.py:394-403`) and `_APP_VERSION` are the env
    pattern.
  - `sweep_orphaned_jobs` (`src/jobs/route_jobs.py:174`) is the query shape.
  - The `sweep_degraded_segments` scheduler entry (`api/router.py:150`) is
    the registration to copy.
- **Do:**
  1. Add `MIN_CLIENT_VERSION` (default `"0.0.0"`, env override) and return
     it as `min_client_version`.
  2. `warn_stuck_route_jobs()` logs one WARNING per job still pending or
     running 30 minutes after it started (or after it was created, when it
     never started). The line carries the job id, project id and segment id.
     It changes nothing.
  3. Run it hourly.
- **Acceptance:**
  - Tests: `/api/version` returns both keys, and the env override works.
  - A job 31 minutes old logs, one 29 minutes old does not, and a finished
    one does not.
  - No row is modified.
  - The pytest CI command passes.
- **Out of scope:** re-queueing stuck jobs; the client gate (U21).
- **Latitude:** none.
- **Escalate if:** the job row has no usable start or creation time; X3.
- **Depends on:** —

### Wave 3 — #397, #401 and version-gate clients (disjoint)

**U6 — Per-fetch geometry timing on the client (#401)**
- **Goal:** the perf report splits each simplified fetch into server time
  and the rest, and records payload sizes as a distribution (Decision 14).
- **Scope:**
  - `flutter_client/lib/src/api/client.dart` (a bytes call that also
    returns headers)
  - `flutter_client/lib/src/projects/project_service.dart`
    (`getSimplifiedGeo` only)
  - `flutter_client/lib/src/shared/shared_project_screen.dart` (the share
    fetch's spans only)
  - `flutter_client/lib/src/core/perf_timing.dart`
  - tests: `flutter_client/test/core/perf_geo_lod_split_test.dart` (new),
    `flutter_client/test/api/client_test.dart`
- **Context:**
  - `PerfSpans.stage` and `perfSpanReport` (`perf_timing.dart:167-188`,
    535-546) are how spans are recorded.
  - The existing `fetch_geo_lod` span (`project_service.dart:279-289`) is
    the one to extend.
- **Do:**
  - Parse `Server-Timing`'s `total`.
  - Record `geo_lod_server`, and `geo_lod_wait` as the fetch time minus the
    server time (never negative).
  - Record payload bytes as samples, printed with p50 and p90, and the HIT
    count.
  - A response without the header (an older server) records no server
    sample and no error.
- **Acceptance:** unit tests:
  - header parsing, including a missing or malformed header;
  - the report shows both spans with p50 and p90;
  - bytes are recorded per fetch;
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** changing the fetch itself; hysteresis (U11).
- **Latitude:** local design.
- **Escalate if:** on web, `package:http` hides the header even with CORS
  exposure in place; X3.
- **Depends on:** U3 (its contract).

**U7 — The client sends only the days it changed (#397)**
- **Goal:** every day-meta save goes to `PATCH` with only the touched days
  and deletes, and the client adopts the returned map (Decision 12).
- **Scope:**
  - `flutter_client/lib/src/projects/project_notifier.dart` (`saveDayMeta`
    only)
  - `flutter_client/lib/src/projects/day_meta_editor.dart` (`_persist`)
  - `flutter_client/lib/src/projects/activity_panel.dart` (the bulk-tag
    apply)
  - `flutter_client/lib/src/projects/project_settings_screen.dart` (`_save`
    and the diff)
  - tests: `flutter_client/test/settings/project_settings_day_meta_diff_test.dart`
    (new); `flutter_client/test/day_meta_editor_test.dart`;
    `flutter_client/test/settings/project_settings_trip_end_test.dart` and
    `project_settings_rename_conflict_test.dart`, only where they capture
    the PUT body (they now capture the PATCH body)
- **Context:**
  - The PUT-body capture in `project_settings_trip_end_test.dart:33-52` is
    the test pattern.
  - `_check_written_day_notes`'s field diff (`api/projects.py:724-742`) is
    the same diff the settings screen needs.
- **Do:**
  1. `saveDayMeta({days, delete, sleepingOptions?, groups?, counters?})`:
     - apply optimistically;
     - on 200, merge the response into `dayMeta` per Decision 12 (adopt
       returned days, keep empty gap-fill days, re-run
       `_autoFillDaysToToday`);
     - on error, reload day-meta from `/meta` and set `error`.
  2. The day editor sends its one day, or puts it in `delete` when it is
     emptied.
  3. Bulk tags send the selected days.
  4. Settings sends days whose content differs from the `initState`
     snapshot, plus pruned days in `delete`. It sends sleeping options and
     counters only when they changed. A save that changes no day-meta
     sends no day-meta request.
- **Acceptance:**
  - Tests show:
    - a colour-only settings save sends no day-meta request;
    - a tag rename sends exactly the renamed days;
    - trip-end pruning sends the pruned days in `delete`;
    - the editor sends one day;
    - a failed PATCH reloads day-meta;
    - on a trip whose last activity was two days ago, saving a note keeps
      the carousel's tiles up to today (R1-2);
    - a day another device added arrives in `dayMeta` from the response.
  - The existing trip-end tests keep their expectations about which days
    end up stored.
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** conflict UI for the same day; offline queueing.
- **Latitude:** local design.
- **Escalate if:** `_autoFillDaysToToday`'s in-memory gap days would now never
  reach the server, and something depends on them being stored; X3.
- **Depends on:** U2 (its contract).

**U21 — Client: minimum-version gate (Decision 15)**
- **Goal:** a build below `min_client_version` is stopped with an update
  screen (native) or a non-dismissable reload bar (web).
- **Scope:**
  - `flutter_client/lib/src/core/version_gate.dart`
  - `flutter_client/lib/src/core/app_version.dart`
  - `flutter_client/test/core/version_gate_min_test.dart` (new)
  - `docs/RELEASING.md` (a short section on raising the minimum)
- **Context:**
  - `isClientStale` and `_check` in `version_gate.dart` are the code to
    extend.
  - The Android package name is in `api/router.py` (`ANDROID_PACKAGE_NAME`)
    for the store link.
- **Do:**
  - Add a pure `isBelowMinimum(client, minimum)` using numeric x.y.z
    comparison. `dev`, empty and unparsable values return false.
  - Show the blocking screen on native and the bar on web.
  - Re-check on resume.
- **Acceptance:**
  - Unit tests for `isBelowMinimum`, including `dev`, `0.0.0`, equal
    versions, and `0.10.0` greater than `0.9.0`.
  - Widget tests: below the minimum on native shows the screen; on web
    shows the bar; at or above it shows nothing.
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** in-app update APIs; per-endpoint gating.
- **Latitude:** local design.
- **Escalate if:** no store URL exists for a platform (show the screen
  without a link and report it); X3.
- **Depends on:** U20 (its contract).

### Wave 4 — #278

**U8 — Resolve polling survives the queue (#278)**
- **Goal:** Decision 11. A route that resolves late, or one started before
  the trip was opened, reaches the map without reopening the trip. A stale
  refetch never reverts it.
- **Scope:**
  - `flutter_client/lib/src/projects/project_segment_crud_mixin.dart`
  - `flutter_client/lib/src/projects/project_notifier.dart` (seeding the
    pending set from load, and stopping the poller in `clear()`)
  - `flutter_client/lib/src/projects/segment_dialog.dart` and
    `activity_panel.dart` (callers only)
  - tests: `flutter_client/test/segment_resolve_poll_test.dart`,
    `flutter_client/test/segment_overlay_test.dart`,
    `flutter_client/test/segment_resolve_resume_test.dart` (new)
- **Context:**
  - `pollSegmentResolution` (299-352) and the fake `_PollService` in
    `segment_resolve_poll_test.dart:26` are the base.
  - `reconcileSegmentOverlay` (591-603).
  - `applyResolvedSegment` (389-416).
- **Do:**
  1. Replace the per-segment loop with one per-trip poller over a pending
     set, using the backoff schedule from Open decisions.
  2. Compare the trip by name and owner.
  3. Seed the set from pending segments in `load()`. Stop the poller in
     `clear()`.
  4. Drop the deadline error.
  5. Reconcile drops a patch only when the server feature's route state
     matches the patch's.
- **Acceptance:**
  - Tests show:
    - three concurrent resolves, one finishing after 5 minutes of fake
      time, all reach `geo`;
    - a trip loaded with a pending segment polls and applies it;
    - a role change mid-poll does not cancel it;
    - opening another trip stops it;
    - a refetch whose response carries the pre-resolve feature does not
      replace the resolved line.
  - The existing deadline test is replaced, because its behaviour is
    removed by decision, not bent.
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** server queue concurrency; push notifications.
- **Latitude:** local design.
- **Escalate if:** the geometry feature carries nothing that distinguishes
  a resolved route from its great-circle placeholder (then the reconcile
  needs a server field, which is a new unit); X3.
- **Depends on:** U7 (shares `project_notifier.dart`).

### Wave 5 — facet foundation

**U9 — Facet base, bubbling, providers (#294)**
- **Goal:** the shared machinery for Decisions 7-8, with no state moved.
- **Scope:**
  - `flutter_client/lib/src/projects/facets/project_facet.dart` (new)
  - `flutter_client/lib/src/projects/facets/project_facet_providers.dart`
    (new)
  - `flutter_client/lib/src/projects/project_notifier.dart` (owning empty
    facets, bubbling)
  - `flutter_client/lib/main.dart`
  - `flutter_client/lib/src/projects/view_screen.dart` and
    `flutter_client/lib/src/shared/shared_project_screen.dart` (wrapping in
    the providers only)
  - `flutter_client/test/projects/facets/project_facet_test.dart` (new)
- **Context:** `ChangeNotifier` and the provider package's
  `ChangeNotifierProvider.value` are the pattern. `ViewProjectNotifier` is
  provided at `view_screen.dart:137`.
- **Do:**
  1. `ProjectFacet extends ChangeNotifier` with `int version` and a
     protected `changed()` that bumps the version and notifies.
  2. Choose and document the mutator-restriction mechanism (Conventions).
  3. `ProjectNotifier` constructs the five facets and re-notifies on each
     facet change.
  4. `ProjectFacetProviders(notifier, child)` provides the five facets.
  5. Wrap the three places that provide a `ProjectNotifier`.
- **Acceptance:**
  - Tests show that a facet change bumps its version and notifies both the
    facet's listeners and the root.
  - The providers resolve to the innermost notifier's facets inside a
    `ViewScreen`.
  - The restriction mechanism is tested.
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** moving any field.
- **Latitude:** local design.
- **Escalate if:** X3.
- **Depends on:** U8.

### Wave 6 — state moves (serial, one facet per wave)

U10, U12, U13, U14 and U15 each move one facet's fields from
`ProjectNotifier` and its mixins into the facet, remove the root getters,
and update every reader the compiler finds to `notifier.<facet>.<field>`.
Their Scope is therefore:
- `flutter_client/lib/src/projects/project_notifier.dart`;
- the six `project_*_mixin.dart` files;
- the facet's file under `facets/`;
- every file under `flutter_client/lib/` and `flutter_client/test/` that
  reads a moved field, for the read only.

They run one at a time because they share those files. Each is
behaviour-preserving (Conventions).

**U10 — `GeoFacet` with `GeoLod`, and post-mutation refresh at the current
LOD (#294, #379)**
- **Goal:** Decisions 7 and 10.
- **Scope:** as above, plus `flutter_client/lib/src/projects/facets/geo_facet.dart`
  (new) and `flutter_client/test/projects/facets/geo_facet_lod_test.dart`
  (new).
- **Context:**
  - The bucket and box stamping in `_refetchGeoForZoomInner`
    (`project_notifier.dart:1380-1436`).
  - The assignment table in this plan's Current state.
  - `test/geo_zoom_lod_test.dart` and `test/geo_upgrade_single_swap_test.dart`
    are the tests to follow.
- **Do:**
  1. Move `geo`, the bucket, the box and `isGeoLoaded` into `GeoFacet`.
     `replace(geo, GeoLod)` is the only writer.
  2. Convert every assignment from the Current state table.
  3. `_silentReload`, `_applyRefreshedProject` and `_resyncOnConflict` fetch
     simplified geometry at the current bucket and box (full for E2EE),
     stamp it, and stop writing full resolution to L1.
  4. The camera, debounce and refetch machinery stay in the notifier.
- **Acceptance:**
  - Tests show:
    - after a track save, a remove, a split, a Strava refresh and a segment
      409, `geo` holds simplified geometry stamped with the current bucket
      and box, and L1 holds no new full-resolution entry;
    - an E2EE trip stays `full`;
    - the 409 path shows the server's state, not the optimistic one.
  - `geo_zoom_lod_test`, `geo_upgrade_single_swap_test` and every other
    existing test pass with renames only.
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** hysteresis (U11); subscriptions.
- **Latitude:** local design.
- **Escalate if:**
  - the offline disk copy would serve pre-edit geometry after a mutation
    (`project_data_cache.dart:119-130` freshness by `lock_version` is
    expected to prevent it; prove it or stop);
  - export or the offline seed reads `geo` expecting full resolution;
  - X3.
- **Depends on:** U9.

**U11 — Zoom-bucket hysteresis (#401)**
- **Goal:** a zoom oscillating around an integer boundary does not refetch
  (Decision 14).
- **Scope:**
  - `flutter_client/lib/src/projects/project_notifier.dart`
    (`_geoIsStaleForCamera` and the bucket helpers)
  - `flutter_client/lib/src/projects/facets/geo_facet.dart`, if the
    predicate lives there
  - `flutter_client/test/geo_bucket_hysteresis_test.dart` (new)
- **Context:**
  - `_bucketOf` and `_geoIsStaleForCamera` (1293-1308).
  - The self-healing disarm (1425-1428).
  - The `fetchBoxFor` contains property test in `test/geo_viewport_test.dart`
    is the shape for the property test.
- **Do:**
  - The loaded bucket B (holding zooms in (B-1, B]) is stale only when
    `zoom > B + m` or `zoom <= B - 1 - m`.
  - The requested bucket stays `ceil(zoom)`.
- **Acceptance:**
  - A property test over random zooms shows that after a refetch at
    `ceil(zoom)` the state is never stale for that zoom.
  - A test shows that zooming 5.9 → 6.1 → 5.9 → 6.1 refetches once.
  - `geo_zoom_lod_test` passes.
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** padding changes; debounce changes.
- **Latitude:** none.
- **Escalate if:** X3.
- **Depends on:** U10.

**U12 — `SelectionFacet` (#294)**
- **Goal:** the selection ids, `selectedDays`, filters and `showJournals`
  move into `SelectionFacet`.
- **Scope:** as for the state moves, plus `facets/selection_facet.dart` (new),
  `project_filter_mixin.dart`, and
  `flutter_client/test/projects/facets/selection_facet_test.dart` (new).
- **Context:** U10's diff is the example. The selection setters are at
  `project_notifier.dart:641-710`. UI-state save and restore
  (`_saveUiState`, `_restoreUiState`, 777-917) read these fields.
- **Do:** move the fields. Setters notify the facet only once per call, as
  today.
- **Acceptance:**
  - A test shows a selection change bumps only the selection version.
  - `stale_filter_restore_test` and the selection tests pass with renames
    only.
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** subscriptions.
- **Latitude:** local design.
- **Escalate if:** X3.
- **Depends on:** U11.

**U13 — `StyleFacet` (#294)**
- **Goal:** the style fields in Decision 7 move into `StyleFacet`.
- **Scope:** as for the state moves, plus `facets/style_facet.dart` (new) and
  its test.
- **Context:** U12's diff. The fields are at `project_notifier.dart:545-567`.
- **Do:** move the fields.
- **Acceptance:** a style change bumps only the style version; the existing
  tests pass with renames only; `flutter analyze` and `flutter test` pass.
- **Out of scope:** subscriptions.
- **Latitude:** none.
- **Escalate if:** X3.
- **Depends on:** U12.

**U14 — `ItemsFacet` (#294)**
- **Goal:** the content fields in Decision 7 move into `ItemsFacet`,
  including the `dayStats` and `orderedDayKeys` memos.
- **Scope:** as for the state moves, plus `facets/items_facet.dart` (new) and
  its test.
- **Context:**
  - U12's diff.
  - The fields are at `project_notifier.dart:430-433` and 511.
  - The memos are at 1918-1994.
  - `test/crud_mixin_item_identity_test.dart` pins new-list identity on a
    mutation and must keep passing.
- **Do:** move the fields. A mutation that changes items and geometry
  updates both facets.
- **Acceptance:** an item change bumps only the items version (or items and
  geometry, where both change); the existing tests pass with renames only;
  `flutter analyze` and `flutter test` pass.
- **Out of scope:** subscriptions.
- **Latitude:** local design.
- **Escalate if:** X3.
- **Depends on:** U13.

**U15 — `ElevationFacet` (#294)**
- **Goal:** `_fullTrack`, `_perActivityTracks` and the totals move into
  `ElevationFacet`.
- **Scope:** as for the state moves, plus `facets/elevation_facet.dart` (new)
  and its test.
- **Context:** U12's diff. The getters are at `project_notifier.dart:2060-2071`
  and the totals at 920-922.
- **Do:** move the fields, keeping the `_buildFullTrackGen` supersession.
- **Acceptance:** an elevation rebuild bumps only the elevation version; the
  existing tests pass with renames only; `flutter analyze` and
  `flutter test` pass.
- **Out of scope:** subscriptions.
- **Latitude:** none.
- **Escalate if:** X3.
- **Depends on:** U14.

### Wave 7 — map and side panels (disjoint, after U15)

U16 and U17 have no file in common. U18 runs in its own wave afterwards,
because its listener inventory can reach any file (R1-3).

**U16 — Map panels listen to facets, and version keys replace the guards
(#294)**
- **Goal:** both map panels subscribe to the geometry, style, items and
  selection facets, and the `_lastX` identity guards become version keys
  (Decision 9).
- **Scope:**
  - `flutter_client/lib/src/projects/map_panel.dart`
  - `flutter_client/test/map_panel_facet_scope_test.dart` (new)
  - the existing `test/map_panel_*_test.dart` files, renames only
- **Context:**
  - `geoOrStyleChanged` (1601) and `selectionChanged` (1586), and their
    manage-mode copies.
  - `test/map_panel_selection_cost_test.dart` is the scope test to keep.
- **Do:**
  - Subscribe to the facets.
  - Key the spec build on `(geo, style, items)` versions plus
    `showJournals`, and the restyle on the selection version.
  - Key the encounter markers on the items version.
  - Delete the identity fields this makes redundant.
- **Acceptance:**
  - `map_panel_selection_cost_test` and `map_panel_activities_toggle_test`
    pass unchanged.
  - The new test shows that a root-only notify (for example `isLoading`)
    does no spec work.
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** the screens around the panel.
- **Latitude:** local design.
- **Escalate if:** a guard protects something no facet version covers;
  X3.
- **Depends on:** U15.

**U17 — Activity panel, day carousel and elevation chart listen to facets
(#294)**
- **Goal:** these three widgets subscribe to the facets they read instead of
  the root. The elevation chart also follows `ElevationFacet`, which fixes
  its stale-`fullTrack` selectors.
- **Scope:** `activity_panel.dart`, `day_carousel.dart`, `elevation_chart.dart`
  (under `flutter_client/lib/src/projects/`); tests, renames only unless
  new: `flutter_client/test/activity_panel_*_test.dart` (7 files),
  `day_carousel_test.dart`, `elevation_chart_color_test.dart`, and
  `flutter_client/test/projects/facets/side_panels_scope_test.dart` (new).
- **Context:** the direct listeners at `activity_panel.dart:415/431` and
  `day_carousel.dart:111/180`; the per-tile `Selector`s at
  `activity_panel.dart:1678-2082`.
- **Do:** replace each root subscription with the facet or facets the
  widget reads.
- **Acceptance:**
  - Tests show the chart rebuilds when the elevation track is rebuilt.
  - A day-carousel tile does not rebuild on a geometry-only change.
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** map panel; screens.
- **Latitude:** local design.
- **Escalate if:** X3.
- **Depends on:** U15.

### Wave 8 — screens

**U18 — Screens listen to facets (#294)**
- **Goal:** every remaining `Consumer`, `Selector`, `context.watch` and
  `context.select` on a `ProjectNotifier` type reads only root state, or is
  converted to the facet it reads.
- **Scope:**
  - `app_screen.dart` and `view_screen.dart`
  - `../shared/shared_project_screen.dart`
  - `project_stats_screen.dart` and `travel_companions_section.dart`
  - any other file the inventory below finds
  - their tests
- **Context:** the listener inventory in this plan's Current state.
- **Do:**
  - Inventory every listener with a grep and record the list in the report.
  - Convert each listener.
  - Leave `Consumer<ProjectNotifier>` only where the builder reads root
    state alone.
- **Acceptance:**
  - The report lists every listener before and after.
  - A grep-based test (`test/projects/facets/root_listener_audit_test.dart`,
    new) fails if a root listener's builder references a facet.
  - `flutter analyze` and `flutter test` pass.
- **Out of scope:** `map_panel.dart`, `activity_panel.dart`,
  `day_carousel.dart`, `elevation_chart.dart` (done in U16 and U17). A
  listener found there is reported, not changed.
- **Latitude:** local design.
- **Escalate if:** X3.
- **Depends on:** U16, U17.

### Wave 9 — cut the bubble

**U19 — The root stops re-notifying facet changes (#294)**
- **Goal:** the last step of Decision 8.
- **Scope:** `flutter_client/lib/src/projects/project_notifier.dart`;
  `flutter_client/test/projects/facets/no_bubble_test.dart` (new); tests that
  counted root notifies for facet changes (renames to the facet listener
  only).
- **Context:** U9's bubbling code.
- **Do:**
  - Remove the bubbling.
  - Every remaining `notifyListeners()` on the root must change root state.
- **Acceptance:**
  - `no_bubble_test` shows a change to each facet does not notify the root.
  - A widget test shows that selecting a day rebuilds no polyline geometry
    (the #294 verification).
  - The account-switch, resolve-poll, LOD and day-meta tests from earlier
    units all pass.
  - `flutter analyze` and `flutter test` pass.
  - The `ProjectNotifier` line and notify counts are recorded in the
    report.
- **Out of scope:** further splitting.
- **Latitude:** local design.
- **Escalate if:** a widget stops updating and no facet covers what it reads;
  X3.
- **Depends on:** U18.

## Definition of done

- **#418**
  - After a logout or a 401, a second account on the same device sees none
    of the first account's trip, filters, selection, last-opened trip,
    cached metadata or unlocked E2EE key, including when it opens the same
    trip name by deep link.
  - `User.id` is the real account id after a session restore, online or
    offline.
- **#397**
  - Two devices editing different days of one trip both keep their changes.
  - A settings save no longer sends day-meta it didn't change.
  - An old build's `PUT` gets 426 with a readable message and changes
    nothing.
  - Saving a note keeps the carousel's days up to today.
- **Minimum version:** `/api/version` serves `min_client_version`. A build
  that ships the gate and later falls below the minimum is stopped with an
  update screen or a reload bar.
- **Stuck route jobs:** a route job stuck for more than 30 minutes is logged
  hourly.
- **#379**
  - After any edit, `geo` holds geometry at the zoom on screen, stamped with
    the bucket and box it was fetched for.
  - No mutation writes full-resolution geometry to memory, except on E2EE
    trips.
- **#278**
  - A train route that resolves minutes later appears on the map without
    reopening the trip, also when several resolves are queued.
  - A stale refetch never puts the placeholder back.
- **#478:** switching auto-zoom on fits the current selection.
- **#294**
  - Geometry, selection, style, items and elevation are separate facets.
  - Selecting a day rebuilds no polyline geometry.
  - The root notifier notifies only for its own state.
  - `map_panel.dart` has no identity guard that a facet version covers.
- **#401**
  - The perf report shows `geo_lod_server` and `geo_lod_wait` with p50 and
    p90, and payload sizes.
  - Oscillating around a zoom boundary does not refetch.
  - The owner has recorded one device session on #401, and the next step is
    decided from it.
- The pytest CI command, `flutter analyze` and `flutter test` pass.
