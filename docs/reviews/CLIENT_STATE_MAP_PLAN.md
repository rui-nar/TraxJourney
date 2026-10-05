# Review ledger — Client state and the map (package C)

Subject: docs/CLIENT_STATE_MAP_PLAN.md
Envelope: the plan's `## Review envelope` section plus REVIEW.md §2 defaults

## Round 1 — 2026-10-04, reviewed at a97701a7

Envelope question raised: should mixed old/new builds accept "last whole-map PUT wins on every day" (R1-1), or should the plan retire the blind PUT for old builds?
Answer (user, 2026-10-04): retire it — option (b). The PUT answers 426, and a minimum-client-version gate is added for later changes (Decisions 12, 15; U20, U21). Table approved as triaged.

### R1-1 — An old build's PUT reverts a day another device PATCHed
- Trigger: old build opens Settings (snapshot) → new build PATCHes a note on day D → old build saves a colour change, PUT sends the stale whole map → day D's note is gone, no error
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3) — becomes D1 if the user accepts last-writer-wins for mixed builds
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-2 — Adopting the PATCH response drops the in-memory gap days up to today
- Trigger: user on an active trip saves a note on the last activity's day → client replaces dayMeta with the response → carousel tiles after the last activity, including today, vanish until reload
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-3 — Wave 7 is not provably disjoint (W1)
- Trigger: U16-U18 run in parallel worktrees; U18's open-ended inventory or U17's unnamed tests overlap U16/U17 files → conflicting edits or an X3 stall
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-4 — The clear()-completeness test cannot catch a field added later
- Trigger: a later change adds a per-trip field and forgets clear() → a hand-listed getter test still passes → the field leaks from account A to account B on a shared device
- Scores (corrected by triage): trigger=theoretical, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D4), flagged to the user
- Revisit when: —
- Guard: source-scan test in U5 listing every instance field of project_notifier.dart and its mixins; fails unless reset in clear() or allow-listed with a reason. Decision 2 reworded to promise only that.
- Override: —
- Outcome: guard added

### R1-5 — No deadline means a stuck 'pending' segment is polled until the API restarts
- Trigger: worker killed mid-resolve while the API stays up → segment stays pending → every open session polls /meta every 60 s
- Scores (corrected by triage): trigger=plausible, impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: hourly scheduler logs a warning listing route jobs pending/running more than 30 min after started_at
- Override: —
- Outcome: guard added

### R1-6 — U2 cites PUT date-key validation that does not exist
- Trigger: implementer invents strict ISO validation → a trip with a legacy odd key gets 400 on prune → no current path writes such keys
- Scores: trigger=theoretical, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: rejected

## Round 2 — 2026-10-04, reviewed at a63b8d45 (fixes since a97701a7)

Envelope question raised: option (b) was accepted on the premise that old builds show the 426 detail. They do not: an old build's day-meta save fails silently (R2-1). Does the owner still accept retiring the PUT, knowing old builds lose their own unsaved day-meta edit silently, or reconsider?
Answer (user, 2026-10-04): keep (b) with the wording corrected to the real behaviour, plus a release-order note in docs/RELEASING.md. Table approved as triaged.

### R2-1 — An old build's day-meta save fails silently, not with the promised message
- Trigger: user on an installed pre-gate build types a day note and saves → the note looks saved, no message, PUT returns 426 → on the next reload the note is gone; same for bulk tags and the trip-end prune
- Scores: trigger=concrete, impact=silent-wrong, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3) — the plan must state the real behaviour; the envelope answer is the owner's
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R2-2 — U17's test list misses two tests that construct ActivityPanel
- Trigger: U17 changes how ActivityPanel subscribes → test/crypto/encrypted_display_test.dart and test/segment_degraded_route_indicator_test.dart fail outside Scope → X3 stall
- Scores (corrected by triage): trigger=plausible, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: U17's implementer finds either file needs editing, or another wave-7 unit claims either file
- Guard: —
- Override: —
- Outcome: open

