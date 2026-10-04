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
