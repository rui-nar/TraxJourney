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

## Round 2 — 2026-10-10, reviewed at 565d425d (fixes since f6b570a0)

Fable reviewer, Opus triager. 6 findings, no envelope questions.

### R2-1 — Backfill runs after Copy, so every copy gets strava_id = its fresh random id and strava_user_id = the trip owner
- Trigger: a Strava ride in two trips → the migration copies it (strava_id still NULL, owner overwritten), then backfills strava_id = id → the copy's strava_id is not a Strava id; refresh 404s, sync re-adds a duplicate.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=expensive, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R2-2 — "Head and none of its tails" also matches a head whose tails the splitting trip deliberately deleted
- Trigger: a user splits a ride and removes the tail; the head keeps split_base_name and its snapshot → the migration restores the full track over the piece the user kept.
- Scores: trigger=concrete, impact=silent-wrong, detect=logged, later=expensive, fix=M/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: — (owner decision on the open case: when no trip holds a tail, leave every head as it is, logged)
- Outcome: fixed (plan)

### R2-3 — A head that was split and later reset still carries split_base_name but has no snapshot
- Trigger: a user splits a shared activity, then resets the head (snapshot nulled, split_base_name kept) → the restore has no snapshot: it aborts the migration or writes NULL over good geometry.
- Scores: trigger=concrete, impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R2-4 — Dangling items are deleted after Copy, so step 1 enumerates ids that have no row
- Trigger: "Keep both" plus a reset in one trip leaves the other's items on a deleted tail → step 1 treats the missing id as shared and tries to copy a row that does not exist.
- Scores: trigger=plausible, impact=wrong-visible, detect=logged, later=expensive (triager, from cheap), fix=S/local, confidence=verified
- Decision: Fix now (D5)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R2-5 — U7 does not say what account deletion does with encrypted rows kept in other users' trips
- Trigger: a companion's encrypted activity in the owner's trip keeps the companion as owner; the companion deletes their account → a dangling item, or unreadable ciphertext owned by a deleted user.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: — (owner approved the extended privacy sentence)
- Outcome: fixed (plan)

### R2-6 — An exception row repaired to plaintext stays owned by the companion; the owner can never encrypt it
- Trigger: the companion's client decrypts the exception row to plaintext → it stays owned by the companion; the trip owner's encrypt 404s on every pass, and the audit flags it on every run.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=expensive (triager, from cheap), fix=S/local, confidence=verified (triager, from inferred)
- Decision: Fix now (D5)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

## Round 3 — 2026-10-10, reviewed at c050381d (fixes since 565d425d)

Fable reviewer, Opus triager. 2 findings, no envelope questions. This is the
last round under the cap (§6).

### R3-1 — Step 3 only lists heads held by two or more trips, so a split whose head and tails ended up in different trips is never repaired
- Trigger: the same ride in trips A and B; split in A, then the head removed from A (its row kept because B references it) → B keeps the cut "(1/2)" head and A's tail is detached, although A holding the tail is the evidence. Mirror: A and B hold the head and only C holds the tails.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=expensive, fix=S/local, confidence=verified
- Decision: Fix now (D5)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

### R3-2 — Step 6's restore leaves elevation_profile_low_res_json as the cut head's downsample
- Trigger: a user opens a trip whose head the migration restored → the map shows the full track but the elevation chart is still the cut one; nothing re-derives the low-res profile.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=expensive, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

## Round 4 (owner-requested) — 2026-10-10, reviewed at 56ea6000 (fixes since c050381d)

Fable reviewer, Opus triager. 1 finding, no envelope questions.

### R4-1 — The precedent cited for an inline downsample imports the app's helper instead
- Trigger: U1's implementer follows the plan → d1e2f3a4b5c6 imports `src.project.elevation_downsample.downsample_elevation`; they either hand-copy the LTTB into the migration (which then drifts from the app) or import it against the plan's stated rule.
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan)

## Unit review U1 — 2026-10-11, reviewed at a9459f8f (worktree-agent-a4096de40ba92d331)

Fable reviewer, Opus triager. 1 finding, 1 envelope question.

Envelope question EQ-U1-1 (to the owner, not triaged): a "Keep both" copy
of the user's own split trip, where the user then removed the tail from the
copy on purpose, looks to the migration exactly like a trip that never split
the activity (it holds the head and no tail, and another trip holds tails). The
migration restores the full track in that copy. Accept, or treat it
differently?
Owner answer: accepted (2026-10-11) — the restore stands; every restore is logged per trip.

### U1R1-1 — A tail whose parent chain dead-ends on a deleted row is detached although the trip holds its split_root_id
- Trigger: no current code path produces it. A trip holding head H and a tail whose `split_parent_id` names a missing row → the walk returns None and the migration permanently clears the tail's split columns although the trip holds H.
- Scores: trigger=theoretical, impact=data-loss (triager, from degraded-ux), detect=logged, later=expensive, fix=S/local, confidence=verified
- Decision: Guard (D4), approved by the owner
- Revisit when: —
- Guard: WARNING log plus a summary counter when a tail is detached although its own root is held. (e173a2a1)
- Override: —
- Outcome: guard added (e173a2a1)

