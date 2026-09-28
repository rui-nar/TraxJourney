# Review ledger — fix/430-share-photo-exif

Subject: branch fix/430-share-photo-exif (issue #430)
Envelope: REVIEW.md defaults. Owner decisions: originals keep their GPS; share links get a stripped copy; the app's social share sends the stripped copy; companions and the ZIP export keep originals (trusted).

## Round 1 — 2026-09-24, reviewed at 9e20a35 (before docs/REVIEW.md; findings recorded after the fact)

### R1-1 — Share thumbnails keep the source JPEG comment (COM) segment
- Trigger: An owner uploads a photo whose JPEG has a comment → anonymous share viewers get the comment in the thumbnail.
- Scores: trigger=concrete, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F1)
- Override: —
- Outcome: fixed

### R1-2 — The app's social share sends the owner's original, GPS included
- Trigger: An owner shares a memory to WhatsApp or mail → the recipient gets the photo with its GPS EXIF.
- Scores: trigger=concrete, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now
- Override: user: send the stripped copy
- Outcome: fixed (the side effect is R2-1)

### R1-3 — Companions and the ZIP export get originals
- Trigger: A viewer-role companion downloads a photo → gets the original with GPS.
- Scores: trigger=concrete, impact=security, detect=silent, later=cheap, fix=M/local, confidence=verified
- Decision: Reject
- Override: user: companions are trusted and the ZIP export is the owner's own backup (2026-09-24)
- Outcome: —

### R1-4 — A photo deleted during a first serve leaves an orphan share copy and blocks the memory directory's removal
- Trigger: A share viewer opens a photo for the first time while the owner deletes it → the stripped copy lands after the delete and stays on disk.
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F2)
- Override: —
- Outcome: fixed

### R1-5 — Leaked temp files are charged to the user's quota
- Trigger: The server crashes mid-write of a share copy → the temp file is counted by reconcile_usage.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3)
- Override: —
- Outcome: fixed

### R1-6 — An undecodable original gives a 500 on every share request
- Trigger: A share viewer opens a photo whose file is corrupt → 500 on every request.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (pre-policy)
- Override: —
- Outcome: fixed

### R1-7 — delete_memory creates the directory it deletes
- Trigger: An owner deletes a memory with no photos → an empty memories/ directory is created.
- Scores: trigger=concrete, impact=cosmetic, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed

## Rebase note — 2026-09-28

Rebased onto origin/main 3dec5e35, which added src/utils/photo_paths (stored
photo names are validated and kept inside the owner's folder). The share routes
and _delete_photo_files now build paths through photo_folder/photo_file, and a
share copy is removed only for names photo_file accepts.

## Round 2 — 2026-09-24 / 2026-09-28, reviewed at 351e4a4 (fixes since 9e20a35), plus its rebase onto 3dec5e35 (7af0c26c)

### R2-1 — Attaching photos to a social share silently creates a share link when "Link to memory" is off
- Trigger: An owner turns "Link to memory" off and shares a memory with photos → fetchPhotos calls createShareToken(), so the trip gets a live memory-bearing share link the owner did not ask for.
- Scores: trigger=concrete, impact=security, detect=silent, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D3, F1)
- Override: user (how): serve the stripped copy through an authenticated route, so no share token is needed
- Outcome: fixed (authenticated /shareable route; the app fetches it signed in, no share token involved)

### R2-2 — The upload-time thumbnail comment fix has no test that fails without it
- Trigger: A later change drops img.info.clear() → on-disk thumbnails carry comments again and every test still passes.
- Scores: trigger=theoretical (triager corrected from plausible), impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11). Share viewers stay covered by the tested route-level strip.
- Override: —
- Outcome: —

### R2-3 — strip_jpeg_metadata_segments passes non-canonical JPEGs through unchanged (fail-open)
- Trigger: A thumbnail with a fill byte, a stray RST, a COM between progressive scans or a post-EOI trailer is served → its comment survives.
- Scores: trigger=theoretical, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D4), flagged to the owner. Every thumbnail served is written by Pillow as baseline.
- Guard: log a warning when the marker walk stops before SOS
- Override: user: approved as a warning log (2026-09-28)
- Outcome: guard added (warning when the marker walk stops before SOS; tested)

### R2-4 — Thumbnail route TOCTOU returns 500 instead of 404
- Trigger: The owner deletes a photo between exists() and read_bytes() of a share-viewer request → the viewer gets a 500.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: A 500 from shared_photo_thumb appears in logs or a user report, or the thumbnail route is next touched.
- Override: —
- Outcome: open

### R2-5 — share_asset_source_impl creates an http.Client per call and never closes it
- Trigger: An owner shares several times on Android or iOS → each share leaves a socket open until GC.
- Scores: trigger=concrete, impact=maintainability (triager corrected from degraded-ux), detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7), folded into the R2-1 rewrite
- Override: —
- Outcome: fixed (fetch goes through the app's ApiClient; no client is created)

### R2-10 — Import-replace deletes memory photos but not their share copies
- Trigger: An owner whose shared trip was viewed re-imports a ZIP with on_conflict=replace → {uuid}_share.jpg and temp copies stay, the folder's rmdir fails silently, and the copies are never counted or reported.
- Scores: trigger=concrete, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F2)
- Override: —
- Outcome: fixed (_remove_photos removes each accepted original's share copy and temp copies)
