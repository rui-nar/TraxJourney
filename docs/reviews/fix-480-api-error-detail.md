# Review ledger — #480

Subject: branch fix/480-api-error-detail
Envelope: REVIEW.md defaults

## Round 1 — 2026-10-10, reviewed at 229680b7

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 1 finding, well-formed.

### R1-1 — The share-link card never shows the detail the rewritten throws now carry
- Trigger: an editor on a shared trip (or an owner during a 409/5xx) presses "Create link" / "Revoke" under Sharing → the share endpoints need co-owner, the server answers 403, and the button does nothing visible; the message is thrown out of the onCreateLink/onRevoke callbacks (project_settings_screen.dart:846-855, 891-900) with no try/catch and only reaches the unhandled-async-error log.
- Scores: trigger=concrete, impact=degraded-ux, detect=logged, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

## Round 2 — 2026-10-10, reviewed at 5d7a8672 (fixes since 229680b7)

Reviewer: adversarial-reviewer (Opus; Fable weekly limit reached). No findings: round clean, review closed (§6).