## Round 3 — 2026-10-04, reviewed at 91908266 (fixes since a63b8d45)

Envelope question raised: Android ships only as a sideloaded APK on the GitHub release (docs/ANDROID.md), with no Play Store and no iOS build, so publishing a release updates no installed device and a pre-gate build is never told to update. Does the owner's acceptance of the silent old-build loss (R2-1) stand on that basis, or should Decision 12 add a lever that reaches installed builds?
Answer (user, 2026-10-04): the acceptance stands, with an `Upgrade-Note:` on the release page (the recommended option). Table approved as triaged.

### R3-1 — The release-order note relies on store builds that do not exist
- Trigger: U21's implementer writes "publish native store builds before or with the deploy" → there is no store (sideload APK attached by the same v* tag; no iOS) → the 426 window lasts until each user installs the APK by hand; Decision 15's store link from ANDROID_PACKAGE_NAME points to a Play listing that does not exist
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R3-2 — A new APK published before the server deploy sends PATCH to a server without it
- Trigger: the v* tag attaches the new APK at once while the server deploy is a separate manual step → a user installs it and saves a note → PATCH gets 405 → U7 reloads day-meta → the note vanishes with no message
- Scores (corrected by triage): trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

## Unit review U1 — 2026-10-04, reviewed at fcd5630f (pkgc/u1)

Envelope question raised: the envelope says old builds "ignore" `id` on `/me`; they do not (the shipped client keys `last_opened_project_` and `projectDataCache` on it). Accept the old-build regression for the window, or hold `id` off `/me`?
Answer (user, 2026-10-04): leave `id` off `/me`. U1 is dropped (not merged); new builds read `sub` (Decision 3 amended).

### U1-R1-1 — Installed old APKs lose last-trip reopen and offline cache hits once /me carries id
- Trigger: user on an un-updated APK relaunches after the deploy → the redirect reads under '' / cache under 0 while later writes go under the real id → the last trip is not reopened and an offline launch misses the trip cached online
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7) — becomes D1 if the owner accepts the regression
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (U1 dropped)

## Unit review U5 — 2026-10-04, reviewed at 45846265 (pkgc/u5)

### U5-R1-1 — A fetch in flight at sign-out writes A's trip into B's cache
- Trigger: A signs out (or 401) while details/geo/meta are in flight → B signs in → the late response is keyed to B → B's same-named trip is served A's payload from memory and disk
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=M/shared, confidence=verified
- Decision: Fix now (D3)
- Outcome: fixed (cc7c2b20)

### U5-R1-2 — An E2EE unlock in flight at sign-out lands after lock()
- Trigger: A's restore awaits unlock → A signs out → lock() then unlock() assigns A's CMK → B (E2EE off / device unapproved) is left unlocked with A's key
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Outcome: fixed (cc7c2b20)

### U5-R1-3 — load() assigns low-res geo before its currency check
- Trigger: A signs out before getLowResGeo answers → A's route lands in the cleared notifier → B's first frame draws it
- Scores: trigger=plausible, impact=security, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Outcome: fixed (cc7c2b20)

### U5-R1-4 — Reuse restores a view-mode selection on top of the stale edit selection
- Trigger: day D selected in edit → activity 5 in view mode → back to edit → both selected until the next tap
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Outcome: fixed (cc7c2b20)

### U5-R1-5 — The cache's account lags the token during a blocking restore
- Trigger: deep link with a nearly expired token → load keyed to user 0, then re-keyed mid-load → stale disk details/geo shown once with no sign
- Scores (corrected by triage): trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Outcome: fixed (cc7c2b20)

## Unit review U5, round 2 — 2026-10-04, reviewed at cc7c2b20 (fixes since 45846265)

