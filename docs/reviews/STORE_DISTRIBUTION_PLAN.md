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
- Override: user 2026-10-13: Fix now — owner asked to fix every open finding before the package merges
- Outcome: fixed (c8a6b253, 2f1bf42b: names kept in secure storage keyed by userIdentifier or the token's sub until a sign-in succeeds)

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
- Override: user 2026-10-13: Fix now — owner asked to fix every open finding before the package merges
- Outcome: fixed (c3211472: Services ID left out of the accepted audiences without APPLE_REDIRECT_URI, one warning per process)

### R2-3 — The 4xx → code_rejected mapping hides Apple's OAuth error code
- Trigger: The owner configures a wrong key id or Services ID → Apple answers invalid_client → every sign-in logs "code_rejected", the same as a replayed or expired code.
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: A code_rejected failure on val or prod is first traced to server configuration (invalid_client or unauthorized_client), or debugging the Apple setup stalls on the logs.
- Guard: —
- Override: user 2026-10-13: Fix now — owner asked to fix every open finding before the package merges
- Outcome: fixed (already logged by U1's exchange; c3211472 adds the test that invalid_client and invalid_grant log differently)

### R2-4 — The "before any release" validation case is reachable only through a swallowed `gh release list` failure
- Trigger: A validation run's `gh release list ... || true` fails → no latest tag → the build uploads as 0.0.1 / 0.<run> → App Store Connect refuses it, or TestFlight shows a bogus 0.0.1 build.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: An iOS or Android validation run produces a 0.0.1, 0.<run> or BASE=0 build, or its log shows `gh release list` failing.
- Guard: —
- Override: user 2026-10-13: Fix now — owner asked to fix every open finding before the package merges
- Outcome: fixed (c9ae995f: validation needs a published vX.Y.Z release; the lookup failure fails the step)

## Round 3 — 2026-10-12, fix for R2-1 (plan uncommitted, on 48029e8f)

No findings. The reviewer traced web prod and val (www redirects to the apex; val is its own origin), Android (the baked `https://traxjourney.com` matches prod's `APPLE_REDIRECT_URI`), dev builds (always a non-empty base) and iOS (native, no redirect_uri). The review is closed: this round has no Fix now decision (§6).

## Unit review U1 (DELIVERY.md §5.3) — round 1, 2026-10-12, reviewed at 1427c1ff

### U1R1-1 — A malformed JWKS answer during the unknown-kid refetch is reported as an invalid token, not an Apple outage
- Trigger: Apple signs a token with a newly rotated key while our 1 h key-set cache holds the old set → the refetch of `/auth/keys` returns a malformed 200 → PyJWT raises a parse/set error → `_signing_key` maps it to `AppleTokenInvalid("unknown_kid")` → 401 instead of 502 for a valid token.
- Scores: trigger=plausible, impact=degraded-ux (triage lowered it from wrong-visible), detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: Logs show a burst of unknown_kid 401s across several users, an Apple JWKS incident is seen, or clients start treating 401 differently from 502 for Apple sign-in.
- Guard: —
- Override: user 2026-10-13: Fix now — owner asked to fix every open finding before the package merges
- Outcome: fixed (c3211472: a bad key set on the unknown-kid refetch is AppleUnavailable; refetch path now tested)

## Unit review U9 (DELIVERY.md §5.3) — round 1, 2026-10-12, reviewed at 4b7a7add

### U9R1-1 — With a self-hosted server set, the Apple button sends an unregistered redirect URI and fails silently
- Trigger: A self-hoster runs the official APK, sets their own server URL and taps Continue with Apple → the Custom Tab asks Apple for `https://<their-host>/api/auth/apple/callback` under TraxJourney's Services ID → Apple refuses; the user closes the tab → the plugin reports cancel → the app shows nothing.
- Scores: trigger=concrete (triage raised it from plausible), impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (2313acf1: button hidden on web and Android while a custom server is set)

## Unit review U2 (DELIVERY.md §5.3) — round 1, 2026-10-13, reviewed at 185e0627

No findings. The deletion revoke and the `appletoken` delete are owed to U3 by design, so they were not raised.

## Unit review U3 (DELIVERY.md §5.3) — round 1, 2026-10-13, reviewed at 96d38f25

### U3R1-1 — An Apple sign-in during the deletion's network window leaves the account deleted but Apple still authorized
- Trigger: A user deletes an Apple account while the same Apple ID signs in on another device → `apple_login` commits a new refresh token into `appletoken` during the deletion's pre-lock window → the deletion revokes only the old token, then deletes the row holding the new one → the account is gone and Apple still lists TraxJourney as authorized.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, floor F3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (9048ce0d: token re-read under the lock, a swapped-in token revoked after commit with its own client_id)

### U3R1-2 — The mirror race: a sign-in that commits after the deletion leaves a fresh Apple token unrevoked (api/auth.py, U2's code)
- Trigger: `apple_login` reads the account and token row, Apple issues a fresh refresh token, the deletion commits first → the login's UPDATE hits a deleted row (StaleDataError → 500) → the fresh token is in no row and is never revoked → Apple shows TraxJourney authorized for an account that no longer exists.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=inferred (raised by the triager from the U3 reviewer's note)
- Decision: Fix now (D3, floor F3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (ed217271: account re-checked after flush; if gone, roll back, revoke the fresh token, 401)

## Integrated review (DELIVERY.md §5.2) — round 1, 2026-10-13, reviewed at c76aa00d

### I1-1 — No official web or Android build carries APPLE_SERVICES_ID, so the Apple button never appears outside iOS
- Trigger: The owner sets the `APPLE_SERVICES_ID` repository variable and tags a release → the APK, the Play AAB and the Docker web image are built without `--dart-define=APPLE_SERVICES_ID` → the button is hidden on web and Android → an Apple-only user can't sign in there (DoD 4).
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (9e80e9a2: APK, AAB, Docker web and deploy.ps1 builds pass APPLE_SERVICES_ID)

### I1-2 — The TestFlight entitlement check may read a text dump from `codesign -d --entitlements :-` and fail every run
- Trigger: On a macOS image whose codesign no longer writes XML for the `:` form → PlistBuddy can't parse the file → under pipefail the step blames the Developer portal → nothing uploads.
- Scores: trigger=plausible (triage lowered it from concrete), impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: A run's "Verify the archived app" log shows a text entitlements dump or a PlistBuddy parse error, codesign's `:` deprecation becomes an error, or ios-release.yml is edited for another reason.
- Guard: —
- Override: user 2026-10-13: Fix now — one flag, and a failed first TestFlight run costs a full setup round
- Outcome: fixed (9e80e9a2: codesign --entitlements - --xml)

### I1-3 — The privacy policy says Apple's script loads on the sign-in page; index.html loads it on every page, share links included
- Trigger: A visitor opens a public share link → index.html fetches appleid.auth.js from Apple's CDN → Apple receives the visitor's IP while the policy says it only receives data at sign-in.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, floor F3) — load the script only from the login screen, not reword the policy
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (06fcd3c6: script removed from index.html, loaded by the login screen when the Apple button shows)

### I1-4 — Google's sign-in script also loads on every web page, share links included (pre-existing, raised by triage of I1-3)
- Trigger: A visitor opens a public share link → `main.dart` calls `GoogleSignIn.instance.initialize()` before `runApp`, so google_sign_in_web loads Google's GSI script → Google receives the visitor's IP while privacy.html says Google is involved only at sign-in.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, floor F3) — google_sign_in_web loads GSI in its constructor before main(), so our code cannot defer it; owner 2026-10-13: reword privacy.html now, patch the plugin in a follow-up issue
- Revisit when: —
- Guard: —
- Override: user 2026-10-13: fix in this branch rather than a separate issue
- Outcome: fixed in wording (f832b5fe: privacy.html says Google's script loads on every web page); code fix tracked in #611

## Integrated review — round 2, 2026-10-13, fixes d5151bbe..f7ae44ba (G1, G2, H1)

### I2-1 — A tap that beats the on-demand Apple script load opens Apple's popup outside the click
- Trigger: A web user on a slow connection taps "Continue with Apple" before appleid.auth.js has loaded → the sign-in waits for the load, then opens the popup in a later task → the browser's popup blocker refuses it → an error shows, and the second tap works.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: A failed first Apple sign-in on web or a blocked Apple popup is reported. Fix then with a three-state button (loading = disabled, loaded = enabled, failed = enabled so the tap retries and reports). Do not just disable it until the load finishes: preloadAppleJs swallows failures, so a failed load would leave the button stuck.
- Guard: —
- Override: user 2026-10-13: Fix now — three-state Apple button
- Outcome: fixed (83622dc9: three-state Apple button on web; review round 3 clean)

## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Apple identity library | Opus | S5 | 1 | — | yes | U1R1-1 |
| U2 | Apple login endpoint, callback, schema | Opus | S4 | 1 | X3 (two existing tests outside Scope) | yes | U3R1-2 |
| U3 | Revoke Apple on account deletion | Opus | S5 | 1 | — | yes | U3R1-1 |
| U4 | AASA, /delete-account, privacy update | Sonnet | — | 1 | — | yes | I1-3 |
| U6 | iOS entitlements, privacy manifest, PR build | Opus | S2 | 1 | — | yes (static; CI ios job green) | — |
| U7 | iOS release workflow to TestFlight | Opus | S2 | 1 | — | yes | R2-4, I1-2 |
| U8 | AAB and Play upload | Sonnet | — | 1 | — | yes | I1-1 |
| U9 | Sign in with Apple in the client | Opus | S5 | 1 | X3 (auth_notifier.dart) | yes | U9R1-1, R1-4, I1-3, I2-1 |
| U9a | Hide Apple on a custom server (U9R1-1) | Sonnet | — | 1 | — | yes | — |
| F1 | Deletion race (U3R1-1) | Opus | S5 | 1 | — | yes | — |
| F2 | Sign-in race (U3R1-2) | Opus | S5 | 1 | — | yes | — |
| F3 | Config, OAuth error and key-set fixes (R2-2, R2-3, U1R1-1) | Opus | S5 | 1 | — | yes | — |
| F4 | Keep Apple's first-sign-in name (R1-4) | Opus | S5 | 2 | — (acceptance widened by the orchestrator to key on the token's sub) | yes | — |
| F5 | Validation build needs a release (R2-4) | Sonnet | — | 1 | — | yes | — |
| G1 | Services ID in web/Android builds, codesign --xml (I1-1, I1-2) | Sonnet | — | 1 | X3 (deploy.env.example, test_deploy_script.py) | yes | — |
| G2 | Apple script only on the sign-in page (I1-3) | Opus | S5 | 1 | X2 for I1-4 (plugin loads GSI before main; owner chose reword + #611) | yes | I2-1 |
| H1 | Privacy wording for Google's script (I1-4) | Sonnet | — | 2 | — | yes (verifier asked for one wording change) | — |
| J1 | Three-state Apple button on web (I2-1) | Opus | S5 | 1 | — | yes | — |
| K1 | Merge main (uv #613); Play upload installs a uv `play` group | Opus | S2 | 2 | X3 (owner chose: allow-list android-release.yml in test_ci_workflows.py, plus a narrow `play` group) | yes | — |

Delivery notes:
- U9 started before U1 was integrated (it was disjoint and built against the plan's contract). U7 and U3 also started as soon as their own dependencies were in.
- Agent worktree isolation failed repeatedly on a drive-letter case mismatch (`e:` vs `E:`), so from F1 on the worktrees were created by the orchestrator under `E:\Dev\TraxJourney-fixes\` and the agents were pinned to them.
- CI on the integrated branch hit two failures that came from main, not this branch. Starlette 1.8.0 broke a test fake: fixed in PR #614 and merged. A storage-counter race made a photo test flaky: issue #612, fixed on its own branch.

## Integrated review — round 3, 2026-10-13, fix 83622dc9 (J1, for I2-1)

No findings. The review traced every state transition of the Apple script loader: unmount mid-load, several login screens sharing the loader, no Apple call after an await on web, the StateError being unreachable from every caller, and the interaction with the U9 stale-attempt guard and F4's kept-name logic. It also confirmed the two removed tests are covered by the new ones. The integrated review is closed.
