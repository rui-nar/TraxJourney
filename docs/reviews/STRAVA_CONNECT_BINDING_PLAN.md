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