### U5-R2-1 — The R1-2 fix covers unlock() only; enable() and recovery still set the key after awaits
- Trigger: A submits enable-encryption or recovery → signs out while it is pending → lock() runs → the call resumes and sets A's key → B (E2EE off) is left unlocked with A's key
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Outcome: fixed (7521c6c9)

## Unit review U5, round 3 — 2026-10-04, reviewed at 7521c6c9 (fixes since cc7c2b20)

### U5-R3-1 — enable() commits the server write for an ended session; the recovery key is never shown
- Trigger: A taps Enable (recovery-key option) → signs out or gets a 401 while key generation or the request is pending → the server enables encryption; the one-time secret goes to a closed screen and is discarded → a recovery method nobody holds; losing the device loses the data
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3). Pre-existing on main, not introduced by U5; U5's doc comment promised the opposite
- Outcome: partly fixed (4885887b: no server call once the session ended before sending; setup screen cannot be left while busy). Session ending during the request itself: pending owner decision on a follow-up unit

## Integrated review Part 1, round 1 — 2026-10-04, reviewed at eeedd614 (code since 44f73762)

### I1-R1-1 — A stale refetch still replaces a resolved train route: the pre-resolve feature already says route_mode 'rail'
- Trigger: user sets a train segment to follow the route and saves → the PUT stores route_mode 'rail' before the resolve → a refetch in flight across the resolve brings back the arc tagged 'rail' → reconcile drops the patch → the map draws the arc until the next zoom bucket
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10) — flagged: the plan's #278 goal "a stale refetch never puts the placeholder back" is not met under this decision
- Revisit when: the owner wants the #278 goal met in Part 1, or a resolved segment is seen drawn as an arc after a refetch
- Override: user: Fix now — the #278 goal must be met in Part 1
- Outcome: fixed

### I1-R1-2 — The 405 fallback PUT reads dayMeta after the await, with no trip check
- Trigger: new APK against a not-yet-deployed server → save a note on trip A, open trip B → B's /meta lands before A's 405 → the whole-map PUT sends B's days to A's path → the old server replaces all of A's days
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Outcome: fixed

## Integrated review Part 1, round 2 — 2026-10-04, reviewed at 8f04e53f (fixes since eeedd614)

### I1-R2-1 — The 405 fallback's trip check throws away a valid save
- Trigger: new APK against a not-yet-deployed server → save a note on trip A, open trip B before the 405 → the check returns before the PUT → A's note is never stored, no error
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3) — introduced by the I1-R1-2 fix
- Outcome: fixed (F3)

### I1-R2-2 — A resolved patch pins its route after another writer re-routes the segment
- Trigger: a resolve lands (hash H1) → the hourly degraded sweep or another device writes H2 → refetches carry H2, the hash differs, the patch is kept → the old route is drawn until a full load
- Scores: trigger=plausible, impact=wrong-visible, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9): warn when a resolved patch is kept against a resolved server feature with a different hash
- Override: user: Fix now — request ordering: a refetch started after the patch was applied shows the server's route; one started before it keeps the patch
- Outcome: fixed (F3)

## Plan amendment U5b (Decision 16), round 1 — 2026-10-04, reviewed at 1a496f08

Envelope question raised: a stolen session may replace then confirm an unconfirmed recovery key, turning it into garbage that the 409 rule then locks in, with no repair path (rotate/delete/disable out of scope). Accept, or add confirm-bound-to-wrap (U5b-R1-1) and/or a rotate path for a confirmed key?
Answer (user, 2026-10-04): accept the risk (recorded in the envelope); U5b-R1-1 and U5b-R1-3 approved; U5b-R1-2 overridden to Fix now.

### U5b-R1-1 — Confirm is keyed by method, not by wrap
- Trigger: two unlocked devices, key unconfirmed → replace on A (K1 shown) → replace on B (K2) → Done on A confirms K2 → the saved K1 does not unwrap the CMK; seen only when every device is lost
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=expensive, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Outcome: fixed in plan

