# Privacy and security follow-ups (package A) — Plan for #470, #507, #508, #509, #510, #512

## Problem

The reviews of privacy package 1 (#430, #431, #440, #443) and the #452 photo
work left six defects in code those changes did not touch:

- **#510** — a failed Google sign-in logs google-auth's exception text, which
  for a malformed id_token embeds the raw token (`Wrong number of segments in
  token: b'…'`). Logs are kept 30 days in Loki.
- **#508** — the server-side Strava client saved refreshed tokens to
  `~/.config/traxjourney/tokens.json` under a shared key.
- **#512** — activity enrichment builds its own Strava client without the
  token-rotation callback added by #440. A refresh during enrichment rotates
  the refresh token and saves nothing, so the stored one stops working: the
  next Strava call asks the user to re-authenticate, and a disconnect revokes
  a token Strava no longer knows (it answers 200), leaving the app authorised.
- **#509** — Strava activity rows that are in no trip stay in the database for
  good, including after the user disconnects Strava.
- **#507** — companions and invite previews show a user's **email address**
  when that user has no display name.
- **#470** — people avatars are stored, served and deleted under the
  **caller's** data folder, not the trip owner's: a companion gets 404 for
  avatars the owner uploaded. Upload also writes the files before checking
  the caller may edit that person, so a refused upload leaves orphan files
  counted against the caller's storage.

## Current state

- `api/auth.py:266-283` — `google_auth` catches every exception from
  `verify_oauth2_token` and logs `"%s", exc`.
- **#508 is already fixed on main** by `a19fc327` (PR #550, quiet fix,
  unreleased): `StravaAPI` keeps tokens in memory only and `TokenStore` is
  gone. The container's home directory is not a volume
  (`docker-compose.yml.example`), so an existing `tokens.json` disappears when
  the container is recreated by the next deploy (`docker compose up -d`).
- `api/strava.py:185-226` — `_strava_client_for_token` wires
  `StravaAPI.on_token_refresh` to `_persist_rotated_token`, which saves the
  rotation (or revokes the new tokens if the user disconnected meanwhile).
  `api/activities.py:160-174` — `_strava_client_for_user` builds a
  `StravaAPI` from the same row with no callback; used by enrichment
  (`:249`, `:948`) and as a connected check (`:1072`).
- `api/strava.py` `strava_disconnect` deletes `StravaToken` and
  `DBStravaCache` only. `api/project_items.py:62-82` deletes a removed
  activity's row only for **negative** (local, split-tail) ids, via
  `_repo.activity_rewritable_by_trip` / `_repo.delete_local_activity`.
  `DBActivity` (`models/project_db.py:237`) is keyed by the Strava id and
  shared by every trip that references it; `DBActivityGeoPrepared` is its
  side table.
- `api/members.py:133-136` `_display_name` returns `display_name or email`;
  used by the members list (`:400`, `:412`), invite previews (`:487`, `:503`)
  and the invite email (`:309`). `api/projects.py:176` repeats it for
  `owner_name`. `PUT /api/auth/me` (`api/auth.py:408-423`) stores
  `display_name.strip()` with no check, so a blank name can be saved.
  Sign-up already requires first and last names (`api/auth.py:105-114`);
  Google sign-in falls back to the email's local part (`:309`).
- `api/people.py` — `_avatar_dir`, `upload_avatar`, `delete_avatar`,
  `serve_avatar`, `serve_avatar_thumb` and `delete_person` all key the folder
  on `current_user["sub"]`. Memories already do it right: `_owner_dir_id`
  (`api/memories.py:150`) and `upload_photo` (`:631-660`) — permission first,
  owner's folder, owner's quota, size limit, thumbnail off the event loop.

## Decisions

1. **#510: log the exception class and a mapped reason, never `str(exc)`.**
   google-auth's exception types don't separate the reasons:
   - expired, used too early and wrong audience all raise `InvalidValue`;
   - wrong segment count and bad signature both raise `MalformedError`;
   - a wrong issuer raises `GoogleAuthError`.
   
   So the reason is classified by fixed keywords looked up in `str(exc)`
   ("expired", "too early", "audience", "issuer", "signature", "segments").
   Only the class name and the mapped reason are logged ("other" when no
   keyword matches); the text is never logged. Rules out redacting the
   message text, which depends on google-auth's wording, and logging the class
   name alone, which loses the expired/too-early distinction clock-skew issues
   need. (R1-5)
