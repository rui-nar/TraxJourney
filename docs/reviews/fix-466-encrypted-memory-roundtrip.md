# Review ledger — fix/466-encrypted-memory-roundtrip

Subject: branch fix/466-encrypted-memory-roundtrip (issue #466)
Envelope: REVIEW.md defaults

## Round 1 — 2026-09-27, reviewed at 41dc2a82

### R1-1 — The memory and journal editors still pre-fill their text fields with the raw ciphertext envelope
- Trigger: A user who imported another account's .traxj (or the owner on a locked device) opens a memory — the detail view says "Encrypted content unavailable" — and taps Edit → the Name and Description fields hold v1.<b64>.<b64>; the same for a journal entry's description.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=verified
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-2 — Plaintext shaped like "v1.<x>.<y>" is displayed as "Encrypted content unavailable" and dropped from posters and social posts
- Trigger: A user names a memory or activity "v1.2.3" → on their own device the list, detail view, map label and shared view show "Encrypted content unavailable", and posters/social posts leave the name out.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=M/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: A user reports a real name shown as "Encrypted content unavailable" or missing from a poster or social post; or isEnvelope/isUndecrypted is changed for another reason (adding a base64/length check there is the cheap fix).
- Guard: —
- Override: —
- Outcome: open

### R1-3 — The GPX-import duplicate warning quotes the encrypted activity's ciphertext name
- Trigger: A user whose trip holds an encrypted activity they cannot decrypt uploads the GPX file that activity came from → the dialog says "This trip already has “v1.<b64>.<b64>” from the same track."
- Scores: trigger=plausible, impact=degraded-ux, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Defer (D10)
- Revisit when: A user reports the duplicate-import warning quoting ciphertext; or the GPX-import duplicate dialog is touched again (applying shownText at gpx_import_dialog.dart:595 is a one-line change).
- Guard: —
- Override: —
- Outcome: open

## Round 2 — 2026-09-27, reviewed at ba0a048a (fixes since 41dc2a82)

### R2-1 — A typed title shaped like "v1.<x>.<y>" is saved unencrypted on an encrypted account, then can't be edited (new evidence for R1-2)
- Trigger: A user with encryption unlocked titles a memory "v1.2.3" (or "v1.Dinner with Dr. Smith") and saves → the passthrough added to protect() (encryption_service.dart:325) sends it unencrypted; the list shows "Encrypted content unavailable"; reopening Edit shows the field read-only and Save resends the same text; the only way out is to delete the memory. Same for a journal note.
- Scores: trigger=plausible, impact=security (triager corrected from wrong-visible), detect=user-visible, later=expensive (triager corrected from cheap: plaintext rows that look like envelopes can only be repaired client-side), fix=S/shared, confidence=verified
- Decision: Fix now (D3, floor F1: breaks E6 and the ENCRYPTION.md promise that memory.name is encrypted on every save)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
- Fix note (triager): a base64 check in isEnvelope alone is not enough ("v1.abcd.efgh" passes). Pass an envelope through only when it is the original stored value the user left untouched (the dialog's _nameEnvelope/_descEnvelope), and encrypt every other value.
