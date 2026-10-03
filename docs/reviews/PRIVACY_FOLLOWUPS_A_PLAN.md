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
