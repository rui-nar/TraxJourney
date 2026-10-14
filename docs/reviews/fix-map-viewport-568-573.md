# Review ledger — fix/map-viewport-568-573

Subject: branch fix/map-viewport-568-573 (#568 dropped refetch during in-flight one, #573 edit map refits on open)
Envelope: REVIEW.md defaults

## Round 1 — 2026-10-14, reviewed at f9d68b64

### R1-1 — A pan back into the old box during a box refetch is never served
- Trigger: owner at zoom 15 with box B1 loaded pans outside B1 (refetch for B2 starts), pans back inside B1 but outside B2 and stops → B2 lands and replaces B1; the tracks in view are drawn as coarse floor lines until the next camera move.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: a device report of floor lines after panning back during a refetch
- Guard: —
- Override: owner (delegated 2026-10-14, "trust your recommendations"): Fix now — the flaw is in this branch's own #568 fix, its new comment asserted the invariant this breaks, and #568's expected behaviour is that a pan made during a refetch is served; S/local.
- Outcome: fixed

### R1-2 — The #573 change exercises a first build with autoZoom on, which no production caller produces
- Trigger: no actor — `_autoZoom` starts false in app_screen.dart and view_screen.dart and only the AppBar button flips it; the edit panel's state survives layout switches via a GlobalKey.
- Scores: trigger=theoretical, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: open (the seed is kept as the pre-refactor guard restored; the PR states #573 was latent)
