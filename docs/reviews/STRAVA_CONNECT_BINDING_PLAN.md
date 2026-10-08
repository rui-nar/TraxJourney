# Review ledger — Strava connect binding (GHSA-66h3-p265-gw7c)

Subject: docs/STRAVA_CONNECT_BINDING_PLAN.md
Envelope: plan section "Review envelope" (REVIEW.md defaults + custom-scheme boundary, unauthenticated callback, E5 cut accepted, no iOS)

## Round 1 — 2026-10-08, reviewed at 1418d07d

Envelope question (to owner): the callback's query string (code + state) lands in the reverse proxy's access log and in browser history. The verifier makes a logged code useless. Is the proxy log covered by the "never log a code or state" convention, or is it E3 server config and out?
Envelope answer: owner approved the recommendation 2026-10-08 — proxy access logs and browser history are E3/out; the logging convention covers application logs only (added to the plan envelope).

### R1-1 — Android cold-start return calls complete before the session is restored
- Trigger: Android user approves on Strava after the OS killed the app → custom-scheme relaunch → StravaReturnScreen posts complete before api.setToken → 401 → verifier cleared, "failed"
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager)
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-2 — Expired state on an app-started flow is redirected to the web page
- Trigger: Android user spends >10 min at Strava → callback hits ExpiredSignatureError, ret unknown → web oauth_callback.html with no opener → stuck on "Connecting…"
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-3 — exchange_code has no HTTP timeout; src/auth/oauth.py outside U1 Scope
- Trigger: Strava token endpoint stalls → complete blocks a worker thread; client times out at 30 s and shows "failed"
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible (triager), later=cheap, fix=S/shared, confidence=verified
- Decision: Defer (D10)
- Revisit when: a token-endpoint stall or threadpool exhaustion is observed, or src/auth/oauth.py is next changed
- Guard: —
- Override: user: Fix now — the plan moves this exact call behind an authenticated request with a 30 s client timeout; one-argument fix
- Outcome: fixed

### R1-4 — D8 says an old web bundle reports failure; it hangs silently
- Trigger (narrowed by triager): a web tab starts Connect before the deploy and approves after it → callback answers update_required → the new oauth_callback.html posts an object the old listener ignores → nothing shown until reload
- Scores: trigger=plausible, impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: one warning log line (no code/state) in strava_callback per update_required refusal; correct D8's wording
- Override: —
- Outcome: guard added (in plan: U1 warning log + D8 corrected)

## Round 2 — 2026-10-08, reviewed at ec8bdc09 (fixes 1418d07d..ec8bdc09 only)

### R2-1 — The no-opener fallback in oauth_callback.html never runs when the opener navigated to another origin
- Trigger: a web user clicks Connect, then navigates the opener tab to another site while the popup is open → the popup reads window.opener.origin → cross-origin SecurityError → the script aborts → the popup stays on "Connecting…" and does not close
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager)
- Decision: Defer (D10)
- Revisit when: a user reports a Strava popup stuck on "Connecting, please wait…", or the owner asks for D8's "opener navigated away" claim to be made true during U2 step 3
- Guard: —
- Override: user: Fix now — one try/catch in a file U2 already owns makes D8's claim true
- Outcome: fixed

Round 2 produced no Fix now decision from triage → review stops (REVIEW.md §6). R2-1 was overridden to Fix now by the owner and folded into U2 step 3.

## Unit U2 review — 2026-10-08, reviewed at 99739a8f (e3d53258..99739a8f, DELIVERY.md §5 point 3)

### U2-1 — Leaving Settings while the web popup is open silently loses the connect
- Trigger: a web user presses Connect, presses back out of Settings during Strava consent, then approves → the listener and in-memory verifier died with the Settings State → no complete, no message, Strava stays not connected
- Scores: trigger=plausible (triager, was concrete), impact=degraded-ux, detect=silent, later=cheap, fix=M/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: info log in strava_callback per web relay and in complete per link (no code/state/verifier); correct D7 wording to "stays alive while Settings is open"
- Override: user: Fix now — regression vs server-side linking; pending connect + popup listener move to app scope (U2 attempt 2)
- Outcome: fixed (17bb5332)

### U2-2 — A popup closed by the user, or blocked, never resolves connect()
- Trigger: a web user closes the Strava popup without approving, or the browser blocks it → no SnackBar; the listener lingers until the next Connect
- Scores: trigger=plausible (triager), impact=cosmetic (triager), detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a user reports Connect Strava doing nothing on web, or the connect click is reworked to open the popup before the await
- Guard: —
- Override: —
- Outcome: open

## Unit U1 review — 2026-10-08, reviewed at 652e2f0d (e3d53258..652e2f0d, DELIVERY.md §5 point 3)

