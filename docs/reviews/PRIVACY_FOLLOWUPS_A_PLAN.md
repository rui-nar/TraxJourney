# Review ledger — PRIVACY_FOLLOWUPS_A_PLAN

Subject: docs/PRIVACY_FOLLOWUPS_A_PLAN.md (issues #470, #507, #508, #509, #510, #512)
Envelope: the plan's "Review envelope" section (REVIEW.md defaults + SQLite only, U6 concurrency, U5 startup work)

## Round 1 — 2026-10-03, reviewed the plan as first written (uncommitted draft)

### R1-1 — U3 leaves the email visible when the display name is the email (legacy auto-created profiles)
- Trigger: A local user with no UserInfo row signs in → login() auto-creates UserInfo(display_name=username), and the username is the email since #110 → the members list, invite preview, invite email and owner_name show the email.
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified (triager: reachable via register()'s two commits or pre-UserInfo accounts)
- Decision: Fix now (D3, F1). The auto-created row has email="", so comparing against UserInfo.email does not work: compare against LocalUser.username, or make the auto-create set email=username and leave the name blank.
- Override: —
- Outcome: fixed (plan amended; passage tagged with this id)

### R1-2 — U5 searches only current members' folders; an avatar uploaded by a companion who has left stays unreachable
- Trigger: A companion uploads a person's avatar, then leaves before the release → the sweep finds it nowhere, logs a warning, the avatar 404s.
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: U5's "avatar found nowhere" WARNING appears in val or prod logs after the first start on the new release, or a user reports a broken avatar on a shared trip.
- Override: —
- Outcome: open

### R1-3 — U6 removal path filters on the wrong user, so a companion-imported row in a shared trip is never deleted
- Trigger: An owner removes a Strava activity a companion imported → the method filtered on the caller's id matches nothing → the row stays orphaned.
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (plan amended; passage tagged with this id)

### R1-4 — U6 misses trip deletion
- Trigger: A user deletes a trip → its items go, its Strava activity rows stay in no trip.
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local (triager corrected from M), confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (plan amended; passage tagged with this id)

### R1-5 — U1 cannot tell expired, too-early and wrong-audience apart by exception type
- Trigger: The U1 implementer maps reasons from exception types → three reasons share InvalidValue, two share MalformedError → the escalate clause fires on day one.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7). Classify on fixed keywords in str(exc); log only the class name and the mapped reason.
- Override: —
- Outcome: fixed (plan amended; passage tagged with this id)

### R1-6 — A split root kept while its tails remained is never reconsidered when the last tail goes
- Trigger: A user removes the head piece of a split Strava activity, then its last tail → the root stays orphaned.
- Scores: trigger=concrete (triager corrected from plausible), impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (plan amended; passage tagged with this id)

## Round 2 — 2026-10-03, reviewed the round-1 amendments at 4d0a6826

### R2-1 — The trip-deletion rule (R1-4) never frees a split Strava activity: the trip's tails stay and keep naming the root
- Trigger: A user splits a Strava activity in a trip, then deletes the trip → delete_project keeps the tail rows → the cleanup keeps the root because the tails name it → root, tails and their prepared geometry stay for good.
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7). New evidence that the R1-4 fix does not hold for split activities; not a duplicate.
- Override: —
- Outcome: fixed (plan amended; passages tagged R2-1)

## Round 3 — 2026-10-03, reviewed the R2-1 amendment at 5e175550 (the policy's cap)

### R3-1 — The trip-delete tail purge does not re-parent a surviving tail-of-tail; a later reset of the root restores the track over it
- Trigger: Split family R → T1 → T2 with T2 also in a second trip; deleting the first trip deletes T1 and keeps T2 pointing at it → resetting R misses T2 → the stretch is counted twice, unwarned.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3) if a tail held by two trips is a supported state; Reject (D1) if not (envelope question to the owner).
- Override: user: split tails belong to one trip (2026-10-03) → Reject (D1); the plan acceptance case that asserted a multi-trip tail is dropped
- Outcome: — (rejected)

### R3-2 — Surviving split pieces are not renumbered after the trip-delete purge
- Trigger: A root kept by another trip shows "Ride (1/3)" as the only piece left.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6). Triager note: main already shows this today; the purge is the natural place to fix it.
- Override: —
- Outcome: fixed (plan amended; passage tagged R3-2)

### R3-3 — Ids are collected outside delete_project's transaction; a split or add committed in between is never collected
- Trigger: A companion splits an activity while the owner deletes the trip → the new tail is orphaned and pins its root.
- Scores: trigger=plausible, impact=maintainability, detect=silent, later=cheap, fix=S/shared, confidence=verified
- Decision: Guard (D9): warn when a root is kept only by unreferenced tails.
- Override: —
- Outcome: guard specified in the plan (tagged R3-3 guard)

### R3-4 — The tail filter (activity_rewritable_by_trip) and the root rule (any owner, R1-3) disagree for a former member's split family
- Trigger: A companion imports and splits an activity, leaves; the owner deletes the trip → tails aren't collected and pin the root forever.
- Scores: trigger=concrete (triager corrected from plausible), impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed (plan amended; passage tagged R3-4)

