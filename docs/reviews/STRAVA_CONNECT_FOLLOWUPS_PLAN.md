# Review ledger — Strava connect follow-ups (#584, #587, #438)

Subject: docs/STRAVA_CONNECT_FOLLOWUPS_PLAN.md
Envelope: plan section "Review envelope" (REVIEW.md defaults + docs/STRAVA_CONNECT_BINDING_PLAN.md envelope as amended + unauthenticated app page, untrusted display name, no iOS build, browser-only popup behaviour)

## Round 1 — 2026-10-08, reviewed at 5a9c034b

Envelope questions (to owner) — answered 2026-10-08: EQ1 yes (in scope; plan envelope amended), EQ2 yes (the device check covers Strava's in-app browser).
- EQ1 (from R1-1, floor): is the forwarded attacker-initiated app flow in scope for this plan's confirmation page, since #584 exists only to mitigate it?
- EQ2: does the owner's Android device check cover the Strava app's in-app browser (Strava app installed), or is that path out like iOS?

### R1-1 — The page's only identifier is the attacker-chosen display name
- Trigger: the attacker sets their display name to the victim's name or to instructive text, then forwards an app connect → the page shows a trusted-looking name → the victim taps Continue → the hostile scheme app relays, as in U1R2-1
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3) — owner put the case in scope (EQ1); triage's D1 reject no longer applies
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan: fixed warning first, name truncated)

### R1-2 — The closed-poll can report "cancelled" after a successful approval
- Trigger: approve → the popup posts and closes in one task → an already-due poll sees closed first → listener removed, verifier cleared → "Strava connection cancelled." after approving
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: the owner's browser check or a user report shows "cancelled" after approving
- Guard: —
- Override: user: Fix now — plan stage, one sentence in a unit
- Outcome: fixed (plan)

### R1-3 — Cancel navigates into the app as reason=denied
- Trigger: Continue (connected), then back to the browser tab and Cancel → the app shows "Strava access was not granted" while connected
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a report of "not granted" while connected, or Cancel starts to do more than show a message
- Guard: —
- Override: user: Fix now — plan stage, one sentence in a unit
- Outcome: fixed (plan)

### R1-4 — Callback behaviour when the state's account no longer exists is unspecified
- Trigger: a user deletes their account within the state's 10 minutes, then approves → UserInfo lookup returns None → 500
- Scores: trigger=plausible, impact=degraded-ux (triager), detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: a 500 from GET /api/strava/callback appears in the logs
- Guard: —
- Override: user: Fix now — plan stage, one sentence in a unit
- Outcome: fixed (plan)

### R1-5 — The FlutterDeepLinkingEnabled claim is wrong for current Flutter
- Trigger: a maintainer reads the plan's reasoning as "required" → no user-visible effect
- Scores: trigger=theoretical, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=inferred
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: wording corrected in the plan anyway (no cost)

## Round 2 — 2026-10-08, reviewed at f4baa7de (fixes 5a9c034b..f4baa7de only)

### R2-1 — The name has no bidi isolation or overflow clipping, so it can overlay or reorder the fixed warning
- Trigger: the attacker's display name is a letter plus 39 stacked combining marks, or contains U+202E → the marks cover the fixed warning or reverse the following text → the only non-attacker signal is defeated
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified (triager)
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R2-2 — The Cancel view says "Nothing was connected." even after Continue
- Trigger: a victim in the forwarded flow taps Continue (the hostile app catches it, and their own app shows nothing), returns and taps Cancel → the page falsely reassures them → they never revoke on Strava
- Scores: trigger=plausible, impact=silent-wrong (triager), detect=silent (triager), later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R2-3 — The grace-period test can only exercise the fake popup, not the web implementation
- Trigger: the implementer tests the R1-2 grace period with _FakePopup → the test passes whatever strava_oauth_popup_web.dart does → the fix ships effectively untested
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=M/local, confidence=verified
- Decision: Defer (D8)
- Revisit when: the browser check or a user report shows "cancelled" after approval, or the closed-check/grace logic is next changed
- Guard: —
- Override: user: Fix now — as planned the test certifies nothing; arbitration moves to a platform-neutral class tested with fakeAsync
- Outcome: fixed (plan)

