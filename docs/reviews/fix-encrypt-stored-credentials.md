# Review ledger — third-party credentials stored encrypted

Subject: branch fix/encrypt-stored-credentials
Envelope: REVIEW.md defaults; SQLite only (owner, 2026-09-28)

Owner decisions taken before review: CREDENTIALS_ENCRYPTION_KEY is a required
runtime variable in every environment (the server refuses to start without it,
like JWT_SECRET); AES-256-GCM; rotation through retired keys; losing the key
means users reconnect their services.

## Round 1 — 2026-10-03, reviewed at a19fc327

### R1-1 — Rotation instructions say "restart", which does not reload .env
- Trigger: an operator rotates the key following .env.example and restarts with `docker compose restart` → the API keeps the old key while the rotation script re-encrypts under the new one → connections read as disconnected until the container is recreated with `up -d`
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: before the first key rotation on any instance (the wording should say `docker compose up -d`)
- Guard: —
- Override: —
- Outcome: open

### R1-2 — Polarsteps reports "connected" when the stored token no longer decrypts
- Trigger: a user whose Polarsteps token no longer decrypts (key lost, or a retired key dropped before rotation) opens Settings → shown as connected, then gets "token expired" on use
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: any instance loses or drops a key, or a user reports Polarsteps shown as connected but failing
- Guard: —
- Override: —
- Outcome: open

Round 1 produced no Fix now decision. Stop (§6).

## Delivery

| Unit | Goal | Route | Rule | Attempts | Escalated | Verified first time | Findings traced |
|---|---|---|---|---|---|---|---|
| U-C | Encrypt stored third-party credentials | Opus | S4 | 1 | — | yes | R1-1, R1-2 |