Triager's recommendation: all four come from R2-1 re-implementing part of delete_local_activity. Either (a) reuse delete_local_activity inside delete_project's transaction (removes the class; shared risk; needs its own review), or (b) take split families out of trip-delete pruning for this package (revert the R2-1 amendment, reopen R2-1 as Defer).

Owner decisions 2026-10-03: patch the four findings rather than revert R2-1, which authorises a round 4 limited to these amendments; split tails belong to one trip (Review envelope).

## Round 4 — 2026-10-03, reviewed the round-3 amendments at f61be8ab (owner-authorised)

### R4-1 — A former member's split tail removed from the timeline is refused by activity_rewritable_by_trip and pins its root; the R3-3 warning then fires with no race
- Trigger: A companion imports and splits a Strava activity in a shared trip, then leaves → the owner removes the tail's item → the tail is not deleted (not rewritable) and its root is never re-checked → after a trip delete the R3-3 warning logs, indistinguishable from the race, and root and tail stay.
- Scores: trigger=concrete, impact=maintainability, detect=silent (triager corrected from logged), later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7). Step 3 frees a Strava-family tail under the same ownership rule as step 4 (R3-4), via delete_local_activity without the rewritable gate.
- Override: —
- Outcome: fixed (plan amended; passage tagged R4-1)

Owner decision 2026-10-03: apply R4-1 and stop plan reviews; U6's code review after delivery covers it. The plan is approved for delivery.

## Unit U4 review — round 1, 2026-10-03, reviewed at d6e00db3 (DELIVERY.md §5 point 3)

### U4-R1-1 — Two concurrent avatar uploads for one person leave the losing upload's files on disk, charged to the owner
- Trigger: Two editors (or a client retry) upload the same person's avatar at once → both write and charge the owner, both read the same old avatar, the second commit wins → the first upload's files stay, referenced by nothing, counted in the owner's storage.
- Scores: trigger=plausible, impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Guard: after the old-avatar cleanup, list the person's avatar folder and log a WARNING naming any file that isn't the new avatar's pair.
- Override: —
- Outcome: guard added (62be0639; warning names stray files in the avatar folder; tested)

### U4-R1-2 — A non-HTTP failure after the files are written leaves them on disk, charged to the owner
- Trigger: The row update fails with e.g. "database is locked" → 500, the written files stay counted; a retry doubles the cost.
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: A non-HTTP exception from upload_avatar shows up in val or prod logs, the U4-R1-1 warning fires, or a user reports storage usage that doesn't match their content.
- Override: —
- Outcome: open

## Unit U2 review — round 1, 2026-10-03, reviewed at 7b8ae342 (DELIVERY.md §5 point 3)

### U2-R1-1 — A rotation whose DB write fails mid-enrichment keeps running on the new tokens while the row keeps the retired refresh token
- Trigger: An import's enrichment refreshes an expired token while SQLite stays locked past busy_timeout → _persist_rotated_token raises, enrichment logs and continues → the row keeps the retired token; the next Strava action asks to re-authenticate, and a disconnect revokes the retired token while the app stays authorised.
- Scores: trigger=plausible, impact=wrong-visible, detect=logged, later=cheap, fix=S/shared, confidence=verified
- Decision: Defer (D10). The real fix is _persist_rotated_token retrying or failing loudly for every caller (api/strava.py), outside U2's Scope.
- Revisit when: A log shows _persist_rotated_token / _claim_token_row failing ("database is locked") in val or prod; a user reports Strava still listing the app after disconnecting; a user is asked to reconnect soon after an import.
- Override: —
- Outcome: open

## Unit U6 review — round 1, 2026-10-03, reviewed at 22999e85 (DELIVERY.md §5 point 3)

### U6-R1-1 — A Strava re-fetch job in flight recreates the row U6 just deleted
- Trigger: A user starts a re-fetch, then removes the activity from its only trip, deletes the trip or disconnects → U6 deletes the row → force_update_activity finds no row and inserts a fresh one no item references.
- Scores: trigger=plausible, impact=maintainability, detect=silent, later=cheap, fix=S/shared, confidence=verified
- Decision: Guard (D9)
- Guard: WARNING in force_update_activity's insert branch ("row deleted while the re-fetch was in flight"); its only production caller runs on a row marked pending, so it fires only on this race.
- Override: —
- Outcome: guard added (fb4c0e04). Delivery found the insert branch never commits (_upsert_activity only sess.add, the refresh session closes without commit), so the row is not actually recreated today; the warning logs the attempt.

Envelope question from the reviewer, owner answer 2026-10-03: removal (and the R4-1 tail rule) deletes a row only when the caller owns that activity or is the trip owner. An editor removing someone else's activity only unlinks it; the owner's later disconnect or own removal cleans it up. Trip deletion is done by the trip owner, so it keeps the any-owner rule (R1-4, R3-4).

Owner approval 2026-10-03: U4-R1-1 Guard, U4-R1-2 Defer, U6-R1-1 Guard, as triaged.
