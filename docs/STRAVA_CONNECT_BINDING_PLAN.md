# Strava connect bound to the client that started it — Plan for GHSA-66h3-p265-gw7c

## Problem

`GET /api/strava/callback` decides which TraxJourney account to link from the
OAuth `state` alone, and nothing ties that state to the browser or app that
started the flow. A signed-in user can call `GET /api/strava/connect`, take the
authorize URL it returns (its state names *their* account) and get someone else
to open it. When that person approves TraxJourney on Strava's real consent
screen, their Strava tokens are stored under the requester's account. The
requester can then browse and import that person's Strava activities. The
victim does not need a TraxJourney account.

The same gap works the other way: a victim who is signed in can be made to
finish a flow the attacker started, linking the attacker's Strava account to the
victim's TraxJourney account (login-CSRF style). Nothing in the callback
notices either case.

Found in the adversarial review of fix/strava-oauth-state (ledger
`docs/reviews/fix-strava-oauth-state.md`, R1-1), filed as draft advisory
GHSA-66h3-p265-gw7c (medium). That fix made the state a 10-minute,
purpose-bound token (`api/deps.py` `create_strava_oauth_state`), but the state
is still a bearer value that can be forwarded.

## Current state

- `api/strava.py:281` `strava_connect` (GET, authenticated) returns
  `{"url": <strava authorize URL>&state=<state JWT>}`. The state carries `aud`,
  `sub`, `jti` and `exp` (`api/deps.py:133-170`).
- `api/strava.py:309` `strava_callback` (GET, unauthenticated, the Strava
  `redirect_uri`, `STRAVA_REDIRECT_URI`) decodes the state, exchanges the code
  with the client secret, upserts the `StravaToken` row for `sub`, and redirects
  to `${FRONTEND_ORIGIN}/oauth_callback.html?strava=connected|error&reason=…`.
  On exchange failure it puts `str(exc)[:80]` in the URL.
- Web: `flutter_client/lib/src/settings/settings_screen.dart:359`
  `_connectStrava` opens the URL in a popup (`strava_oauth_popup_web.dart`).
  `flutter_client/web/oauth_callback.html` posts
  `strava_oauth:connected|error[:reason]` to the opener and closes itself.
- Android: the same handler calls `launchUrl(…, externalApplication)`. The flow
  ends on `oauth_callback.html` in the system browser (no `window.opener`), and
  the user returns to the app by hand. There is no return into the app. The app
  has `flutter_deeplinking_enabled` and https App Links for `/share`, `/join`
  and `/verify-email` on `traxjourney.com` only (`AndroidManifest.xml:45-52`,
  `docs/ANDROID.md` "Deep links").
- The API base URL is build-time (`client.dart:14`), so val and debug APKs talk
  to other hosts, which App Links never verify.
- Tests: `tests/test_strava_oauth_state.py` pins the state token (not a session
  token, expires, fresh nonce, connect→callback round trip).
  `flutter_client/test/settings/strava_oauth_popup_stub_test.dart` covers the
  stub only.
