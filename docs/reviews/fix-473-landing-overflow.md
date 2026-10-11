# Review ledger — #473

Subject: branch fix/473-landing-overflow
Envelope: REVIEW.md defaults

## Round 1 — 2026-10-10, reviewed at 2ed4686f

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 4 findings, all well-formed.

### R1-1 — Hero mock clips its 'Barcelos' label and memory card on wide windows
- Trigger: a visitor opens the landing page in a window 900 px or wider → _MapView places 'Barcelos' (0.48×) and the memory card (0.32×) by screen width, not map width (welcome_screen.dart:648, :655); the Stack clips them silently, and "nothing overflows at 1280 px" cannot see it.
- Scores: trigger=concrete, impact=cosmetic, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-2 — At 2x on 360/390 px the free card reads "Free ·" over "AGPL"
- Trigger: a reader at 200%+ text size opens pricing on a 360 or 390 px phone → the separator ends line 1 and "AGPL" sits alone on line 2, instead of "· AGPL" wrapping under "Free".
- Scores: trigger=concrete, impact=cosmetic, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D7) — no-break space, with a 2x wrap test at 360/390 px.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-3 — The 16 px phone gutter only reaches the nav; sections keep 32 px
- Trigger: a visitor opens the page on a phone → the nav logo starts at x=16, every section and pricing card at x=32; the _kPhone comment claims the cards use the narrower gutter. _Shell applies `gutter` only when padding is null, and all six sections pass an explicit 32.
- Scores: trigger=concrete, impact=cosmetic, detect=user-visible, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-4 — ignoreFontFetchErrors() filters nothing; an unloaded weight would fall back to the test font silently
- Trigger: the page starts using a font weight loadRealFonts does not load → google_fonts only debugPrints the failed fetch, the text lays out in the full-em test font, and the wrap/overflow tests measure the wrong metrics with no signal. (A removed file already throws; a google_fonts family rename is followed, since names come from GoogleFonts.*().fontFamily.)
- Scores: trigger=plausible (triager corrected from concrete), impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: loadRealFonts checks each family it loads measures narrower than the test font (a "iiiiiiiiii" probe), and welcome_screen_overflow_test.dart checks every family the page draws was loaded (shown to fail on a dropped weight); ignoreFontFetchErrors and its 5 call sites removed.
- Override: —
- Outcome: guard added

## Round 2 — 2026-10-10, reviewed at eb92be7b (fixes since 2ed4686f)

Reviewer: adversarial-reviewer (Opus; Fable weekly limit reached). Triager: review-triager (Opus). 1 finding, well-formed.

### R2-1 — The R1-4 font-family guard only checks the phone layout
- Trigger: a developer gives text that only appears on wide screens (_NavLink, _HeroWide, the wide section/footer branches, all built only at width >= 900) a weight loadRealFonts does not load → the 1280 px overflow and hero-map tests lay it out in the full-em test font, and "the page draws only fonts the test loaded" still passes because it only pumps 390x844.
- Scores: trigger=plausible, impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Guard (D9)
- Revisit when: —
- Guard: the family check runs at 320, 360, 390 and 1280 px; shown failing at 1280 px only when _NavLink (wide-only) uses Inter w300.
- Override: —
- Outcome: guard added

Round 2 produced no Fix now decision: review closed (§6).
