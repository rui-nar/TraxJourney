# Review ledger — SPA catch-all off /api; hermetic local tests (PR #637)

Subject: branch fix/local-test-hermeticity
Envelope: REVIEW.md defaults

## Round 1 — 2026-10-16, reviewed at 7993a3b3

No findings, no envelope questions.

Noted outside the subject (pre-existing, not raised): `api/strava.py` builds
`Config("config/config.json")` at import, which writes a default
`config/config.json` into the checkout when none exists.