## Unit review U3 — 2026-10-11, reviewed at f03c21c4 (u592/u3, U2's files excluded)

Fable reviewer, Opus triager. 2 findings, no envelope questions.

### U3R1-1 — Trip deletion leaves a split root the trip kept for its tails as a permanent orphan row
- Trigger: a user splits a ride, removes the head from the timeline while keeping the tails (the head row is kept because the tails name it), then deletes the trip → the tails go, the head row and its prepared blob stay forever, named by nothing. Same class: a reset whose split walk sees only held rows leaves an unlinked tail orphaned and still naming the head.
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (d63fafd3)

### U3R1-2 — Every heavy project load parses the full edit snapshot of every edited row, although only the export reads it
- Trigger: any heavy load or retried save of a trip with many split pieces parses one extra elevation profile per edited row and discards it.
- Scores: trigger=concrete, impact=maintainability (triager, from degraded-ux), detect=silent, later=cheap, fix=M/shared (triager, from S/local), confidence=verified (triager, from inferred)
- Decision: Defer (D8)
- Revisit when: a profile of a heavy get_project or save_project_with_retry on a split-heavy trip shows the original_* parsing as a measurable share of load time, or the #401 device measurement points at server-side load time on split-heavy trips.
- Guard: —
- Override: — (owner approved the table 2026-10-11)
- Outcome: open

## Unit review U7 — 2026-10-11, reviewed at 5fa118b4 (u592/u7)

Fable reviewer, Opus triager. 2 findings, no envelope questions.

### U7R1-1 — Withdrawing a companion's encrypted activity leaves the owner's trip stats counting it
- Trigger: a companion who contributed an encrypted activity deletes their account → its item is removed and lock_version bumped, but stats_json is not cleared → the owner's totals still include it until the trip is next edited.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: — (owner approved 2026-10-11)
- Outcome: fixed (da4890b7)

### U7R1-2 — An encrypted split head kept for pieces another account owns is handed to that account instead of deleted
- Trigger: a companion's encrypted split family in the owner's trip, one tail already decrypted and handed over (R2-6); the companion deletes their account → the head row (unreadable ciphertext) is re-owned to the trip owner instead of deleted, against Decision 4 and the privacy sentence O1.
- Scores: trigger=plausible, impact=maintainability, detect=logged, later=expensive (triager, from cheap), fix=M/local, confidence=verified
- Decision: Fix now (D5)
- Revisit when: —
- Guard: —
- Override: — (owner approved 2026-10-11)
- Outcome: fixed (da4890b7)

## Unit review U4 — 2026-10-11, reviewed at b668db0d (u592/u4)

Fable reviewer, Opus triager. 2 findings, no envelope questions. The triager
noted the same gate in api/project_items.py:114 (U6's file): there removal
unlinks the item and leaves the row orphaned. Passed to U6 as part of its
EQ1-1 work.

### U4R1-1 — Delete-local still runs the trip-rewrite gate for non-Strava rows, so the owner is refused on a departed companion's encrypted local row
- Trigger: a companion's encrypted GPX import kept with them by the migration (Decision 4 exception); the companion leaves; the owner presses "Delete local activity" → 404 "Activity not in project", and the row stays.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10), overridden to Fix now
- Revisit when: the U1 migration log lists an encrypted row kept with a companion owner who is no longer owner or member, or a user reports "Activity not in project" when deleting a local activity.
- Guard: —
- Override: user: Fix now — EQ1-1: any editor's removal deletes the trip's row; delete-local must match timeline removal
- Outcome: fixed (51915f5f, f2bb9ce6)

### U4R1-2 — Refresh job marks the row "resolved" when force_update_activity silently declines to write it
- Trigger: the owner refreshes a companion's activity; the companion deletes their account mid-job → force_update_activity skips the write silently, and the job reports success with the old data.
- Scores: trigger=plausible, impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: WARNING log on both silent early exits of force_update_activity (strava_user_id mismatch; trip no longer holds the row).
- Override: — (owner approved 2026-10-11)
- Outcome: guard added (51915f5f)

## Unit review U5 — 2026-10-11, reviewed at 9c02faf6 (u592/u5)

Fable reviewer. No findings, no envelope questions. (The Project.add_activities
id comparison was already known and is scheduled as a follow-up fix unit.)

## Unit review U6 — 2026-10-11, reviewed at 06e0d5f1 (u592/u6)

Fable reviewer, Opus triager. 1 finding, no envelope questions.

### U6R1-1 — U6's GPX export writes the Strava identity, but only U4's import matches it
- Trigger: only if U6 were integrated without U4 → a GPX re-import into its own trip would duplicate Strava tracks. U4 and U6 are integrated together, U4 first.
- Scores: trigger=theoretical (triager, from concrete), impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: — (recorded in the delivery record as an unlisted U6-on-U4 dependency)

## Unit review U10 (follow-up unit) — 2026-10-11, reviewed at 14f0d05b (u592/u10)

Fable reviewer. No findings, no envelope questions. U10 (found in U5's report):
an added activity is matched to the trip by strava_id, never by row id. The
save path drops, with a WARNING, an activity whose id names a held row but
whose strava_id differs. That is unreachable from current code, and stored
data is identical either way, so the drop is kept as the guard; raising
would abort the whole save.
