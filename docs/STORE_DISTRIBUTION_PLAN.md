# Store distribution — Plan for #579, #582, #580

Status: **plan only, nothing implemented yet.**

## 1. Problem

v1.0 is a public launch on web, Google Play and the App Store (milestone v1.0).
Today:

- **iOS (#579).** `flutter_client/ios/` holds an Xcode project with the bundle id
  `com.traxjourney.app`, but nothing builds, signs or uploads it. There is no
  macOS workflow, no TestFlight, no Universal Links, no privacy manifest.
- **Play (#582).** Android ships only as a sideloaded APK on each GitHub release.
  Play needs an App Bundle (AAB), a store entry, a data-safety form, an
  account-deletion web link and a testing track.
- **Sign in with Apple (#580).** The app offers Google Sign-In, so App Store
  guideline 4.8 requires an equivalent privacy-focused login. Without it the iOS
  submission will most likely be rejected.

Owner constraints (2026-10-12):

- The Apple Developer and Play Console accounts exist, and **both are personal**.
  A personal Play account created after November 2023 must run a **closed test
  with at least 12 testers for 14 days** before it can publish to production.
- **No Mac and no iPhone.** Every iOS build runs on GitHub's macOS runners. The
  only real-device test is TestFlight on someone else's iPhone, and that person
  has to be added as an App Store Connect user (an internal tester).
- **Store review waits for #581** (in-app purchase). Both stores reject an app
  that sells digital plans through Stripe inside the app. This package builds the
  pipelines and gets builds onto TestFlight and Play's internal track. It does
  not submit either app for store review.

## 2. Current state

| Area | Today | File |
|---|---|---|
| Android release | Signed arm64 APK on every `v*` and `validation` tag; fails fast without the `ANDROID_*` secrets; real APKs ship (v0.53.1 has one) | [android-release.yml](../.github/workflows/android-release.yml) |
| Build number | `vX.Y.Z → X·10000 + Y·100 + Z` | [scripts/android_version_code.py](../scripts/android_version_code.py) |
| PR build check | `android` job builds a debug APK on every PR | [test.yml](../.github/workflows/test.yml) |
| App Links | `/.well-known/assetlinks.json` from `ANDROID_CERT_FINGERPRINTS`, 404 when unset | [api/router.py:450-490](../api/router.py#L450-L490), [tests/test_assetlinks.py](../tests/test_assetlinks.py) |
| Deep-link paths | `/share`, `/join`, `/verify-email` on `traxjourney.com` | [AndroidManifest.xml:49-51](../flutter_client/android/app/src/main/AndroidManifest.xml#L49-L51) |
| iOS project | bundle id set; `CODE_SIGN_STYLE = Automatic`, no team; `TARGETED_DEVICE_FAMILY = "1,2"` (iPhone + iPad); no entitlements file; deployment target 13.0; Google and `traxjourney` URL schemes declared; `FlutterDeepLinkingEnabled` | [project.pbxproj](../flutter_client/ios/Runner.xcodeproj/project.pbxproj), [Info.plist](../flutter_client/ios/Runner/Info.plist) |
| Google login | `POST /api/auth/google`: verify the id_token, look up `UserInfo.google_sub`, create a shadow `LocalUser` + `UserInfo` on first sight, no linking by e-mail | [api/auth.py:292-362](../api/auth.py#L292-L362), [tests/test_google_auth.py](../tests/test_google_auth.py) |
| Account deletion | `delete_user_and_data` settles billing, then revokes Strava best-effort before taking the account lock | [src/auth/account_deletion.py:347-380](../src/auth/account_deletion.py#L347-L380), [src/auth/strava_deauth.py](../src/auth/strava_deauth.py) |
| Encrypted tokens | `EncryptedString` column type, e.g. `StravaToken` | [models/user.py:67-77](../models/user.py#L67-L77) |
| Legal pages | `/privacy`, `/terms` served from `legal/` by the API; placeholder gate test | [api/router.py:494-523](../api/router.py#L494-L523), [tests/test_legal_pages.py](../tests/test_legal_pages.py) |
| Client auth | `AuthService.loginWithGoogle`; login screen with the GIS button on web and a native picker on mobile | [auth_service.dart](../flutter_client/lib/src/auth/auth_service.dart), [login_screen.dart](../flutter_client/lib/src/auth/login_screen.dart), [test/auth/login_screen_test.dart](../flutter_client/test/auth/login_screen_test.dart) |
| Dependencies | `PyJWT`, `cryptography`, `google-auth`, `requests`, `httpx` already installed | [requirements.txt](../requirements.txt) |
| Launch QA | rows A4 (Apple, hidden e-mail) and A6 (in-app deletion) per platform | [docs/LAUNCH_QA.md](LAUNCH_QA.md) |

## 3. Decisions

**D1 — Sign in with Apple on iOS, web and Android.** iOS uses the native sheet.
Web uses Apple JS in popup mode. Android uses Apple's web flow in a Custom Tab,
which returns through a server callback. *Why:* someone who signs up with Apple
on an iPhone, often with a hidden e-mail, must still be able to sign in on the
web and on Android. *Ruled out:* iOS only. An Apple-only account would then be
locked out of every other platform.

**D2 — Apple accounts work exactly like Google accounts.** They are looked up by
the Apple `sub` and created with a shadow `LocalUser` on first sign-in. They are
never linked to an existing account by e-mail. *Why:* the issue asks for this,
and linking by e-mail allows an account to be taken over before its owner signs
up. *Ruled out:* automatic linking by e-mail. Linking providers to one account
is a separate feature, filed as a follow-up.

**D3 — The server exchanges the authorization code on every Apple sign-in and
stores Apple's refresh token, encrypted.** An exchange that fails rejects the
sign-in (502). *Why:* deleting an account must revoke the Apple token, and the
token can only be revoked with what this exchange returns. If we let a sign-in
through without the token, that account could never be revoked. The exchange
also proves the code, which adds a check beyond the id_token signature. *Ruled
out:* verifying the id_token only.

**D4 — A nonce is required.** The client generates a random nonce and gives
Apple its SHA-256. It then sends the raw nonce to the server, which checks that
`sha256(nonce)` equals the token's `nonce` claim. *Why:* without it, a leaked
id_token can be replayed. *Ruled out:* trusting the id_token alone, as the
Google flow does. Apple supports a nonce on all three platforms; Google's v7
plugin does not expose one for us.

**D5 — Two Apple audiences.** Native iOS tokens carry the bundle id
(`com.traxjourney.app`). Web and Android tokens carry the Services ID. The
server accepts either one. The code exchange and the later revoke use the same
`client_id` as the token's `aud`, so that `client_id` is stored with the
refresh token.

**D6 — Play App Signing uses the existing release key.** At the first Play
release, the owner uploads the current keystore as the app signing key through
Play's PEPK tool, and the same key stays the upload key. *Why:* Play builds and
GitHub APKs then share one signing identity. Existing sideload installs can move
to Play, and nothing changes in `ANDROID_CERT_FINGERPRINTS` or the Android OAuth
client. *Ruled out:* a key generated by Google. It would orphan every sideload
install, since one cannot update the other, and would need a second fingerprint
everywhere. **This choice is permanent and is made at the first release in Play
Console**: the owner must not accept the default.

**D7 — Our own script uploads to Play.** `scripts/play_upload.py` calls the
Android Publisher v3 edits API (insert → upload bundle → update track → commit),
authenticated with `google-auth`, which is already a dependency, and a
service-account JSON secret. It is unit-tested with a mocked HTTP session.
*Ruled out:* a third-party action that receives the service-account key. That is
supply-chain risk on a credential that can publish the app.

**D8 — Only real `vX.Y.Z` tags upload to Play.** `validation` builds keep
producing only the APK. *Why:* Play requires each upload's versionCode to be
strictly greater than every earlier upload, and the validation code
(`latest + 1`) would block the next patch release. Uploads go to the
**internal** track. Promotion to closed testing or production is done by hand.

**D9 — The APK stays on the GitHub release** next to the AAB upload, for
sideloading and self-hosters (owner decision).

**D10 — iOS signing is cloud-managed, with an App Store Connect API key.**
`xcodebuild archive` and `-exportArchive` run with `-allowProvisioningUpdates`
and the `-authenticationKey*` flags. Xcode then creates and keeps the
distribution certificate and profile itself. `ExportOptions.plist` uses
`method = app-store-connect` and `destination = upload`, so the export step
uploads straight to TestFlight. *Why:* there is no Mac to create or renew a
`.p12` and a profile, and no certificate expires inside a repo secret. *Ruled
out:* a manual `.p12` and profile in secrets, or fastlane match. Both need
renewal by hand, with nowhere to do it. A manual `.p12` stays the fallback if
cloud signing turns out not to work on the runner (see Escalate in U7).

**D11 — iOS build numbers.** `CFBundleShortVersionString` is `X.Y.Z`.
`CFBundleVersion` is the same integer as Android's versionCode for a real tag.
A `validation` build gets its own numbering:

- Its short version is the latest real tag with the **patch bumped**: after
  `v1.0.0` it is `1.0.1`. A validation build needs a published `vX.Y.Z`
  release; without one it fails.
- Its build number is `<latest real code>.<github.run_number>`, for example
  `10000.17`.

That build number is unique, increases from one run to the next, and stays
below the code the real `1.0.1` would get (`10001`). *Why:* App Store Connect
rejects a duplicate build number under the same version. It requires
`CFBundleShortVersionString` to be numeric, so the Android `validation-<sha>`
naming cannot be reused. And it closes a version's train to new builds once
that version is released on the App Store (ITMS-90186). A validation build
filed under the latest released version would therefore fail from launch
onward. The patch-bumped version is never a released one until the real tag
ships, and that tag's integer build number sorts above every validation build
under it (R1-3).

**D12 — Validation tags upload to TestFlight.** *Why:* TestFlight is the only
way to run the app on a real iPhone, and validation is where device testing
happens.

**D13 — A PR job builds iOS without signing.** A new `ios` job in `test.yml`
runs `flutter build ios --release --no-codesign`. It then checks that the bundle
contains `PrivacyInfo.xcprivacy` and that the entitlements file is wired in.
*Why:* with no Mac, a broken iOS build would otherwise surface only when a tag
is pushed. The repo is public, so macOS minutes are free. This mirrors the
`android` job.

**D14 — Universal Links.** `/.well-known/apple-app-site-association` is served
from `APPLE_TEAM_ID` and returns 404 when that is unset, the same pattern as
`assetlinks.json`. It covers the same three path prefixes as the Android
manifest, on `traxjourney.com` only, matching the URL the builds have baked in.

**D15 — The account-deletion page is public.** `/delete-account` is a static
legal page served like `/privacy`. It explains deletion in the app, on the web
and by e-mail. *Why:* Play requires a deletion URL that works without the app
installed.

## Review envelope

REVIEW.md defaults apply, plus:

- **New external service: Apple** (`appleid.apple.com`: the JWKS, `/auth/token`,
  `/auth/revoke`). It falls under E2: its responses can be slow, malformed,
  missing or hostile. Its JWKS rotates.
- **New public endpoint `POST /api/auth/apple/callback`.** It accepts a
  `form_post` from anyone and redirects to an `intent://` URL. In scope: open
  redirects, forwarding unbounded or unexpected fields, and reflecting input
  into HTML.
- **New server secret:** the Sign in with Apple private key (`.p8`), as runtime
  env vars only, following `.env.example`.
- **New CI secrets that can publish the app:** the App Store Connect API key and
  the Play service-account JSON. In scope: leaking them into logs or artifacts,
  exposing them to `pull_request` runs (forks), and third-party actions that see
  them.
- **Unverifiable on the dev machine:** nothing iOS can be built or run locally.
  The iOS units are verified only by CI runs (the `ios` PR job and a
  `validation` tag run). A finding that needs a Mac to reproduce is still in
  scope if the CI log or the built bundle can show it.
- **Out of scope:** store review outcomes, store listing content, and the
  owner's legal answer on encryption export (O1).

## 4. Boundaries crossed

- **API, additive only.** New: `POST /api/auth/apple`,
  `POST /api/auth/apple/callback`, `GET /.well-known/apple-app-site-association`
  and `GET|HEAD /delete-account`. Existing endpoints are unchanged, so installed
  clients (E5) are unaffected.
- **Schema.** A new column `userinfo.apple_sub` (string, default `""`, indexed)
  and a new table `appletoken` (`user_info_id` unique FK,
  `refresh_token EncryptedString`, `client_id`). Both are added in one Alembic
  migration. Existing rows get `apple_sub = ""`. `auth_provider` gains the value
  `"apple"`. It is a free string column, so the admin views show it as is.
  Before opening the PR, check `alembic heads` after rebasing: PR #608 may land
  a migration first.
- **Stored data.** Apple refresh tokens, encrypted at rest. They are deleted
  with the account, after the revoke.
- **Export formats.** None. The account export does not include
  `google_sub` today, and `apple_sub` follows it.
- **Legal text.** `legal/privacy.html` gains Sign in with Apple, Apple as a
  recipient, and the two stores. The owner approves the wording in the PR.

## 5. Conventions

- **API contract for `POST /api/auth/apple`.** U2 and U9 build against this
  contract in parallel:

  ```
  request  { "id_token": str, "authorization_code": str, "nonce": str,
             "given_name": str | null, "family_name": str | null }   // names ≤ 100 chars
  200      TokenResponse                         // same model as /api/auth/google
  401      { "detail": "Invalid Apple id_token" }   // bad signature/iss/aud/exp/nonce, or code rejected (4xx)
  502      { "detail": "Apple sign-in unavailable" } // JWKS down, /auth/token 5xx or timeout
  503      { "detail": "Apple auth not configured" }
  ```

  The names are used only when the account is created. Apple sends them on the
  first sign-in only.

- **Callback contract.** `POST /api/auth/apple/callback` takes form fields. It
  returns a `303` to
  `intent://callback?<urlencoded fields>#Intent;package=com.traxjourney.app;scheme=signinwithapple;end`,
  forwarding **only** `code`, `id_token`, `state`, `user` and `error`, each at
  most 8 KiB. Any other field is dropped. The response never includes HTML
  built from the input.

- **Server env vars**, all runtime and listed in `.env.example`:
  - `APPLE_TEAM_ID`: used by both Sign in with Apple and the AASA file.
  - `APPLE_SIGNIN_KEY_ID`.
  - `APPLE_SIGNIN_PRIVATE_KEY`: PEM text, with literal `\n` sequences accepted.
  - `APPLE_BUNDLE_ID`: defaults to `com.traxjourney.app`.
  - `APPLE_SERVICES_ID`.
  - `APPLE_REDIRECT_URI`: this deployment's own
    `https://<host>/api/auth/apple/callback`, so val and prod each have their
    own value. It is sent with the code exchange for Services-ID codes (web and
    Android), which Apple requires whenever the authorization request carried
    one. The clients build the same URI from the origin they post to, so the two
    always match (R1-1). That origin is `api.baseUrl`, or `Uri.base.origin` when
    the base is empty, which is the case for the production and val web builds
    (they are same-origin) (R2-1).

  Apple sign-in counts as configured when the team id, key id and private key
  are set.

- **Logging.** Apple failures log the exception class and a fixed reason label.
  They never log a token, a code or the exception text. This is the rule from
  `test_google_login_failure_never_logs_the_token` (#510).

- **Release notes.** User-visible commits carry a `Release-Note:` trailer
  (docs/RELEASING.md).

## 6. Open decisions

| # | Decision | Affects | Decide by |
|---|---|---|---|
| O1 | **Encryption export compliance.** *Decided 2026-10-12:* the owner answers the compliance question by hand on each build in App Store Connect. `ITSAppUsesNonExemptEncryption` stays out of `Info.plist`. The legal side (the US self-classification report, and an ANSSI declaration for France) is the owner's to settle before the first App Store submission. | U6 (no plist key) | Decided |
| O2 | **iPad.** *Decided 2026-10-12:* iPhone only for v1 (`TARGETED_DEVICE_FAMILY = 1`). iPad can be added in a later release. | U6 | Decided |
| O3 | **When the Play closed test starts.** *Decided 2026-10-12:* after #581 ships, so the closed-track build that Play reviews carries Play Billing, not Stripe. This accepts 14 extra days on the critical path after #581. | Launch date; no unit | Decided |
| O4 | **Store screenshots.** The App Store wants 6.9-inch iPhone screenshots, and there is no simulator here. Options: the owner borrows an iPhone over TestFlight, or a follow-up issue adds a macOS workflow that takes screenshots on the simulator through an integration test. | Owner task, possibly a follow-up issue | Before the first App Store submission |

## 7. Execution units

### Wave 1 — parallel, disjoint files

#### U1 — Apple identity library

- **Goal:** A pure, tested module that verifies Apple id_tokens, mints the
  client secret, exchanges a code and revokes a token.
- **Scope:** `src/auth/apple.py` (new), `tests/test_apple_identity.py` (new).
- **Context:** Read D3–D5 and Conventions. Follow the shape of
  [src/auth/strava_deauth.py](../src/auth/strava_deauth.py): plain arguments,
  no ORM, logs without tokens. Use PyJWT (`PyJWKClient` against
  `https://appleid.apple.com/auth/keys`, with caching) and `requests`, as the
  Strava revoke does.
- **Do:**
  1. `verify_identity_token(token, nonce, audiences) -> AppleIdentity(sub, email, email_verified, is_private_email, aud)`.
     Check RS256, `iss == https://appleid.apple.com`, `aud ∈ audiences`, `exp`
     with 10 s leeway, and `nonce == sha256_hex(nonce)`. Raise
     `AppleTokenInvalid(reason)` with a fixed reason label. JWKS or network
     failures raise `AppleUnavailable`. Parse `email_verified` and
     `is_private_email` as either a JSON boolean or the string `"true"` or
     `"false"`, which is what Apple usually sends. Anything else, or a missing
     claim, is False. Never use `bool(value)` (R1-2).
  2. `client_secret(team_id, key_id, private_key_pem, client_id, now)`: an
     ES256 JWT with `iss` = team, `sub` = client_id,
     `aud = https://appleid.apple.com` and a lifetime of at most 5 minutes.
  3. `exchange_code(code, client_id, secret, redirect_uri) -> refresh_token`,
     by POSTing to `/auth/token`. `redirect_uri` is sent when it is not None:
     callers pass it for Services-ID codes and None for bundle-id (native iOS)
     codes (R1-1). A 4xx answer, such as `invalid_grant`, raises
     `AppleTokenInvalid("code_rejected")`, so a configuration mistake shows as
     a rejected code and not as an Apple outage. A 5xx answer, a timeout or a
     missing `refresh_token` raises `AppleUnavailable`.
  4. `revoke(refresh_token, client_id, secret) -> bool`, by POSTing to
     `/auth/revoke` with `token_type_hint=refresh_token`. It is best effort:
     it returns False and logs on failure, and never raises.
  5. A loader for the env config, following Conventions (the `\n` unescape
     included).
- **Acceptance:** `tests/test_apple_identity.py` passes. It uses a locally
  generated RSA key as a fake JWKS and an EC P-256 key for the secret, and
  covers: a valid token; the wrong `aud`, `iss` or nonce; an expired token; an
  unknown `kid`; JWKS down; `email_verified` and `is_private_email` as `"true"`,
  `"false"`, `true`, `false` and missing (`"false"` must give False); secret
  claims and lifetime; exchange success, `redirect_uri` sent when given and
  absent when None, 4xx → `AppleTokenInvalid`, 5xx or timeout →
  `AppleUnavailable`, and a missing field; revoke success and failure; and logs never
  containing the token, code or secret (`caplog`). `pytest
  tests/test_apple_identity.py` passes. Network is mocked.
- **Out of scope:** routes, the DB, server-to-server notifications.
- **Latitude:** local design.
- **Escalate if:** PyJWT cannot verify against a JWK set without a new
  dependency; anything outside Scope is needed.
- **Depends on:** —

#### U4 — Store-facing server pages

- **Goal:** Serve the Apple Universal Links file and the public
  account-deletion page, and bring the privacy policy up to date.
- **Scope:** `api/router.py` (the well-known and legal sections only),
  `legal/delete-account.html` (new), `legal/privacy.html`,
  `tests/test_apple_app_site_association.py` (new),
  `tests/test_legal_pages.py`, `.env.example` (only the `APPLE_TEAM_ID` line,
  with a comment).
- **Context:** D14, D15. Follow `android_assetlinks` and
  [tests/test_assetlinks.py](../tests/test_assetlinks.py) for the AASA route,
  and `/privacy` with its placeholder gate for the page. Register both before
  the SPA catch-all.
- **Do:**
  1. `GET /.well-known/apple-app-site-association`. Return 404 when
     `APPLE_TEAM_ID` is unset. Otherwise return `application/json` with
     `applinks.details[0].appIDs = ["<TEAM>.com.traxjourney.app"]` and
     `components` for `/share/*`, `/join/*` and `/verify-email/*`. Do not
     redirect.
  2. `GET|HEAD /delete-account`, serving `legal/delete-account.html`: delete in
     the app (Settings), on the web at traxjourney.com, or by e-mail to the
     contact address already in `privacy.html`. Say what is deleted and what is
     kept, taken from `privacy.html`'s existing retention text, not new claims.
  3. In `privacy.html`, add Sign in with Apple (what Apple sends: the
     identifier, an e-mail that may be a relay address, and the name on first
     sign-in), Apple as a recipient, and the App Store and Play as distribution
     channels. Link `/delete-account`.
- **Acceptance:** `tests/test_apple_app_site_association.py` covers 404 when
  unset, the exact JSON and content type, the paths matching
  `AndroidManifest.xml`, and a route that is not shadowed by the SPA fallback.
  `tests/test_legal_pages.py` covers `/delete-account` (200, HTML, no
  placeholders). `pytest tests/test_apple_app_site_association.py
  tests/test_legal_pages.py tests/test_assetlinks.py` passes.
- **Out of scope:** `webcredentials`, a `val.traxjourney.com` AASA, any other
  legal page.
- **Latitude:** local design for the page layout; none for the legal claims.
- **Escalate if:** a statement in the privacy text would need a fact that the
  code or the existing text does not support.
- **Depends on:** —

#### U6 — iOS project: entitlements, privacy manifest, PR build check

- **Goal:** Get the Xcode project ready to sign for the store, and catch a
  broken iOS build on every PR.
- **Scope:** `flutter_client/ios/Runner/Runner.entitlements` (new),
  `flutter_client/ios/Runner/PrivacyInfo.xcprivacy` (new),
  `flutter_client/ios/ExportOptions.plist` (new),
  `flutter_client/ios/Runner.xcodeproj/project.pbxproj`,
  `flutter_client/ios/Runner/Info.plist`, `.github/workflows/test.yml` (a new
  `ios` job only).
- **Context:** D10, D13, D14 and O1/O2. Follow the `android` job in
  [test.yml](../.github/workflows/test.yml). The data types in the privacy
  manifest come from [legal/privacy.html](../legal/privacy.html), which is
  written from what the code does.
- **Do:**
  1. Entitlements: `com.apple.developer.associated-domains =
     [applinks:traxjourney.com]` and `com.apple.developer.applesignin =
     [Default]`. Set `CODE_SIGN_ENTITLEMENTS` for the Runner's Debug, Release
     and Profile configurations.
  2. Privacy manifest: `NSPrivacyTracking = false`, no tracking domains,
     `NSPrivacyCollectedDataTypes` matching `privacy.html` (none used for
     tracking), and `NSPrivacyAccessedAPITypes` for the app's own use (for
     example, UserDefaults through shared_preferences, reason `CA92.1`; check
     which plugins already ship their own manifest). Add it to the Runner
     target's Resources build phase in `project.pbxproj`.
  3. Write `ExportOptions.plist` as described in D10, with `teamID` left as a
     placeholder that the workflow fills in with `plutil`.
  4. iPhone only (O2): set `TARGETED_DEVICE_FAMILY = 1` in every Runner
     configuration, and drop the `~ipad` orientations from `Info.plist`.
  5. Add the `ios` job: `macos-latest`, Flutter stable,
     `flutter build ios --release --no-codesign`. Then assert that
     `build/ios/iphoneos/Runner.app/PrivacyInfo.xcprivacy` exists and that the
     build settings show `CODE_SIGN_ENTITLEMENTS` (`xcodebuild
     -showBuildSettings`).
- **Acceptance:** the `ios` job passes on the PR, and its log shows both
  assertions. `plutil -lint` on the three new plists passes in that job.
  `flutter test` and the `android` job stay green.
- **Out of scope:** signing and upload (U7); `ITSAppUsesNonExemptEncryption`
  (O1: answered by hand per build); the Sign in with Apple Dart code (U9).
- **Latitude:** local design, listing the pbxproj identifiers used.
- **Escalate if:** the pbxproj edit makes Flutter regenerate or reject the
  project; a plugin's privacy manifest conflicts with the app's.
- **Depends on:** —

#### U8 — Android: AAB, Play upload, target-SDK gate

- **Goal:** Every `vX.Y.Z` tag uploads a signed AAB to Play's internal track
  and still attaches the APK.
- **Scope:** `.github/workflows/android-release.yml`, `scripts/play_upload.py`
  (new), `tests/test_play_upload.py` (new), `docs/ANDROID.md`.
- **Context:** D6–D9. Follow
  [scripts/android_version_code.py](../scripts/android_version_code.py) for the
  script shape (a docstring that explains why, `main(argv)`, explicit errors).
  The Android Publisher v3 edits API is documented at
  developers.google.com/android-publisher.
- **Do:**
  1. After the APK build, run `flutter build appbundle --release` with the same
     `--build-number`, `--build-name` and dart-defines.
  2. Gate: read `targetSdkVersion` from the built APK (`aapt2 dump badging`)
     and fail below `PLAY_MIN_TARGET_SDK`. That is a workflow env value, with a
     comment naming Play's current requirement and the yearly August deadline.
  3. `play_upload.py <aab> --package com.traxjourney.app --track internal
     --release-name vX.Y.Z`. Credentials come from `PLAY_SERVICE_ACCOUNT_JSON`
     (env, never argv). The steps are edits.insert, bundles.upload,
     tracks.update (status `completed`) and edits.commit. Errors print the API
     status and message only. On failure, delete the edit.
  4. In the workflow, run the Play steps only when the tag matches
     `^v[0-9]+\.[0-9]+\.[0-9]+$` (D8). If the secret is missing, print a
     `::warning::` and skip the upload, so the APK still ships. Attach the AAB
     to the release next to the APK.
  5. In `docs/ANDROID.md`, change the distribution row and add a Play section
     with the owner steps from §7 "Owner actions", including the D6 warning in
     bold.
- **Acceptance:** `tests/test_play_upload.py` mocks the HTTP session and
  covers: the call order and payloads; the track body for `internal`; edit
  deletion on upload failure; the credential never appearing in output; and a
  usage error on missing args. `pytest tests/test_play_upload.py` passes.
  `actionlint` (or the workflow parsing in a `validation` run) is clean.
  **Owner check:** after the owner's first manual release (Owner actions A5),
  the next real tag's run shows the bundle on the internal track.
- **Out of scope:** promotion to other tracks, release notes uploaded to Play,
  store-listing automation.
- **Latitude:** local design.
- **Escalate if:** Play needs a draft-status release for an app that has never
  been published, beyond the first manual one; the versionCode scheme clashes
  with anything Play reports.
- **Depends on:** —

#### U9 — Client: Sign in with Apple on iOS, web, Android

- **Goal:** An Apple button that signs in through `POST /api/auth/apple` on all
  three platforms.
- **Scope:** `flutter_client/pubspec.yaml` and `pubspec.lock`,
  `flutter_client/lib/src/auth/auth_service.dart`,
  `flutter_client/lib/src/auth/login_screen.dart`,
  `flutter_client/lib/src/auth/apple_sign_in.dart` (new),
  `flutter_client/web/index.html` (the Apple JS script tag only),
  `flutter_client/android/app/src/main/AndroidManifest.xml` (the callback
  activity only), `flutter_client/test/auth/` (new and changed tests).
- **Context:** D1, D4, D5, and the API and callback contracts in Conventions.
  Follow the Google path in `login_screen.dart` and
  `AuthService.loginWithGoogle`. Use the `sign_in_with_apple` package with
  `webAuthenticationOptions` (Services ID, and redirect
  `<origin>/api/auth/apple/callback`) on Android and web. Its README gives the
  Android activity and the web script. `<origin>` is
  `api.baseUrl.isEmpty ? Uri.base.origin : api.baseUrl`: the web build has no
  `API_BASE_URL` and posts same-origin
  ([client.dart:12-14](../flutter_client/lib/src/api/client.dart#L12-L14)).
  Follow [share_link_resolver_impl.dart:21-23](../flutter_client/lib/src/share/share_link_resolver_impl.dart#L21-L23),
  which does the same (R2-1).
- **Do:**
  1. In `apple_sign_in.dart`: generate a 32-byte random nonce, request a
     credential with the SHA-256 nonce and the `email` and `fullName` scopes,
     and return the id_token, code, raw nonce and names. Put the platform
     specifics behind one function.
  2. `AuthService.loginWithApple(...)` posts to the contract.
  3. Show the Apple button on the login screen on all three platforms, wherever
     Google's sits, using the package's `SignInWithAppleButton`, which meets
     Apple's button rules. Treat user cancellation as silent. Show other errors
     the way the Google ones are shown.
  4. Take the Services ID from `--dart-define=APPLE_SERVICES_ID`. Without it,
     hide the button on web and Android. On iOS, show it always.
- **Acceptance:** widget tests in `test/auth/` cover: the button is present
  when configured, and absent on web and Android when it is not; cancel shows
  nothing; a successful credential calls `loginWithApple` with the raw nonce,
  and the hash sent to Apple equals `sha256(raw)`; a 401 or 502 shows an error;
  the redirect URI is absolute in both cases, `https://traxjourney.com/api/auth/apple/callback`
  with an empty base on a `traxjourney.com` page and `<base>/api/auth/apple/callback`
  with a set base. Keep the URI building in a pure function so it can be tested.
  The plugin is faked behind `apple_sign_in.dart`. `flutter test` passes in the
  Flutter test container, `flutter analyze` is clean, and the `android` and
  `ios` PR jobs stay green.
- **Out of scope:** the register and welcome screens, beyond what the login
  screen already covers for them (check whether the Google button appears
  there; if it does, add Apple next to it and list it); linking accounts.
- **Latitude:** local design.
- **Escalate if:** the package needs a Flutter or iOS deployment-target bump; a
  CSP or other header blocks the Apple JS on web.
- **Depends on:** — (builds against the contract; integrated after U2)

### Wave 2

#### U2 — Server: Apple login endpoint, callback, schema

- **Goal:** `POST /api/auth/apple` and `/callback` per the contract, with the
  account model and migration.
- **Scope:** `api/auth.py` (new routes plus a request model only),
  `models/user.py` (`UserInfo.apple_sub` and the `AppleToken` model), a new
  `alembic/versions/<rev>_add_sign_in_with_apple.py`,
  `tests/test_apple_auth.py` (new), `.env.example` (the Apple lines other than
  `APPLE_TEAM_ID`).
- **Context:** D2, D3, D5 and Conventions. Follow `google_login`
  ([api/auth.py:292-362](../api/auth.py#L292-L362)) and its tests in
  [tests/test_google_auth.py](../tests/test_google_auth.py). Follow
  `StravaToken` for the encrypted column. Read the DB migrations memory rule:
  Alembic only, and check `alembic heads` after rebasing.
- **Do:**
  1. In the model, add `apple_sub` (indexed) and `AppleToken`. Write the
     migration with a down-migration.
  2. The route: return 503 if not configured. Verify through U1 with
     audiences `{bundle id, services id}` (unset ones left out). Exchange the
     code with the token's `aud` as `client_id`, passing `APPLE_REDIRECT_URI`
     when `aud` is the Services ID and None when it is the bundle id. A
     rejected code returns 401; Apple being unavailable returns 502. Look up
     by `apple_sub`. On first sight, create the shadow `LocalUser` and
     `UserInfo` (`auth_provider = "apple"`, `email_verified` from the claim,
     the display name from the given and family names, falling back to the
     e-mail's local part as Google does, `is_admin` from the e-mail as Google
     does), and upsert `AppleToken` with the refresh token and `client_id` on
     every sign-in. Count metrics with the `"apple"` labels.
  3. The callback, per the callback contract.
- **Acceptance:** `tests/test_apple_auth.py`, with U1 patched at its function
  boundary, covers: 503 when unconfigured; 401 on an invalid token; 401 on a
  rejected code and 502 when Apple is unavailable, with no account created
  either way; `exchange_code` getting `APPLE_REDIRECT_URI` for a Services-ID
  token and None for a bundle-id token; `email_verified` stored as a real
  boolean; a first sign-in that creates the
  account with names and a relay e-mail; a second sign-in that finds the same
  account, ignores names and updates the stored token; a Google user with the
  same e-mail staying a separate account; the callback forwarding only allowed
  fields, capping sizes, returning a 303 to the exact intent prefix, and
  redirecting nowhere else; and logs never containing a token or code. Also:
  `alembic upgrade head` then `downgrade -1` on a file SQLite,
  `tests/test_migrations*` if present, and the full `pytest` suite.
- **Out of scope:** account deletion (U3), linking, Apple server-to-server
  notifications.
- **Latitude:** local design inside the contract.
- **Escalate if:** `alembic heads` shows two heads; the contract cannot be met
  as written.
- **Depends on:** U1.

### Wave 3

#### U3 — Revoke Apple on account deletion

- **Goal:** Deleting an account revokes its Apple token and removes the
  `AppleToken` row.
- **Scope:** `src/auth/account_deletion.py`,
  `tests/test_account_deletion_apple.py` (new),
  `tests/test_account_deletion_owned_tables.py` (only if it lists owned
  tables).
- **Context:** Follow the Strava revoke block in
  [account_deletion.py:362-374](../src/auth/account_deletion.py#L362-L374): read
  the fields, make the call without holding the lock, best effort.
- **Do:** Next to the Strava revoke, read the `AppleToken` row, build the
  client secret, and call `revoke` with the stored `client_id`. Delete the row
  with the user's other rows, whatever Apple answered.
- **Acceptance:** `tests/test_account_deletion_apple.py` covers: revoke called
  with the stored token and `client_id`; the row deleted after success; the row
  still deleted, and the failure logged without the token, when revoke fails; a
  refused deletion (billing) not revoking; and the admin delete path revoking
  too. `test_account_deletion_owned_tables.py` passes, with `appletoken` listed
  if that test enumerates tables. The full `pytest` suite passes.
- **Out of scope:** retrying a failed revoke.
- **Latitude:** none.
- **Escalate if:** the deletion's lock ordering needs to change.
- **Depends on:** U1, U2.

#### U7 — iOS release workflow

- **Goal:** Every `vX.Y.Z` and `validation` tag uploads a signed build to
  TestFlight without manual steps (O1 aside).
- **Scope:** `.github/workflows/ios-release.yml` (new),
  `scripts/ios_build_number.py` (new), `tests/test_ios_build_number.py` (new),
  `docs/IOS.md` (new), `docs/RELEASING.md` (one line pointing to it),
  `docs/LAUNCH_QA.md` (only the Setup lines for TestFlight and Play, if any are
  missing).
- **Context:** D10–D12. Mirror
  [android-release.yml](../.github/workflows/android-release.yml): fail fast
  without secrets, resolve the version, build, verify. Follow
  `scripts/android_version_code.py` and its tests, and import `version_code`
  from it rather than copying the formula.
- **Do:**
  1. `ios_build_number.py <tag> [--validation-run N --latest-tag vX.Y.Z]`
     prints `build_name build_number` per D11.
  2. The workflow runs on `macos-latest` and fails fast without
     `APP_STORE_CONNECT_KEY_ID`, `APP_STORE_CONNECT_ISSUER_ID`,
     `APP_STORE_CONNECT_KEY_P8_BASE64` or `APPLE_TEAM_ID`. It writes the key to
     `$RUNNER_TEMP` and never echoes it. Then: `flutter build ios --release
     --config-only` with the build name, number and the same dart-defines as
     Android (plus `APPLE_SERVICES_ID`); `xcodebuild archive` on
     `Runner.xcworkspace` with `DEVELOPMENT_TEAM`, `-allowProvisioningUpdates`
     and the auth-key flags; then `xcodebuild -exportArchive` with
     `ExportOptions.plist` (teamID filled in). Before uploading, verify that
     the archive's entitlements contain the associated domain and applesignin.
  3. In `docs/IOS.md`, write up the Apple-side owner steps from §7 "Owner
     actions", the secrets table, the build-number scheme, and "no Mac: how to
     debug a failing run".
- **Acceptance:** `tests/test_ios_build_number.py` covers: a real tag; a
  validation build after `v1.0.0`, which gives `1.0.1` and `10000.<run>`, not
  `1.0.0`; a validation build before any release (`0.0.1`, `0.<run>`); a
  validation build number that sorts below the real code of its short version
  (`10000.N < 10001`) and above the previous run's; and a bad tag.
  `pytest tests/test_ios_build_number.py` passes. **Owner check:** a
  `validation` tag run ends with the build in TestFlight, and the run log shows
  no secret.
- **Out of scope:** submitting for review (held for #581), TestFlight
  external-group distribution, screenshots.
- **Latitude:** local design.
- **Escalate if:** cloud-managed signing fails on the runner (fallback: a
  manual `.p12` plus profile, which needs the owner to generate the CSR with
  openssl on Windows; ask first); App Store Connect rejects the D11 build
  number.
- **Depends on:** U6 (entitlements and ExportOptions), U9 (the
  `APPLE_SERVICES_ID` dart-define).

### Owner actions (not units)

These happen in the store consoles, in order where it matters.

**Apple** (A1 and A2 before U7's owner check):

- **A1 — App and capabilities.** Developer portal: on the App ID
  `com.traxjourney.app`, enable Sign in with Apple and Associated Domains.
  Create the Services ID (for example `com.traxjourney.app.signin`) with
  domains `traxjourney.com` and `val.traxjourney.com` and return URL
  `https://traxjourney.com/api/auth/apple/callback` (and the val one). Create a
  Sign in with Apple key (`.p8`). Note the team id and key id.
- **A2 — CI access.** App Store Connect: create the app record, and an API key
  with the **Admin** role (cloud signing needs it). Add the three `APP_STORE_CONNECT_*`
  and `APPLE_TEAM_ID` repository secrets. Add the iPhone-owning tester as an
  App Store Connect user and internal tester.
- **A3 — Server config.** Set the `APPLE_*` env vars on val and prod
  (DEPLOYMENT_VPS.md), each with its own `APPLE_REDIRECT_URI`, and redeploy, which turns on AASA and Apple sign-in. Add
  `APPLE_SERVICES_ID` as a repository variable for the client builds.
- **A4 — Relay e-mail.** Developer portal, Sign in with Apple for Email
  Communication: register the sending domain and address used through Brevo,
  with SPF passing. Our e-mails to `@privaterelay.appleid.com` arrive only after
  this.

**Google Play:**

- **A5 — First release, by hand.** Play Console: create the app
  `com.traxjourney.app`. Upload the first AAB by hand to the internal track,
  from a release's assets. When asked about app signing, **choose to upload
  your own key (PEPK) with the existing release keystore (D6)**. Then confirm
  that the app-signing SHA-256 Play shows equals `ANDROID_CERT_FINGERPRINTS`,
  and that the SHA-1 equals the Android OAuth client's. This closes #136's owed
  steps too.
- **A6 — CI upload.** Google Cloud: create a service account and grant it
  release permission for this app in Play Console. Add the
  `PLAY_SERVICE_ACCOUNT_JSON` secret.
- **A7 — Store forms.** Play Console app content: privacy policy
  (`/privacy`), account deletion (`/delete-account`), the data-safety form
  (from `privacy.html`), content rating, target audience (18+ per the terms),
  and the store listing.
- **A8 — Closed test.** Recruit 12 or more closed testers now. Start the
  14-day closed test once #581 ships (O3).

**Both, after #581:** store listings and screenshots (O4), then submission for
review.

## 8. Definition of done

**For this package:**

1. A `validation` tag produces a TestFlight build with no manual step other
   than the O1 compliance answer. The run logs contain no secret.
2. A `vX.Y.Z` tag produces a TestFlight build, an AAB on Play's internal track,
   and the APK and AAB on the GitHub release.
3. On a TestFlight build, a tester can sign up with Apple using "Hide my
   e-mail", sign out and sign back in to the same account. The account's
   display name comes from the first sign-in.
4. The same Apple ID signs in to that same account on web and on Android.
5. Deleting that account, in the app or by an admin, calls Apple's revoke, and
   no `appletoken` row remains.
6. `https://traxjourney.com/share/<token>` opens the app on the TestFlight
   build and on a Play-installed build.
7. Google Sign-In works on a Play-installed build. Play's app-signing
   fingerprint equals `ANDROID_CERT_FINGERPRINTS`.
8. `/delete-account` and the updated `/privacy` are live, and the Play forms
   point at them.
9. The `ios` PR job runs on every PR. The full `pytest` suite, `flutter test`
   and `flutter analyze` are green.
10. LAUNCH_QA rows A4 and A6 can be run on all three platforms.

**Left for #581:** submitting to App Store review and promoting to Play
production. #579 and #582 close only then. #580 can close once items 3–5 hold.
