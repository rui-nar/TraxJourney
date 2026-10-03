# Review ledger — console email backend logs recipient and subject only

Subject: branch fix/console-email-body
Envelope: REVIEW.md defaults

Envelope question (R1, open for the owner): is a self-hosted deployment without
`SMTP_HOST` inside E1? Without SMTP, email verification and invites now have no
supported path; README now says so instead of calling the console backend a
working substitute.

## Round 1 — 2026-10-03, reviewed at ad8f1036

### R1-1 — docs/LOGGING.md still allows the console backend to log the full body
- Trigger: a maintainer reads the logging policy's console-backend exception → re-adds body logging as allowed by policy
- Scores: trigger=concrete, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed (LOGGING.md and README.md updated, docstring no longer claims html_body is logged)

## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U-D | Console email backend stops logging bodies | Sonnet | — | 2 | — (blocked once on an unclear brief: no production flag exists) | yes | R1-1 |
