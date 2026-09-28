# Review ledger — fix/431-visitor-email

Subject: branch fix/431-visitor-email (issue #431)
Envelope: REVIEW.md defaults

## Round 1 — 2026-09-24, reviewed at 5672d62 (before docs/REVIEW.md; findings recorded after the fact)

### R1-1 — visitor_key test asserts on random hex content and fails under half of all secrets
- Trigger: CI or a dev shell exports a different JWT_SECRET → test_share_visitor_key_is_stable_pseudonymous_and_owner_scoped fails at random (4/8 secrets).
- Scores: trigger=concrete, impact=maintainability, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed

### R1-2 — The privacy-policy draft (#427) still says owners see visitors' email addresses
- Trigger: A reader of the policy in PR #427 is told owners see their email → the policy contradicts the code once #431 merges.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6), in PR #427, not this branch
- Override: —
- Outcome: open (coordinator updates #427)

### R1-3 — Docstring claims owners cannot pool visitor stats, though display_name and avatar_url are globally stable
- Trigger: A maintainer relies on the docstring's unlinkability claim → the privacy property is overstated.
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Override: —
- Outcome: fixed

### R1-4 — Companions see a user's email when that user has no display name (members.py:136, projects.py:160)
- Trigger: A user with no display name joins a trip via a forwarded invite → every member sees their email.
- Scores: trigger=concrete, impact=security, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Out of this change's scope (pre-existing, untouched code); filed as a follow-up issue
- Override: —
- Outcome: open (follow-up issue)

### R1-5 — Rotating JWT_SECRET silently relabels every visitor_key
- Trigger: The admin rotates JWT_SECRET → nameless visitors get new labels.
- Scores: trigger=plausible, impact=cosmetic, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (one-line docstring note, pre-policy)
- Override: —
- Outcome: fixed

## Round 2 — 2026-09-28, reviewed at e286445e (fixes since 5672d62)

No findings. Review stops (§6).
