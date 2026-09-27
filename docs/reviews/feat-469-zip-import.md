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
