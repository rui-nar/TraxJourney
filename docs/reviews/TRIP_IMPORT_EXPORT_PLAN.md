# Review ledger — Trip import and export plan (#492, #367, #365, #368, #408)

Subject: docs/TRIP_IMPORT_EXPORT_PLAN.md
Envelope: the plan's `## Review envelope` plus REVIEW.md §2 defaults

## Round 1 — 2026-10-03, reviewed at a44519c8 (plan uncommitted in the working tree)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 6 findings, all well-formed.

Envelope question (to the owner): does Decision 5's accepted consequence cover R1-4 (a day shift on GPX export/re-import of pre-release typed rows, and the relabel erasing the only selector for them)?
Answer (owner, 2026-10-03): no. Table approved as is. R1-4 is fixed by option (a): Decision 5 is reversed (no relabel, U6 removed), and the export leaves out times for GPX rows labelled "UTC".

### R1-1 — Installed clients will store GPX activities shifted by the zone offset once typed times are read as local
- Trigger: a user on an installed build imports a stamped Tokyo GPX after the deploy and corrects only the end time → the client sends all three times as the UTC wall clock it showed; the new import-gpx reads them as Tokyo time → start_date 9 h early, on the previous day, no error.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=expensive, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-2 — No unit changes the client, so the review step keeps showing UTC and hits the same shift
- Trigger: a user on the new client imports a Tokyo track → gpx_import_model.dart's `.toUtc()` prefills 00:00, not 09:00; touching a time stores it 9 h off.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent (triager corrected from user-visible), later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-3 — A GPX round trip zeroes moving time and average speed (only first and last points timed)
- Trigger: a user exports a trip and imports it back → every activity over 5 minutes gets moving_time 0 and average_speed 0 (importer.py:179-191 skips the single >300 s interval); the row shows "12.3 km • 0:00".
- Scores: trigger=concrete, impact=wrong-visible (triager corrected from silent-wrong), detect=user-visible, later=expensive, fix=M/local, confidence=verified
- Decision: Fix now (D5)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-4 — Pre-release typed GPX rows shift by the zone offset on a GPX round trip; the relabel erases their only marker
- Trigger: a user who typed 20:00 for a Japan ride before this release (stored 20:00Z, timezone UTC) exports after U3 and re-imports after U5 → 20:00Z resolved in Asia/Tokyo → 05:00 the next day, silently. After U6, `source='gpx' AND timezone='UTC'` no longer selects these rows.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=expensive, fix=M/local, confidence=verified
- Decision: Fix now (D3) — becomes D1 if the owner rules pre-release typed rows outside the envelope
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-5 — Activity types outside run/ride/hike/walk come back as "Workout" on the round trip
- Trigger: a user exports a trip with a Kayaking/Swim/AlpineSki activity and uses Import all → created as "Workout" (importer.py `_TYPE_ALIASES` covers 4 types; activities.py:807 fallback); the mapping table is outside U3's and U5's scope.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-6 — U6 forbids importing src/ while naming a precedent migration that imports src/
- Trigger: the U6 implementer needs `is_encrypted_envelope` to skip encrypted coordinates → the plan's own escalation fires, or the check is reimplemented ad hoc.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

## Round 2 — 2026-10-03, reviewed at a44519c8 (plan uncommitted; round-1 fixes only)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 4 findings, all well-formed.

Envelope question (to the owner): after the R1-4 fix, every GPX export of a trip with a pre-release GPX row has an untimed track, and U5 refuses the whole file with 400. Should Import all import the timed tracks and list the untimed ones as skipped instead?
Answer (owner, 2026-10-03): no, keep the refusal (option b). Table approved as is.

