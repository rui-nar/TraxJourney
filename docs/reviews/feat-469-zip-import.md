# Review ledger — feat/469-zip-import

Subject: plan docs/TRIP_ZIP_IMPORT_PLAN.md (issues #469, #484), then its delivery
Envelope: the plan's `## Review envelope` plus REVIEW.md defaults

## Round 1 — 2026-09-28, plan draft reviewed on origin/main 27a2d645

Envelope question raised: may two ZIP imports run at once within 768 MB, or is a concurrency guard needed? Owner answer (2026-09-28): add a guard. One import (.traxj or ZIP) runs at a time; a second gets 503 (plan Decision 12).

### R1-1 — Replace with the trip's own ZIP overwrites, re-counts and, on a failed commit, deletes photos the kept memory already has
- Trigger: The owner restores trip X from its own ZIP with Replace → the staged photos are moved over the kept memory's existing files and counted again with record_written (a wrong 402 later) → if the commit fails, "remove the placed files" deletes the user's existing photos.
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, floor F2)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R1-2 — Staged photos are moved inside the ingest that owns commit and retry
- Trigger: Two requests import the same ZIP → the loser's commit hits an IntegrityError and ingest retries → the staged files were already moved by attempt 1 → the trip lands with no photos and orphaned files; a commit that raises also loses the placed list, so nothing is cleaned up.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D3, floor F3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R1-3 — "The names that now exist in the folder" can be read as a directory listing, which re-stores names the same Replace deletes
- Trigger: The U2 implementer builds photos_json from the folder listing → Replace with fewer photos writes the dropped names back, then deletes their files after commit → dangling names again.
- Scores: trigger=plausible (triager corrected from concrete), impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, floor F3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R1-4 — The entry-count limit runs after zipfile has loaded the whole central directory
- Trigger: Any user uploads a ZIP declaring millions of entries (~100 MB) → ZipFile() builds every ZipInfo before the count check → the API is OOM-killed and every user's requests fail until restart.
- Scores: trigger=concrete, impact=wrong-visible (triager corrected from security: F1 covers data access, auth bypass, injection and secrets; an outage is not one of them), detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R1-5 — A 100-megapixel limit plus a full decode doesn't meet the envelope's memory bound
- Trigger: A ZIP holds a 100 MP JPEG → a full RGB decode is ~300 MB, on top of a trip file parse of up to ~285 MB, or alongside a second import.
- Scores: trigger=plausible (triager corrected from concrete), impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: The pixel limit is set at U3 routing; the owner answers the concurrency envelope question; or an OOM kill is logged during a ZIP import or photo upload.
- Guard: —
- Override: —
- Outcome: open

### R1-6 — No unit implements "photos already on disk for a kept memory are not counted again"
- Trigger: A user near their storage limit restores their own trip with Replace → the check counts every staged byte → a wrong 402 for an import that stores nothing new.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R1-7 — A test fixture depends on the dangling name being stored, and its file is outside U2's Scope
- Trigger: The U2 implementer runs the suite → the alps fixture in test_import_replace.py imports photo a1 before its file exists, so the replace test fails → an X3 stop, or a bent assertion.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R1-8 — The client reads the whole ZIP into memory on every platform
- Trigger: A user on Android picks a ZIP of a few hundred MB → it is read wholly into memory → the app may be killed.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: Device testing or a user report shows a mobile ZIP import failing on a large archive; or a streamed upload path is designed anyway (e.g. when U5 escalates the web limit).
- Guard: —
- Override: —
- Outcome: open

### R1-9 — The 409 name conflict is checked only after the whole archive has been staged
- Trigger: A user imports a ZIP under a taken name → every photo is decoded and staged, then 409 → the choice re-uploads and re-stages everything.
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R1-10 — The staging directory's location is unspecified, so the move to the data volume becomes a copy
- Trigger: An import on the VPS stages in the container's temp dir → the move into the /app/data bind mount is a copy plus unlink → files are written twice, and placement is not atomic.
- Scores: trigger=concrete, impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

## Round 2 — 2026-09-28, plan reviewed at 523130c4 (round-1 revisions only)

