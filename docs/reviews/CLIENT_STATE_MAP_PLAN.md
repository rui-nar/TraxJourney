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
- Outcome: open

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
- Outcome: open

### I1-R2-2 — A resolved patch pins its route after another writer re-routes the segment
- Trigger: a resolve lands (hash H1) → the hourly degraded sweep or another device writes H2 → refetches carry H2, the hash differs, the patch is kept → the old route is drawn until a full load
- Scores: trigger=plausible, impact=wrong-visible, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9): warn when a resolved patch is kept against a resolved server feature with a different hash
- Override: user: Fix now — request ordering: a refetch started after the patch was applied shows the server's route; one started before it keeps the patch
- Outcome: open
