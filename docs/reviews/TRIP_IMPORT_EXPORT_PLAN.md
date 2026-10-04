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
- Outcome: fixed (FU1, 29afd6c5)
## Integrated review — round 1, 2026-10-04, feat/import-export-d at 1816ace2 against main 8b647ae8 (docs excluded)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 3 findings, all well-formed.

Envelope questions (to the owner):
- Q1: importing a TraxJourney GPX export back into the trip it came from duplicates every activity (Strava rows fingerprint on their Strava id; GPX rows on the original file's points), so nothing matches. Is "import your own export into the same trip" inside #367's envelope?
  Answer (owner, 2026-10-04): yes — the export carries each activity's original fingerprint and import treats a match as a duplicate.
- Q2: installed clients (E5) don't read is_connection, so they list connection tracks as importable and can import one as a timeless activity. Should import-gpx refuse is_connection candidates server-side?
  Answer (owner, 2026-10-04): yes — import-gpx refuses a connection track with a readable 400. PIR1-3's revisit condition is met, so it is fixed together with this. Table approved.

### PIR1-1 — The new client's deliberate "Other" pick is overwritten by the file's exact type
- Trigger: a user on the new client changes a Kayaking track's type to "Other" → the server reads Workout as an installed-client echo → stores Kayaking.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6). The new client marks its type as exact (an additive form field); the server stores it verbatim.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (PF1 55a29dfb, PF2 4479d252)

### PIR1-2 — Cold start via Share shows "You have no trip" before the session restore finishes
- Trigger: a signed-in user cold-starts the app by sharing a .gpx → /import-gpx renders while AuthNotifier is still restoring → the trips list hasn't loaded yet → the empty-list message shows until load() completes.
- Scores: trigger=concrete (triager corrected), impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified (corrected)
- Decision: Fix now (D7). The picker shows a loading state until auth is restored and the trips are loaded.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (PF2 4479d252)

### PIR1-3 — A file whose only error-free track is a connection opens review on the connection
- Trigger: a user picks an export whose activity tracks all have errors but a connection track doesn't → the review opens on the arc → it can be imported as an activity.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: the owner rules Q2 a defect (then fix together with a server-side refusal of is_connection), or a user reports an imported arc.
- Guard: —
- Override: revisit condition met (owner Q2 answer); fixed in PF1/PF2
- Outcome: fixed (PF1 55a29dfb, PF2 4479d252)
## Integrated review — round 2 (server fixes PF1), 2026-10-04, pkgd/pf1 (55a29dfb, 32a68c80) against 925317ca

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 1 finding, well-formed. No envelope questions. The PF2 half of round 2 was cut off by a session limit and is being rerun.

### PIR2-1 — Re-importing an export into its own trip still duplicates split tails and rows without a stored fingerprint
- Trigger: a user splits a ride, exports the trip as GPX, and runs Import all back into the same trip → the tail has no identity (fresh negative id, no source_id) → it is imported again and appears twice. The same applies to Strava split tails and to GPX imports made before #462 (source NULL).
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=expensive (export format), fix=S/local, confidence=verified
- Decision: Fix now (D5), approved by the owner 2026-10-04. Rows with neither a fingerprint nor a Strava id export a third identity ("local", id). On read, accept only a negative integer with |id| < 2^53. Match it by DBActivity.id, joined to this trip only, with no source filter. The fingerprint branch stays first. Triager: no realistic wrongful suppression (random 53-bit ids, trip-scoped).
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (PF3 7765fba4, 8a2f8148)
## Integrated review — round 2 (client fixes PF2), 2026-10-04, pkgd/pf2 (4479d252) against 925317ca

Reviewer: adversarial-reviewer (Fable), rerun after a session limit. Decision by the orchestrator under the D table; scores taken as given (D10 needs no floor verification). 1 finding.

### PIR2C-1 — A mixed file (broken activity tracks plus connections) hides the real refusal reasons behind "only connecting segments"
- Trigger: a user picks an export whose activity tracks all have inspect errors while its connection tracks don't → the pick step says "This file has no importable track, only connecting segments", and the activity tracks' refusal reasons are never shown.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10). Fix if wanted: also list the non-connection candidates' errors (the same shape as the existing branch).
- Revisit when: a user reports the misleading message, or gpx_import_dialog's pick-step errors are touched again.
- Guard: —
- Override: user: Fix now (2026-10-04)
- Outcome: fixed (PF4 4d04e2e9)
## Integrated review — round 3, 2026-10-04, PF3 (7765fba4, 8a2f8148) and PF4 (4d04e2e9) against 788ce264

Reviewer: adversarial-reviewer (Fable). No findings, no envelope questions. Review closed: the round came back clean (REVIEW.md §6).

## Delivery

Plan reviewed in 4 rounds (2026-10-03). Delivery started 2026-10-04 after #469/#484 merged (PR #553). Feature branch: feat/import-export-d, with worktrees created by hand (E:/Dev/TraxJourney-pkgD-*).

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Per-activity timezone for GPX imports (#365) | Opus | S4 | 1 (interrupted by a session restart, resumed) | — | yes | PU1R1-1 |
| U2 | Spoken order of an imported activity row (#408) | Sonnet | — | 1 | — | yes | — |
| U3 | GPX export: one track per activity (#367) | Opus | S4 | 1 (interrupted, resumed) | — (flagged the installed-client type gap → plan amended, Decision 3 E5) | yes | — |
| U4 | Open a shared .gpx in the app (#368) | Opus | S2 | 1 (+ scope widening) | X3: MainActivity.kt platform channel instead of a plugin | yes | PIR1-2 |
| U5 | Import every track of a GPX file (#367) | Opus | S4 | 1 | — | yes | PIR1-1, PIR1-3, PIR2-1 |
| U7 | Client: local times + Import all (#365, #367) | Sonnet | — | 1 | — | yes | PIR1-1, PIR1-3, PIR2C-1 |
| U8 | Trip import checks the trip-length quota (#492) | Opus | S5 | 1 | — | yes | — |
| FU1 | U1 unit-review fix (tzdata fallback + warning; skip zone lookup for invalid candidates) | Opus | S4 | 1 | — | yes | PU1R1-1 |
| PF1 | Integrated round-1 server fixes (Q1 identity, Q2 refusal, PIR1-1) | Opus | S4 | 1 | stopped once on an assertion encoding the old extension; option A approved | yes | PIR1-1, PIR1-3 |
| PF2 | Integrated round-1 client fixes (PIR1-1/2/3) | Sonnet | — | 1 | — | yes (the first verifier run was cut off by a session limit and rerun) | PIR1-1, PIR1-2, PIR1-3 |
| PF3 | PIR2-1: "local" identity for split tails and early rows | Opus | S4 | 2 (orchestrator addendum widened the 2^53 bound to the id range) | — | yes | PIR2-1 |
| PF4 | PIR2C-1 (owner override): show activity tracks' refusals | Sonnet | — | 1 | — | yes | PIR2C-1 |

Delivery notes:
- U6 was dropped at plan review (R1-4).
- The plan's text named another project's licence and broke tests/test_license.py. Fixed in 859b0b07.
- Owed by the owner, on a device: U4's share-sheet checklist (Gmail, Files, Chrome × open/share, cold/warm start, App Links still working); this also decides application/octet-stream.