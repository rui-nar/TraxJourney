# Review ledger — fix/467-trip-name-race-409

Subject: branch fix/467-trip-name-race-409 (issue #467)
Envelope: REVIEW.md defaults

## Round 1 — 2026-09-27, reviewed at 7a28ec97

### R1-1 — A refused rename leaves the confirmed day prune applied to the still-open settings screen, so a later save deletes notes without asking
- Trigger: The owner types a trip name they already use, sets an end date that orphans days with notes and confirms "Delete" → the rename is refused (409) and the screen now stays open with `_dayMeta` already pruned → the owner moves the end date back to keep those days and saves → no dialog appears, and saveDayMeta sends the pruned map → the notes on those days are deleted with nothing saying why.
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, floor F2)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
- Fix note (triager): prune a copy that goes only to saveDayMeta, or run the rename before the orphan check and the prune.

## Round 2 — 2026-09-28, reviewed at 75f6a783 (fixes since 7a28ec97)

No findings.
