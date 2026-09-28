# Review ledger — fix/443-access-log-privacy

Subject: branch fix/443-access-log-privacy (issue #443)
Envelope: REVIEW.md defaults. Owner decision: keep client IPs truncated (IPv4 /24, IPv6 /48); never log query strings; uvicorn's access log off.

## Round 1 — 2026-09-24, reviewed at 9219030 + d160e9e (before docs/REVIEW.md; findings recorded after the fact)

### R1-1 — The graphify commit makes test_no_legacy_brand fail
- Trigger: CI runs the suite on the branch → the allowlist check fails because GRAPH_REPORT.md no longer names the old brand.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D6)
- Override: —
- Outcome: fixed (graphify commit dropped)

### R1-2 — Requests that match no route still log the raw path, share token included
- Trigger: A visitor opens /api/share/<tok>/meta/ (trailing slash, 307) or a browser preflights it → the share token lands in the access log.
- Scores: trigger=concrete, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F1)
- Override: —
- Outcome: fixed

### R1-3 — SQLAlchemy exception text logs bound parameters, share tokens included
- Trigger: A DB error during a share-token lookup → the catch-all handler logs "[parameters: ('<token>',)]".
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F1)
- Override: —
- Outcome: fixed (hide_parameters=True)

### R1-4 — FORWARDED_ALLOW_IPS=* trusts the leftmost X-Forwarded-For; safe only while Caddy strips client headers
- Trigger: An operator adds trusted_proxies to Caddy → clients can choose the logged IP.
- Scores: trigger=plausible, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, F3): docs recommend a CIDR list
- Override: —
- Outcome: fixed

### R1-5 — IPv4-mapped IPv6 addresses truncate to ::/48
- Trigger: uvicorn receives ::ffff:203.0.113.77 → logs ::/48.
- Scores: trigger=theoretical, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (pre-policy)
- Override: —
- Outcome: fixed

### R1-6 — Query-string tests pass on pre-fix code; nothing proves uvicorn's access log is off
- Trigger: Someone re-enables uvicorn's access log → no test fails.
- Scores: trigger=plausible, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (pre-policy)
- Override: —
- Outcome: fixed

### R1-7 — api/auth.py:277 logs a malformed Google id_token verbatim
- Trigger: A client posts a malformed id_token → the raw token string is logged.
- Scores: trigger=plausible, impact=security, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Out of this change's scope (pre-existing, untouched code); filed as a follow-up issue
- Override: —
- Outcome: open (follow-up issue)

## Round 2 — 2026-09-28, reviewed at c677c1db (fixes since 9219030)

No findings. Review stops (§6).