Envelope question: the proxy-log exclusion rested on "a logged code is useless without the verifier", which U1-1 disproved. Owner's U1-1 decision (code binding) restores it, and it also stands on E3; the envelope was amended to say both.

### U1-1 — complete never ties the code to the state
- Trigger: a hostile app registered for traxjourney:// receives the victim's code → the attacker mints their own state and verifier and calls complete with the stolen code → the victim's Strava is linked to the attacker's account
- Scores: trigger=plausible, impact=security, detect=silent, later=expensive, fix=M/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: — (owner chose a DB table for the binding over an in-memory dict; plan D2/D4/D6/D9, envelope and boundaries amended)
- Outcome: fixed (e457f42f); residual prune-on-insert rebind window accepted by owner 2026-10-08 (plan D9)

### U1-2 — complete's upsert can 500 (StaleDataError) when a disconnect races it, orphaning new tokens
- Trigger: a user re-runs Connect on one device while pressing Disconnect on another → zero-row UPDATE → 500; the new Strava tokens are stored nowhere and never revoked
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a StaleDataError or 500 from complete in the logs, or the upsert is next changed
- Guard: —
- Override: user: Fix now — U1 is reworked anyway; use the module's _claim_token_row-then-INSERT idiom
- Outcome: fixed (e457f42f)

## Unit U1 review, round 2 — 2026-10-08, reviewed at e457f42f (652e2f0d..e457f42f, fixes only)

U1-1 and U1-2 confirmed closed by the reviewer.

Envelope question: does the hostile-app boundary cover attacker-initiated, forwarded app flows? Owner 2026-10-08: no. The boundary covers interception of flows the legitimate app started (RFC 8252 §8.1); the envelope was amended.

### U1R2-1 — Forwarded app-state link plus an attacker-controlled traxjourney:// app on the victim's phone
- Trigger: the attacker starts a ret=app flow, forwards the link; the victim, whose phone runs the attacker's scheme-claiming app, approves → the app relays code + state → the attacker completes with their own bearer and verifier → the victim's Strava is linked to the attacker
- Scores: trigger=plausible, impact=security, detect=silent, later=expensive, fix=L/local, confidence=verified
- Decision: Reject (D1) — outside the envelope as amended by the owner
- Revisit when: —
- Guard: —
- Override: — (follow-up #584: callback confirmation page)
- Outcome: open

## Integrated diff, round 1 — 2026-10-08, reviewed at 3e267258 (146c5884..3e267258)

### I1-1 — A Strava return that cold-started the app is replayed when Android recreates the activity
- Trigger: an Android user swipes the app away during consent, approves → a new task is rooted on the traxjourney:// intent → the connect completes → later the OS kills the process and the user reopens from Recents → the same intent is replayed → "not started here" SnackBar, and the app lands on Settings, on each such relaunch
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager)
- Decision: Defer (D10)
- Revisit when: a report of a spurious "not started here" message or an unexpected Settings landing after reopening; MainActivity.onCreate/takeIncomingFile or the strava-return route next changed; another custom-scheme route with side effects added
- Guard: —
- Override: user: Fix now — a misleading error for a user who is already connected, repeated on each reopen; fixed Dart-side as U4 (no Android SDK locally to compile/test a MainActivity change)
- Outcome: fixed (U4, 04633c51)

## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U1 | Server links Strava only via authenticated complete; code bound to its state | Opus | S4 | 2 | — | yes (both attempts) | U1-1, U1-2, U1R2-1, R1-3 |
| U2 | Client connect flow with verifier; web popup relay; callback page | Opus | S5 | 2 | — | yes (both attempts) | U2-1, U2-2, R1-4, R2-1 |
| U3 | Android custom-scheme return; waits for session restore | Opus | S5 | 1 | — | yes | R1-1, I1-1 |
| U4 | Replayed Android return does nothing (I1-1) | Sonnet | — | 2 | X3 (scope widened to app_router_redirect_test.dart) | yes | I1-1 |

Notes:
- Second attempts on U1 and U2 were review fixes (owner overrides U1-2 and U2-1; floor finding U1-1), not verification failures.
- U3 had no single-unit review (orchestrator call); the integrated review covered it and found I1-1.
- U4 fixed I1-1 on the Dart side rather than in MainActivity.kt: no Android SDK is available locally to compile or test a Kotlin change.
- Checks at the end: full pytest (CI command, minus test_rail_extract_workflow.py) 6905 passed / 0 failed on 81bcd8ed (server code unchanged afterwards); flutter analyze clean and flutter test 2611 passed on 3b358f58 (after merging origin/main); alembic heads: a8d3f5c2e917 only.
- Owed: graphify update from main after merge; owner device checks (Android adb command in docs/ANDROID.md, web popup in a real browser); publish GHSA-66h3 after the release reaches production.