### R2-1 — "Keep both" of the trip's own ZIP subtracts already_present from the quota although every photo is placed
- Trigger: A user at their storage limit imports their own trip's ZIP with Keep both → every uuid is subtracted, so the quota check passes with ~0 incoming → the copy writes and counts every photo → over quota with no 402, and it can be repeated.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, floor F3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R2-2 — The guard is taken in the endpoint, after the whole upload has been received
- Trigger: A user starts a 600 MB ZIP import while another runs → the whole body is spooled before the endpoint runs → only then a 503 → the upload was wasted, and two archives spooled at once.
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R2-3 — StagedPhotos is defined by U2 and returned by U3 in the same wave with no shared file
- Trigger: U2 and U3 each define their own type in parallel worktrees → U4 can't join them within its Scope → an X3 stop or a shim.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R2-4 — A crash between commit and placement leaves rows naming photos whose only copies the stale-staging sweep later deletes
- Trigger: The API is killed after the ingest commit, before placement ends → broken images → a day later the sweep deletes the staging directory silently.
- Scores: trigger=plausible, impact=wrong-visible, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9). Not F2: the files were never placed, the uploader keeps the ZIP, and a Replace re-import restores them.
- Revisit when: —
- Guard: The sweep logs at ERROR each directory it removes that still holds files, with its age, its file count and (from a manifest written by the stager) the importer and trip.
- Override: —
- Outcome: guard written into the plan (Decision 8.5, U3 manifest, U4 Do 9)

### R2-5 — Between commit and the end of placement, viewers of a replaced shared trip see photo names without files
- Trigger: The owner replaces a shared trip from a large ZIP → a companion loading it during placement gets 404 thumbnails until reload.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: A user reports broken thumbnails on a shared trip right after a ZIP Replace; placement of a large archive is measured at more than a few seconds; or the implementation calls record_written per photo or renames on the event loop (check both at delivery review).
- Guard: —
- Override: —
- Outcome: open

### R2-6 — The rename-failure follow-up rewrites photos_json without the per-memory photo lock (flagged: floor F3, theoretical)
- Trigger: theoretical — a same-volume rename fails while a companion uploads to the same kept memory → the follow-up drops the companion's new name.
- Scores: trigger=theoretical, impact=silent-wrong, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D4), flagged to the owner: re-reading under photo_lock(kind, row id) and removing only that uuid costs about the same as the guard.
- Revisit when: —
- Guard: The follow-up's ERROR log records the kind, the row id, the removed uuid, and the photos_json it read and the one it wrote.
- Override: Owner upgraded it to the lock-based fix (2026-09-28): "put the lock-based fix in the plan".
- Outcome: fixed (plan revised: Decision 8.3, U4 Do 8)

## Round 3 — 2026-09-28, plan revisions 523130c4..754a8eff reviewed at 754a8eff

Envelope question raised: FastAPI reads the multipart body before dependencies, so an unauthenticated request can spool up to the full cap (1 GB on /import-zip, 50 MB on /import today) before its 401. Is that inside the envelope, or should the capped wrapper check the bearer token before reading any body? Owner answer (2026-09-28): authenticate in the wrapper before any body read (plan Decision 13).

### R3-1 — An unauthenticated upload holds the import guard for its whole spool
- Trigger: Anyone without an account trickles a body to /import-zip → the wrapper takes the guard, the body spools before the 401 → meanwhile every user's import gets 503; repeatable without credentials.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: The owner answers the round-3 envelope question with a token check in the wrapper (fold it in then); or logs show 401s on /import-zip for large or slow bodies; or a user reports the 503 with no import running.
- Guard: —
- Override: —
- Outcome: fixed (plan revised: Decision 13, the owner's envelope answer)

### R3-2 — The placement kind "memories" is not photo_lock's "memory" (flagged: floor F3, theoretical)
- Trigger: theoretical — the follow-up locks ("memories", id) while uploads lock ("memory", id) → the race R2-6 fixes remains, and its test still passes.
- Scores: trigger=theoretical (triager corrected from plausible: it needs R2-6's disk error first), impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D4), flagged to the owner. The one-line mapping is about the same cost.
- Revisit when: —
- Guard: A U4 test holds photo_lock("memory", id), and another photo_lock("journal", id), while the follow-up runs, and asserts the follow-up blocks until the lock is released; the ERROR log records the lock key.
- Override: Owner kept the guard (2026-09-28)
- Outcome: guard written into the plan

### R3-3 — U3 writes the manifest first yet must leave nothing in staging_dir on failure; the signature lacks importer and name
- Trigger: The U3 implementer follows both lines → every fault test fails on the manifest → a bent assertion, or no manifest; U4 inherits whatever signature U3 picks.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised)

### R3-4 — A thumbnail rename that fails after its full file moved leaves a counted orphan (flagged: floor F3, theoretical)
- Trigger: theoretical — a same-volume rename fails between a pair's two moves → the full file stays, unnamed and counted.
- Scores: trigger=theoretical, impact=silent-wrong, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D4), flagged to the owner. The fix (unlink the placed half and count neither) is about the same size.
- Revisit when: —
- Guard: place_photos logs at ERROR, for a failed rename, which half was already placed, with its path and bytes; a U2 test simulates the thumbnail failing after the full file moved.
- Override: Owner kept the guard (2026-09-28)
- Outcome: guard written into the plan

