# Review ledger — #601 #602

Subject: branch fix/601-602-bundled-fonts
Envelope: REVIEW.md defaults

## Round 1 — 2026-10-11, reviewed at 5d357df5

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 3 findings, all well-formed; 1 envelope question (to the owner).

Envelope question: does "drop the run-time download from Google Fonts" cover the Flutter web engine's own fallback-font fetches (Roboto at every start, Noto for scripts the bundled fonts lack), which go to fonts.gstatic.com outside the google_fonts package? Decides R1-1's fix: reword the policy, or self-host.

### R1-1 — The privacy policy now says Google gets nothing at app start, but the web engine still fetches fonts from fonts.gstatic.com
- Trigger: any visitor opens the web app → the Flutter web engine downloads Roboto from fonts.gstatic.com before the first frame (FontManifest has no 'Roboto'; fontFallbackBaseUrl defaults to https://fonts.gstatic.com/s/), and Noto files for glyphs the bundled fonts lack → Google receives the visitor's IP while the edited policy lists Google only for sign-in and translation.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, floor F3). Owner answered the envelope question: yes, self-host the engine fallback fonts.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

### R1-2 — The web first frame waits for all ten bundled font files
- Trigger: a visitor opens the web app for the first time, or after the 1-day assets/ cache expires, on a mobile connection → engine init awaits all ten .ttf (in parallel with CanvasKit) before the first frame → the boot splash stays up longer.
- Scores: trigger=concrete, impact=degraded-ux, detect=user-visible, later=cheap, fix=M/local, confidence=verified (triager corrected from inferred)
- Decision: Defer (D8)
- Revisit when: a measured web cold load on a throttled mobile profile (docs/LAUNCH_QA.md pass, or the #401 device measurement) shows the bundled fonts adding a noticeable delay to first paint beyond the CanvasKit download, or a user reports a slow first load. Likely fix then: subset the fonts to Latin/Latin-ext.
- Guard: —
- Override: —
- Outcome: open

### R1-3 — The "nothing in lib/ asks for a font the app does not bundle" test only catches two literal strings
- Trigger: a developer writes fontFamily: "monospace", 'Courier New', or a generic in fontFamilyFallback → the test passes and platforms fall back silently. Already true today: fontFamily: 'Georgia' (social_share_dialog.dart:869, the Facebook "f" badge) is not bundled and is absent on Android and the web.
- Scores: trigger=concrete (triager corrected from plausible), impact=maintainability, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D7). Owner: the Facebook "f" uses Inter ExtraBold, no allowlist.
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

## Round 2 — 2026-10-11, reviewed at 3209713c (fixes since 5d357df5)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 1 finding, well-formed.

### R2-1 — CanvasKit is still downloaded from www.gstatic.com at every web start
- Trigger: any visitor opens the web app → flutter.js resolves CanvasKit to https://www.gstatic.com/flutter-canvaskit/<engine-rev>/ (the build passes no --no-web-resources-cdn, so the build config has no useLocalCanvasKit) → canvaskit.js and canvaskit.wasm come from Google before the first frame, while the edited privacy policy lists Google only for sign-in and translation. New evidence relative to R1-1, which traced only font fetches; the author's Chrome check filtered on fonts.gstatic.com/s/ and dismissed www.gstatic.com as browser noise — the log does show both CanvasKit files fetched from it and none locally.
- Scores: trigger=concrete, impact=silent-wrong, detect=silent, later=cheap, fix=S/local, confidence=verified
- Decision: Fix now (D3, floor F3)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed

## Round 3 — 2026-10-11, reviewed at 5593a460 (fixes since 3209713c)

Reviewer: adversarial-reviewer (Fable). Triager: review-triager (Opus). 1 finding, well-formed. A regression introduced by the R2-1 fix.

### R3-1 — Self-hosted CanvasKit lost the engine revision from its URL but kept a one-day cache
- Trigger: a daily user opens the web app → the owner releases a build on a newer Flutter SDK → the user reloads within 24 h → main.dart.js and flutter_bootstrap.js are no-cache and fresh, but canvaskit/chromium/canvaskit.js and .wasm come from the HTTP cache (public, max-age=86400) → the new engine runs against the previous CanvasKit; where their APIs differ the boot splash never hands over, until the cache expires or a hard reload. (Before R2-1 the gstatic URL carried the engine revision.) Making canvaskit/ no-cache instead is not enough: Starlette 1.7's FileResponse never answers 304 (checked), so the ~7 MB wasm would download on every load.
- Scores: trigger=concrete, impact=wrong-visible, detect=user-visible, later=cheap, fix=S/local, confidence=inferred
- Decision: Fix now (D6)
- Revisit when: —
- Guard: —
- Override: —
- Outcome: fixed
