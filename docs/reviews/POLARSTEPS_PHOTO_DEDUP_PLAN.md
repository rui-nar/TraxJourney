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

### R1-1 — U1's three-key state breaks the reorder script and exact-state tests outside U1's scope
- Trigger: owner runs scripts/reorder_polarsteps_memory_photos.py --apply after this ships → `_write_reorder` passes a two-key dict to `dump_state` → KeyError on the first memory (or, with `.get`, the memory's sources silently wiped).
- Scores: trigger=concrete (triager, from plausible), impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open

### R1-2 — Cleanup script unlinks files before writing the row, and its missing-file rule then refuses to finish the job
- Trigger: owner's `--apply --api-stopped` run is interrupted after a duplicate's files were unlinked but before the row was written → the memory lists a UUID with no file (broken thumbnail, 404), and a re-run reports it as "file missing, never a duplicate" and leaves it.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (triager, from inferred)
- Decision: Defer (D10)
- Revisit when: an --apply run of dedupe_memory_photos.py is interrupted, or a later run reports a missing-file photo in a Polarsteps memory.
- Guard: —
- Override: —
- Outcome: open