## Round 4 — 2026-09-28, plan revisions 754a8eff..0db85cf6 reviewed at 0db85cf6 (owner extended the cap by one round)

### R4-1 — The wrapper's own token check bypasses dependency_overrides[get_current_user], which every existing /import test uses
- Trigger: The U4 implementer adds Decision 13's check as written → the 16 test files posting to /import authenticate only through dependency_overrides and get 401 → an X3 stop, or bent tests.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan revised: Decision 13, U4 Do 3 and acceptance). Owner: no further plan review rounds (2026-09-28).
- Fix note (triager): in the wrapper, solve only get_current_user's Dependant through FastAPI's dependency machinery (never the endpoint's, whose File/Form parameters read the body). That honours overrides, gives HTTPBearer's real 401, and follows any future change to get_current_user. solve_dependencies is not public API, but it fails closed and the existing import tests catch a break on upgrade. Rejected: reading dependency_overrides directly, which is a test seam in auth code and a hand-copy of the check that can drift. Add an acceptance test: an override-authenticated test passes the wrapper, and a request with no header gets the same response get_current_user gives.


## Unit review U2 — 2026-10-03, reviewed 09c7867b against 1c8b21f6 (DELIVERY §5 point 3, run after integration by mistake)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 2 findings, both well-formed. No envelope questions.

### U2R1-1 — On Replace, a deleted row's removal and a new row's placement can name the same folder and uuid (SQLite reuses ids)
- Trigger: the owner replaces trip X from the ZIP of a Keep-both copy of X while X's memories hold the highest ids → the new rows reuse id N with the same uuids → if place_photos ran before _remove_photos, the placed photo would be deleted and the row would name a missing file, silently.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3). U4's code already removes before it places (project_transfer.py:565, then 566), but no test pins the order.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (FU4, 2beab092)

### U2R1-2 — replace_project without data_dir would drop every on-disk photo name from kept rows (flagged: floor F2, theoretical)
- Trigger: theoretical — a future caller omits data_dir → kept memories are stored with [] while their files stay on disk and counted. Both current callers pass it.
- Scores: trigger=theoretical, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D4), flagged to the owner.
- Revisit when: —
- Guard: (to add) replace_project raises when data_dir is None; one test.
- Override: owner kept the guard (2026-10-03)
- Outcome: guard added (FU4, 2b054158)
## Unit review U4 — 2026-10-03, reviewed u469/u4 (f114f077, c9b0464e) against 8405cc19 (DELIVERY §5 point 3)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 2 findings, both well-formed.

Envelope question (to the owner): any authenticated account can hold the one-import guard for hours by trickling a 1 GB body (no body-read timeout in uvicorn or Caddy), so everyone else's imports get 503. Is a per-upload deadline in the wrapper inside the envelope?
Answer (owner, 2026-10-03): approved with the recommendation, inside the envelope. Idle 60 s / total 30 min deadline, 408, guard released (in fix unit FU4).

### U4R1-1 — A Replace that stores nothing is refused with 402 once the account is already over its storage limit
- Trigger: a user whose subscription lapsed (over the free limit) restores a trip from its own ZIP with Replace → staged_total is 0, `used + 0 > limit` → 402 for an import that writes nothing. A photo-less ZIP under a new name gets the same 402.
- Scores: trigger=concrete (triager corrected from plausible), impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6). Skip the storage check in _ingest_zip when staged_total is 0; add a test with usage above the limit.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (FU4, 2beab092)

### U4R1-2 — An exception in the post-commit photo removal deletes the staging directory before placement
- Trigger: theoretical — the OS refuses to unlink a dropped file in the API's own users/<id>/ folder during a ZIP Replace → place_photos never runs, staging is removed → 500; a second Replace re-places the photos (already_present checks the disk).
- Scores: trigger=theoretical (triager corrected from plausible), impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11). The same class as R2-6 and R3-4. Cheap close-out if wanted: catch and log at ERROR in _remove_photos so that placement still runs.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open
## Integrated review — round 1, 2026-10-03, feat/469-zip-import at 9a98c76a against main a44519c8 (docs excluded)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 2 findings, both well-formed.

