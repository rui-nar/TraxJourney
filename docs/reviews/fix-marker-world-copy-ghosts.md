# Review ledger — ghost markers from duplicate world-copy keys

Subject: branch fix/marker-world-copy-ghosts (PR #578)
Envelope: REVIEW.md defaults

## Round 1 — 2026-10-08, reviewed at acda4b3b

### R1-1 — Keyed marker state follows world index 0, so the visible copy loses its thumbnail state when it changes world
- Trigger: A user pans across the 180° meridian over memories in Fiji, Tonga or eastern NZ → flutter_map renormalises the camera centre by ±360°, the single visible copy moves from world ±1 to world 0, its key changes, and the memory thumbnail is rebuilt and flashes its placeholder.
- Scores: trigger=concrete (reviewer: plausible; triager corrected), impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
