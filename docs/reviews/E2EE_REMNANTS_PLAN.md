# Review ledger — E2EE plaintext remnants (#504, #505, #506, #366)

Subject: docs/E2EE_REMNANTS_PLAN.md
Envelope: docs/E2EE_REMNANTS_PLAN.md § Review envelope

## Round 1 — 2026-10-04, reviewed at 622c5619 (+ uncommitted plan)

### R1-1 — Catch-up nulls the edit snapshot, so Reset on an encrypted edited activity wipes its track
- Trigger: encrypted owner trims a GPX track (decision 7) → catch-up's extracted `_migrateActivity` nulls `original_*` → Reset restores a null track (or 500s on envelope decode if originals kept)
- Scores: trigger=concrete, impact=data-loss, detect=user-visible, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R1-2 — Catch-up writes are not compare-and-swap; a stale pass reverts a concurrent edit permanently
- Trigger: catch-up walks plaintext activities → user saves a track edit/split on X meanwhile → pass PUTs encrypt(old geometry) of X with no version check → edit lost, row enveloped, never revisited (same for memory text)
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=M/shared, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R1-3 — "Before unknown" also disables time apportioning on trim/split of encrypted activities
- Trigger: encrypted user trims or splits a 3 h ride → frac=1.0 → pieces keep full moving/elapsed time, split tail dated at the original end
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=expensive, fix=M/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R1-4 — Journal entries an encrypted user wrote as a companion are never encrypted
- Trigger: user journals on a friend's trip, then enables encryption → migration skips shared trips, catch-up on non-owned trips only repairs memories → journal stays plaintext
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R1-5 — Catch-up cannot encrypt companion-owned activity rows; plan's skip+log contradicts its DoD
- Trigger: owner's former companion imported activities into the trip, owner enables → PUT 404 each load, skipped → plaintext permanently, nothing shown
- Scores: trigger=plausible, impact=silent-wrong, detect=logged, later=cheap, fix=M/shared, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R1-6 — U4 removes `_compute_low_res_geo`, which `api/geo.py` still calls
- Trigger: U4 implementer deletes the function as instructed → `api/geo.py` import fails
- Scores: trigger=concrete, impact=maintainability (triage-corrected from wrong-visible), detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (in plan)

### R1-7 — Poster preview consent 409 breaks the live preview and protects nothing
- Trigger: encrypted owner opens the poster dialog with memories → preview POST → 409 with no consent path → preview error; preview stores nothing
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager)
- Decision: Fix now (D6)
- Override: —
- Outcome: fixed (in plan)

### R1-8 — In-memory Strava cache bypasses disconnect, cache-status and account deletion
- Trigger: encrypted user disconnects and connects a different athlete within the hour → picker shows the previous athlete's list
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a user reports a previous Strava account's activities after reconnecting, a client calls /api/strava/cache/status, or the cache moves out of process
- Override: —
- Outcome: open

### R1-9 — Catch-up fed revealed (decrypted-in-place) maps re-encrypts everything on every load
- Trigger: encrypted owner opens a fully encrypted trip → reveal overwrites map values → step sees plaintext everywhere → N PUTs, lock bumps, cache busts per load; repair can't find its own envelopes
- Scores: trigger=concrete, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (in plan)

### R1-10 — Structural envelope check refuses ordinary "v1.x.y" text under the new 409s
- Trigger: plaintext-account user saves a memory titled "v1.2.3" → treated as envelope → 409 encryption_not_shared
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/shared, confidence=verified
- Decision: Defer (D10)
- Revisit when: a plaintext-account user reports an encryption_not_shared 409 on ordinary text, or another refusal starts depending on is_encrypted_envelope
- Override: —
- Outcome: open

## Round 2 — 2026-10-04, reviewed the round-1 plan fixes (uncommitted)

