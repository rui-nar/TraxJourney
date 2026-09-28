# Review ledger — fix/442-drop-heart-rate

Subject: branch fix/442-drop-heart-rate (issue #442)
Envelope: REVIEW.md defaults. Owner decision: drop heart-rate data completely (no storage, no payloads, existing values deleted).

## Round 1 — 2026-09-28, reviewed at 5d428a58 (whole change against 3dec5e35)

### R1-1 — Two test modules from #501 still build Activity with the removed has_heartrate kwarg
- Trigger: CI runs pytest on the branch → every _activity() in test_video_timeline.py and test_video_renderer.py raises TypeError; the PR cannot go green.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed

### R1-2 — The migration reads every Strava cache blob into memory at once, at API startup
- Trigger: The owner deploys to a VPS whose stravacache holds one full-history blob per user → alembic upgrade fetchall()s them all; past the container's memory the process is killed, the migration rolls back and the API stays down.
- Scores: trigger=plausible, impact=wrong-visible, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: Before the prod deploy, if SELECT SUM(LENGTH(activities_json)) FROM stravacache on val or prod is a large share of the API container's memory limit, or the migration is OOM-killed on val.
- Override: user: Fix now — a startup migration on a memory-tight VPS; precedent 6c1f0e9a2b47 reads one row at a time
- Outcome: fixed

### R1-3 — The .traxj import validator still types and bounds the five heart-rate fields the import now discards
- Trigger: A user uploads a hand-edited or third-party .traxj with "max_heartrate": 5000 → 400 naming a field the app throws away.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10). The same refusal existed before this change; app-written files are unaffected.
- Revisit when: A user reports a .traxj refused over a heart-rate field, or traxj_schema.py / value_bounds.py is next edited.
- Override: —
- Outcome: open

Envelope questions, owner answers 2026-09-28: (1) the 30 daily backups keep heart-rate values for up to 30 days after deploy — accepted, stated in the privacy policy (#427); (2) native clients' on-device project caches keep the old values until the trip's lock_version advances — the migration advances lock_version on every project holding an activity with a heart-rate value, before the drop.

## Round 2 — 2026-09-28, reviewed at ea543f7d (fixes since 5d428a58)

No findings. Review stops (§6).