2. **#508: no code change.** It is closed after the next release is deployed,
   with a comment saying `tokens.json` is gone once the container is
   recreated. Rules out a startup step that deletes the file, since the home
   directory does not survive a deploy anyway.
3. **#512: one factory for every server-side Strava client.**
   `_strava_client_for_user` loads the row and returns
   `api.strava._strava_client_for_token(row)`, so enrichment gets the same
   rotation callback as the Strava routes. `api/activities.py` already imports
   from `api.*`, and `api/strava.py` does not import `api.activities`, so no
   import cycle appears. Rules out moving the factory into `src/` in this
   change (only two call sites).
4. **#509 (owner decision): delete orphan Strava activity rows in two
   places.**
   - On **disconnect**: every Strava-origin activity row of that user that no
     project item anywhere references.
   - On **removal from a trip**: the removed activity's row, when it is
     Strava-origin and no project item anywhere still references it. The
     check is on the row whatever its `user_info_id`: in a shared trip the row
     may belong to the companion who imported it, not to the caller or the
     owner. (R1-3)
   - On **trip deletion**: every Strava-origin row the deleted trip
     referenced, under the same rule. (R1-4) Split tails of a Strava-origin
     root that the deleted trip held, and that no other project item
     references, go with it, and the root is judged after them. Tails are
     local rows, but they belong to a Strava family, so they are the one
     exception to "GPX and local rows are never touched". (R2-1)
   - After a **split tail is deleted**, the tail's `split_root_id`, when
     positive, is checked again: a root kept only because a tail named it
     becomes an orphan when its last tail goes. (R1-6)
   
   The owner accepts that track edits and splits on such a row are lost:
   re-adding the activity fetches it fresh from Strava. Its
   `DBActivityGeoPrepared` rows go with it. A row that other activities name
   as their `split_root_id` or `split_parent_id` is kept, since its split
   tails still need it. "Strava-origin" means a positive id with
   `source` empty or `strava`. GPX and local rows are never touched by this
   rule.
5. **#507 (owner decisions): the email is never a display-name fallback.**
   - Wherever another user's name is shown (members list, invite preview,
     invite email, shared-trip `owner_name`), a blank display name shows as
     **"Traveller"**.
   - `PUT /api/auth/me` refuses a blank name with 422, and the app's Settings
     name field can't be saved blank.
   - Google sign-in keeps defaulting to the email's local part (owner chose to
     keep it).
   - A local account with no `UserInfo` row gets one created at sign-in
     (`api/auth.py:186-196`). That path today copies the username, which is
     the email since #110, into `display_name`, and sets `email` to `""`. It
     changes to set `email=username` and leave `display_name` blank, which
     shows as "Traveller". The public-name helper also treats a display name
     equal to the account's sign-in address (case-insensitive, `UserInfo.email`
     or the linked `LocalUser.username`) as blank, which covers rows already
     created that way. (R1-1)
   
   Existing accounts with a blank name are not backfilled: they show as
   "Traveller" until they set a name.
6. **#470: avatars follow the memories pattern.**
   - The folder is keyed on the person's trip owner (an `_owner_dir_id`
     equivalent for people), for upload, serve, thumbnail, delete and person
     delete.
   - Upload checks edit permission and the **owner's** storage quota before
     reading the body into files, applies the same size limit as memory
     photos, and builds the thumbnail in the thread pool.
   - Rules out keeping per-uploader folders and searching them on serve.
7. **#470 (owner decision): move existing companion-uploaded avatars once.**
   An idempotent startup sweep, run from the lifespan after
   `alembic upgrade`, handles each person whose avatar is missing from the
   owner's folder. It looks for the avatar in a current member's
   `people/<id>/` folder, moves the full-size file and the thumbnail into the
   owner's folder, and moves the storage usage with them (subtract from the
   member, add to the owner). It logs one line per move and a summary.
   Rules out an Alembic migration (files, not rows) and a manual script (it
   would be forgotten on val and prod).

## Review envelope

REVIEW.md defaults apply, with:

