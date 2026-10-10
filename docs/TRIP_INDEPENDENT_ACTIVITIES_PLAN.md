# Trip-independent activities — Plan for #592

Supersedes #551.

## Problem

The owner's model (2026-10-10): **trips are independent.** When the same
activity is in two trips, nothing ties the two together. Anything done to it in
one trip is invisible to the other: a track edit, a split, a reset, a rename, a
Strava refresh or encryption.

The code does not work that way. An `activity` row is one global row, and its
primary key doubles as the Strava id (`models/project_db.py:234-240`). Trips
only reference it, through `projectitem.activity_id`. Every write goes to the
shared row, but only the acting trip is bumped and cache-busted, so the other
trip either changes under the user or serves stale data. Known effects:

- **A split leaks into the other trip.** Trip B shows the cut-down head and the
  "(1/N)" name, but not the tails (`split_activity`,
  `src/project/repo_activities.py:654-762`; `_renumber_split_family` :617-652).
- **A reset destroys another trip's data.** A reset deletes the split pieces'
  rows outright (`_remove_split_descendants`, :439-479). Any other trip holding
  them is left with items pointing at nothing (#551 is one route into this).
- **Re-adding renames.** Adding or syncing an activity into trip B, and every
  `save_project` of B, overwrites the row's name in A (`_upsert_activity`,
  :1331-1349). For example, a split's "(1/2)" reverts to the Strava name.
- **Edits, Strava refreshes and enrichment** change both trips. Elevation
  gain and E2EE field writes fan out to every trip holding the row.
- **A companion's activity in the owner's trip is forced to stay plaintext**
  (`shared_with_others`, the 409 `shared_with_other_trip`, and the client's
  `_repairShared`).

## Current state

The inventory behind this plan comes from the 2026-10-10 investigation. Each
line names where to look.

- **Table.** `activity` (`models/project_db.py:234-368`): PK `id` with no
  autoincrement. Positive ids are Strava ids; negative ids are local, drawn at
  random by `allocate_local_activity_id` (`src/project/local_ids.py:65-79`).
  - `user_info_id` is the owner: today, the account that imported the row.
  - `source` is NULL, "" or "strava" for Strava rows, and "gpx" for GPX
    imports. `source_id` is the GPX fingerprint.
  - **No column holds the Strava id apart from `id`.**
  - Referenced by `projectitem.activity_id` and by
    `activity_geo_prepared.activity_id` (its PK). SQLite does not enforce
    foreign keys here.
  - Split columns: `split_root_id`, `split_parent_id`, `split_base_name`.
  - Edit snapshot: `original_*`, taken at the **first** edit
    (`_snapshot_first_edit`, :162), so it holds the untouched track.
- **Creating and reusing rows.** `_upsert_activity` (:1316-1413) looks a row up
  by id and reuses it across trips. It is called from:
  - `save_project` (`repo_core.py:469-471`), on every save of every trip;
  - `add_activities` (`api/activities.py:396-496`);
  - `strava_sync` (`api/strava.py:779-850`);
  - the `.traxj`/ZIP import (`repo_transfer.py:102-150, 441-443`).

  GPX imports and split tails always create new rows.
- **The id used as a Strava id:**
  - enrichment (`api/activities.py:303-338`);
  - refresh (`:1393-1491`, `force_update_activity` repo :1127-1218);
  - the Strava browser's `in_project` (`api/strava.py:723-749`);
  - `/sync/check` (`api/projects.py:1097-1107`);
  - dedup on add (`src/models/project.py:347-368`);
  - GPX export identity (`src/gpx/export_format.py:51-53, 109-110`);
  - GPX re-import match (`api/activities.py:726-728`);
  - orphan deletion (`delete_unreferenced_strava_activities`, repo
    :995-1112, called from `project_items.py:107-113`,
    `activities.py:2237`, `projects.py:1164-1177`,
    `project_transfer.py:504-536` and `strava.py:641`);
  - the `id > 0` / `id < 0` proxies in repo :428, :944, :1266.
