# iOS

The Flutter client ships to iPhones through TestFlight. Every `vX.Y.Z` tag and
every move of the `validation` tag runs
[ios-release.yml](../.github/workflows/ios-release.yml), which builds, signs and
uploads the app to App Store Connect. Submitting a build for App Store review is
done by hand (#581). Nobody on the project has a Mac, so everything below is
done in a browser, from Windows, or by the workflow.

| | |
|---|---|
| Bundle id | `com.traxjourney.app`, permanent |
| Devices | iPhone only (`TARGETED_DEVICE_FAMILY = 1`); iPad can come later |
| Distribution | TestFlight, from every `vX.Y.Z` and `validation` tag |
| API endpoint | Baked at build time: `--dart-define=API_BASE_URL=https://traxjourney.com` |
| Signing | Cloud-managed by Xcode, with an App Store Connect API key |

## Owner setup

Done once, in this order. A1 and A2 must be done before the first run can
succeed.

### A1: App ID and capabilities

In the Apple Developer portal (Certificates, Identifiers & Profiles):

1. On the App ID `com.traxjourney.app`, enable **Sign in with Apple** and
   **Associated Domains**. Without them the signed app has no entitlement for
   either, and the workflow's "Verify the archived app" step fails.
2. Create a **Services ID** (for example `com.traxjourney.app.signin`) and
   configure Sign in with Apple on it: domains `traxjourney.com` and
   `val.traxjourney.com`, return URLs
   `https://traxjourney.com/api/auth/apple/callback` and
   `https://val.traxjourney.com/api/auth/apple/callback`. Web and Android sign
   in with it.
3. Create a **Sign in with Apple key** and download its `.p8`. This one is for
   the server (A3), not for CI. Note its key id.
4. Note the **team id** (top right of the portal, or Membership).

### A2: CI access

In App Store Connect:

1. Create the app record for `com.traxjourney.app`.
2. Users and Access, Integrations, App Store Connect API: create a **team** key
   with the **Admin** role. Cloud-managed signing creates certificates and
   profiles, which lower roles may not do. Download the `.p8` (it can only be
   downloaded once) and note the key id and the issuer id.
3. Add the repository secrets in the table below.
4. Add the person with the iPhone as an App Store Connect user, then as an
   internal tester in a TestFlight internal group.

### A3: Server and client config

- On val and prod, set the `APPLE_*` server env vars (`.env.example`,
  [DEPLOYMENT_VPS.md](DEPLOYMENT_VPS.md)): `APPLE_TEAM_ID`,
  `APPLE_SIGNIN_KEY_ID`, `APPLE_SIGNIN_PRIVATE_KEY` (the A1 key),
  `APPLE_SERVICES_ID`, and `APPLE_REDIRECT_URI`, which is each host's own
  `https://<host>/api/auth/apple/callback`. Redeploy. That turns on Sign in
  with Apple and serves `/.well-known/apple-app-site-association`, which share
  links need to open the app.
- Add `APPLE_SERVICES_ID` as a repository **variable** (not a secret). The
  client builds read it; without it web and Android hide the Apple button.

### A4: E-mail to private relay addresses

Users who pick "Hide my e-mail" get an `@privaterelay.appleid.com` address.
Apple only forwards mail from registered senders. In the Developer portal,
Sign in with Apple for Email Communication: register the sending domain and
address used through Brevo, and check that SPF passes. Until then, verification
and password e-mails to those users never arrive.

## Secrets

| Name | Kind | Value |
|---|---|---|
| `APP_STORE_CONNECT_KEY_ID` | secret | The A2 API key's id |
| `APP_STORE_CONNECT_ISSUER_ID` | secret | The issuer id shown above the API keys list |
| `APP_STORE_CONNECT_KEY_P8_BASE64` | secret | The A2 `.p8`, base64-encoded (below) |
| `APPLE_TEAM_ID` | secret | The A1 team id |
| `MAPBOX_TOKEN` | secret | Already set for Android |
| `APPLE_SERVICES_ID` | variable | The A1 Services ID |

The workflow fails at its first step when one of the four Apple secrets is
missing. To encode the key on Windows, without changing its bytes:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("$PWD\AuthKey_ABC123XYZ.p8")) | Set-Clipboard
```

The workflow writes the key to the runner's temp directory, passes it to
`xcodebuild` by path, and deletes it at the end, whether the run passed or not.

## Build numbers

App Store Connect files each upload under its version
(`CFBundleShortVersionString`) and refuses a build number (`CFBundleVersion`)
it already has under that version. Once a version is released on the App
Store, it accepts no more builds under it.
[scripts/ios_build_number.py](../scripts/ios_build_number.py) picks both
(plan D11):

| Tag | Version | Build number | Example |
|---|---|---|---|
| `vX.Y.Z` | `X.Y.Z` | Android's versionCode, `X*10000 + Y*100 + Z` | `v1.0.0` gives `1.0.0`, `10000` |
| `validation` | the latest release, patch bumped | `<latest code>.<run number>` | after `v1.0.0`, run 17 gives `1.0.1`, `10000.17` |
| `validation`, before any release | `0.0.1` | `0.<run number>` | run 17 gives `0.0.1`, `0.17` |

Build numbers compare number by number, so `10000.17` sorts above `10000.16`
and below the `10001` that the real `v1.0.1` gets. A validation build never
lands under a released version, because `1.0.1` is not released until its tag
ships.

## Each build

- **Export compliance.** `ITSAppUsesNonExemptEncryption` is deliberately not
  in `Info.plist` (plan O1), so App Store Connect asks the encryption question
  on every build. Answer it under TestFlight, on the build, before testers can
  install it.
- **Processing.** The upload finishes when the run ends, but App Store Connect
  takes a few minutes to process the build before it appears in TestFlight.
  Processing problems (a missing privacy reason, an invalid binary) arrive by
  e-mail from App Store Connect, not in the run.

## Debugging a failing run without a Mac

The run log is all there is, so read the first step that failed, and its first
`error:` line rather than the last.

- **Fail fast step.** A secret is missing or misspelled: compare with the
  table above.
- **Resolve build version.** A tag that is not `vX.Y.Z` (for example
  `v1.0.0-rc1`) is refused, because App Store Connect only takes numeric
  versions. Android still builds such a tag; iOS does not.
- **Configure the Xcode project.** A Flutter or CocoaPods problem. The PR `ios`
  job in [test.yml](../.github/workflows/test.yml) runs the same project build
  unsigned, so it should have failed on the PR too; reproduce there.
- **Archive.** Usually signing. "No Accounts", "No profiles for
  'com.traxjourney.app' were found" or a cloud signing permission error mean
  the API key is not an Admin team key (A2), or the App ID lacks a capability
  the entitlements ask for (A1). Fix it in the portal, then re-run the job.
- **Verify the archived app.** The app was signed without Associated Domains or
  Sign in with Apple: enable the capability on the App ID (A1) and re-run. The
  step prints the entitlements it found.
- **Export and upload.** Errors here come from App Store Connect. "The bundle
  version must be higher" or a duplicate build number means that number was
  already uploaded (see below). ITMS-90186 means the version is closed, which
  the numbering above should prevent; report it.

**Re-running.** "Re-run jobs" keeps the run number, so a validation re-run
gets the same build number. That is fine when the earlier attempt failed before
uploading, and refused when it uploaded. To get a new number, move the
`validation` tag again, which starts a new run. A real tag's build number is
fixed by the tag, so a real tag uploads once; a second attempt needs the next
version.

**If cloud-managed signing cannot work on the runner**, the fallback is a
distribution certificate (`.p12`) and a provisioning profile in secrets, with
the certificate request generated with `openssl` on Windows. That needs renewal
by hand every year, so it is a decision for the owner, not a quick fix.
