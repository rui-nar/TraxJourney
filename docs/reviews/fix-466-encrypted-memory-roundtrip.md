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

## Round 3 — 2026-09-27, reviewed at ba96247e (fixes since ba0a048a)

### R3-1 — The keep flag still comes from a shape check on decrypted items, so a "v1.2.3" title is sent unencrypted on the second save (new evidence for R2-1)
- Trigger: A user with encryption unlocked saves a memory titled "v1.2.3" (sent encrypted) → the optimistic update, or any reload via reveal(), puts the plaintext back into items → the user reopens Edit, the title shows read-only as "Encrypted content unavailable", they change the date and save → isUndecrypted('v1.2.3') is true, keepStoredName is set, and the plaintext is sent without protect(); every later save resends it. Same for a memory description and a journal note.
- Scores: trigger=plausible, impact=security, detect=user-visible, later=expensive, fix=M/shared, confidence=verified
- Decision: Fix now (D3, floor F1: breaks E6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
- Fix note (triager): record provenance at reveal time. _revealItems (and reveal's failure path) marks each (item id, field) it could not decrypt; the optimistic update clears the mark for fields the user saved; the dialogs set keepStored* only from that mark. Defence in depth: updateMemory/updateJournal still protect() a keep value that is not a strictly valid envelope (base64 parts, minimum lengths). Rejected: a stricter isEnvelope alone (still a shape guess), and unconditional re-encryption (double-wraps foreign ciphertext). Tests: a second save after the optimistic update and after a reload, for memory name, memory description and journal description.

## Round 4 — 2026-09-27, reviewed at 765a09be (fixes since ba96247e; owner extended the cap by one round)

### R4-1 — A row the old client already double-wrapped reveals to its inner foreign envelope, is not marked, and the editor shows that ciphertext editable
- Trigger: On a client before this branch, a user with encryption unlocked edited a memory holding another key's ciphertext and saved (row = own-key(foreign envelope)) → reveal() decrypts the outer layer, no mark is made → Edit shows "v1.<b64>.<b64>" editable while the list says "Encrypted content unavailable". Same for a journal note.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10). Main produces and shows these rows the same way today, and nothing is lost (a save re-wraps at the same depth; a save after a photo poll strips only our own layer, which is a repair). The suggested fix, marking when the decrypted text is well-formed, brings back the shape check R3-1 removed.
- Revisit when: A user reports an editor showing v1.<b64>.<b64> after unlocking; or a client-side repair pass for pre-#466 double-wrapped rows is planned (record "decrypted to a foreign envelope" as provenance at reveal time, not by trusting typed text's shape).
- Guard: —
- Override: —
- Outcome: open

### R4-2 — The undecrypted record is reset at the start of _revealItems and rebuilt across awaits
- Trigger: theoretical — every await in the reveal loop is microtask-only today (pure-Dart cryptography_plus, no platform channel or isolate), so no tap can land mid-reveal.
- Scores: trigger=theoretical, impact=wrong-visible (triager corrected from silent-wrong: the editor shows the ciphertext), detect=user-visible (corrected from silent), later=cheap, fix=S/local, confidence=verified
- Decision: Reject (D11)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: rejected. If _revealItems is edited for another reason, building the marks in a local set and swapping it in at the end is a free hardening.