## Round 3 — 2026-10-08, reviewed at afdc5402 (fixes f4baa7de..afdc5402 only; review cap reached)

### R3-1 — The Cancel text names "TraxJourney", but the Strava API app may still carry its old name
- Trigger: a victim taps Continue (the hostile app relays it), returns and taps Cancel → looks for "TraxJourney" under My Apps on Strava → doesn't find it (the app is registered under its old name; the rename runbook console step is open) → never revokes
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified (triager)
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R3-2 — The fakeAsync test needs fake_async, which is not a declared dependency
- Trigger: the U2 implementer imports fake_async → flutter analyze fails on depend_on_referenced_packages → pubspec is outside Scope with Latitude none → stall
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R3-3 — The "never split a surrogate pair" rule is vacuous for Python str
- Trigger: the implementer writes name[:40] and the prescribed emoji test → it passes unconditionally → a check that proves nothing
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R3-4 — The clipping test cannot tell whether overflow: hidden applies to the name's block
- Trigger: the CSS lands on the wrong selector or on the inline <bdi> → the test still passes → stacked marks overlay the warning again
- Scores: trigger=plausible, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: in the test, assert the name block has class "name" containing the <bdi>, and that a .name rule in <style> has both overflow: hidden and a line-height
- Override: —
- Outcome: guard added (plan: U1 acceptance)

## Unit U1 review — 2026-10-08, reviewed at 949d7154 (e84b8d6e..949d7154, DELIVERY.md §5 point 3)

### FU1-1 — test_page_is_not_logged cannot see a DEBUG-level leak from the callback's own logger
- Trigger: a maintainer adds a DEBUG log of the code to strava_callback → the test still passes (caplog lowers root only; configure_logging pins api/src to INFO)
- Scores: trigger=theoretical, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=inferred
- Decision: Reject (D11) — the callback logs no code, state or name today
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

## Integrated diff, round 1 — 2026-10-08, reviewed at 8c09aabf (origin/main..8c09aabf)

### FI1-1 — A replaced web attempt still steers the shared named popup
- Trigger: a web user double-clicks Connect Strava → both attempts share the named window → (a) the first start answers last and navigates to state 1, which attempt 2 completes with verifier 2 → "started from another account or device"; (b) the first start fails and closes the window attempt 2 uses → "cancelled" plus "Could not open Strava"
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager)
- Decision: Defer (D10)
- Revisit when: a double-click report or a browser check shows either message, or the Connect button, connectWeb generation handling or popup open/close changes next
- Guard: —
- Override: user: Fix now — this delivery rewrote that code and added close(); a replaced attempt must not touch the popup (U2 attempt 2, Sonnet)
- Outcome: fixed (039ae148)

## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Confirmation page for Android returns (#584) | Opus | S4 | 1 | — | yes | R1-1, R1-3, R1-4, R2-1, R2-2, R3-1, R3-3, R3-4, FU1-1 |
| U2 | Web popup opens on the click; closed/blocked reported; arbiter (#587) | Sonnet | — | 2 | — | yes (both attempts) | R1-2, R2-3, R3-2, FI1-1 |
| U3 | iOS URL schemes and deep linking (#438, partial) | Sonnet | — | 1 | — | yes | R1-5 |

Notes:
- U2's second attempt was a review fix (FI1-1, owner override), not a verification failure. Its tests were proven to fail on the previous code.
- The plan text carried the legacy bundle id, which test_no_legacy_brand caught. The orchestrator reworded it (a20295cc).
- Checks at the end: full pytest (CI command, minus test_rail_extract_workflow.py) 6923 passed / 0 failed on 8c09aabf (server unchanged afterwards); flutter analyze clean and flutter test 2628 passed on 2ccb879a; `flutter build web --release` succeeded on U2 (the web-only popup file compiles); alembic heads unchanged.
- Owed: owner device checks (Android: page, Continue and Cancel, with and without the Strava app installed; web: closing and blocking the popup in a real browser); O1 regenerate GoogleService-Info.plist for com.traxjourney.app (#438 stays open); graphify update from main after merge.
