# Review ledger — Trip-independent activities (#592)

Subject: docs/TRIP_INDEPENDENT_ACTIVITIES_PLAN.md
Envelope: plan § Review envelope, plus REVIEW.md defaults

## Round 1 — 2026-10-10, reviewed at f6b570a0

Fable reviewer, Opus triager. 10 findings, 2 envelope questions.

Envelope questions (to the owner, not triaged):
- EQ1-1: Decision 9 makes removal destructive for every editor. Today a companion removing someone else's activity only unlinks it and the row survives (owner decision 2026-10-03). Should removal by a non-owner stay an unlink, with the row deleted only by the owner or on trip deletion? Owner answer: yes, removal deletes the row for every editor (plan Decision 9).
- EQ1-2: the envelope accepts stale stats. Does it also accept stale public share tiles after the migration (see R1-8)? Owner answer: accepted (plan envelope).

### R1-1 — Decision 10 restores the wrong trip's head when the splitting trip is not the oldest reference
- Trigger: trip B added Strava activity X first; trip A added it later and split it. Migration → A's copy is restored to the full track over its own tail; B keeps the cut "(1/2)" head it never split; the tail's split ids name B's row.
- Scores: trigger=concrete, impact=data-loss, detect=silent, later=expensive, fix=M/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R1-2 — Split links that cross trips survive the migration; a reset in one trip then deletes another trip's rows
- Trigger: "Keep both" import, then removing only the split head from the copy; migration copies the tails but their split ids still name the head in the other trip → a later reset of that head deletes the copy's tails and leaves its items dangling.
- Scores: trigger=plausible (triager, from concrete), impact=data-loss, detect=silent, later=expensive, fix=M/shared, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R1-3 — A restored split head keeps the copied prepared-geometry blob of the cut track
- Trigger: a user opens a trip whose head the migration restored, at a simplified zoom → the track is drawn cut at the old split point; nothing rebuilds the blob.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R1-4 — Restore path undefined for an encrypted root — split_base_name is NULL and name is NOT NULL
- Trigger: an encrypted activity in two of the owner's trips, split in one → the migration writes name = NULL into a NOT NULL column, the lifespan migration aborts and the API does not start (or the name is blanked).
- Scores: trigger=plausible, impact=data-loss (triager, from wrong-visible), detect=logged, later=expensive (triager, from cheap), fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R1-5 — A companion's Strava token keeps serving the owner's trip after the companion leaves it
- Trigger: a companion adds their Strava activity to the owner's trip, then leaves or is removed; the owner presses "Re-fetch from Strava" → the server uses the former companion's token and pulls that account's current data into a trip they no longer belong to.
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified (triager, from inferred)
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R1-6 — Migration hands an encrypted row to an owner who cannot decrypt it and removes the repair path
- Trigger: user X adds an already-encrypted activity to Y's trip as a companion and does not reopen the app before deploy → the migration gives Y ownership of ciphertext under X's key; X's client repair then 404s, and nothing can decrypt it.
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=expensive, fix=S/local, confidence=verified (triager, from inferred)
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R1-7 — Negative-id rows with NULL source lose their "local" status under U8 and get no strava_id
- Trigger: a user imports a trip exported by another account (negative ids, source NULL) → after the new client ships, these rows show "Reset to Strava" and lose "Delete local activity".
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R1-8 — Public share tiles of trips the migration changed keep the pre-migration map
- Trigger: a trip with a share link whose head the migration restored; a visitor opens the public page → the disk tiles still draw the cut head until the trip is next changed.
- Scores: trigger=plausible (triager, from concrete), impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: the migration log reports a restored head in a trip with an active share link, or a stale public map is reported after deploy.
- Guard: —
- Override: —
- Outcome: open

### R1-9 — Rows no trip references survive the migration and the new audit flags them on every run
- Trigger: the owner runs the audit after deploy → every pre-existing unreferenced row is reported "held by zero trips" on every run, with no path that removes it.
- Scores: trigger=concrete, impact=maintainability, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R1-10 — The precedent the plan cites for chunking holds the whole result set in memory
- Trigger: U1's implementer follows b7f1a3c9d204 → a fetchall() of every shared row, which the envelope forbids.
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)