### R2-1 — Memory/journal PUTs never bump the lock, so the catch-up's CAS can't see a concurrent plain save
- Trigger: pass starts → user saves memory/journal text (no lock_version) → pass's CAS still matches → stale envelope overwrites the edit permanently
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/shared, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R2-2 — Decision 14 puts a former companion's Strava row under the owner's key
- Trigger: companion's rows encrypted by owner's catch-up → companion syncs into own trip, row reused with envelopes → their activity unreadable
- Scores: trigger=plausible, impact=data-loss (triage-corrected from wrong-visible), detect=user-visible, later=expensive, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R2-3 — Decision 14 is vacuously true for orphan rows: any user can write another's activity
- Trigger: non-freeable positive row left unreferenced → any user PUTs envelopes to it → owner's next sync reuses it unreadable
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R2-4 — The client never receives original_*, so the catch-up can't encrypt edit snapshots
- Trigger: catch-up built from the server payload has no original_* → snapshots stay plaintext (or nulled)
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=M/shared, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R2-5 — The editor has no originals to send, so Reset on encrypted edited activities always 409s
- Trigger: owner taps Reset → no original points available → 409 "Reset failed" every time
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/shared, confidence=verified
- Decision: Fix now (D6)
- Override: —
- Outcome: fixed (in plan)

### R2-6 — Stored Strava distance as "before" makes edit→reset drift times permanently
- Trigger: trim then reset an encrypted Strava activity → net factor hav(original)/distance_strava → times/avg speed off by a few %
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R2-7 — Catch-up hook pinned to _applyDetails never runs on a plain trip open
- Trigger: user opens a trip → load() reveals without _applyDetails → no pass → plaintext stays
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R2-8 — U11's unlocked notice may need app_screen.dart if U6 mounts its banner only when locked
- Trigger: U6 follows the offline-banner Selector pattern → U11 can't show the notice when unlocked → X3 escalation
- Scores: trigger=plausible (triage-corrected), impact=maintainability, detect=logged (triage-corrected), later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: U6's banner is mounted conditionally in app_screen.dart, or U11 escalates under X3
- Override: user: Fix now — one sentence in U6 avoids a likely wave-3 stall
- Outcome: fixed (in plan)

### R2-9 — Decision 6 and decision 13/U7 disagree on whether a stale write ends the pass
- Trigger: implementer follows decision 6 and continues after stale_write → every chained write 409s
- Scores: trigger=concrete, impact=maintainability, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (in plan)

## Round 3 — 2026-10-04, reviewed the round-2 plan fixes (uncommitted)

### R3-1 — Hook sites receive /meta: catch-up never sees the polyline and writes the low-res profile over the full one
- Trigger: owner opens a trip with a plaintext activity → pass fed /meta (polyline null, low-res profile) → polyline never encrypted; downsampled profile encrypted into both profile columns
- Scores: trigger=concrete, impact=data-loss, detect=silent, later=expensive, fix=M/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R3-2 — Editor on an unlocked E2EE trip never calls GET …/track, so Reset still has no originals
- Trigger: owner taps Reset → editor opened from the decrypted in-memory copy → no originals, posts {} → 409 every time
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Override: —
- Outcome: fixed (in plan)

### R3-3 — Split after unsaved trims on an encrypted row uses the trimmed track as "before"
- Trigger: user trims, then splits without saving → head+tail keep 100 % of stored times → inflated times/avg speed
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R3-4 — GET …/track would return pre-edit originals to viewer-role members
- Trigger: owner trims the home stretch → a viewer calls GET …/track → receives the untrimmed original track
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R3-5 — The new flag and originals need src/models/activity.py, outside U2 Scope
- Trigger: U2 implementer follows Scope → Activity dataclass lacks the fields → X3 stop; adding them to to_strava_dict changes the .traxj export
- Scores: trigger=concrete, impact=maintainability, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (in plan)

## Round 4 — 2026-10-04, reviewed the round-3 plan fixes (uncommitted; fourth round at the user's request)

