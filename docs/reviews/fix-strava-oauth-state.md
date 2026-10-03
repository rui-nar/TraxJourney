# Review ledger — purpose-bound Strava OAuth state

Subject: branch fix/strava-oauth-state
Envelope: REVIEW.md defaults

## Round 1 — 2026-10-03, reviewed at 8fb34f9c

### R1-1 — The OAuth state is not bound to the browser that started the flow
- Trigger: a signed-in user forwards a Strava connect link they generated → another person approves it on Strava → that person's Strava account is linked to the first user's account
- Scores: trigger=plausible, impact=security, detect=silent, later=expensive, fix=L/shared, confidence=verified
- Decision: Fix now (D3). Not fixable inside this change: it predates it, and binding the flow needs the web and mobile clients to take part in completing OAuth (shipped clients, E5), a design change that is the owner's decision. Tracked privately.
- Revisit when: —
- Guard: —
- Override: pending owner decision
- Outcome: open

### R1-2 — An expired state is reported as `invalid_state`, with no server log
- Trigger: a user spends more than 10 minutes signing in on Strava's side → the callback reports `invalid_state` and nothing is logged, so expiry and forgery look the same
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (78a7bcac: `reason=state_expired`; other refusals log a warning without the token)

## Round 2 — 2026-10-03, reviewed at 78a7bcac

No findings. Stop (§6).

## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U-B | Strava OAuth state no longer carries a session token | Opus | S5 | 2 | — | yes | R1-1, R1-2 |