- Precedent for retiring a route that old clients still call:
  `api/projects.py:889` (PUT day-meta answers 426, issue #397).

## Decisions

**D1 — Linking moves out of the callback into an authenticated completion
call.** The callback stops writing tokens. It relays `code` and `state` back to
the client that started the flow. That client, signed in, calls
`POST /api/strava/complete`, and the server links only if the bearer's user is
the state's `sub`. A forwarded URL then fails: the code lands with the victim,
who has neither the requester's session nor the verifier (D2). The reverse
attack fails too: a victim finishing the attacker's flow sends the victim's
bearer, which does not match the state's `sub`. Ruled out: a cookie checked at
the callback. The web app has no cookie session on the API origin (bearer
tokens), and the Android flow finishes in the system browser, whose cookie jar
is not the app's.

**D2 — A PKCE-style verifier held by the starting client.** The client
generates 32 random bytes (`verifier`, base64url) and sends
`challenge = base64url(sha256(verifier))` (no padding) to `POST /api/strava/connect`.
The server signs the challenge into the state (`chal` claim). `complete` checks
that `sha256(verifier)` matches `chal`. This binds the **state** to one client
instance, not just to one account. It does not bind the **code**: Strava has no
PKCE, so on its own the verifier would let an attacker pair a stolen code with
a state they minted for their own account. D9 closes that. Ruled out: relying on the `sub` check
alone. It stops cross-account linking, but not an attacker who holds both the
victim's code and a session for the same account (a shared device). Strava's
own OAuth has no PKCE, so the binding lives in our completion step. The code
exchange still uses the client secret, server side.

**D3 — `POST /api/strava/connect` with a body; `GET /api/strava/connect`
answers 426.** Body: `{"challenge": str, "return_to": "web" | "app"}`. The
challenge goes in a body, not a query string, so it stays out of access logs.
The GET that installed APKs call is retired with
`426 Upgrade Required`, detail `"Update the app to connect Strava."`, shaped
like PUT day-meta. Owner decision (2026-10-08): **cut**, no transition window.
Keeping the GET path open would keep the hole open, and the advisory could not
be published until it closed. Accepted cost: an old APK shows
`Could not open Strava: …` on Connect until it is updated. Every other feature
keeps working, and already-connected accounts keep working.

**D4 — Android returns through a custom scheme,
`traxjourney://app/strava-return?…`.** Owner decision (2026-10-08). It works for
release, val and debug builds and does not depend on App Link verification or
on the API host. A custom scheme can be claimed by another app (RFC 8252
§8.1). D2's verifier and D9's code binding together make an intercepted return
URL useless: the state is bound to the client, and the code to the state. This
is the RFC 8252 pattern for private-use schemes, with the binding Strava's
missing PKCE would have given. The URI has host `app` so that
go_router sees the path `/strava-return`. A bare
`traxjourney://strava-return` would put the name in the host and give an empty
path. Ruled out: https App Links (`traxjourney.com/strava-return`). Only
verified release builds against traxjourney.com would return into the app.

**D5 — The callback redirects by the state's `ret` claim.**
`create_strava_oauth_state(user_info_id, challenge, return_to)` adds `chal` and
`ret` to the signed state. The callback first validates the state (signature,
audience, expiry, `chal` and `ret` present), then redirects:
- `ret == "web"` → `${FRONTEND_ORIGIN}/oauth_callback.html?strava=code&code=…&state=…`
- `ret == "app"` → `traxjourney://app/strava-return?code=…&state=…`
- Errors (Strava `error`, no code, bad or expired state) go to the same target
  with `strava=error&reason=<fixed token>` when `ret` is known, and to the web
  page otherwise. An **expired** state still names its target: after
  `ExpiredSignatureError`, the callback decodes it again with expiry checking
  off (signature and audience still verified) only to read `ret`. An app flow
  whose user lingered at Strava then returns to the app with `state_expired`
  instead of stranding them in the browser (R1-2). Reasons come from a fixed set (`denied`, `no_state`,
  `state_expired`, `invalid_state`, `update_required`). Exception text never
  goes in a URL again.

A state with neither `chal` nor `ret` (issued by the old GET before deploy,
alive for at most 10 minutes) is refused with `reason=update_required`, and
each such refusal logs one warning line (no code or state), so connects caught
across a deploy show up (R1-4 guard). The callback never exchanges the code. The exchange happens only in `complete`.

**D6 — `POST /api/strava/complete` does the exchange and the link.** Body:
`{"code", "state", "verifier"}`, authenticated. In order:
1. Decode the state: expired → 400 `state_expired`; invalid or missing claims →
   400 `invalid_state`.
2. `sub` ≠ bearer `sub` → 403.
3. Verifier mismatch → 403 (compared with `hmac.compare_digest`).
3a. Code binding (D9): the `strava_oauth_code` row for `sha256(code)` must
   exist, be unexpired and name this state's `jti`, else 403
   `code_not_bound`.
4. Exchange the code: failure → 502 with a generic detail, logged without the
   code. `OAuth2Session.exchange_code` gains `timeout=self.TOKEN_TIMEOUT`,
   which `refresh_token` and `revoke` already pass. Without it, a stalled
   Strava endpoint holds a worker thread past the client's 30 s timeout
   (R1-3, owner override).
5. Store the `StravaToken` with the module's #440 idiom: `_claim_token_row`
   as the session's first write, INSERT only when it returns False. A
   disconnect committed between a read and the write can then never give a
   `StaleDataError` 500 that leaves the new tokens stored nowhere and
   unrevoked (U1-2, owner override). Delete the binding row in the same
   transaction. Return `{"connected": true}`.

Codes, states and verifiers are never logged. A replay of the same code fails
twice over: the binding row is gone and Strava refuses a second exchange.

**D9 — The callback binds each code to the state it arrived with, first seen
wins.** Owner decision (2026-10-08, U1-1). Strava's redirect always reaches
our callback before any app or browser page sees the code, so the callback is
the one place that knows which state Strava returned the code with. On a valid
state with a code, the callback INSERTs into a new table `strava_oauth_code`
(`code_hash` = hex sha256 of the code, primary key; `state_jti`; `expires_at` =
the state's `exp`). It then relays as in D5. If a row for that hash already exists
with a different `jti`, the callback refuses with `reason=invalid_state` and
writes nothing. The same `jti` (a reloaded callback) relays again. Each insert
first deletes expired rows. `complete` requires the match (D6 step 3a). An
attacker who holds a stolen code and calls the callback with their own state
finds the code already bound to the victim's state. Ruled out: an in-memory
dict (owner chose the table: it survives an API restart during consent and
any future second API process). Also ruled out: exchanging the code at the
callback and parking the tokens for the state. Then the forwarded-link attacker
completes with their own state and verifier without ever holding the code,
which reopens the original hole.

Accepted residual risk (owner, 2026-10-08): expired binding rows are pruned on
the next insert. A stolen code that is still unused and still valid at Strava
after the victim's 10-minute state expires could then be bound to a fresh
attacker state. This relies on Strava codes living no longer than about the
state's 10 minutes (RFC 6749 §4.1.2 recommends at most 10). Strava does not
document the lifetime. The rejected alternative was a 24 h prune grace.

**D7 — The client keeps one pending connect, with an expiry.** The new
`StravaConnectFlow` (in `flutter_client/lib/src/settings/`) owns the verifier.
On web it stays in memory: the opener page stays alive while the popup is open.
On the app it goes to `flutter_secure_storage` as
`{verifier, expires_at = now + 10 min}`, because Android may kill the app while
the browser is in front. A new connect replaces the pending one. Completion and
expiry clear it. Ruled out: keying several pending flows by `jti`. One connect
at a time is the only real use, and a stale verifier simply fails the check.

**D8 — The web popup protocol carries the code.** `oauth_callback.html` posts a
structured message (a plain object
`{type: "strava_oauth", status, code?, state?, reason?}`) to the opener, only
when `window.opener.origin === window.origin`, as today.
`StravaOAuthPopup.connect` returns `({String? code, String? state, String? error})`.
The settings screen then calls `flow.complete(code, state)`. When the page has
no opener (an Android browser tab, or a popup whose opener navigated away), it
shows the outcome and its reason as text instead of staying on
"Connecting, please wait…" (R1-2).

Old web bundles: after the deploy, an old bundle calls the retired GET and gets
the 426 error, so it never starts a flow. The one gap is a flow started before
the deploy and approved after it. The callback answers `update_required`, the
old listener ignores the new object message, and nothing is shown until a
reload. That gap is accepted and only logged (R1-4). The web client is served
by the same server, so it updates with the deploy.

## Review envelope

REVIEW.md defaults apply, with these additions:
- **New trust boundary:** the `traxjourney://` custom scheme. Any installed app
  may register it and receive the return URL (code + state). That is in the
  envelope: the design must hold when the return URL of a flow the legitimate
  app on that device started is read by a hostile app (RFC 8252 §8.1; D2 + D9).
  Out (owner, 2026-10-08, U1R2-1): a flow the attacker started and forwarded
  to a victim whose device also runs an attacker-controlled app claiming the
  scheme. No PKCE-style design stops that. A confirmation page naming the
  account to be linked is a follow-up. An attacker controlling the victim's
  device or browser beyond that is out.
- **The OAuth callback stays unauthenticated** and is reachable by anyone with
  any query string (E2). Its only write is D9's code-binding row, and only for
  a state whose signature, audience and expiry check out. It never writes or
  changes a `StravaToken`, and never picks a user. Its redirect target must
  come only from the signed state, never from a request parameter.
- **E5 applies with the owner's cut decision (D3):** old APKs lose Strava
  connect, and that is accepted. A finding that "old APKs cannot connect" is D1
  (by design). A finding that an old APK can still link an account through any
  path is a defect.
- **No iOS build exists** (`docs/ANDROID.md`). iOS URL-scheme registration is
  out (see O1).
- **Reverse-proxy access logs and browser history are out** (E3, owner
  2026-10-08). The callback URL's `code` and `state` reach them. A logged code is
  useless: it is bound to its state (D9), whose verifier never left the client
  (D2). The exclusion also stands on E3: the admin and the proxy are trusted.
  The logging convention covers application logs only.

## Boundaries crossed

- **API contract.** `GET /api/strava/connect` is retired with 426. New
  `POST /api/strava/connect` and `POST /api/strava/complete`.
  `GET /api/strava/callback` changes behaviour: it no longer links, and it
  redirects with `code` and `state` to the client. The OpenAPI schema gains the
  two POST models. Old APKs: Connect fails with the 426 detail (D3). Commit
  carries `Upgrade-Note: Connecting Strava needs this version of the Android
  app; older versions show an error on Connect. Accounts already connected keep
  working.`
- **State token format.** Gains `chal` and `ret`. States issued before deploy
  are refused at the callback (`update_required`). They live 10 minutes at
  most.
- **Schema / stored data.** One new table, `strava_oauth_code` (D9), added by
  an Alembic migration on the current single head. Rows live 10 minutes and
  hold no secret: only the code's hash. Nothing existing is migrated.
  `StravaToken` rows are written as today, by a different endpoint, through
  `_claim_token_row`. Downgrade drops the table.
- **Server config.** None. `STRAVA_REDIRECT_URI` and the Strava app's callback
  domain are unchanged: the callback is still the `redirect_uri`.
- **Android manifest.** New intent filter for scheme `traxjourney`, host `app`,
  path `/strava-return`, with `BROWSABLE`.

## Conventions

- Never log a code, state, verifier or challenge, at any level, on either side,
  in application logs (the reverse proxy's access log is out, see the
  envelope).
  Tests assert this with `caplog` for the server paths.
- Error reasons travel as fixed tokens (D5). The client maps them to messages.
  No exception text in URLs or response details.
- Base64url without padding for both verifier and challenge, on both sides.
  The server test vectors and the Dart test vectors use the same fixed
  verifier/challenge pair, the RFC 7636 Appendix B example:
  `dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk` →
  `E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM`.

## Open decisions

- **O1 — iOS URL scheme.** When an iOS build exists, `CFBundleURLTypes` needs
  `traxjourney`. Bundle it with #438 (Google Sign-In URL scheme in Info.plist).
  Not needed before then.
- **O2 — Advisory publication.** Publish GHSA-66h3 with `patched_versions` set
  to the release carrying this change, after production's `/api/version`
  reports it. Owner action, after deploy.

## Execution units

### Wave 1 (independent)

#### U1 — Server: bound state, POST connect, relaying callback, complete

- **Goal:** the server never links a Strava account except through an
  authenticated `POST /api/strava/complete` whose bearer matches the state and
  whose verifier matches the state's challenge.
- **Scope:** `api/strava.py`, `api/deps.py`, `src/auth/oauth.py`
  (`exchange_code` timeout only), `models/user.py` (the `StravaOAuthCode`
  model only), one new file in `alembic/versions/`,
  `tests/test_strava_oauth_state.py`, `tests/test_strava_connect_binding.py`
  (new). Amended after the U1 unit review (U1-1, U1-2): D6 steps 3a and 5,
  and D9.
- **Context:** read D1–D6 and Conventions. Follow the current `strava_connect`
  and `strava_callback` (`api/strava.py:281-360`) for structure and the upsert.
  Follow PUT day-meta's 426 (`api/projects.py:889-905`) for the retired GET.
  Follow `tests/test_strava_oauth_state.py`'s fixtures (`engine`, `users`,
  `exchange`, `client`, `_expired_state`) for the new test file, and reuse them
  by import or a shared conftest only if one already exists for these tests.
  Otherwise copy them.
- **Do:**
  1. `api/deps.py`: `create_strava_oauth_state(user_info_id, challenge,
     return_to)` adds `chal` and `ret`. Add `decode_strava_oauth_state_full(token)
     -> Optional[StravaOAuthState]` (a small dataclass: `user_info_id`,
     `challenge`, `return_to`) that requires `aud, sub, jti, exp, chal, ret` and
     validates `ret ∈ {"web","app"}`. Keep `ExpiredSignatureError` raising as
     today. Keep the never-log-the-token rule. Remove the old
     `decode_strava_oauth_state` if nothing else uses it (grep first). If
     something does, escalate.
  2. `api/strava.py`: Pydantic models `ConnectIn {challenge: str (43 chars,
     base64url), return_to: Literal["web","app"]}` and
     `CompleteIn {code: str, state: str, verifier: str (43–128 chars,
     base64url)}`. Bound the lengths so E4 shapes are tight.
  3. `POST /api/strava/connect`: same checks as today (503 when not configured,
     404 unknown user), returns `ConnectUrlOut`.
     `GET /api/strava/connect`: 426, detail `"Update the app to connect Strava."`.
  4. `GET /api/strava/callback`: per D5. It no longer exchanges or writes
     anything. It builds the redirect target from the decoded state's `ret`
     only. Query values are URL-encoded with `urllib.parse.urlencode`. For an
     expired state, add a deps helper that returns `ret` from a
     signature-verified decode with `verify_exp` off. Never use it to pick a
     user. Log one warning (no token) per `update_required` refusal.
  5. `POST /api/strava/complete`: per D6, steps in that order. Return
     `{"connected": true}` (add a response model).
  5a. `src/auth/oauth.py`: pass `timeout=self.TOKEN_TIMEOUT` in
     `exchange_code`, like `refresh_token`. Nothing else in that file changes.
  6. Update the module docstring's endpoint list.
  7. Rewrite `tests/test_strava_oauth_state.py` to the new state shape. Keep
     every existing property (not a session token in either direction, fresh
     nonce, expiry reported, foreign key refused, invalid state logged without
     the token). Callback assertions now check the redirect target, not a
     token row.
- **Acceptance:** `tests/test_strava_connect_binding.py` with at least:
  `test_get_connect_is_retired_with_426`,
  `test_post_connect_signs_challenge_and_return_target_into_state`,
  `test_callback_never_writes_a_token_row` (any state, including a valid one),
  `test_callback_relays_code_and_state_to_web_page`,
  `test_callback_relays_code_and_state_to_app_scheme`,
  `test_callback_refuses_a_state_without_challenge_as_update_required`,
  `test_callback_error_reasons_are_fixed_tokens` (Strava `error=access_denied`,
  exchange never called),
  `test_complete_links_the_bearer_when_state_and_verifier_match`,
  `test_complete_refuses_another_users_state_403` (the forwarded-link attack:
  A's state, B's bearer → no row for either),
  `test_complete_refuses_a_wrong_verifier_403`,
  `test_complete_refuses_an_expired_state_400`,
  `test_complete_exchange_failure_is_502_without_exception_text`,
  `test_rfc7636_vector_matches` (Conventions pair),
  `test_callback_routes_an_expired_app_state_to_the_app` (`ret=app`, expired →
  `traxjourney://app/strava-return?strava=error&reason=state_expired`),
  `test_expired_state_with_a_foreign_signature_goes_to_the_web_page`,
  `test_update_required_refusal_logs_one_warning_without_the_state`,
  `test_exchange_code_passes_a_timeout` (mock `requests.post`, assert
  `timeout`),
  `test_no_code_state_or_verifier_in_logs` (caplog over connect, callback,
  complete, including failures).
  Then `pytest tests/test_strava_oauth_state.py tests/test_strava_connect_binding.py tests/test_strava_disconnect.py tests/test_strava_upstream_errors.py`
  passes.
- **Out of scope:** client code. Changing `STRAVA_REDIRECT_URI` or the callback
  path. A `jti` replay store. Deauthorizing a previous Strava account on
  reconnect.
- **Latitude:** local design (helper names, dataclass shape, test layout).
- **Escalate if:** something outside `api/strava.py` imports
  `decode_strava_oauth_state`. `tests/test_strava_upstream_errors.py` depends on
  the callback exchanging. A file outside Scope must change.
- **Depends on:** —

#### U2 — Client: connect flow, settings screen, web popup relay

- **Goal:** web and app clients start Strava connect with a verifier, and the
  web client completes it from the popup's relayed code.
- **Scope:** `flutter_client/lib/src/settings/strava_connect_flow.dart` (new),
  `flutter_client/lib/src/settings/settings_service.dart`,
  `flutter_client/lib/src/settings/settings_screen.dart` (the Strava section and
  `_connectStrava` only), `flutter_client/lib/src/settings/strava_oauth_popup_web.dart`,
  `flutter_client/lib/src/settings/strava_oauth_popup_stub.dart`,
  `flutter_client/web/oauth_callback.html`,
  `flutter_client/test/settings/strava_connect_flow_test.dart` (new),
  `flutter_client/test/settings/strava_oauth_popup_stub_test.dart`.
- **Context:** read D2, D3, D7, D8 and Conventions. The API contract is U1's:
  `POST /api/strava/connect {challenge, return_to}` → `{url}`;
  `POST /api/strava/complete {code, state, verifier}` → `{connected: true}`,
  with 400 `state_expired|invalid_state`, 403 or 502 on failure. Follow
  `settings_service.dart`'s existing Strava methods for API calls. For random
  bytes and SHA-256 use `cryptography_plus` (already a dependency; see
  `flutter_client/lib/src/crypto/` for its use) or `dart:math`
  `Random.secure()` plus `crypto`, whichever is already a dependency. Do not add
  a package. Follow `crypto/device_key_store.dart` for `flutter_secure_storage`
  use behind an injectable interface. Settings-screen test gotchas are in
  memory note "Settings-screen widget tests": pump narrow, swap the `api`
  global, no `pumpAndSettle` while saving.
- **Do:**
  1. `StravaConnectFlow`: `start({required bool app}) → Future<Uri>` generates
     the verifier, stores the pending connect (in memory for web; secure storage
     with `expires_at` for app, D7), posts connect and returns the authorize
     URL. `complete(code, state) → Future<StravaConnectOutcome>` reads the
     pending verifier (expired or missing → `noPendingConnect` without calling
     the API), posts complete, clears pending on every outcome, and maps errors
     to an enum (`connected, noPendingConnect, expired, wrongAccount, failed,
     updateRequired`). Make storage, API, clock and random source injectable.
  2. `settings_service.dart`: replace `getStravaConnectUrl` with
     `startStravaConnect(challenge, returnTo)` and add `completeStravaConnect`.
     Remove the old method (orphan).
  3. `oauth_callback.html`: post `{type:"strava_oauth", status, code, state,
     reason}` per D8. Keep the opener-origin check and `window.close()`, but
     wrap the `window.opener.origin` read in `try/catch` and treat a throw as
     "no same-origin opener": an opener that navigated to another site makes
     that read throw a SecurityError (R2-1). When
     there is no same-origin opener, replace "Connecting, please wait…" with
     the outcome and a plain-text reason (`textContent`, never `innerHTML`),
     plus a line telling the user to return to the app (R1-2).
  4. `strava_oauth_popup_web.dart`: accept only messages with
     `type == "strava_oauth"` from the same origin. Return
     `({String? code, String? state, String? error})`. Keep the stub in step.
  5. `settings_screen.dart` `_connectStrava`: web → `flow.start(app:false)`,
     popup, then on a code `flow.complete`. App → `flow.start(app:true)` and
     `launchUrl` (the return is U3's). Messages per outcome (existing SnackBar
     style). Keep `_loadStravaStatus` after success.
- **Acceptance:** `flutter_client/test/settings/strava_connect_flow_test.dart`:
  RFC 7636 vector (Conventions); `start` posts a 43-char challenge and the right
  `return_to`; `complete` sends the stored verifier and clears it; an expired
  pending connect yields `noPendingConnect` without an API call; 403 →
  `wrongAccount`; 400 `state_expired` → `expired`; 426 on start →
  `updateRequired`; a new `start` replaces the previous verifier. Updated stub
  test passes. Run `flutter analyze` and
  `flutter test test/settings/` in the Flutter test container (memory note
  "Flutter test container"). Manual check (reported, not automated):
  `oauth_callback.html?strava=code&code=x&state=y` in a popup posts the object
  to its opener; `oauth_callback.html?strava=error&reason=state_expired` opened
  directly shows the reason text; with the opener tab navigated to another
  site, the popup shows the reason text instead of staying on "Connecting…".
- **Out of scope:** the Android return route and manifest (U3). Clearing the
  pending connect on logout. Any change to Strava import screens.
- **Latitude:** local design (class shape, enum names, message wording).
- **Escalate if:** neither `cryptography_plus` nor an existing dependency gives
  SHA-256 and secure random on web and Android. The settings screen needs
  changes outside its Strava section. A file outside Scope must change.
- **Depends on:** — (contract fixed by this plan; integrates after U1)

### Wave 2

#### U3 — Android: custom-scheme return into the app

- **Goal:** after Strava consent in the system browser, Android opens
  `traxjourney://app/strava-return?…` in the app, which completes the connect
  and lands on Settings with the outcome shown.
- **Scope:** `flutter_client/android/app/src/main/AndroidManifest.xml`,
  `flutter_client/lib/src/core/app_router.dart`,
  `flutter_client/lib/src/settings/strava_return_screen.dart` (new),
  `flutter_client/test/core/app_router_redirect_test.dart`,
  `flutter_client/test/settings/strava_return_screen_test.dart` (new),
  `docs/ANDROID.md`.
- **Context:** read D4 and D7. Follow the `/verify-email/:token` route and
  `auth/verify_email_screen.dart` (a screen that finishes a server call from a
  link, then routes on). Follow `authRedirectTarget`'s `/verify-email/` handling
  and its tests in `app_router_redirect_test.dart`. The intent filter sits next
  to the App Links filter in the manifest (`AndroidManifest.xml:45-52`). Use
  U2's `StravaConnectFlow.complete`. For the restore wait, read
  `AuthNotifier.isRestoring` (`auth/auth_notifier.dart:106-111`) and
  `SplashGate` (`core/splash_screen.dart`).
- **Do:**
  1. Manifest: a second `VIEW` intent filter (no `autoVerify`) with
     `DEFAULT` + `BROWSABLE`, `scheme="traxjourney" host="app"
     pathPrefix="/strava-return"`, with a comment pointing to this plan's D4.
  2. Router: `GoRoute('/strava-return')` builds `StravaReturnScreen` from the
     `code`, `state`, `strava` and `reason` query parameters. In
     `authRedirectTarget`, a signed-out user on `/strava-return` goes to login.
     Do not carry it as a `return_to`: the pending verifier will have outlived
     a fresh sign-in only if it is the same account, and the server's `sub`
     check handles that. Keep the existing redirect behaviour for every other
     path.
  3. `StravaReturnScreen`: shows progress, calls `flow.complete` (or shows the
     relayed `reason` when `strava=error`), then `go('/settings')` and shows the
     outcome message. On a cold start the route is built while the session is
     still being restored (`authRedirectTarget` returns null while loading,
     and SplashGate mounts the child under the splash). The screen therefore
     waits until `AuthNotifier.isRestoring` is false, listening to the
     notifier, before calling `complete`. If restore ends signed out, the
     router's redirect to login applies (R1-1).
  4. `docs/ANDROID.md`: under "Deep links", add the custom-scheme row and one
     paragraph: why it is not an App Link (D4), and how to test it with
     `adb shell am start -a android.intent.action.VIEW -d "traxjourney://app/strava-return?strava=error&reason=denied" com.traxjourney.app`.
- **Acceptance:** `app_router_redirect_test.dart` gains: a signed-in
  `/strava-return?...` stays; a signed-out one goes to login.
  `strava_return_screen_test.dart`: a code+state calls `complete` once and
  routes to `/settings`; `strava=error&reason=denied` shows the denied message
  without calling `complete`; `wrongAccount` and `noPendingConnect` show their
  messages; built while `isRestoring` is true, the screen does not call
  `complete` until the notifier reports restore finished, then calls it once. Run `flutter analyze` and `flutter test test/core test/settings`
  in the Flutter test container. Manual check (owner, on device): the `adb`
  command above opens the app on Settings with the denied message.
- **Out of scope:** iOS (O1). App Links for this path. Handling a return while
  a different account is signed in, beyond the server's 403 message.
- **Latitude:** local design (screen layout, message wording).
- **Escalate if:** go_router does not deliver the custom-scheme URI as path
  `/strava-return` with its query. `initialLocationFor()` needs changing. A
  file outside Scope must change.
- **Depends on:** U2

### Wave 3 — review fix

#### U4 — A replayed Strava return does nothing (I1-1)

- **Goal:** when Android hands the app the same `traxjourney://app/strava-return?…`
  link a second time (activity recreated, relaunch from Recents), the return
  screen goes quietly to `/` instead of completing again and showing "not
  started here".
- **Scope:** `flutter_client/lib/src/settings/strava_return_screen.dart`,
  `flutter_client/test/settings/strava_return_screen_test.dart`.
- **Context:** the pending-connect storage in `strava_connect_flow.dart`
  (`SecureKvStore` / `FlutterSecureKvStore` from `crypto/device_key_store.dart`,
  injectable) is the pattern to follow. Existing tests in
  `strava_return_screen_test.dart` show the fixture style.
- **Do:** compute a key from the return's query parameters (`strava`, `reason`,
  `code`, `state`, in a fixed order), as hex sha256 (`cryptography_plus`
  `Sha256`). Before handling, read the last handled key from secure storage
  (one entry, its own storage key). If it is equal, `go('/')` with no message
  and no `complete`. Otherwise store the key first, then handle as today. The
  store is injectable for tests. The code and state are never stored in
  plain text.
