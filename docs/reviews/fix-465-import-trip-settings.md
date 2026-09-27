# Review ledger — fix/465-import-trip-settings

Subject: branch fix/465-import-trip-settings (issue #465)
Envelope: REVIEW.md defaults

## Round 1 — 2026-09-27, reviewed at 424b60bb

Reviewed by an Opus adversarial review before the adversarial-review skill
existed; the findings were put into the §3 schema and triaged afterwards
(triage-only mode).

### R1-1 — Replace resets sleeping-option groups when the file carries none
- Trigger: The owner replaces a trip with an older export (or the ZIP export's trip file) that has no sleeping_option_groups → every sleeping option on that trip falls back to the default group "Other".
- Scores: trigger=concrete, impact=data-loss, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-2 — Colours inside type_styles are not validated, so an imported value crashes the client's track drawing
- Trigger: A user imports a trip file whose type_styles hold {"ride":{"color":"#GGGGGG"}} or {"run":{"color":7}} → import answers 201, then the app throws when it draws that trip's tracks.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: A client error report traces back to resolveTypeStyle; or a producer other than TraxJourney starts writing .traxj files; or type_styles gets a typed schema or validation in the settings API (fix both paths together then).
- Guard: —
- Override: —
- Outcome: open

### R1-3 — trip_start / trip_end are not checked as dates, so a bad value makes sync/check return 500
- Trigger: A user imports a trip file with trip_end="2024-13-45" → import answers 201; every later GET /{name}/sync/check for that trip returns 500.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: A 500 from sync/check is logged with a strptime ValueError in production; or trip dates get validated in the settings API (fix import and API validation together then); or another .traxj producer appears.
- Guard: —
- Override: —
- Outcome: open

### R1-4 — An explicit null for a setting in a file resets it on Replace instead of keeping the trip's value
- Trigger: A user replaces a trip with a hand-edited file holding "counters": null → the trip's counters become [] and "trip_end": null clears the end date.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Defer (D10)
- Revisit when: A user reports counters or dates cleared by a Replace; or another .traxj producer that writes explicit nulls appears; or the null-versus-absent contract for trip files gets defined.
- Guard: —
- Override: —
- Outcome: open

### R1-5 — Replace from the ZIP export's trip file can invert the trip's start and end dates
- Trigger: The owner replaces a trip with the trip file from a ZIP export (trip_start but no trip_end) → the file's start is taken and the current end kept; if the start is after the end, start > end.
- Scores: trigger=plausible, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=verified (corrected from inferred by the triager)
- Decision: Defer (D10)
- Revisit when: A user reports an inverted date range after a Replace; or a start<=end check is added to the settings API (apply it to import too); or code starts assuming trip_start <= trip_end without sorting.
- Guard: —
- Override: —
- Outcome: open
