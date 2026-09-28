# Review ledger — fix/440-strava-disconnect

Subject: branch fix/440-strava-disconnect (issue #440)
Envelope: REVIEW.md defaults

## Round 1 — 2026-09-24, reviewed at ae9e733 (before docs/REVIEW.md; findings recorded after the fact)

### R1-1 — Revoke sent as a Bearer header to /oauth/deauthorize, not the documented form; Strava drops that endpoint on 2027-06-01
- Trigger: A user disconnects Strava → Strava answers 401, the failure is logged as a warning, the user gets 204 and the app stays authorized.
- Scores: trigger=concrete, impact=silent-wrong, detect=logged, later=cheap, fix=M/local, confidence=inferred
- Decision: Fix now (D3, F3)
- Override: —
- Outcome: fixed (POST /oauth/revoke, Basic auth, refresh token)

### R1-2 — Refresh-then-revoke discards a rotated refresh token when the revoke fails
- Trigger: An expired token is refreshed, then the revoke times out → the only copy of the new refresh token is deleted and nothing can revoke the app.
- Scores: trigger=plausible, impact=silent-wrong, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3)
- Override: —
- Outcome: fixed (refresh step removed)

### R1-3 — DB transaction held across up to ~40 s of Strava I/O
- Trigger: Strava is slow during a disconnect → a pooled connection is pinned for the whole call.
- Scores: trigger=plausible, impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (pre-policy)
- Override: —
- Outcome: fixed in the disconnect route; in account deletion see the rebase note

### R1-4 — An in-flight sync can recreate the Strava cache row after disconnect
- Trigger: A user starts a refresh, then disconnects → _save_cache writes the cache for the disconnected user.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3)
- Override: —
- Outcome: fixed

### R1-5 — The server writes users' Strava tokens to ~/.config/traxjourney/tokens.json; disconnect never clears it
- Trigger: The server refreshes a token → a live token sits on disk under a shared key after disconnect.
- Scores: trigger=concrete, impact=security, detect=silent, later=cheap, fix=M/shared, confidence=verified
- Decision: Out of this change's scope (pre-existing); filed as a follow-up issue
- Override: —
- Outcome: open (#508)

### R1-6 — Strava activities in no trip survive a disconnect
- Trigger: A user removed a Strava activity from every trip, then disconnects → the row stays.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=M/local, confidence=verified
- Decision: Out of this change's scope (pre-existing); filed as a follow-up issue
- Override: —
- Outcome: open (#509)

## Rebase note — 2026-09-28

Rebased onto origin/main 3dec5e35. Account deletion now runs the #441 billing
settlement first. The Strava revoke is placed after refund_inside_window and
before lock_account: a refused deletion leaves Strava connected, and no lock is
held across the call. The no-op sess.commit() from ef55a62 was dropped there,
matching the billing calls, which also hold only a read transaction across the
network.

## Round 2 — 2026-09-28, reviewed at 05317ca2 (fixes since ae9e733, rebased onto 3dec5e35)

### R2-1 — Disconnect revokes the refresh token the DB holds, but an in-flight fetch may already have rotated it
- Trigger: A user whose access token expired opens the Strava browser (paginated fetch refreshes the token, persisted only after the last page) and disconnects meanwhile → the old refresh token is revoked, Strava answers 200, the app stays authorized, nothing logged.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified (triager: code path read end to end; Strava's rotation and 200-on-unknown-token from its docs)
- Decision: Fix now (D3, F3). The rotated token lives only in the fetch's memory, so the fix must persist the rotation as soon as it happens, or revoke the new token when _save_refreshed_token finds the row gone.
- Override: —
- Outcome: fixed (StravaAPI.on_token_refresh persists the rotation the moment it happens, by an UPDATE whose rowcount 0 means "disconnected meanwhile" and revokes the new tokens; _save_refreshed_token removed)

### R2-2 — The R1-4 _save_cache guard is not atomic; a disconnect can commit between the token check and the cache insert
- Trigger: An in-flight fetch passes the token check → the disconnect commits → _save_cache inserts a fresh cache row → the raw Strava list is kept after the user was told it was removed.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3). Take the write lock first, then check the token.
- Override: —
- Outcome: fixed (a no-op UPDATE of the token row is the session's first write and its check; rowcount 0 rolls back)

### R2-3 — The billing_changed refusal comes after the Strava revoke, leaving a revoked token on a surviving account
- Trigger: A Stripe webhook lands during an account deletion's unlocked window → the revoke has gone out → 409 billing_changed, nothing removed → status says connected, the next fetch asks the user to re-authenticate.
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: A billing_changed 409 is seen in logs or support, a user reports re-authentication prompts after a refused deletion, or delete_user_and_data's step order changes.
- Override: —
- Outcome: open

## Round 3 — 2026-09-28, reviewed at d904438e (fixes since 05317ca2)

### R3-1 — Disconnect and account deletion delete a token row a concurrent refresh may have rotated after the revoke
- Trigger: A user whose access token expired opens the Strava browser (the first call refreshes) and clicks Disconnect during that refresh → the old refresh token is revoked (Strava answers 200), the callback commits the rotated tokens before the delete, and the row holding the new tokens is deleted unrevoked; the app stays authorised, nothing logged. Same gap in delete_user_and_data.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=L/local (triager corrected from M), confidence=verified
- Decision: Fix now (D3, F3). New evidence of the R2-1 class; not a duplicate.
- Override: user: Fix now, with a round-4 review limited to this fix (2026-09-28)
- Outcome: fixed (disconnect claims the row, reads it under the claim, deletes, and revokes the row's tokens after the commit when its refresh token is not the one revoked; delete_user_and_data re-reads under lock_account and revokes after its final commit)

Envelope question from the reviewer (enrichment builds a StravaAPI without the rotation callback): filed as #512.