### R4-1 — Activity writes with /track's lock_version re-open the stale-write hole for memories
- Trigger: device A loads (v10) → device B saves memory M (v11) → A's pass chains from /track's v11 → encrypt(old M) wins the CAS
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R4-2 — Reset on a row whose originals the shipped migration nulled wipes the track
- Trigger: owner resets an activity edited before enabling → null originals copied into geometry → no track, irreversible
- Scores: trigger=concrete, impact=data-loss, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R4-3 — Client measures before_distance_m with latlong2 Vincenty, server with haversine R=6371
- Trigger: owner saves an encrypted track → times scaled by mismatched lengths → drift per save, edit→reset not exact
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan: design replaced — decision 7 moved editing to the device, before_distance_m removed)

### R4-4 — Split fallback to stored distance is 0 for the tail
- Trigger: old build splits an encrypted activity without before_distance_m → tail frac=1.0 → keeps 100 % of times
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan: design replaced — decision 7 moved editing to the device, before_distance_m removed)

### R4-5 — U7 doesn't state that the low-res profile column gets the full profile's envelope
- Trigger: implementer leaves low-res plaintext or writes a 300-point envelope → endless re-flagging or U8 recomputes from a downsample
- Scores: trigger=plausible, impact=maintainability (triage-corrected from silent-wrong), detect=logged (triage-corrected), later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: U7's implementation or review shows the low-res column written from anything but the full-profile envelope, or still in plain_fields after a pass
- Override: —
- Outcome: open

### R4-6 — plain_fields computed from the row on the light path lazy-loads deferred columns
- Trigger: any trip open → /meta touches polyline/profile per activity → #276 load regression returns
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (in plan)

### R4-7 — GET …/track loads the whole project heavy; the pass calls it per plaintext activity
- Trigger: enabling on a 200-activity trip → 200 heavy project loads in the single API process
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (in plan)

### R4-8 — Rows the owner may not encrypt are re-fetched and re-PUT on every load
- Trigger: trip with a former companion's Strava rows → every open fetches /track and 404s on PUT for each
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: logs show repeated catch-up 404s on the same activity for real users, or owner-unwritable rows become common
- Override: —
- Outcome: open

### R4-9 — U11 sends a reset lock_version that U2 never defines
- Trigger: U2 adds only points to the reset body → U11's lock_version dropped → reset runs unchecked
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (in plan)

## Design change after round 4 — 2026-10-04

User decision: track edit, split and reset of encrypted activities move to the device (decision 7 rewritten). The server-side "edit with plaintext in transit" design accounted for R1-1, R1-3, R2-4–R2-6, R3-2–R3-4, R4-2–R4-4 and R4-9. Units U2 and U3 rewritten, U11 retired, U12–U14 added; plan now has 5 waves.

## Round 5 — 2026-10-04, reviewed the redesigned plan parts (uncommitted; fifth round at the user's request)

### R5-1 — GET …/track returns two of the four geometry originals, so endpoint snapshots stay plaintext
- Trigger: plaintext edit then enable → catch-up can't read original_start/end_latlng_json from /track → stay plaintext, row re-fetched every load
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R5-2 — Strict envelope check named base64url; client envelopes are standard base64
- Trigger: owner saves an encrypted track → envelope contains + or / → 422 on nearly every save/split
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Override: —
- Outcome: fixed (in plan)

### R5-3 — Reset of an encrypted edited activity nulls the low-res profile
- Trigger: owner resets encrypted activity → _low_res_ep_json(envelope) = None → /meta chart empty, never repaired
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Override: —
- Outcome: fixed (in plan)

### R5-4 — Encrypted edit/split can't express an activity with no elevation profile
- Trigger: owner edits an encrypted GPX track without elevations → profile/elev_high/elev_low None → 422, never editable
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Override: —
- Outcome: fixed (in plan)

