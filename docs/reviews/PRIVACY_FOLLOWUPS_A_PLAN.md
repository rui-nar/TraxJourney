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