### U5b-R1-2 — A new APK against a not-yet-deployed server fails to parse /status and cannot unlock
- Trigger: APK installed between tag and server deploy → the status parser throws on the missing field → unlock fails silently → encrypted content stays locked until the deploy
- Scores (corrected by triage): trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: the tag-to-deploy gap stops being short, or a user reports encrypted content locked right after an update
- Override: user: Fix now — a few lines, removes the deploy-order trap
- Outcome: fixed in plan

### U5b-R1-3 — U5b-1's migration test has no file in Scope
- Trigger: the implementer writes the "existing rows migrate to confirmed" test → test_encryption.py cannot run Alembic and test_alembic_migrations.py is not in Scope for it → X3 stop or a dropped acceptance
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Outcome: fixed in plan

Owner (2026-10-04): R2-1, R2-2, R2-4 approved; R2-3 overridden to Fix now; merge origin/main before U5b.

## Plan amendment U5b, round 2 — 2026-10-04, reviewed at 2cb8093a (fixes since 1a496f08)

### U5b-R2-1 — Replace "returns the stored wrap" invites a re-read that hands back the other device's wrap
- Trigger: two devices replace within milliseconds → A re-reads after commit and gets B's wrap → A shows K1 but confirms K2 → the saved K1 does not unwrap
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=expensive, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Outcome: fixed in plan

### U5b-R2-2 — The migration would chain from a parent main has already used
- Trigger: the migration is generated in the worktree on e3a91c5d7f20 → main already has 87200bcb9342 on that parent → two heads, the API will not start
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Outcome: fixed in plan

### U5b-R2-3 — An older server answers the confirm POST with 405, not 404
- Trigger: new APK before the server deploy → confirm gets 405 → the planned 404 handling never runs
- Scores (corrected by triage): trigger=plausible, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a confirm against an older server shows anything other than the "couldn't record" message, or the tag-to-deploy gap stops being short
- Override: user: Fix now — same deploy-order trap as U5b-R1-2
- Outcome: fixed in plan

### U5b-R2-4 — The 409 confirm message promises a second chance that never comes
- Trigger: A shows K1, B confirms K2, A taps Done → 409 → "you'll be asked again" → no banner ever → the user keeps K1
- Scores (corrected by triage): trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Outcome: fixed in plan

Owner (2026-10-04): I1-R3-1 and I1-R3-2 approved; I1-R3-3 overridden to Fix now; round 4 requested on the fix commit.

## Integrated review Part 1, round 3 — 2026-10-04, reviewed at 5166190b (fixes since 8f04e53f)

### I1-R3-1 — The 409 resync's trip check lets a signed-out account's geometry land on the next account's same-named trip
- Trigger: A's segment edit gets 409 → the resync's full-res fetch is in flight → A signs out, B opens B's own trip of the same name → A's geometry is assigned as B's
- Scores: trigger=plausible, impact=security, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3). Pre-existing on main (no check at all); this branch narrowed but did not close it
- Outcome: fixed (F4)

### I1-R3-2 — The 405 fallback PUT has no account check
- Trigger: new APK against a not-yet-deployed server → A saves a note on T → A signs out, B signs in while the 405 is in flight → the PUT goes out under B's token → B's trip T day-meta replaced by A's
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3). Introduced by this branch (I1-R2-1's fix removed the only post-await check)
- Outcome: fixed (F4)

### I1-R3-3 — A request joining another notifier's in-flight request is judged by its own start
- Trigger: view-mode request in flight → switch to manage mode → resolve lands → manage refetch joins the view request → judged newer → stale arc replaces the patch until the next refetch
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10); flagged: the #278 goal the owner held to in I1-R1-1 is not met across notifiers
- Revisit when: the owner applies the I1-R1-1 override here, or a resolved segment is seen as an arc after a view/manage switch
- Override: user: Fix now — the #278 goal holds across notifiers
- Outcome: fixed (F4)

## Unit review U5b-1 — 2026-10-05, reviewed at 4cd43f11 (pkgc/u5b1)

