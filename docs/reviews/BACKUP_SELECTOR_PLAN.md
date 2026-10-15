# Review ledger — Backup drop-down selector (#625)

Subject: docs/BACKUP_SELECTOR_PLAN.md
Envelope: plan section "Review envelope" (REVIEW.md defaults)

## Round 1 — 2026-10-15, reviewed at a1fd2c11

### R1-1 — U1 names only one existing test to update; another asserts the exact date text the `date · size` item replaces
- Trigger: implementer applies U1 as written → `loads backups when admin status arrives after the screen is up` fails on `find.text('2026-01-01')` (L133), item text is now `2026-01-01 · 2 KB`; Latitude none forces escalate-or-bend. Non-admin `find.text` at L104 turns vacuous.
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