- **Code that exists only because rows are shared:**
  - `own_activities_only`, `activity_owners` (repo :1288-1314);
  - `_drop_activities_of_other_accounts` (`repo_core.py:200-252`);
  - `_refuse_shared` (`api/activities.py:1612-1627`);
  - the `shared_with_others` flag (`repo_core.py:167-180, 592-611`);
  - the holder fan-out in `PUT /api/activities/{id}` and `…/elevation-gain`
    (`api/activities.py:2349-2403, 2427-2504`);
  - the cross-trip refusal in `delete_local_activity` (repo :949-957);
  - the account-deletion sweep of the user's rows in other users' trips
    (`src/auth/account_deletion.py:441-470`).
- **Client** (`flutter_client/lib/src/`): activities are raw maps.
  - It holds no `strava_id` field and builds no strava.com links.
  - It treats a negative id as "local" in two places:
    `activity_editor_page.dart:88` (the reset label) and
    `activity_panel.dart:1768` (the "Delete local activity" button).
  - "Re-fetch from Strava" in the import picker passes the Strava id as the
    row id (`strava_import_screen.dart:377-386` → `refreshActivity`), and
    `_pollActivityRefresh` matches `/meta` by that id
    (`project_notifier.dart:3240`).
  - The on-disk cache invalidates on `lock_version`, or on
    `_kSchemaVersion` (`project_data_cache.dart:58, 190-208`).
- **Backend.** Production runs SQLite (`docs/DEPLOYMENT_VPS.md:248`), and
  `entrypoint.sh` runs `alembic upgrade head` on start. The Alembic head is
  `a8d3f5c2e917`; check `alembic heads` at delivery. Precedents for a table
  rebuild: `4b9d2e7a1c63` (`batch_alter_table`, re-runnable). Precedent for a
  chunked row rewrite: `b7f1a3c9d204`.
- **Tests that pin sharing**, all of which must change:
  - `tests/test_activity_shared_rows.py`, all of it;
  - `tests/test_orphan_strava_activities.py`;
  - in `tests/test_activity_ownership.py`, :209, :322 and :147-264;
  - in `tests/test_local_delete_scope.py`, :203 and :262;
  - in `tests/test_replace_import_prunes_strava.py`, :161 and :187;
  - in `tests/test_activity_fields_update_cas.py`, :116.

## Decisions

1. **One row per trip** (owner, 2026-10-10). Every activity row is held by
   exactly one trip. *Rules out* copy-on-write: every writer would have to
   remember to fork first, which is the class of bug being fixed.
2. **Ids are opaque, and existing rows keep theirs** (owner). A new
   `strava_id` column carries the Strava identity. Every new row gets an
   allocated id. No code may derive meaning from an id again, except the one
   compatibility convention in Decision 3. *Rules out* renumbering every row,
   which would mean a primary-key rewrite on SQLite with no precedent, and a
   change to every id clients and exports have seen.
3. **Sign convention kept for installed builds.** A Strava-sourced row gets a
   positive allocated id, and a local row (GPX, split piece) a negative one.
   Both are random 53-bit ids, checked against the whole table as today.
   - Installed APKs (E5) still read the sign as "Strava or local", for the
     reset label and the delete button. Keeping the convention keeps them
     correct.
   - New code reads `source` / `strava_id`, never the sign.
   - *Rules out* all-negative allocation, which would show "Delete local
     activity" on every new Strava activity in an old build.
4. **The trip's owner owns every row in the trip** (owner, 2026-10-10),
   including a companion's activity in the owner's trip.
   - The owner can encrypt it.
   - It survives the companion deleting their account.
   - `user_info_id` therefore means "owner of the trip that holds it".
   - *Rules out* keeping the importing account as owner. That would keep
     companion copies plaintext and leave cross-trip ownership checks in place.
   - The privacy policy must say this (U9, O1).
