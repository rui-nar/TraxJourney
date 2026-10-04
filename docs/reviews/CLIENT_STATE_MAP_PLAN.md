# Review ledger — Client state and the map (package C)

Subject: docs/CLIENT_STATE_MAP_PLAN.md
Envelope: the plan's `## Review envelope` section plus REVIEW.md §2 defaults

## Round 1 — 2026-10-04, reviewed at a97701a7

Envelope question raised: should mixed old/new builds accept "last whole-map PUT wins on every day" (R1-1), or should the plan retire the blind PUT for old builds?
Answer (user, 2026-10-04): retire it — option (b). The PUT answers 426, and a minimum-client-version gate is added for later changes (Decisions 12, 15; U20, U21). Table approved as triaged.

### R1-1 — An old build's PUT reverts a day another device PATCHed
- Trigger: old build opens Settings (snapshot) → new build PATCHes a note on day D → old build saves a colour change, PUT sends the stale whole map → day D's note is gone, no error
- Scores: trigger=plausible, impact=data-loss, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3) — becomes D1 if the user accepts last-writer-wins for mixed builds
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-2 — Adopting the PATCH response drops the in-memory gap days up to today
- Trigger: user on an active trip saves a note on the last activity's day → client replaces dayMeta with the response → carousel tiles after the last activity, including today, vanish until reload
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-3 — Wave 7 is not provably disjoint (W1)
- Trigger: U16-U18 run in parallel worktrees; U18's open-ended inventory or U17's unnamed tests overlap U16/U17 files → conflicting edits or an X3 stall
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-4 — The clear()-completeness test cannot catch a field added later
- Trigger: a later change adds a per-trip field and forgets clear() → a hand-listed getter test still passes → the field leaks from account A to account B on a shared device
- Scores (corrected by triage): trigger=theoretical, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D4), flagged to the user
- Revisit when: —
- Guard: source-scan test in U5 listing every instance field of project_notifier.dart and its mixins; fails unless reset in clear() or allow-listed with a reason. Decision 2 reworded to promise only that.
- Override: —
- Outcome: guard added

### R1-5 — No deadline means a stuck 'pending' segment is polled until the API restarts
- Trigger: worker killed mid-resolve while the API stays up → segment stays pending → every open session polls /meta every 60 s
- Scores (corrected by triage): trigger=plausible, impact=degraded-ux, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: hourly scheduler logs a warning listing route jobs pending/running more than 30 min after started_at
- Override: —
- Outcome: guard added

### R1-6 — U2 cites PUT date-key validation that does not exist
- Trigger: implementer invents strict ISO validation → a trip with a legacy odd key gets 400 on prune → no current path writes such keys
- Scores: trigger=theoretical, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open
