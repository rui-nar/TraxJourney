# Review ledger — account deletion removes every row the account owns

Subject: branch fix/account-deletion-orphans
Envelope: REVIEW.md defaults; SQLite only (owner, 2026-09-28)

Decision taken before review (orchestrator, to confirm with the owner): poster
and video jobs a companion started on a trip whose owner deletes their account
are kept. They are the companion's rows, their files are in the companion's
folder, and video jobs count toward the companion's quota.

## Round 1 — 2026-10-03, reviewed at 7cbc41ec

### R1-1 — Route jobs are described as possibly a companion's, but every writer stamps the trip owner
- Trigger: a maintainer reads the comment and the companion fixture → believes a companion's route jobs exist and are deleted with the companion; no current path creates such a row
- Scores: trigger=theoretical, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: —

Round 1 produced no Fix now decision. Stop (§6).

## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U-A | Account deletion clears every owned row; migration cleans orphans | Opus | S4 | 1 | — | yes | R1-1 |