5. **"Imported by" is kept apart from ownership.** A new nullable
   `strava_user_id` column holds the account whose Strava connection the
   activity came from. Enrichment and refresh use that account's token. It is
   set on create from Strava (sync, add, picker) and by the migration (the
   row's current `user_info_id`). It is set to NULL when that account is
   deleted, and refresh then answers a clear error.
   - *Rules out* using the trip owner's token: the companion's activity is not
     in the owner's Strava account, so Strava would answer 404.
6. **Strava updates touch only the requesting trip's copy** (owner).
   - Refresh, enrichment and sync write the row of the trip that asked.
   - Another trip's copy of the same `strava_id` keeps what it has until that
     trip is synced or refreshed.
   - *Rules out* fanning Strava data out to every unedited copy.
7. **Adding a Strava activity to a trip finds the trip's own row by
   `strava_id`, or creates one.**
   - Re-adding an activity the trip already holds changes nothing. It does not
     overwrite the name, so a split's "(i/N)" survives.
   - `save_project` upserts only the rows of the trip being saved, by row id.
     It never reaches a row of another trip.
8. **Import always creates new rows.**
   - A `.traxj`/ZIP import, whether new, "Keep both" or "Replace", allocates
     fresh rows for every activity in the file. It copies the file's content
     and sets `strava_id` from the file. In an old file, a positive id with a
     Strava source *is* the Strava id.
   - "Replace" deletes the trip's old rows. The cross-account remapping in
     `_as_importers_activities` goes away, because nothing is shared to protect.
   - Split columns, `is_edited` and `original_*` come back from the file where
     it carries them. Today `_upsert_activity` drops them on insert, so an
     imported split loses its family.
9. **Removing an activity from a trip deletes its row; deleting a trip deletes
   its rows.**
   - Orphan deletion (`delete_unreferenced_strava_activities`) and every
     "another trip still holds it" check go away.
   - Strava disconnect no longer deletes activity rows. The privacy text
     already says activities stay in their trips.
10. **The migration splits shared rows** (U1). For every row referenced by
    more than one trip:
    - the trip whose reference is oldest (lowest `projectitem.id`) keeps the
      original;
    - every other trip gets a copy: a fresh id under Decision 3, every column
      copied, `user_info_id` set to that trip's owner, and the
      `activity_geo_prepared` row copied;
    - **split families are remapped inside each copy**, so a copied tail's
      `split_root_id`/`split_parent_id` point at that trip's copies.

    In the same migration:
    - a copied **split head whose tails are not in that trip** is restored from
      its first-edit snapshot. It gets back its original geometry and figures,
      the split columns are cleared, and its name is reset to
      `split_base_name`. That trip never split it; this undoes the leak.
    - every row's `strava_id` is backfilled (from `id`, for Strava-sourced
      positive rows), along with `strava_user_id` (from `user_info_id`, for
      Strava rows);
    - every row whose holding trip has a different owner is given to that owner
      (Decision 4);
    - activity items pointing at missing rows are deleted (owner), with a
      per-trip count in the log;
    - every touched trip has `lock_version` bumped.
11. **Old clients (E5).** The API shapes stay the same; activity dicts gain
    `strava_id` and that is the only addition.
    - `POST …/activities/{id}/refresh` and `/meta` keep working with row ids.
    - Compatibility for the installed picker: a refresh whose id is not a row
      of this trip, but is the `strava_id` of one, refreshes that row. An old
      picker's poll still never matches (it looks for the Strava id in
      `/meta`), so it ends its spinner early. That is accepted and cosmetic.
12. **The new client release** (U8, U9 in Wave 3):
    - reads `source` / `strava_id` instead of the id's sign;
    - refreshes from the picker by the trip's row id;
    - bumps the cache `_kSchemaVersion`, so no on-device cache built before the
      migration survives.

## Review envelope

REVIEW.md §2 defaults apply, with these additions:

- **Migration scale.** A few thousand trips, and the activity table in the low
  hundreds of thousands of rows. The migration runs at API start (`alembic
  upgrade head` in the lifespan). It must be re-runnable after an interrupted
  first run. It must not hold the whole table in memory: chunk the work as in
  `b7f1a3c9d204`.
- **Downgrade is not supported.** A shared row cannot be re-formed safely from
  its copies. The downgrade raises, as the plan's restore path is the DB backup
  taken before deploy (RELEASING.md).