- **Acceptance:** new tests: the same code+state return handled twice →
  `complete` called once, and the second shows no SnackBar and lands on `/`; the
  same error return twice → the message shows once; a different return after one
  was handled is handled normally; the restore-wait test still passes. Run
  `flutter analyze` and `flutter test test/settings test/core` in the container.
- **Out of scope:** `MainActivity.kt`; any change to `strava_connect_flow.dart`.
- **Latitude:** none.
- **Escalate if:** the guard needs a file outside Scope.
- **Depends on:** U3.

### Wave 4 — integration (orchestrator)

Run the full `pytest` in the py314 citest container (mind the WSL bash probe
caveat) and the full `flutter test` in the Flutter test container. Run
`graphify update .` from main after merge. The commit carries:
- `Release-Note: Connecting Strava is now tied to the browser or app that
  started it, so a Connect link sent by someone else can no longer attach your
  Strava account to their TraxJourney account. On Android, the app now reopens
  by itself after you approve on Strava.`
- The `Upgrade-Note:` from Boundaries crossed.

The PR description names GHSA-66h3 as fixed, but keep it quiet until release,
as with PRs #547–#550. After the release is deployed and production's
`/api/version` reports it, the owner publishes the advisory with
`vulnerable < <release>`, `patched = <release>`, package `traxjourney` (O2).

