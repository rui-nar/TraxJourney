# Strava connect follow-ups — Plan for #584, #587 and #438

## Problem

Three loose ends from the Strava connect binding fix
(`docs/STRAVA_CONNECT_BINDING_PLAN.md`, PR #585, released in v0.53.1):

- **#584.** When an Android connect finishes in the browser, the server sends
  it straight back into the app. Nothing tells the person who approved on
  Strava which TraxJourney account their Strava account is about to be linked
  to. That is the one case the binding fix left out of scope (its envelope,
  U1R2-1). An attacker starts an app connect, forwards the link, and the victim
  approves on a phone where an attacker-controlled app has claimed
  `traxjourney://`. A page naming the account gives the victim a chance to
  stop.
- **#587.** On web, Connect Strava gives no feedback when the user closes the
  Strava popup or the browser blocks it. The Future never resolves, and the
  popup's message listener lingers (review ledger, U2-2).
- **#438.** iOS has no `CFBundleURLTypes`. Google Sign-In cannot return to the
  app, and the Strava return (`traxjourney://app/strava-return`) cannot reach it
  either. The app also lacks an explicit `FlutterDeepLinkingEnabled`. Deep
  linking has been on by default since Flutter 3.27, so this is for parity with
  Android's explicit flag, not a requirement (R1-5).

## Current state

- `api/strava.py` `strava_callback`: for a valid state with a code, `_bind_code`
  records sha256(code) → state jti (D9 of the binding plan), then
  `_return_redirect` answers 302 to
  `traxjourney://app/strava-return?code=…&state=…` (`ret=app`) or to
  `${FRONTEND_ORIGIN}/oauth_callback.html?strava=code&…` (`ret=web`). Errors
  redirect with `strava=error&reason=<fixed token>`.
- Server-rendered HTML precedent: Jinja with autoescape for `.html.jinja2` in
  `src/email/templates.py` (templates under `src/email/templates/`). Static
  pages are served by `api/router.py` `_legal_page`.
- Web popup: `flutter_client/lib/src/settings/strava_oauth_popup_web.dart`
  `StravaOAuthPopup.connect(url)` calls `window.open(url)` and completes only
  from a same-origin `{type: "strava_oauth"}` message. A null `window.open`
  result is ignored. `strava_connect_flow.dart` `connectWeb()` first awaits
  `start(app: false)` (a server round trip), then opens the popup, so the open
  happens outside the click's user activation.
- iOS: `flutter_client/ios/Runner/Info.plist` has neither `CFBundleURLTypes`
  nor `FlutterDeepLinkingEnabled`.
  `flutter_client/ios/Runner/GoogleService-Info.plist` has
  `BUNDLE_ID com.traxjourney.app`, but it was edited by hand (commit 20ecf1f3).
  Its `CLIENT_ID` and `REVERSED_CLIENT_ID` still belong to the old
  `com.viewtrip.client` OAuth client. There is no iOS build or CI job.
  `flutter_client/test/brand/platform_identity_test.dart` already pins the
  plist's `BUNDLE_ID` to the bundle id.

## Decisions

**D1 — App returns get a confirmation page; web returns do not (#584).** Owner
decision, 2026-10-08. For a valid `ret=app` state with a code, the callback
still binds the code first (binding plan D9), then answers **200** with an HTML
page instead of the 302. The page:
- **leads with fixed wording that does not depend on the name** (R1-1, owner
  2026-10-08): "Only continue if you just pressed Connect Strava in the
  TraxJourney app on this phone. If someone sent you this link, cancel." The
  display name is chosen freely by its owner (non-blank only, no length cap, not
  unique), so an attacker can make it look like the victim's own;
- then names the TraxJourney account the state's `sub` belongs to by its
  **display name** (owner decision; never the email), cut to 40 characters
  with an ellipsis (a cut inside an emoji cluster is accepted: it is cosmetic,
  and the clipping contains it; R3-3). The name sits in its
  own block after the warning, wrapped in `<bdi>` so bidi controls such as
  U+202E cannot reorder the fixed text. The block has `overflow: hidden` and a
  fixed `line-height`, so stacked combining marks cannot spill over the
  warning (R2-1);
- offers **Continue**, a link to the same
  `traxjourney://app/strava-return?code=…&state=…` the 302 used to target, and
  **Cancel**, which stays on the page (CSS `:target`, no script) and replaces
  it with: "If you didn't press Continue, nothing was connected. If you did and
  didn't expect to, remove the app you just authorised, under My Apps in your
  Strava settings. You can close this page." The page cannot know whether
  Continue was tapped, so it must not claim nothing was connected (R2-2). It
  also must not name the Strava app: Strava shows the name registered in the
  Strava console, which may differ from TraxJourney's, as on a self-hosted
  instance or before the console rename (R3-1). It does not
  go into the app: a Cancel after Continue would otherwise show "not granted"
  to a connected user (R1-3);
- shows "a TraxJourney account" when the display name is empty;
- is not shown when the state's account no longer exists. The callback then
  redirects to the app with `reason=invalid_state` and binds nothing (R1-4).

Binding before the page keeps D9's first-seen rule at the earliest point. If
the victim cancels, the code never leaves their browser, so a code bound to an
attacker state is harmless.

Ruled out:
- A page for web returns too. The code there stays on the victim's own callback
  page, so the page would add a click with no security gain.
- Showing the page before binding. That would let a later caller bind the code
  first.

Errors and expired states keep their current redirects: there is nothing to
confirm.

**D2 — The page is rendered with Jinja autoescape.** It follows
`src/email/templates.py`: a small renderer and one `.html.jinja2` template. The
display name is user-controlled (E2), so it must be escaped. The page carries:
- `Cache-Control: no-store`, because it holds a code;
- `Referrer-Policy: no-referrer`;
- `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'`.

It uses no script and no external assets. The look is minimal: system font, the
brand primary colour for the Continue button, and fits a phone width. The
English wording is fixed in the template.

**D3 — The web popup opens on the click, then navigates (#587).**
- `connectWeb()` opens a blank popup synchronously, before any await, so it
  keeps the click's user activation. It then awaits `start`, and sets the
  popup's location to the authorize URL.
- If `window.open` returns null (blocked), the attempt ends at once with the
  new outcome `popupBlocked`, message "Allow pop-ups for TraxJourney to connect
  Strava.", and the pending connect is cleared.
- If `start` throws (426 or other), the blank popup is closed before the error
  propagates as today.
- While waiting for the message, a periodic check (every 500 ms) of
  `popup.closed` watches for a closed popup. The callback page posts its
  message and closes in the same task, so `closed` can be seen before the
  message is delivered. The first time the check sees `closed`, it therefore
  waits a grace period (500 ms) with the listener still registered (R1-2). Only
  if no message arrives by then does it end the attempt with the new outcome
  `cancelled`, message "Strava connection cancelled.", remove the listener and
  clear the pending connect. The check stops when a message arrives or the attempt is replaced.

The popup abstraction changes shape from `connect(url)` to
open-then-navigate. The fake used in the flow tests follows suit.

Ruled out: detecting a block only by timing (unreliable).

**D4 — iOS URL schemes and deep linking are added, with the Google scheme taken
from the plist (#438).** `Info.plist` gains:
- `CFBundleURLTypes` with two schemes: the plist's current `REVERSED_CLIENT_ID`
  (Google Sign-In) and `traxjourney` (Strava return);
- `FlutterDeepLinkingEnabled` = true, matching Android's
  `flutter_deeplinking_enabled`.

The brand test pins the Google scheme to the plist's `REVERSED_CLIENT_ID` and
the presence of `traxjourney`. Regenerating `GoogleService-Info.plist` for
`com.traxjourney.app` then fails the test until the scheme follows.

Google Sign-In on iOS still needs that regeneration, which is an owner action
in the Google Cloud console. So the PR references #438 but does not close it.

## Review envelope

REVIEW.md defaults, plus the binding plan's envelope (as amended), plus:
- **The callback's app page is unauthenticated (E2).** Anyone who reaches it
  holds a valid, signed `ret=app` state plus a code that Strava issued for that
  state. It reveals only the display name of the account that minted the state,
  which is the intended disclosure to the person who opened the link. A user
  can mint a state only for themselves.
- **The display name is untrusted input** rendered into HTML.
- **No iOS build exists.** The iOS changes are checked by the brand test only.
  Device behaviour is out until an iOS build exists.
- **The page targets the forwarded attacker-started app flow** (owner,
  2026-10-08, EQ1). It must not rely on anything the account owner chooses,
  such as the display name, to warn the victim (R1-1).
- **Android consent may finish in the Strava app's in-app browser** when the
  Strava app is installed and takes the authorize link (owner, EQ2). Continue
  must work there too, and the owner's device check covers it.
- **The web popup runs in real browsers.** The Flutter VM tests use the stub
  and a fake popup. Browser-only behaviour (`window.open` returning null,
  `popup.closed`) is verified by reading the code plus an owner browser check.

## Boundaries crossed

- **API contract.** `GET /api/strava/callback` for a valid `ret=app` state with
  a code changes from a 302 to a 200 page whose Continue link is the old 302
  target. The Android app receives the same URI as before, after one tap. The
  `ret=web` and error responses are unchanged. Clients are unaffected.
- **Schema / stored data / export formats.** None.
- **iOS Info.plist.** New URL schemes and deep linking. There is no shipped iOS
  build to break.

## Conventions

- Never log a code, state or verifier (binding plan Conventions). The page's
  HTML holds the code; it must never be logged or cached.
- Message wording lives in the shared `stravaConnectMessage` mapping, not in
  widgets.

## Open decisions

- **O1 — Regenerate `GoogleService-Info.plist` for `com.traxjourney.app`**
  (owner, Google Cloud console). Needed before any iOS sign-in can work. After
  it lands, the brand test forces `Info.plist` to follow. #438 closes after that
  plus a device check.

## Execution units

### Wave 1 (independent)

#### U1 — Confirmation page for app returns (#584)

- **Goal:** for a valid `ret=app` state with a code, the callback binds the code
  and answers a 200 page naming the account, with Continue and Cancel links.
- **Scope:** `api/strava.py`, `src/web_pages/__init__.py` (new),
  `src/web_pages/render.py` (new), `src/web_pages/templates/strava_return_confirm.html.jinja2`
  (new), `tests/test_strava_return_confirm.py` (new),
  `tests/test_strava_connect_binding.py` (only the assertions that expect a
  302 to the app scheme).
- **Context:** read D1, D2 and the envelope. Follow `src/email/templates.py`
  for the Jinja environment (autoescape on `.html.jinja2`, `app_name` global
  from `src.brand`). The callback, `_bind_code` and `_return_redirect` are in
  `api/strava.py`. The user's display name is `UserInfo.display_name`
  (`models/user.py`). Look it up by the state's `user_info_id` in the same
  style the module uses for other `UserInfo` reads.
- **Do:**
  1. Add `render_strava_return_confirm(*, display_name: str, continue_url: str,
     cancel_url: str) -> str` in `src/web_pages/render.py`, plus the template.
  2. In `strava_callback`, after a successful bind for `ret == "app"`, return
     an `HTMLResponse` (200) from the renderer with D2's headers. Build both
     URLs with the same `urlencode` path `_return_redirect` uses. Keep every
     other branch (web relay, errors, expired, update_required, rebind
     refusal) exactly as today.
  3. Empty or whitespace display name → the neutral wording. A name over 40
     characters is cut to 40 plus "…". The fixed warning comes before the name.
  3a. No `UserInfo` for the state's `user_info_id` → the error redirect to the
     app with `reason=invalid_state`, and nothing is bound. Check before
     `_bind_code`.
  3b. Cancel is an in-page anchor (`#cancelled`). A CSS `:target` rule hides the
     prompt and shows the D1 Cancel text (hedged, R2-2). No script and no
     navigation into the app.
  3c. The name block follows D1: a block element with class `name` containing
     the `<bdi>`, and a `.name` rule in the page's `<style>` with
     `overflow: hidden` and a fixed `line-height` (R2-1). Truncation is
     `name[:40] + "…"` (R3-3).
  4. Update the binding tests that asserted a 302 to `traxjourney://` for a
     valid app state, so they read the Continue link from the page instead.
     Do not weaken any other assertion.
- **Acceptance:** `tests/test_strava_return_confirm.py`:
  `test_app_return_shows_a_page_naming_the_account`,
  `test_display_name_is_escaped` (`<script>` in the name appears escaped),
  `test_empty_display_name_uses_neutral_wording`,
  `test_continue_link_is_the_app_return_with_code_and_state`,
  `test_cancel_stays_on_the_page` (an in-page anchor, no `traxjourney:` link
  other than Continue),
  `test_fixed_warning_precedes_the_name`,
  `test_long_display_name_is_truncated`,
  `test_name_is_bidi_isolated_and_clipped` (the name is inside a `<bdi>`
  within the element with class `name`; the page's `<style>` has a `.name`
  rule with both `overflow: hidden` and a `line-height`; a U+202E name stays
  inside the `<bdi>`) (R3-4 guard),
  `test_cancel_text_is_hedged` (says what to do if Continue was pressed, and
  does not contain the app name, R3-1),
  `test_deleted_account_redirects_invalid_state_and_binds_nothing`,
  `test_page_headers_forbid_caching_framing_and_referrer`,
  `test_page_has_no_script` (no `<script` in the body),
  `test_web_return_is_still_a_redirect`,
  `test_app_errors_still_redirect` (denied, expired → app scheme 302),
  `test_code_is_bound_before_the_page` (a rebind attempt after the page
  is refused), and `test_page_is_not_logged` (caplog: no code or state).
  Then `pytest tests/test_strava_*.py tests/test_access_log_privacy.py tests/test_brand.py` passes.
- **Out of scope:** web returns; any client change; email templates.
- **Latitude:** local design (module layout under `src/web_pages/`, wording,
  CSS).
- **Escalate if:** the page needs script; a file outside Scope must change.
- **Depends on:** —

#### U2 — Web popup: open on the click, report closed and blocked (#587)

- **Goal:** on web, a blocked popup reports `popupBlocked`, a popup the user
  closes reports `cancelled`, and the popup opens inside the click's user
  activation.
- **Scope:** `flutter_client/pubspec.yaml` and `flutter_client/pubspec.lock`
  (only to add `fake_async` to `dev_dependencies`, R3-2),
  `flutter_client/lib/src/settings/strava_popup_arbiter.dart` (new),
  `flutter_client/test/settings/strava_popup_arbiter_test.dart` (new),
  `flutter_client/lib/src/settings/strava_oauth_popup_web.dart`,
  `flutter_client/lib/src/settings/strava_oauth_popup_stub.dart`,
  `flutter_client/lib/src/settings/strava_connect_flow.dart`,
  `flutter_client/test/settings/strava_connect_flow_test.dart`,
  `flutter_client/test/settings/strava_connect_app_scope_test.dart`,
  `flutter_client/test/settings/strava_oauth_popup_stub_test.dart`.
- **Context:** read D3 and the envelope. Follow the existing `connectWeb()`,
  its injectable `openPopup`, and the generation counter in
  `strava_connect_flow.dart`. Add the outcomes to the existing
  `StravaConnectOutcome` enum and `stravaConnectMessage`. The fake popup in
  `strava_connect_app_scope_test.dart` is the test seam to extend.
- **Do:**
  1. Replace the `openPopup(url)` seam with an open-then-navigate shape (e.g.
     `open() → handle?`, `handle.navigate(url)`, `handle.result` Future,
     `handle.close()`), injectable as today.
  2. `connectWeb()`: open synchronously before any await. Null → publish
     `popupBlocked`, clear the pending connect, return. Then `start`; on a
     throw, close the handle and rethrow. Then navigate and await the result.
  3. Put the closed-versus-message decision in a new platform-neutral class,
     `PopupResultArbiter` (`strava_popup_arbiter.dart`). It takes an injected
     `bool Function() isClosed`, exposes `onMessage(result)`, runs the 500 ms
     check and the 500 ms grace with `dart:async` `Timer`, completes exactly
     once, and cancels its timers on completion (R2-3, owner override).
     The web implementation only wires `window.open('', 'strava_oauth',
     features)`, `navigate` (sets `location.href`), the message listener →
     `onMessage`, and `isClosed` → `popup.closed`. It removes the listener when
     the arbiter completes.
  4. Map `closed` → `cancelled`. Add messages for `cancelled` and
     `popupBlocked`.
- **Acceptance:** tests (with the fake popup):
  `blocked popup publishes popupBlocked and never calls start`,
  `closed popup publishes cancelled and clears the pending connect`,
  `strava_popup_arbiter_test.dart` (new, `fakeAsync` from `package:fake_async`,
  declared in `dev_dependencies` with the same kind of comment the file already
  uses for lint-driven dev dependencies), testing the real
  arbiter, not the fake popup:
  - a message wins over a closed popup seen 200 ms earlier;
  - closed with no message in the grace period → `closed`;
  - completes exactly once (a message after the closed result is ignored, and a
    second message is ignored);
  - no timer is left pending after completion;
  - a message before any check → the message result, with no timers.
  `start failure closes the opened popup and rethrows`,
  `popup is opened before start is awaited` (record the call order),
  plus the existing app-scope tests still passing (generation guard,
  completion after the starter is disposed). Run `flutter analyze` and
  `flutter test test/settings test/core` in the Flutter test container.
- **Out of scope:** the Android flow; the callback page; server changes.
- **Latitude:** none.
- **Escalate if:** the popup handle cannot be opened synchronously from the
  button handler without changing `settings_screen.dart`; a file outside Scope
  must change.
- **Depends on:** —

#### U3 — iOS URL schemes and deep linking (#438, partial)

- **Goal:** `Info.plist` declares the Google `REVERSED_CLIENT_ID` and
  `traxjourney` URL schemes and enables Flutter deep linking, pinned by the
  brand test.
- **Scope:** `flutter_client/ios/Runner/Info.plist`,
  `flutter_client/test/brand/platform_identity_test.dart`.
- **Context:** read D4. Follow the existing
  `GoogleService-Info.plist BUNDLE_ID matches the iOS bundle id` test (its
  `_all`/`_read` helpers) for the new tests. Android's equivalent is
  `flutter_deeplinking_enabled` in `AndroidManifest.xml`.
- **Do:**
  1. Add `CFBundleURLTypes` with one entry whose `CFBundleURLSchemes` holds the
     plist's current `REVERSED_CLIENT_ID`, and one holding `traxjourney`, each
     with a `CFBundleURLName`.
  2. Add `<key>FlutterDeepLinkingEnabled</key><true/>`.
  3. Tests: `Info.plist URL schemes include GoogleService-Info REVERSED_CLIENT_ID`;
     `Info.plist URL schemes include traxjourney`;
     `Info.plist enables Flutter deep linking`.
- **Acceptance:** the three tests pass; `flutter test test/brand` passes in the
  Flutter test container; the plist stays well-formed (an XML parse in the
  test, or `python -c "import plistlib,sys; plistlib.load(open(...,'rb'))"`).
- **Out of scope:** regenerating `GoogleService-Info.plist` (O1); any Dart
  routing change; Xcode project settings.
- **Latitude:** none.
- **Escalate if:** a file outside Scope must change.
- **Depends on:** —

### Wave 2 — integration (orchestrator)

Full pytest in the CI command form, full `flutter analyze` and `flutter test` in
the container, `alembic heads` unchanged. Run `graphify update .` from main
after merge, through its own PR.

Commit trailers:
- `Release-Note: When you connect Strava from the Android app, the browser now
  shows which TraxJourney account it will be linked to before returning to the
  app.`
- `Release-Note: On the web, Connect Strava now says so when the Strava window
  is closed or blocked, instead of doing nothing.`

The PR closes #584 and #587, and references #438 without closing it (O1).

## Definition of done

- A valid app return shows a fixed warning first, then the account's display
  name (escaped, at most 40 characters), with a Continue link to the unchanged
  app return URI and a Cancel that stays on the page. A state whose account
  is gone redirects with `invalid_state` and binds nothing. Web returns and all error returns behave as before.
- The page is not cacheable, not frameable, sends no referrer and runs no
  script.
- On web, a blocked popup and a closed popup each produce one message, and
  leave no pending connect or lingering listener. The popup opens before any
  await in the click handler path.
- `Info.plist` has both URL schemes and Flutter deep linking, and the brand test
  pins the Google scheme to `GoogleService-Info.plist`.
- Full server and Flutter suites pass. Owner checks:
  - an Android connect shows the page and Continue returns to the app, both
    without the Strava app installed and with it (consent in Strava's in-app
    browser). If Continue does not reach the app from the in-app browser, that
    comes back as a finding;
  - in a real browser, closing and blocking the popup show their messages.