- **Database: SQLite only** (owner decision 2026-09-28). Locking arguments
  may rely on SQLite's database-wide write lock.
- **Concurrency (U6):** deleting a Strava activity row can race a request that
  adds the same activity to a trip, from the same user or a companion's trip
  referencing that user's activity. Under the envelope this race is in scope.
  The delete must check "unreferenced" and delete in one write transaction,
  taking the write lock first, as `api/strava.py` `_claim_token_row` does.
- **Startup work (U5):** the sweep runs once per API start, in the single API
  process (E1). It must be safe to interrupt at any point and to run again:
  a half-done move must not lose a file or count it twice.
- **Split tails belong to one trip** (owner decision 2026-10-03). A local
  split piece referenced from more than one trip is an unsupported state:
  `delete_local_activity` already calls it corruption and refuses it. Findings
  that need it are outside the envelope. (That `add_activities` still accepts
  a negative id is filed as its own issue.)
- **Trust:** none new. Companions are trusted members of a trip (as for
  memories: viewer role may read, editor role may write).

## Boundaries crossed

- **API contract:** `PUT /api/auth/me` now answers 422 for a blank display
  name where it used to store it. Older app builds that let the field be
  cleared show their generic save error (E5). Every other response keeps its
  shape: `display_name` and `owner_name` stay strings, only the fallback
  value changes.
- **Stored data:**
  - U6 deletes activity rows that are referenced by nothing. This is the
    F2-sensitive part: it must never delete a row a project item references,
    or a split root.
  - U5 moves avatar files between users' folders and moves their storage
    usage. No schema change, no migration.
- **Export formats:** none.

## Conventions

- Tests that fire concurrent requests use a file-backed SQLite in `tmp_path`
  built with `models.db._make_engine` + `_configure_sqlite`, never
  `StaticPool` (see `tests/test_strava_disconnect.py`).
- Every new test is proved to fail on the code before the change; the unit's
  report says how.
- No graphify output is committed from a unit's worktree.
- Commit messages carry `Closes #N` for the issue the unit finishes, and a
  `Release-Note:` trailer in user vocabulary when users notice the change.

## Open decisions

None. #508 is closed after the next release is deployed (owner action, see
Definition of done).

## Execution units

### Wave 1 (independent)

#### U1 — Google sign-in failures log no token (#510)
- **Goal:** a failed Google sign-in logs the failure reason, never any part of the posted id_token.
- **Scope:** `api/auth.py`, `tests/test_google_auth.py`.
- **Context:** `api/auth.py:266-283`. For the log-capturing test, follow `tests/test_access_log_privacy.py` (its `server_log` fixture captures every logger).
- **Do:**
  1. Replace `"%s", exc` with the exception class name plus a reason mapped by fixed keywords found in `str(exc)` ("expired", "too early", "audience", "issuer", "signature", "segments"), defaulting to "other". The text itself is never logged or put in any other message (Decision 1).
  2. Keep the `LOGINS` failure metric and the generic 401 unchanged.
- **Acceptance:**
  - New tests in `tests/test_google_auth.py`:
    - a malformed id_token (`"SECRETtoken"`) and a three-segment garbage token never appear in any log line;
    - an expired-token error still logs a reason that says so.
  - Both tests fail on the current code.
  - `pytest tests/test_google_auth.py` passes.
- **Out of scope:** other auth logging; changing the 401 body.
- **Latitude:** local design.
- **Escalate if:** an id_token can reach a log line by another path (e.g. a request-body log); a file outside Scope is needed.
- **Depends on:** —

#### U2 — Enrichment saves Strava token rotations (#512)
- **Goal:** every server-side Strava client saves a rotated token the moment Strava issues it.
- **Scope:** `api/activities.py`, `tests/test_strava_disconnect.py` or a new `tests/test_enrichment_token_rotation.py`.
- **Context:** `api/strava.py:185-226` (`_strava_client_for_token`, `_persist_rotated_token`); `api/activities.py:160-174`. For the test, follow the rotation tests in `tests/test_strava_disconnect.py`.
- **Do:**
  1. `_strava_client_for_user` loads the token row, then returns `_strava_client_for_token(row)` imported from `api.strava`.
  2. Keep the `None` return when there is no row.
  3. Check that the `:1072` connected check still works.