Clean: no findings. Compare-and-set atomicity, races, cross-user scoping, the SQLite migration and downgrade, old APKs (E5) and E6 traced end to end.

## Unit review U5b-2 — 2026-10-05, reviewed at 90c5ede8 (pkgc/u5b2)

### U5b2-R1-1 — Against a not-yet-deployed server, the failed-confirm message promises a second chance that never comes
- Trigger: new APK before the server deploy → enable with a recovery key, Done → confirm 405 → "you'll be asked again", Done never succeeds → after the deploy the migration marks the key confirmed → no banner; the user's key works, only the message was wrong
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: the tag-to-deploy gap grows beyond a short same-day window, or a user reports being stuck on the recovery-key screen
- Outcome: open

Owner (2026-10-05): I1-R4-1 and I1-R4-2 fixed structurally: a fresh ProjectNotifier per signed-in account, the old one discarded, so late responses write into an orphaned instance (closes the class). I1-R4-3 overridden to Fix now. Round 5 requested on the fix.

## Integrated review Part 1, round 4 (owner-requested) — 2026-10-05, reviewed at 31105328 (since 4d7d2f5f: F4, U5b-1, U5b-2)

### I1-R4-1 — The day-meta PATCH 200 path adopts the signed-out account's map into the next account's same-named trip
- Trigger: A saves a note → A signs out, B opens B's own same-named trip while the PATCH is in flight → 200 lands, the trip check passes → B's carousel shows A's notes
- Scores: trigger=plausible, impact=security, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Outcome: fixed

### I1-R4-2 — Segment add/update success paths draw the signed-out account's segment on the next account's map
- Trigger: A saves a segment edit → A signs out, B opens any trip while the request is in flight → 200 → A's segment drawn on B's map and kept as a patch
- Scores: trigger=plausible, impact=security, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3). Pre-existing on main
- Outcome: fixed

### I1-R4-3 — The 409 resync's details reload rebinds the notifier to the trip the user left
- Trigger: segment edit on T gets 409 → the user opens X first → the reload for T applies and sets the open trip back to T
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10). Pre-existing on main
- Revisit when: the owner fixes it with R4-1/R4-2, or a user reports a trip switch showing the previous trip
- Override: user: Fix now
- Outcome: fixed

### I1-R4-4 — The full-res fallback can time a cached answer as fresh
- Trigger: needs four independent conditions together; no current actor
- Scores: trigger=theoretical, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Outcome: rejected

Owner (2026-10-05): fix the class now (F6); verifier only, then close the integrated review.

## Integrated review Part 1, round 5 (owner-requested) — 2026-10-05, reviewed at e42cbbc8 (F5 since 22d6e204)

No account-switch findings: the per-account notifier held.

### I1-R5-1 — A full reload still rebinds the notifier to the trip the user left
- Trigger: remove an activity on T → open X before the reload lands → load(X) finishes first → T's reload applies and the open trip becomes T under X's title
- Scores (corrected by triage): trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/shared, confidence=verified
- Decision: Defer (D10); flagged: same class as I1-R4-3 (owner: Fix now)
- Revisit when: the owner applies the I1-R4-3 override to this class, or a user reports a trip showing another trip's items
- Override: user: Fix now — one same-trip check inside _silentReload and _silentReloadDetailsOnly (at begin and after every await), replacing the I1-R4-3 caller check
- Outcome: fixed

### I1-R5-2 — A details-only reload begun after a trip switch rebinds the notifier (memory/journal/people CRUD, sort, reorder)
- Trigger: add a memory on T → open X before the POST returns → the reload for T begins after load(X) and applies → the open trip becomes T
- Scores (corrected by triage): trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/shared, confidence=verified
- Decision: Defer (D10); flagged as above
- Revisit when: as above, or a user reports an item appearing in a trip other than the one they added it to
- Override: user: Fix now — one same-trip check inside _silentReload and _silentReloadDetailsOnly (at begin and after every await), replacing the I1-R4-3 caller check
- Outcome: fixed

