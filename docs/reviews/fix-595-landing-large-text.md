# Review ledger — #595

Subject: branch fix/595-landing-large-text
Envelope: REVIEW.md defaults

## Round 1 — 2026-10-11, reviewed at 85b9777a

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 1 finding, well-formed.

### R1-1 — The 3x "no heading word is split" check skips headings _Heading shrank below 32 px
- Trigger: a developer adds or edits a display heading with an 11+ letter word (or changes _Heading's measurement) → on a 320 px phone at 3x the heading is drawn below 32 px, `_isHeading` (which reads the post-shrink fontSize) returns false, the heading leaves `_splitWords(headingsOnly: true)`, and the test passes while the heading breaks mid-word.
- Scores: trigger=plausible, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified (triager corrected from inferred)
- Decision: Guard (D9)
- Revisit when: —
- Guard: `_Heading` marks its text `Semantics(header: true)`; the 3x heading test finds headings by that role (6 expected) instead of by size, so a heading fitted to any size stays checked. Shown failing on a shrunk heading that still splits (halved measurement + "internationalisation").
- Override: user: guard by semantic role instead of the triaged 19–32 px size band — the band already holds correct text (28 px price, 20 px card titles and icons), so it would fail on correct code.
- Outcome: guard added

Round 1 produced no Fix now decision: review closed (§6).
