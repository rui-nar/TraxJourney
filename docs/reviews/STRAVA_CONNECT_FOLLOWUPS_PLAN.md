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