## Definition of done

- No request to `GET /api/strava/callback`, with any query string, writes or
  changes a `StravaToken` row.
- A code relayed with the victim's state cannot be completed with any other
  state, including one the attacker minted for their own account with their
  own verifier (403 `code_not_bound`). Calling the callback again with the same
  code and the attacker's state does not rebind it.
- A disconnect racing a completion never answers 500, and never leaves
  freshly issued Strava tokens stored nowhere.
- `alembic heads` shows one head.
- A state issued for user A, completed with user B's bearer, links nobody (403),
  whichever of A and B started the flow.
- A correct code and state with the wrong verifier links nobody (403).
- `GET /api/strava/connect` answers 426 with the update message, and a state
  without `chal` is refused at the callback with `update_required`.
- On web, Connect → approve on Strava → the popup closes and Settings shows
  Strava connected, with no full-page navigation.
- On Android (release and debug builds), Connect → approve in the browser →
  the app reopens on Settings showing Strava connected. This holds even if the
  app was killed while the browser was in front, within 10 minutes.
- An Android flow whose state expired at Strava returns to the app with the
  expired message; it never ends on a browser page that says "Connecting…".
- No code, state, verifier or challenge appears in application logs, and no
  exception text appears in any redirect URL or response detail.
- Full server and Flutter suites pass. The owner has run the device check in U3.