Envelope question (to the owner): an export unzipped and re-zipped with Finder or Explorer has one top-level folder (plus __MACOSX/), and Decision 6 refuses it with 400. Should the reader accept one common root prefix?
Answer (owner, 2026-10-03): yes (option a); accepted in fix unit FI1. Table approved as is.

### IR1-1 — Crafted or oddly-encoded archives fail inside zipfile with uncaught exceptions → 500 instead of 400
- Trigger: an authenticated user uploads a ZIP whose entry has the UTF-8 flag set but non-UTF-8 name bytes (UnicodeDecodeError from ZipFile()), or a corrupt bzip2 entry (OSError) or LZMA entry (LZMAError) → 500. Nothing leaks.
- Scores: trigger=concrete (triager corrected from plausible), impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6). Catch ValueError around ZipFile(); add OSError and lzma.LZMAError to _ENTRY_ERRORS, around the zipfile calls only; one test per exception type.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### IR1-2 — Native clients see a socket error instead of the server's early 503/401/408/413
- Trigger: an Android user starts a large ZIP import while another holds the guard → the server answers 503 before reading the body, Caddy closes after more than 256 KiB unread, and dart:io only reads the response after the body is sent → "Connection closed" instead of "Another trip import is in progress".
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local (triager corrected from M), confidence=inferred
- Decision: Defer (D10). Cheap client mitigation if wanted: map a mid-upload socket error to a readable "upload interrupted — another import may be running or your session expired; try again".
- Revisit when: a report or device test shows a native ZIP import ending in a socket error, or the logs show early 503/401/408/413 on /import-zip followed by a client failure, or the native upload path is reworked.
- Guard: —
- Override: —
- Outcome: open
## Integrated review — round 2, 2026-10-03, FI1 (800fcaf9, cbff9234) against 698afde9

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 2 findings, both well-formed.

Envelope question (to the owner): the export only writes stored and deflated entries, but the reader inflates every method the archive declares, so each stdlib decompressor's errors must be caught by hand (IR1-1, IR2-1). Should the reader accept only stored and deflated entries and refuse any other method with a 400 naming it?
Answer (owner, 2026-10-03): yes — only stored and deflated entries; any other method is refused with a 400 naming it, before any entry is opened.

### IR2-1 — A corrupt Zstandard entry (Python 3.14 zipfile) still escapes as a 500
- Trigger: an authenticated user uploads a ZIP with a garbled ZIP_ZSTANDARD entry → compression.zstd.ZstdError is not caught → 500.
- Scores: trigger=concrete, impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D6). Moot if the owner accepts only stored and deflated entries (check compress_type before any open).
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### IR2-2 — The `._*` litter rule drops the app's own export for a trip named "._…"
- Trigger: a user names a trip `._Alps`, exports it, and re-imports the untouched ZIP → `._Alps.traxj` is treated as OS litter → 400 "no trip file". A regression from round 1.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10), overridden. Fix: `._X` counts as litter only when its sibling `X` exists, plus all of `__MACOSX/`.
- Revisit when: a user reports a 400 "no trip file" on their own export, or the litter or root detection is touched again.
- Guard: —
- Override: user: Fix now — a one-line regression in a file already being edited
- Outcome: open

## Integrated review — round 3, 2026-10-03, FI2 (68a59787, 51949a2e) against cbff9234

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 1 finding, well-formed. IR2-1 and IR2-2 are confirmed closed. No envelope questions.

### IR3-1 — A crafted central-directory offset makes zf.open() seek a disk-spooled upload to a negative position → uncaught OSError → 500
- Trigger: an authenticated user uploads a ZIP over 1 MB whose end record's central-directory offset is raised → entry header_offset goes negative → seek on Starlette's spooled temp file raises OSError EINVAL, which FI2 stopped catching → 500. A regression from FI2. A ZIP64 header_offset ≥ 2**63 gives an OverflowError, which predates FI2.
- Scores: trigger=concrete, impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D6). Before any open, refuse an entry whose header_offset is negative or leaves under 30 bytes before the end of the upload; keep OSError uncaught; test both cases with an upload over 1 MB.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open