- **Companions.** Members of a trip (#106) add their own Strava activities to
  the owner's trip. After this change the copy belongs to the trip owner
  (Decision 4). This is accepted by the owner and is not a defect.
- **Stale stats.** Trips whose rows the migration changes need
  `project.stats_json` recomputed. Recomputing needs app code, so the migration
  marks them stale and the app recomputes them (U1 decides how). Stats briefly
  stale until a trip's next load are accepted.
- **Old builds** keep working as Decision 11 states. The early end of an old
  picker's refresh spinner is accepted.

## Boundaries crossed

- **Schema:** `activity.strava_id` (nullable, indexed, not unique) and
  `activity.strava_user_id` (nullable). Added with one Alembic migration (U1).
- **Stored data:**
  - shared rows split into per-trip copies;
  - ownership moved to the trip owner;
  - split heads in trips that never split them restored;
  - dangling items deleted;
  - `lock_version` bumped on every touched trip.

  The migration cannot be downgraded; restore is from the pre-deploy backup.
- **API contract:**
  - Additive only. Activity dicts gain `strava_id`.
  - The 409 `shared_with_other_trip` and the `shared_with_others` flag stop
    occurring. Old clients handle their absence.
  - Refresh accepts a Strava id as a fallback (Decision 11).
- **Export format:** `.traxj` gains a `strava_id` key per activity. Import
  reads it, and falls back to a positive Strava-sourced `id` for old files.
  GPX `traxj:source_id` for Strava rows becomes the `strava_id`. Old exports
  stay importable.
- **Privacy text:** `legal/privacy.html` must say that an activity added to
  someone else's trip belongs to that trip, and stays when the contributor
  deletes their account (O1).
- **Client cache:** `_kSchemaVersion` is bumped (U8).

## Conventions

- **Every unit:** no new code reads meaning from an activity id's sign or
  value. Identity comes from `strava_id`, origin from `source`, and ownership
  from the holding trip.
- **Tests:** server tests run in the `traxjourney-py314-citest` container from
  a `git archive`, with path conversion off in Git Bash
  (`MSYS_NO_PATHCONV=1`). Threaded tests use file-backed SQLite.
- **Migrations:** after any rebase, `alembic heads` shows one head. Tests never
  pin their own head.
- **Commit messages:** user-visible commits carry a `Release-Note:`. The
  migration's commit carries an `Upgrade-Note:` saying to take a DB backup
  first, because downgrade is not possible.

## Open decisions

- **O1 — Privacy-policy wording** for Decision 4. The owner writes or approves
  the sentence before U9 runs. A suggested draft is in U9.

## Execution units

### Wave 1

#### U1 — Migration: one row per trip
- **Goal:** after `alembic upgrade head`, every activity row is held by one
  trip and carries `strava_id` / `strava_user_id`. The trip's owner owns it,
  and no item points at a missing row.
- **Scope:**
  - `alembic/versions/<new>_one_activity_row_per_trip.py` (new);
  - `models/project_db.py`: the two columns, with comments;
  - `tests/test_migration_one_activity_row_per_trip.py` (new).
- **Context:**
  - Plan Decisions 3, 4, 5 and 10, and the Review envelope.
  - Examples to follow:
    - for a chunked, re-runnable data rewrite of `activity`:
      `alembic/versions/b7f1a3c9d204_*.py`;
    - for adding columns: `alembic/versions/e3c8a1d5b742_*.py`;
    - for its test: `tests/test_migration_prune_orphaned_tails.py` and
      `tests/test_migration_split_parent_id.py`.
  - Id allocation: `src/project/local_ids.py`. The migration must not import
    app models, so the random 53-bit allocation with its collision check is
    re-implemented inline, positive for Strava-sourced rows and negative for
    local ones.
  - Split snapshot: `_snapshot_first_edit` / `reset_activity_track`
    (`src/project/repo_activities.py:153-170, 481-603`), for which columns a
    restore puts back.
- **Do:**
  1. Add `strava_id` (BIGINT, nullable, indexed) and `strava_user_id` (INTEGER,
     nullable). Backfill `strava_id = id` and `strava_user_id = user_info_id`
     for Strava-sourced rows (`source` NULL, "" or "strava", and `id > 0`).
  2. Delete activity items whose `activity_id` names no row. Log the count per
     project.
  3. For each row referenced by more than one project, apply Decision 10:
     - the original stays with the lowest `projectitem.id`;
     - every other project gets a copy: all columns, a new id by sign,
       `user_info_id` = that project's owner, and the prepared row copied;
     - split families are remapped within each copy;
     - a copied split head with no tails in its project is restored from its
       snapshot, and its split columns are cleared;
     - the project's items are repointed.
  4. For every row whose single holding project has another owner, set
     `user_info_id` to that owner.
  5. Bump `lock_version` on every project touched by steps 2-4, and mark its
     stats for recompute.
  6. Make the migration re-runnable: a second run is a no-op, and an
     interrupted first run can be run again.
  7. Downgrade raises, with a message naming the backup restore.
- **Acceptance:** `pytest tests/test_migration_one_activity_row_per_trip.py`
  passes, covering:
  - backfill of both columns;
  - a Strava row shared by two trips ends as two rows, with the oldest
    reference keeping the original and the copy positive;
  - a GPX row shared by two trips gets a negative copy;
  - a split family shared by two trips is remapped per copy;
  - a split head in a trip without its tails is restored, with original
    geometry, cleared split columns and the base name;
  - a companion's row in the owner's trip changes owner;
  - dangling items are deleted and counted;
  - `lock_version` is bumped only on touched trips;
  - a second run is a no-op;
  - downgrade raises.

  `alembic heads` shows one head.
- **Out of scope:** any app code path (Waves 2-3), the client, and
  `stats_json` recomputation beyond marking it.
- **Latitude:** local design.
- **Escalate if:**
  - another table turns out to hold activity ids;
  - `stats_json` cannot be marked stale in a way the app already honours;
  - `projectitem.id` is not monotonic in insertion order;
  - a file outside Scope must change (X3).
- **Depends on:** —

### Wave 2

#### U2 — Activity model and file formats carry `strava_id`
- **Goal:** the `Activity` model, `.traxj` and GPX export/import carry
  `strava_id` and keep the split and edit state.
- **Scope:**
  - `src/models/activity.py`, `src/project/project_io.py`,
    `src/project/traxj_schema.py`, `src/gpx/export_format.py`;
  - their tests: `tests/test_traxj_schema*.py`, `tests/test_gpx_export*.py`,
    `tests/test_project_io*.py`, and new tests in those files.
- **Context:**
  - Plan Decisions 2, 8 and 11; Boundaries "Export format".
  - Example: how `source` / `source_id` are already carried through
    `to_strava_dict`, traxj_schema and `activity_identity`.
- **Do:**
  1. `Activity` gains `strava_id`, read and written by its dict forms.
  2. The `.traxj` writer emits `strava_id`. The reader takes it, falling back
     to a positive `id` with a Strava source for old files.
  3. The reader keeps `split_root_id`, `split_parent_id`, `split_base_name`,
     `is_edited` and `original_*` where the file carries them.
  4. GPX `activity_identity` uses `strava_id` for Strava rows.
- **Acceptance:** the listed test files pass, with new tests for:
  - the `strava_id` round-trip;
  - an old file whose positive id becomes the `strava_id`;
  - a split family and edit snapshot surviving a `.traxj` round-trip at the
    model level;
  - GPX identity from `strava_id`.
- **Out of scope:** writing rows to the database (U3, U6).
- **Latitude:** local design.
- **Escalate if:** the `.traxj` schema version has to change, or a file
  outside Scope must change (X3).
- **Depends on:** U1.

#### U3 — Repository: rows belong to one trip
- **Goal:** the repository creates, finds and saves activity rows per trip,
  allocates ids under Decision 3, and has no cross-trip sharing logic left.
- **Scope:**
  - `src/project/repo_activities.py`, `src/project/repo_core.py`,
    `src/project/local_ids.py`;
  - tests: `tests/test_activity_ownership.py`,
    `tests/test_orphan_strava_activities.py`,
    `tests/test_local_delete_scope.py`, `tests/test_activity_shared_rows.py`,
    `tests/test_activity_split_api.py`, `tests/test_activity_lock_version.py`,
    plus new tests.
- **Context:**
  - Plan Decisions 1-9 and Conventions.
  - The inventory under Current state (repo lines).
  - Example, for the shape of a per-trip lookup: the GPX duplicate checks
    `_existing_import` / `_existing_identity` (`api/activities.py:701-742`),
    which are already scoped to the trip.
- **Do:**
  1. Add `allocate_activity_id(sess, *, strava: bool)` to `local_ids.py`,
     covering both signs.
  2. Change `_upsert_activity` / `save_project` as follows:
     - a trip's activity is found by row id among that trip's rows;
     - a Strava activity not yet in the trip gets a new row (positive id,
       `strava_id`, owner = trip owner, `strava_user_id` = the adding account);
     - a row of another trip is never read or written;
     - re-adding never overwrites the name.
  3. Make `force_update_activity` and `update_activity_enrichment` write the
     given row only.
  4. Remove `own_activities_only`, `activity_owners`,
     `_drop_activities_of_other_accounts`, the `shared_with_others` query and
     flag, `delete_unreferenced_strava_activities`, the cross-trip refusal in
     `delete_local_activity` and the `id > 0` / `id < 0` proxies. Leave an
     equivalent per-trip helper where a caller in `api/` still needs one, and
     list those helpers in the report for U4-U7.
  5. Rewrite the tests that pinned sharing to pin independence:
     - the same Strava activity added to two trips makes two rows;
     - editing, splitting or resetting it in one trip leaves the other
       untouched;
     - removing it from one trip deletes only that row.
- **Acceptance:**
  - The Scope's test files pass in the container, with new tests for:
    - two trips → two rows;
    - edit, split and reset isolation;
    - re-add keeping a split name;
    - allocation sign by source;
    - `save_project` never touching another trip's row.
  - The full suite passes, apart from tests U4-U7 own, which are listed in the
    report.
- **Out of scope:** `api/*` routes (U4, U5, U6), import (U6) and the client.
- **Latitude:** local design.
- **Escalate if:** a caller outside Scope breaks in a way a per-trip helper
  cannot bridge until Wave 3, or a file outside Scope must change (X3).
- **Depends on:** U1.

### Wave 3

#### U4 — Activity routes use the trip's row and `strava_id`
- **Goal:** the routes in `api/activities.py` use per-trip rows and
  `strava_id`, and send Strava API calls with `strava_user_id`'s token.
- **Scope:** `api/activities.py`, `tests/test_activity_refresh*.py`,
  `tests/test_activity_enrichment*.py`, `tests/test_activity_fields_update_cas.py`,
  `tests/test_activity_encryption*.py`, `tests/test_gpx_reimport_own_export.py`.
- **Context:**
  - Plan Decisions 4-7, 9 and 11.
  - U3's report, for the helpers it left.
  - Example: the existing refresh route and its tests.
- **Do:**
  1. `add_activities` maps posted Strava objects to the trip's rows by
     `strava_id` (U3).
  2. Enrichment and refresh:
     - call Strava with `strava_id`, using `strava_user_id`'s token;
     - when `strava_user_id` is NULL or disconnected, answer a clear 409;
     - write only the trip's row.
  3. Refresh accepts a Strava id fallback (Decision 11).
  4. Remove `_refuse_shared` and the holder fan-out in the E2EE and
     elevation-gain routes. Each write bumps only the trip that holds the row.
  5. The delete-local route deletes the trip's row.
  6. The GPX re-import match uses `strava_id`.
- **Acceptance:** the Scope's tests pass, with new tests for:
  - refresh with a companion's activity using the companion's token;
  - refresh after the importer was deleted → 409;
  - refresh by Strava id from an old picker;
  - an encrypted write no longer refused for a companion's activity;
  - no cross-trip bump.
- **Out of scope:** the Strava sync and browser routes (U5), import (U6), and
  `src/auth/` (U7).
- **Latitude:** local design.
- **Escalate if:** a file outside Scope must change (X3).
- **Depends on:** U3.

#### U5 — Strava sync, browser and trip routes per trip
- **Goal:** `strava_sync`, the Strava activity browser, `/sync/check`,
  disconnect and trip deletion work per trip by `strava_id`.
- **Scope:** `api/strava.py`, `api/projects.py`,
  `tests/test_strava_sync*.py`, `tests/test_strava_disconnect.py`,
  `tests/test_projects_sync_check*.py`, and the trip-delete tests.
- **Context:**
  - Plan Decisions 6, 7 and 9.
  - U3's report.
  - Example: `in_project` today (`api/strava.py:723-749`).
- **Do:**
  1. Sync adds the Strava activities whose `strava_id` the trip lacks, and
     sets `strava_user_id` to the syncing account.
  2. The browser's `in_project` and `/sync/check` compare against the trip's
     `strava_id`s.
  3. Disconnect deletes no activity rows.
  4. Trip deletion deletes the trip's rows and their prepared rows.
- **Acceptance:** the Scope's tests pass, with new tests for:
  - sync into a trip that shares a Strava activity with another trip creating
    its own row;
  - `in_project` per trip;
  - disconnect keeping rows;
  - trip deletion removing exactly its rows.
- **Out of scope:** `api/activities.py` (U4) and import (U6).
- **Latitude:** local design.
- **Escalate if:** a file outside Scope must change (X3).
- **Depends on:** U3.

#### U6 — Import, item and segment paths create and delete per trip
- **Goal:** `.traxj`/ZIP import always creates fresh rows, and removing an item
  deletes its row.
- **Scope:**
  - `src/project/repo_transfer.py`, `api/project_transfer.py`,
    `api/project_items.py`, `api/segments.py`;
  - tests: `tests/test_replace_import_prunes_strava.py`,
    `tests/test_import_name_conflict.py`, `tests/test_project_transfer*.py`,
    `tests/test_project_items*.py`, `tests/test_segments*.py`.
- **Context:**
  - Plan Decisions 8 and 9.
  - U2's model.
  - U3's report.
  - Example: the existing "Keep both" and "Replace" tests.
- **Do:**
  1. Import allocates a new row for every file activity (Decision 8). It
     restores split and edit state, remaps split ids within the file, and
     drops `_as_importers_activities`' cross-account logic.
  2. "Replace" deletes the trip's previous rows.
  3. Removing an activity item deletes its row and prepared row.
  4. Segment saves never upsert another trip's row.
- **Acceptance:** the Scope's tests pass, with new tests for:
  - "Keep both" of your own trip making independent rows;
  - an imported split family that stays a family;
  - "Replace" removing the old rows;
  - item removal deleting the row.
- **Out of scope:** `api/activities.py` (U4).
- **Latitude:** local design.
- **Escalate if:** a file outside Scope must change (X3).
- **Depends on:** U2, U3.

#### U7 — Account deletion and audit script follow per-trip ownership
- **Goal:** account deletion and `scripts/audit_activity_ownership.py` match
  Decision 4.
- **Scope:** `src/auth/account_deletion.py`,
  `scripts/audit_activity_ownership.py`, `tests/test_account_deletion*.py`,
  and the audit tests in `tests/test_activity_ownership.py`. U3 also edits
  that test file, which is why U7 runs in Wave 3.
- **Context:**
  - Plan Decisions 4 and 5.
  - `account_deletion.py:441-470, 505`.
- **Do:**
  1. Account deletion deletes the rows of the user's own trips. It no longer
     sweeps other users' trips. It sets `strava_user_id` to NULL on rows the
     user imported into other users' trips.
  2. The audit script checks that each row's owner is its holding trip's owner,
     and that each row is held by exactly one trip.
- **Acceptance:** tests pass for:
  - a companion deleting their account leaves their contributed activity in the
    owner's trip, owned by the owner, with `strava_user_id` NULL;
  - the audit flags a row held by two trips, and a row whose owner differs
    from its trip's owner.
- **Out of scope:** the privacy text (U9).
- **Latitude:** local design.
- **Escalate if:** a file outside Scope must change (X3).
- **Depends on:** U3.

#### U8 — Client reads `source` / `strava_id`, refreshes by row id
- **Goal:** the Flutter client stops reading meaning from an activity id.
- **Scope:**
  - `flutter_client/lib/src/projects/activity_editor_page.dart`,
    `activity_panel.dart`, `strava_import_screen.dart`,
    `strava_import_notifier.dart`, `project_notifier.dart` (refresh poll
    only), `project_data_cache.dart` (`_kSchemaVersion`);
  - their tests under `flutter_client/test/`.
- **Context:**
  - Plan Decisions 3, 11 and 12.
  - The client inventory under Current state.
  - Example: the existing `source == 'gpx'` checks in `items_facet.dart:153-161`.
- **Do:**
  1. Decide "local" from `source` (`gpx`, or a split piece via
     `split_parent_id`) for the reset label and the delete button, never from
     the sign.
  2. The picker's re-fetch finds the trip's row whose `strava_id` matches and
     refreshes and polls by that row id.
  3. Bump `_kSchemaVersion`.
- **Acceptance:**
  - Widget and unit tests:
    - a positive-id GPX row gets the local label and button;
    - a Strava row with a negative id does not;
    - the picker refresh posts the row id and its poll ends when `/meta`
      shows that row done;
    - a cache row with the old schema version is a miss.
  - `flutter analyze` is clean.
  - The targeted tests pass in the Flutter test container.
- **Out of scope:** the E2EE `shared_with_others` handling, which stays,
  tolerant of the flag's absence; server changes.
- **Latitude:** local design.
- **Escalate if:** a file outside Scope must change (X3).
- **Depends on:** U4 (refresh route), U2 (the `strava_id` field).

#### U9 — Privacy policy: contributed activities belong to the trip
- **Goal:** `legal/privacy.html` states Decision 4.
- **Scope:** `legal/privacy.html`, plus any test that pins its text (grep
  `tests/` and `flutter_client/test/` for `privacy`).
- **Context:**
  - O1.
  - The deletion paragraph at `legal/privacy.html:362-370` and the Strava
    paragraph at :147-153.
  - Example: how #427 changed this file.
- **Do:** add the owner-approved sentence (O1) to the deletion paragraph.
  Suggested draft: "Activities you add to a trip someone else owns become part
  of that trip: they belong to its owner and stay in it when you delete your
  account."
- **Acceptance:**
  - The sentence is present, verbatim as approved.
  - Any test that checks the policy text passes.
- **Out of scope:** the terms, other policy sections, and the notice to users
  (RELEASING.md §"significant changes" is the owner's call).
- **Latitude:** none.
- **Escalate if:** O1 is not decided.
- **Depends on:** —

**Waves and W1/W3/W4:**
- Wave 1: U1 alone (the migration).
- Wave 2: U2 and U3, which share no files.
- Wave 3: U4, U5, U6, U7, U8 and U9 share no files, but W3 caps a wave at four
  units, so it runs as **3a** (U4, U5, U6, U7) then **3b** (U8, U9). U8 needs
  U4's refresh route integrated first.

## Definition of done

- The same Strava activity added to two trips is two rows. Editing,
  splitting, resetting, refreshing or encrypting it in one trip changes nothing
  in the other.
- Re-adding or syncing never renames an activity in any trip.
- No server code reads meaning from an activity id. Identity is `strava_id`,
  and the Strava token comes from `strava_user_id`.
- After the migration on a copy of production-shaped data:
  - no row is held by two trips;
  - no item points at a missing row;
  - every row is owned by its trip's owner;
  - a split head in a trip that never split it shows its original track.
- A companion's activity in the owner's trip can be encrypted by the owner and
  survives the companion's account deletion; the privacy policy says so.
- Installed APKs keep working: labels are correct, and refresh from the picker
  works, even though its spinner may end early.
- The full pytest and Flutter suites pass, `alembic heads` shows one head, and
  `flutter analyze` is clean.