### R2-1 — Installed client importing an untimed route has its typed times shifted by the zone offset
- Trigger: a user on an installed build imports a Komoot route (no `<time>`) for Japan and types 20:00–22:00 → sent without `times_local`, read as UTC, start_date_local derived in Asia/Tokyo → 05:00 the next day; before this release it stayed 20:00 on the typed day.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=expensive, fix=M/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R2-2 — New client's typed times resolve a DST-fold start to the wrong occurrence
- Trigger: a user on the new client imports a Lisbon track stamped in the repeated October hour and edits only the end time → start 01:30 sent with `times_local=true` → fold=0 picks the first occurrence; a second-occurrence start is stored an hour early, silently, and later exported.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=expensive, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R2-3 — "Average speed round-trips exactly" can't hold for Strava activities (distance comes from the simplified polyline)
- Trigger: a user exports a trip with a Strava ride (100 km) and imports it back → distance is the length of the simplified summary_polyline, and average speed drops with it.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=expensive (triager corrected from cheap: the field must be in the extension from the first release), fix=S/local, confidence=verified
- Decision: Fix now (D5)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R2-4 — U3's acceptance asserts local dates, but U3 has no zone helper in wave 1
- Trigger: the U3 implementer writes the test → src/gpx/timezone.py isn't on its branch → imports a missing module, re-implements the lookup, or drops the assertion.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
## Round 3 — 2026-10-03, reviewed at a44519c8 (plan uncommitted; round-2 fixes only)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 3 findings, all well-formed. No envelope questions.
Owner (2026-10-03): table approved; a fourth round requested on the round-3 fixes (cap extended).

### R3-1 — The R2-1 fix marks a derivable instant as unknown
- Trigger: a user on an installed build imports an untimed Komoot route for Japan and types 20:00–22:00 → stored with timezone "UTC"; its later GPX export has no times, Import all refuses it, and the row can't be told from a pre-release row (activity has no created_at). The installed client sends raw wall-clock picks for untimed tracks, and the server sees `time_span is None`, so the instant can be resolved in the track's zone.
- Scores: trigger=concrete, impact=degraded-ux (triager corrected from wrong-visible), detect=user-visible, later=expensive, fix=S/local, confidence=verified
- Decision: Fix now (D5) — new evidence against R2-1's fix, not a duplicate
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R3-2 — The review step shows recomputed distance and moving time; the import stores the carried values
- Trigger: a user picks an export holding a Strava ride (100 km, 4:30 moving) → the choose step shows "96.8 km · 6:00", and the created activity shows "100.0 km · 4:30".
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R3-3 — Boundaries crossed lists only moving_time in the extension
- Trigger: an implementer reads Boundaries crossed → sees moving_time only, while Decision 3 and U3 carry moving_time and distance.
- Scores: trigger=concrete, impact=maintainability (corrected from cosmetic), detect=silent (corrected), later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
## Round 4 — 2026-10-03, reviewed at a44519c8 (plan uncommitted; round-3 fixes only; cap extended by the owner)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 1 finding, well-formed. No envelope questions. R3-1..R3-3 are confirmed closed.
Owner (2026-10-03): approved; no fifth round. Review closed.

### R4-1 — Re-importing a pre-release untimed route after the release is no longer caught as a duplicate
- Trigger: a user who imported an untimed Komoot route for Japan before the release (typed 20:00) re-imports the same file with the same times after it → the stored fingerprint was based on 20:00+00:00, and the new start_dt is 11:00Z → no 409, a second copy appears; inspect has no duplicate warning for untimed tracks.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=expensive (triager corrected from cheap: source_id is stored), fix=S/local, confidence=verified
- Decision: Fix now (D5)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
## Unit review U1 — 2026-10-04, pkgd/u1 da669833 against 253fba16 (DELIVERY §5 point 3, stored data)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 1 finding, well-formed.

Envelope question (to the owner): the envelope says tzfpy's coordinates are "already range-checked by the GPX parse layer". On the inspect path, zone_at runs on every candidate's first point even when validation failed (the import path is range-checked). tzfpy tolerates bad values (Rust float casts saturate), so no defect is claimed. Correct the envelope sentence, or have inspect skip the zone lookup for candidates with errors?
Answer (owner, 2026-10-04): skip the lookup for candidates with errors (option a). Table approved as is.

### PU1R1-1 — zone_at silently stores Etc/UTC when tzfpy names a zone the image's tz database lacks
- Trigger: a user imports a stamped track in a region whose IANA zone is newer than the image's Debian tzdata but known to tzfpy → ZoneInfoNotFoundError → "Etc/UTC" is stored, and start_date_local equals the UTC instant → shown hours off, nothing logged.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=inferred. Checked 2026-10-04: python:3.14-slim ships /usr/share/zoneinfo, including America/Coyhaique (2025b).
- Decision: Fix now (D3). Add the pip `tzdata` package (zoneinfo falls back to it when the system lacks a name), plus a warning log in zone_at's fallback, plus a test with a patched unknown name.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open