### R5-5 — plain_fields via case-insensitive SQLite LIKE misses "V1.x.y" plaintext
- Trigger: activity named "V1.0.1 shakedown ride" → classed as envelope in SQL → never encrypted
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (in plan)

### R5-6 — Snapshot columns and writers ship in different waves
- Trigger: deploy after wave 1 only → edits get NULL scalar snapshots → after encryption Reset 409s forever
- Scores: trigger=theoretical (triage-corrected: waves integrate into one PR, never deployed separately), impact=wrong-visible, detect=user-visible, later=expensive, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Override: —
- Outcome: —

### R5-7 — Python round half-to-even vs Dart round half-away in time apportioning
- Trigger: t*frac lands exactly on .5 → 1 s difference
- Scores: trigger=theoretical (triage-corrected), impact=cosmetic, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Override: —
- Outcome: —

### R5-8 — Wave 4 U8 and U14 share flutter_client/test/crypto/
- Trigger: parallel worktrees both add tests in the same directory → W1 violated
- Scores: trigger=concrete, impact=maintainability, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (in plan)

Review stopped after round 5 with user approval (2026-10-04): round-5 fixes are wording-level; the integrated diff gets its own review after delivery (DELIVERY.md §5.2).

## Unit U2, round 1 — 2026-10-05, reviewed 0d42da0d..1f516f18 (worktree-agent-a40897001ce2efa82)

### U2-R1-1 — GET …/track gives a departed companion's pre-edit originals to every editor
- Trigger: companion trims his ride in the owner's trip, then leaves → any editor of the trip (incl. later joiners) reads his untrimmed original via GET …/track, while reset of that row is refused
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (29ba80d4)

### U2-R1-2 — Reset still decodes enveloped originals
- Trigger: (once U7 encrypts originals) owner resets a row with an enveloped original_polyline and null original profile → ciphertext decoded and committed as geometry
- Scores: trigger=theoretical, impact=data-loss, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D4) — flagged to the user; U12 item 4 owns the real handling
- Guard: before decoding originals in reset_activity_track, an envelope in either original → log error with activity_id and raise NothingToRestore; one test
- Override: —
- Outcome: guard added (29ba80d4, log asserted 83d76ba9)

## Unit U4, round 1 — 2026-10-05, reviewed 0d42da0d..45bdd8d5 (worktree-agent-a0177b37de6faff2f)

No findings. Backfill traced against reset_activity_track branch by branch; SQLite upgrade runs in one transaction; no remaining reader of low_res_geo_json in code.

## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Memory/journal key guards, invite refusal, CAS on updates | Opus | S4 | 1 | X3 → Scope widened (3 test files; PUT memory/journal 204 → 200 with `{lock_version}`) | yes | — |
| U2 | Old edit routes refuse envelopes, reset fixes, CAS, ownership helper, single-row /track | Opus | S4 | 3 (fix round for U2-R1-1/-2; log assertion added after verifier fail) | — | yes | U2-R1-1, U2-R1-2 |
| U4 | Strava cache in memory for encrypted users, drop low_res_geo_json, snapshot columns + backfill | Opus | S4 | 1 | — | yes | — |
| U5 | Poster consent, scrub, 30-day sweep | Opus | S4 | 1 | — | yes | — |
| U3 | Python parity vectors for the Dart track maths | Opus | S3 | 2 (orchestrator added a sentinel_mask section) | — | yes | — |
| U12 | Encrypted edit/split routes, scalar snapshots, plain_fields, gain route | Opus | S4 | 1 | X3 → Scope widened (tests/test_activity_track_edit_api.py::test_reset_restores_original, new reset contract) | yes | — |
| U6 | Client encryption state, write gating, owner-only memory encryption | Opus | S5 | 1 | — | yes | — |
| U13 | Dart track_metrics port | Sonnet | — | 1 | — | yes | — |
| U9 | Poster consent dialog | Sonnet | — | 1 | X3 → Scope corrected (plan paths wrong; new poster_consent_dialog.dart, app_screen `_startPosterJob`) | yes | — |
| U7 | Catch-up encryption and companion repair on load | Opus | S5 | 2 (fix round U7-R1-1) | — | yes | U7-R1-1, U7-R1-2, U7-R1-3 |