## Integrated review Part 1 — closed 2026-10-05

Closed by the owner after F6's verification (no round 6). Five rounds (rounds 4 and 5 owner-requested beyond the cap). Rounds 1-3 each found defects in the previous round's fixes; round 4's account-switch findings were closed as a class by a fresh ProjectNotifier per account (F5), and round 5 found no account leaks.

Notes left by the final verifier (not findings):
- No test pins a trip switch landing during `_applyDetails`' reveal awaits with encryption unlocked; the code handles it (stale check after the reveals).
- `_buildFullTrack` writes `_fullTrack` after its compute hop guarded only by `_buildFullTrackGen`; a dropped reload can write the old trip's track without a notify. `load(X)` rebuilds it, so it self-heals.

## Delivery

Part 1 (waves 1-4 plus Decision 16) on `feat/package-c-part1`. Part 2 (waves 5-9, #294 facet split) is a separate delivery.

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | `/me` returns the account id | Opus | S4 | 1 | — | yes | U1-R1-1 (unit dropped by owner, not merged) |
| U2 | `PATCH /day-meta`, PUT retired | Opus | S4 | 1 | X3 (cache-bust scan test) | yes | — |
| U3 | `Server-Timing` on simplified geo | Opus | S4 | 1 | — | yes | — |
| U4 | Auto-zoom fits the selection when switched on | Sonnet | — | 1 | — | yes | — |
| U5 | The account owns the client session | Opus | S4 | 4 (unit review rounds 1-3) | X3 (cache store purge) | yes | U5-R1-1..5, U5-R2-1, U5-R3-1 |
| U20 | `min_client_version`, stuck route-job warning | Opus | S4 | 1 | X3 (version endpoint test) | yes | — |
| U6 | Per-fetch geo timing (+ `ApiClient.patch`) | Sonnet | — | 1 | — | yes | — |
| U7 | Client sends only changed days | Sonnet | — | 2 | X3 (no `ApiClient.patch`; rename dialog) | no (rename acceptance) | I1-R1-2, I1-R2-1, I1-R3-2, I1-R4-1 |
| U21 | Client minimum-version gate | Sonnet | — | 1 | — | yes | — |
| U8 | One resolve poller per trip | Opus | S5 | 1 | — | yes | I1-R1-1, I1-R2-2, I1-R3-1, I1-R3-3, I1-R4-2, I1-R4-3 |
| U5b-1 | Recovery wraps confirmed; confirm/replace endpoints | Opus | S4 | 1 | — | yes | — (unit review clean) |
| U5b-2 | Confirm on setup, replace after sign-in | Opus | S5 | 1 | — | yes | U5b2-R1-1 (deferred) |
| F1 | 405 fallback never sends another trip's days | Sonnet | — | 1 | — | yes (rerun after Docker outage) | I1-R2-1 |
| F2 | Segment features carry `route_status`/`route_hash` | Opus | S3 | 1 | — | yes | I1-R2-2 |
| F3 | 405 trip check; request-ordered patches | Opus | S2 | 1 | — | yes | I1-R3-2, I1-R3-3 |
| F4 | Account checks on resync and 405 PUT; shared request clock | Opus | S5 | 1 | — | yes | I1-R4-1, I1-R4-2 |
| F5 | A fresh trip notifier per account; resync stays on the open trip | Opus | S5 | 1 | — | yes | I1-R5-1, I1-R5-2 |
| F6 | A background refresh never reopens the trip left | Opus | S2 | 1 | — | yes | — |

Owner overrides recorded: U1 dropped; I1-R1-1, I1-R2-2 (request ordering), I1-R3-3, I1-R4-3, I1-R5-1/2 overridden to Fix now; U5b-R1-2, U5b-R2-3 overridden to Fix now; U5b2-R1-1 kept Deferred.

Final checks at aa0cdb69 (2026-10-05): flutter analyze clean; flutter test 2110 passed; pytest CI command 6114 passed, 47 skipped (the two git-dependent tests, test_no_raw_extract_is_tracked_in_the_repo and test_shell_script_line_endings, run natively in the worktree: 10 passed).

Process notes:
- Automatic agent worktrees start from `main`; worktrees were created by hand from the feature branch, and orchestration ran from a separate worktree because the main checkout is shared.
- Test runs used per-unit folders inside the persistent Flutter and Python containers. A Docker Desktop engine hang lost the Python container mid-delivery; it was rebuilt from `python:3.14-slim` (plus `libexpat1`, `git`).
- Three API session-limit interruptions; every interrupted agent was resumed from its transcript without losing work.

## Part 2 amendment, round 1 — 2026-10-06, reviewed at 491c5459

### P2-R1-1 — The post-mutation fetch is not ordered against geo requests already in flight
- Trigger: zoom refetch or LOD request in flight → user deletes an activity → the post-mutation fetch lands first → the older response lands and is assigned → the deleted activity is drawn again until the next bucket crossing
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10); flagged: same request-ordering class the owner overrode in I1-R1-1/I1-R3-3; pre-existing on main
- Revisit when: the owner applies that override to geo writers, or a deleted activity is seen drawn after an in-flight zoom
- Outcome: open