- **Acceptance:**
  - New tests: a refresh during `_enrich_activities` stores the rotated tokens in the DB; a refresh after a disconnect revokes the new tokens.
  - Both fail on the current code.
  - The enrichment tests (`tests/test_activity_enrichment_*.py`) and `tests/test_strava_disconnect.py` pass.
- **Out of scope:** moving the factory into `src/`; changing enrichment scheduling.
- **Latitude:** none.
- **Escalate if:** importing from `api.strava` creates an import cycle; a file outside Scope is needed.
- **Depends on:** —

#### U3 — No email as a display-name fallback (#507)
- **Goal:** no API response or email shows another user's email address in place of their name, and a blank display name can't be saved.
- **Scope:** `api/members.py`, `api/projects.py`, `api/auth.py` (`UpdateProfileRequest`, `update_me` and the `UserInfo` auto-create in `login` only), `tests/test_project_members.py`, a new or existing auth test for `PUT /me`, `flutter_client/lib/src/settings/settings_screen.dart`, `flutter_client/test/settings/` (new test).
- **Context:** `api/members.py:133-136`, `api/projects.py:176`, `api/auth.py:105-114` (sign-up's blank-name validator is the example for the 422), `api/auth.py:408-423`. For the Flutter test, see memory "Settings-screen widget tests": pump narrow and swap the `api` global; `flutter_client/test/settings/delete_account_test.dart` is a working example.
- **Do:**
  1. One helper (e.g. `public_name(sess, user)`) returns the display name, or "Traveller" when it is blank or equals the account's sign-in address (case-insensitive: `UserInfo.email`, or the linked `LocalUser.username`). Use it at every site listed in Current state.
  4. In `login`, the `UserInfo` auto-create sets `email=user.username` and leaves `display_name` blank (Decision 5, R1-1).
  2. `UpdateProfileRequest.display_name` gets a non-blank validator, so blank or whitespace-only gives 422.
  3. In the app, the Settings name field refuses to save blank with an inline message, and shows the server's 422 detail if it comes back.
- **Acceptance:**
  - Tests:
    - the members list, invite preview and shared-trip list for a member and an owner with a blank name contain no `@` and show "Traveller";
    - `PUT /api/auth/me` with `""` and `"   "` gives 422 and leaves the stored name unchanged;
    - a member whose display name equals their sign-in address (both an existing row like that, and one created by the `login` auto-create path) is listed as "Traveller", never the address;
    - a widget test: Save with a blank name shows the message and sends no request.
  - The server tests fail on the current code.
  - `pytest tests/test_project_members.py` passes, as does `flutter test test/settings/` in the Flutter container.
- **Out of scope:** backfilling existing blank names; changing Google's email-local-part default; pending-invite `email` (the owner typed it).
- **Latitude:** local design.
- **Escalate if:** another response shows `UserInfo.email` to a user other than its owner (report it, don't fix it); a file outside Scope is needed.
- **Depends on:** —

#### U4 — Avatars live in the trip owner's folder (#470)
- **Goal:** people avatars are stored, served and deleted under the trip owner's folder, and a refused upload writes nothing.
- **Scope:** `api/people.py`, `tests/test_people_api.py` (or a new `tests/test_people_avatar_owner.py`).
- **Context:** follow `api/memories.py` `_owner_dir_id` (`:150`) and `upload_photo` (`:631-660`): permission first, owner's folder and quota, the `_MAX_PHOTO_UPLOAD_BYTES` limit, the thumbnail in the thread pool. Paths go through `src/utils/photo_paths` (`photo_folder`/`photo_file`/`photo_files`).
- **Do:**
  1. Add `_owner_dir_id` for a `DBPerson`: the owner of `row.project_id`.
  2. In `upload_avatar`, call `_get_owned_person(..., min_role="editor")` and `ensure_storage_quota(owner)` before any file is written.
  3. Enforce the size limit, write under the owner's folder, and charge the owner via `record_written`.
  4. `serve_avatar`, `serve_avatar_thumb`, `delete_avatar` and `delete_person` resolve the owner's folder.
  5. Drop `_avatar_dir`'s `mkdir` on read paths: nothing creates a folder on serve or delete.
- **Acceptance:**
  - Tests:
    - a viewer companion gets the owner's avatar (full and thumb), and an editor companion's upload lands in the owner's folder and counts against the owner's usage;
    - an outsider's upload gets 404 or 403 and writes no file and no usage;
    - an oversized upload gets 413 and writes nothing;
    - deleting a person removes the owner-folder files.
  - The companion and refused-upload tests fail on the current code.
  - `pytest tests/test_people_*.py` passes.
- **Out of scope:** moving existing avatars (U5); EXIF orientation of avatars (#511 covers memory thumbnails); the Flutter client (same URLs).
- **Latitude:** local design.
- **Escalate if:** a person row can exist without a project; a file outside Scope is needed.
- **Depends on:** —

#### U6 — Orphan Strava activity rows are deleted (#509)
- **Goal:** a Strava activity row referenced by no trip is deleted on disconnect and when it leaves its last trip, never while anything still references it.
- **Scope:**
  - `api/strava.py` (`strava_disconnect` only)
  - `api/project_items.py` (the removal path)
  - `api/projects.py` (the trip-delete endpoint only)
  - `src/project/repo_activities.py` or `src/project/repo_core.py`: one new repo method, e.g. `delete_unreferenced_strava_activities(sess, user_info_id, ids=None)`
  - `tests/test_strava_disconnect.py`
  - a new `tests/test_orphan_strava_activities.py`
- **Context:** `api/project_items.py:62-82` (the negative-id cleanup is the example to extend: `activity_rewritable_by_trip`, `delete_local_activity`). For the write lock taken first, follow `api/strava.py` `_claim_token_row`. Read memory "Project write lock".
- **Do:**
  1. Add one repo method. Inside a single write transaction it takes the write lock first. It then deletes `DBActivity` rows (and their `DBActivityGeoPrepared` rows) that meet all of these:
     - owned by the user, with a positive id and `source` empty or `strava`;
     - referenced by no `DBProjectItem.activity_id` in any project;
     - named by no other activity's `split_root_id` or `split_parent_id`.
     
     If `ids` is given, it considers only those ids, whoever owns the row: the owner filter applies only when `ids` is not given (R1-3). It also takes `tail_ids=None`, so split tails and their roots are decided in the same transaction (R2-1).
  2. `strava_disconnect` calls it for the user, in the same session as the token and cache deletes.
  3. The item-removal path calls it with the removed activity's id, after the project save, when the id is positive. When the removed item was a split tail (negative id) and its row was deleted, it then calls it with the tail's `split_root_id` when that is positive (R1-6).
  4. Before `delete_project`, the trip-delete endpoint collects:
     - the trip's positive activity ids;
     - its negative (tail) ids whose row has a positive, Strava-origin `split_root_id`, whoever owns the row: the same ownership rule as the roots (R1-3), so a former member's split family is freed too. Since a tail belongs to one trip (Review envelope), the deleted trip's tails are its own to delete (R3-4).
     
     After `delete_project`, in the repo method's single write transaction with the write lock taken first:
     - delete those tail rows (and their `DBActivityGeoPrepared` rows) that no `DBProjectItem` references, treating them as one set, so a tail named only by another tail in the same set as its `split_parent_id` still goes;
     - for each distinct root of the deleted tails that survives, call `_renumber_split_family(sess, root_id)` (as `delete_local_activity` does), so the remaining pieces are renumbered and a lone root gets its base name back (R3-2);
     - then apply the Strava-origin rule to the positive ids and the tails' roots;
     - when a collected root is kept only because tails name it and none of those tails is referenced by any `DBProjectItem`, log a WARNING with the root id and those tail ids (R3-3 guard).
     
     (R1-4, R2-1)
  5. Bust the geo and stats caches the same way the negative-id path does, where needed.
- **Acceptance:**
  - Tests:
    - disconnect deletes an unreferenced Strava row and its prepared geometry, and keeps one still in a trip, a GPX row and a split root whose tail is in a trip;
    - removing an activity from its last trip deletes it, and removing it from one of two trips keeps it;
    - an owner removing a companion-imported Strava activity from a shared trip deletes it when no other trip references it;
    - deleting a trip deletes its now-unreferenced Strava rows and keeps those another trip still references;
    - deleting a trip that holds a split Strava activity (head, a tail and a tail of a tail) deletes the root, both tails and their prepared geometry;
    - deleting a trip whose split activity's root is also held by another trip deletes the tails, keeps the root, and the root is renumbered (a lone root shows its base name, not "(1/3)");
    - deleting a trip holding a split family imported and split by a companion who has since left deletes that family;
    - a root kept only by unreferenced tails logs the R3-3 warning;
    - removing the head piece of a split Strava activity, then its last tail, deletes the root;
    - a concurrent add of the same activity to another trip, racing the removal, ends with the row present and referenced (file-backed SQLite, real threads);
    - a companion's Strava activity in the owner's trip survives the companion's disconnect.
  - The deletion tests fail on the current code.
  - `pytest tests/test_strava_disconnect.py tests/test_orphan_strava_activities.py tests/test_project_items*.py` passes.
- **Out of scope:** pruning rows that are already orphaned in existing data (only new removals and disconnects); GPX or local rows.
- **Latitude:** local design.
- **Escalate if:**
  - a project item can reference an activity by anything other than `activity_id`, such as segments or prepared caches keyed elsewhere;
  - the add-activity path doesn't recreate a deleted row from Strava;
  - a file outside Scope is needed.
- **Depends on:** —

### Wave 2

#### U5 — Move companion-uploaded avatars to the owner's folder (#470)
- **Goal:** avatars uploaded before U4 into a companion's folder are moved once into the trip owner's folder, with their storage usage.
- **Scope:** a new `src/people/avatar_move.py` (or `src/maintenance/`), `api/router.py` (one call in `lifespan`), a new `tests/test_avatar_move_sweep.py`.
- **Context:** the lifespan sweeps in `api/router.py:108-175` (`sweep_orphaned_jobs` is the example of a startup sweep), `src/billing/usage.py` (`record_delta`, `record_written`, `unlink_and_record`), `src/utils/photo_paths`. U4's `_owner_dir_id` for people.
- **Do:**
  1. For each `DBPerson` with `avatar_photo` whose full-size file is missing from the owner's folder, look in each current member's `people/<id>/` folder.
  2. If found, copy the full-size file and the thumbnail into the owner's folder (temp file then `os.replace`), verify, then delete the member's copies. Move the usage with `record_delta` (−bytes for the member, +bytes for the owner).
  3. Idempotent: a person whose owner-folder file exists is skipped, and a copy that exists in both places is finished, not duplicated.
  4. Log one INFO line per move (person id, from user id, to user id) and a summary. Log a WARNING per avatar found nowhere.
  5. Wrap the sweep so a failure logs and never stops the API from starting.
- **Acceptance:**
  - Tests:
    - a member-folder avatar is moved with both files and the usage moves;
    - running twice changes nothing the second time;
    - an interrupted move (owner copy written, member copy still present) completes on the next run without double-counting;
    - an avatar found nowhere logs a warning and the sweep continues;
    - a sweep exception doesn't break startup.
  - `pytest tests/test_avatar_move_sweep.py tests/test_people_*.py` passes.
- **Out of scope:** avatars of people whose trip no longer exists; any client change.
- **Latitude:** local design.
- **Escalate if:** an avatar is found in two members' folders with different bytes; a file outside Scope is needed.
- **Depends on:** U4

## Definition of done

- No log line contains any part of a posted Google id_token (U1 tests).
- Enrichment and the Strava routes save token rotations the same way; a refresh during enrichment leaves a working token in the DB (U2 tests).
- Disconnecting Strava, removing an activity from its last trip, or deleting a trip, leaves no unreferenced Strava activity row and keeps every referenced row and split root (U6 tests).
- No response or email shows another user's email address as their name; a blank display name can't be saved (U3 tests).
- A companion sees and manages avatars in the owner's folder; a refused upload writes nothing; existing companion-uploaded avatars are moved at the next start (U4, U5 tests).
- The full Python suite and the Flutter suite pass in CI; the plan's adversarial review ledger is committed.
- After the next release is deployed: #508 closed with a comment that `tokens.json` is gone with the recreated container and the code no longer writes it (PR #550).
