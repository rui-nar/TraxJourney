# Review ledger — Store distribution plan (#579, #582, #580)

Subject: docs/STORE_DISTRIBUTION_PLAN.md
Envelope: plan section "Review envelope" + REVIEW.md defaults

## Round 1 — 2026-10-12, reviewed at 48029e8f (plan uncommitted)

### R1-1 — Code exchange has no redirect_uri, so every web and Android Apple sign-in ends in 502
- Trigger: A user signs in with Apple on web or Android → the server exchanges the code at `/auth/token` without the `redirect_uri` the authorization request carried → Apple answers `invalid_grant` → the user sees 502 "Apple sign-in unavailable".
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan: Conventions `APPLE_REDIRECT_URI` + 401/502 split, U1 Do 3, U2 Do 2, acceptance in U1/U2, owner action A3)

### R1-2 — Apple sends email_verified / is_private_email as strings; plan and tests assume booleans
- Trigger: A real user's first Apple sign-in → the id_token carries `"email_verified": "true"` → it is written into the Boolean column → 500 after the code is consumed (or `bool("false")` reads as True).
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan: U1 Do 1 claim parsing + U1/U2 acceptance)

### R1-3 — D11 validation builds reuse the released version string, which App Store Connect closes after its release
- Trigger: After a version is released on the App Store, the owner moves the `validation` tag → the build uploads with that released `CFBundleShortVersionString` → ITMS-90186 closed train → no validation TestFlight build until the next real tag.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=inferred
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan: D11 patch-bumped validation version, U7 acceptance)

### R1-4 — A failed first Apple sign-in loses the user's name for good
- Trigger: A new user signs up with Apple → Apple sends the name once → the `POST /api/auth/apple` call fails (502 or a dropped connection) → the retry carries no name → the account's display name is the relay address's local part.
- Scores: trigger=plausible, impact=degraded-ux (triage lowered it from wrong-visible), detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: Apple sign-up failures (502s or dropped first calls) show in the logs after launch, or a user reports a relay-address display name after an Apple sign-up.
- Guard: —
- Override: —
- Outcome: open

## Round 2 — 2026-10-12, fixes for R1-1..R1-3 (plan uncommitted, on 48029e8f)

### R2-1 — R1-1 only half fixed: the web client's API base is empty, so its redirect URI is relative
- Trigger: A user on traxjourney.com clicks Sign in with Apple → the client builds `${API_BASE_URL}/api/auth/apple/callback`, which is relative because the web build has no API_BASE_URL → Apple refuses the redirect → web Apple sign-in never works.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan: Conventions APPLE_REDIRECT_URI bullet, U9 Context + acceptance)

### R2-2 — APPLE_REDIRECT_URI is not part of the "configured" gate
- Trigger: The owner sets APPLE_SERVICES_ID on a deployment but forgets APPLE_REDIRECT_URI → web and Android sign-ins exchange without redirect_uri → invalid_grant → a misleading 401 and "code_rejected" in the log.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible (triage corrected it from logged), later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: A val or prod deployment shows web or Android Apple sign-ins failing with code_rejected after its Apple setup, or a third deployment or Services-ID client is added.
- Guard: —
- Override: —
- Outcome: open

### R2-3 — The 4xx → code_rejected mapping hides Apple's OAuth error code
- Trigger: The owner configures a wrong key id or Services ID → Apple answers invalid_client → every sign-in logs "code_rejected", the same as a replayed or expired code.
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: A code_rejected failure on val or prod is first traced to server configuration (invalid_client or unauthorized_client), or debugging the Apple setup stalls on the logs.
- Guard: —
- Override: —
- Outcome: open

### R2-4 — The "before any release" validation case is reachable only through a swallowed `gh release list` failure
- Trigger: A validation run's `gh release list ... || true` fails → no latest tag → the build uploads as 0.0.1 / 0.<run> → App Store Connect refuses it, or TestFlight shows a bogus 0.0.1 build.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: An iOS or Android validation run produces a 0.0.1, 0.<run> or BASE=0 build, or its log shows `gh release list` failing.
- Guard: —
- Override: —
- Outcome: open

## Round 3 — 2026-10-12, fix for R2-1 (plan uncommitted, on 48029e8f)

No findings. The reviewer traced web prod and val (www redirects to the apex; val is its own origin), Android (the baked `https://traxjourney.com` matches prod's `APPLE_REDIRECT_URI`), dev builds (always a non-empty base) and iOS (native, no redirect_uri). The review is closed: this round has no Fix now decision (§6).