Notes:
- Wave 1 interrupted once by an API session limit; all four implementers resumed from their worktrees with no work lost.
- Local Windows full-suite runs hung (U1); full checks run in the `traxjourney-py314-citest` container instead. Wave 1 without U2: 6131 passed, 39 skipped. Full wave 1 (a01ecf3c): 6175 passed, 39 skipped.
- Wave 2 (a7b1e14e): server 6292 passed, 40 skipped; flutter analyze clean; flutter test 1986 passed.
- Wave 3 (3519a0a3): server 6293 passed, 39 skipped; flutter analyze clean; flutter test 2169 passed.
- Orchestrator decisions in wave 2: plan updated (fixture name `track_metrics_vectors.json`; `_sentinel_mask` added to the U13 port list); U6 accepted narrowings (memory Save gate on owned trips only; sync import refused only with Polarsteps steps; import ownership from the session user id).

## Unit U12, round 1 — 2026-10-05, reviewed ae7469f7..ffad0cbe (worktree-agent-a52415c6772734e32)

No findings. One envelope question raised for the owner: should the encrypted track routes also require `activity_e2ee_writable_by` for the head row, so a trip editor can never put another user's (positive-id) Strava row under a key that user cannot use (the reverse direction of the Trust bullet; R2-2's reasoning applied to the edit routes)?

Envelope decision (owner, 2026-10-05): **no** — trip editors are trusted to edit any track in the trip, as on the plaintext routes; the first-edit snapshot keeps the row owner's original. A finding that needs a trip editor to overwrite another member's row on the encrypted routes is outside the envelope (D1).

## Unit U7, round 1 — 2026-10-05, reviewed 3e1cbf15..a51be17b (worktree-agent-abfe5fd3f45f562b1)

### U7-R1-1 — The catch-up pass outlives the session; a following sign-in finishes it under another account's key
- Trigger: owner's pass running → owner signs out (pass continues with 401s) → a legacy encrypted companion signs in on the same device before the loop ends → owner's remaining memories stored under the companion's key
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Override: —
- Outcome: fixed (6570a572)

### U7-R1-2 — Companion repair on a now-encrypted owner's trip is refused and retried every load
- Trigger (triage-corrected): only legacy state — an invite accepted after the owner enabled, before U1 — leaves #505 memories under the companion's key on an encrypted owner's trip → repair PUT 409 encryption_locked on every load
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a DB query finds members on trips of encrypted owners; logs show repeated catch-up encryption_locked on the same memory; or when R4-8 is fixed (mark both refusals non-repairable together)
- Override: —
- Outcome: open

### U7-R1-3 — Enable-time notice wrongly says items couldn't be encrypted when the trip screen's pass won the race
- Trigger: enable → run() in background → user opens a trip → passes collide → run()'s pass ends → misleading snackbar
- Scores: trigger=plausible (triage-corrected), impact=cosmetic, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a user reports the notice while trips are fully encrypted, or run() and the trip-screen pass start sharing state
- Override: —
- Outcome: open

Envelope question for the owner: with U7-R1-2 deferred, is "a #505 memory on a trip whose owner is now encrypted stays under the companion's key, unreadable to the owner, until #108 key sharing" accepted for this package?

Owner decisions (2026-10-05): U7 table approved as is. Envelope: a companion's #505 memories on a trip whose owner has since enabled encryption (legacy state only) stay under the companion's key, unreadable to the owner, until #108 key sharing — accepted; ENCRYPTION.md (U10) lists it as a remaining case.

