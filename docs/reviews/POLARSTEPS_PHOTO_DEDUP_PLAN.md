# Review ledger — Polarsteps photo dedup (#566)

Subject: docs/POLARSTEPS_PHOTO_DEDUP_PLAN.md
Envelope: plan § Review envelope, plus REVIEW.md defaults

## Round 1 — 2026-10-10, reviewed at b41aae9e

Fable reviewer, Opus triager. 2 findings, 1 envelope question.

Envelope question (to the owner, not triaged): Decision 2 assumes a
Polarsteps photo keeps the same URL path across two step fetches, only the
query varying. Nothing in the repo shows a real media URL. Confirm the shape
from a live step payload (scripts/inspect_polarsteps_steps.py), or key on the
sha256 of the fetched bytes instead.
Owner answer: key on the content (sha256 of the fetched bytes), plan
Decision 2.

### R1-1 — U1's three-key state breaks the reorder script and exact-state tests outside U1's scope
- Trigger: owner runs scripts/reorder_polarsteps_memory_photos.py --apply after this ships → `_write_reorder` passes a two-key dict to `dump_state` → KeyError on the first memory (or, with `.get`, the memory's sources silently wiped).
- Scores: trigger=concrete (triager, from plausible), impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan: U1's Scope gains the reorder script and its tests; Decision 5)

### R1-2 — Cleanup script unlinks files before writing the row, and its missing-file rule then refuses to finish the job
- Trigger: owner's `--apply --api-stopped` run is interrupted after a duplicate's files were unlinked but before the row was written → the memory lists a UUID with no file (broken thumbnail, 404), and a re-run reports it as "file missing, never a duplicate" and leaves it.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager, from inferred)
- Decision: Defer (D10), overridden to Fix now
- Revisit when: an --apply run of dedupe_memory_photos.py is interrupted, or a later run reports a missing-file photo in a Polarsteps memory.
- Guard: —
- Override: user: Fix now — a listed photo with no file is close to the corruption floor, and the fix is a few lines
- Outcome: fixed (plan: Decision 6 and U3 write the row first, then unlink)

## Round 2 — 2026-10-10, reviewed at 7efcf96b (fixes since b41aae9e)

Fable reviewer, Opus triager. 2 findings, 1 envelope question.

Envelope question (to the owner, not triaged): with the content key, a
Polarsteps step that holds the same photo twice (two media URLs, identical
bytes) imports as one photo, with only an info log. Decision 6 keeps manual
memories out because a user may upload the same photo twice on purpose.
Is one copy acceptable for Polarsteps steps?
Owner answer: one copy is fine. Added to the plan's envelope.

### R2-1 — U3 now depends on U1 but still sits in Wave 1, which DELIVERY.md W1 forbids
- Trigger: deliver-plan starts Wave 1 → U1 and U3 run at once in separate worktrees → U3 has no `hashes` in the state functions, and hand-writes the JSON or escalates; the unit is wasted.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (plan: U3 moved to Wave 2)

### R2-2 — Files left by an interrupted cleanup stay on disk and counted against the user's storage
- Trigger: owner's `--apply --api-stopped` run is interrupted after a memory's row commits and before its duplicates are unlinked → the files stay on disk and in the usage counter; every re-run reports them and does nothing.
- Scores: trigger=plausible, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a dedupe_memory_photos.py --apply run on val or prod reports any unlisted files, or a user asks about storage usage they cannot account for after the cleanup ran.
- Guard: —
- Override: —
- Outcome: open

## Unit review U1 — 2026-10-10, reviewed at f7157d40 (worktree-agent-ac8cca895fccb0c61)

Fable reviewer, Opus triager. 1 finding, no envelope questions. Every writer
of `photo_order_json` was traced through `load_state`; the format change reads
old rows as no hashes.

### U1R1-1 — has_hash answers True for content_hash=None whenever any listed photo carries no hash
- Trigger: no current caller. A future path calling `has_hash(state, photos, None)` on a memory holding a manual upload (no hash) gets True and skips a download as a duplicate; the user sees a photo missing, with only an info log.
- Scores: trigger=theoretical, impact=silent-wrong, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D4), overridden to Fix now
- Revisit when: —
- Guard: — (proposed: has_hash raises unless given a sha256 hex digest; superseded by the override)
- Override: user: Fix now — has_hash returns False for a missing or invalid hash rather than a guard
- Outcome: fixed (2a1ce9a9)