### P2-R1-2 — Derived root getters (hasActiveFilter …) hide facet reads from U18's audit
- Trigger: after U19, ticking a tag does not rebuild the filter badge's Consumer
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6): U12 removes or moves the derived getters with the field
- Outcome: open

### P2-R1-3 — U19's root dirty flag has no completeness mechanism
- Trigger: a root field written without marking dirty → its Consumer never updates (banner, spinner)
- Scores (corrected by triage): trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6): private root fields behind marking setters + a completeness scan test
- Outcome: open

### P2-R1-4 — listVersion bumps on day-meta/people/groups and rebuilds the specs
- Trigger: a day-note save re-runs every spec builder although items and geo did not change
- Scores: trigger=concrete, impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7): day-meta, people, groups out of the spec key; people/groups in the encounter key
- Outcome: open

### P2-R1-5 — The two subclass isGeoLoaded writers break the mutator convention
- Trigger: U10 moves view_screen.dart:68 / shared_project_screen.dart:216 into the facet → the restriction rejects both → X3
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Outcome: open

### P2-R1-6 — joinInFlight breaks seven test fakes outside U10's Scope
- Trigger: the new named parameter makes every getSimplifiedGeo override fail to compile → X3
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7): a separate non-dedup service method; U10's Scope lists the fakes that must override it
- Outcome: open

Owner (2026-10-06): both approved (P2-R2-1 overridden to Fix now); round 3 requested.

## Part 2 amendment, round 2 — 2026-10-06, reviewed at 9dec4cdd (fixes since 491c5459)

### P2-R2-1 — Decision 24 stamps geometry with the oldest in-flight start, so the race it targets stays open
- Trigger: zoom refetch R1 in flight → delete an activity → the post-mutation fetch R2 is stamped with R1's start → R2 lands first, R1 lands with an equal stamp and is applied → the deleted activity is drawn again
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/shared, confidence=verified
- Decision: Defer (D10); flagged: defect in the owner's Decision 24 override; U10's acceptance cannot pass as written
- Revisit when: the owner applies the P2-R1-1 override here, or U10 cannot pass its Decision 24 test
- Override: user: Fix now — stamp each answer with the start of the request that produced it
- Outcome: fixed in plan

### P2-R2-2 — hasFilterableContent and the available* getters derive from items, not selection
- Trigger: first activity added to an empty trip → only ItemsFacet bumps → the filter button, now on SelectionFacet, stays greyed out until a selection change
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6); introduced by the P2-R1-2 fix
- Outcome: fixed in plan