Owner decision during wave 4 (2026-10-05): the #366 gain recompute (U8) corrects **legacy rows only** — GPX imports, and edited activities with no gain snapshot (edit predates #386). Later edits, on-device ones included, keep the Strava-scaled gain as on plaintext accounts. New unit U15 exposes `has_gain_snapshot` in the payload; U8 narrows its selection (second attempt).

## Unit U14, round 1 — 2026-10-05, reviewed 3d27ea98..80424bc2 (worktree-agent-a70bf5b31fe711da1)

### U14-R1-1 — A user without encryption is told to "unlock encryption" for someone else's ciphertext
- Trigger: owner without encryption has an encrypted companion's Strava ride in the trip (encrypted by the companion's catch-up) → taps Edit track → "Unlock encryption on this device…", which they cannot do
- Scores: trigger=concrete (triage-corrected), impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (16c7d163)

### U14-R1-2 — Encrypted save/split CAS against a version the catch-up pass is advancing; stale_write drops the edits
- Trigger: owner edits an encrypted activity while a catch-up pass writes other rows → Save → 409 stale_write → editor closes, unsaved edits lost
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=M/local, confidence=verified (triage-corrected)
- Decision: Defer (D10)
- Revisit when: a user reports "changed elsewhere" on an encrypted activity right after enabling or importing; the catch-up starts writing with no plaintext left; or R4-8/U7-R1-2 refusals get retried while a trip is open
- Override: —
- Outcome: open

## Unit U8, round 1 — 2026-10-05, reviewed 3d27ea98..90d7cb93 (worktree-agent-a0d75663b3bd19211)

### U8-R1-1 — Every load re-decrypts and re-measures every legacy encrypted profile, with no memo
- Trigger: encrypted owner with GPX imports (up to 50 000 samples each) opens a trip → each converged profile is decrypted, parsed and measured again on the main isolate on every load/refresh → a hitch per row after every open
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local (triage-corrected from M), confidence=verified (triage-corrected; cost estimated, not measured)
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (00d735a1)

Owner approved (2026-10-06): U14 table (R1-1 fix now, R1-2 deferred) and U8 table (R1-1 fix now).

## Integrated diff, round 1 — 2026-10-06, reviewed e96f8613..5edeba4b (feat/e2ee-remnants)

### I1-1 — Encrypted Polarsteps re-import can't adopt legacy pre-step-id memories (duplicates)
- Trigger: encrypted owner re-imports a Polarsteps trip first imported before step ids → name sent as envelope → server name+date adoption never matches → duplicate memory per step
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: an encrypted user reports duplicates after a Polarsteps re-import, or a query finds Polarsteps memories with NULL step id on encrypted owners' trips
- Override: —
- Outcome: open

### I1-2 — A plaintext owner can't change date/place of a legacy companion-key memory (encryption_not_shared)
- Trigger: owner without encryption edits a #505 memory written under a companion's key → stored envelope resent → 409
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a plaintext owner reports encryption_not_shared on a date/place edit; companion repair found not to run (companion left); or #108 lands
- Override: —
- Outcome: open

### I1-3 — encryption_locked tells a plaintext-account legacy companion to "unlock encryption"
- Trigger: legacy member without encryption saves a memory on a now-encrypted owner's trip → misleading unlock message
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a companion reports it; U7-R1-2 / R4-8 refusals reworked; or #108 lands
- Override: —
- Outcome: open

### I1-4 — ENCRYPTION.md's remaining-plaintext list omits your own activity rows that live only in another user's trip
- Trigger: encrypted companion imports their ride into a friend's plaintext trip only → row stays plaintext; doc claims the list is complete
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: open

Envelope question for the owner: three Definition-of-done bullets no longer hold literally after owner decisions recorded here (trip editors may write encrypted geometry; ENCRYPTION.md lists the legacy companion-memory case; #366 gain recompute is legacy rows only). Amend the plan's DoD to match, or keep the ledger as the record?

Owner decisions (2026-10-06): integrated-review table approved as is; the plan's Definition of done amended to match the delivery-time owner decisions.
