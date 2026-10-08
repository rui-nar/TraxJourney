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

## Round 2 — 2026-10-08, reviewed at da4ad3f3

### R2-1 — Plain key jumps to the main copy when it re-enters view, remounting the ±1 copy on screen
- Trigger: A user at about zoom 2 on a desktop-width browser sees memories only as world copy -1 (plain key), pans east, and the main-world copy enters at the right edge → the plain key moves to copy 0 and the copy they were watching is remounted; a thumbnail still loading or evicted from cache goes blank and re-fetches.
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
