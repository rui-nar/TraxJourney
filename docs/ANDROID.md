# Android

The Flutter client ships as a native Android app alongside the web build. One
`vX.Y.Z` tag produces both: [docker-build.yml](../.github/workflows/docker-build.yml)
builds the server image with the web client inside it, and
[android-release.yml](../.github/workflows/android-release.yml) builds a signed
APK and attaches it to the same GitHub release.

| | |
|---|---|
| applicationId | `com.traxjourney.app` — permanent; see below |
| Launcher label | TraxJourney |
| Distribution | Signed arm64-v8a APK on the GitHub release (sideload). No Play Store. |
| API endpoint | Baked at build time: `--dart-define=API_BASE_URL=https://traxjourney.com` |

## Running it on an emulator

```powershell
.\run-android.ps1                 # debug build against production
.\run-android.ps1 -Local          # debug build against the dev server on this machine
.\run-android.ps1 -Release        # release build, installed and launched
.\run-android.ps1 -Apk .\traxjourney-v0.48.0.apk   # a prebuilt APK from a release
.\run-android.ps1 -DeepLink "https://traxjourney.com/share/TOKEN"
```

The script boots the AVD if none is running (`-Avd <name>`, default
`MyPixelEmulator`), installs, launches, and streams `flutter` logcat. Debug runs
use `flutter run`, so `r` hot-reloads.

Three things it handles that otherwise waste an afternoon:

- **JDK.** Gradle 8.14 supports JDK 17–24, not 25. The script finds a usable JDK
  and uses it for that run. If `JAVA_HOME` points at a directory that no longer
  exists — what a JDK update leaves behind, since the version is in the path —
  it says so; fix it with `setx JAVA_HOME "<path to a JDK 21>"`.
- **TLS trust.** A JDK carries its own CA list and ignores the Windows one, so
  behind TLS inspection every dependency download fails with `PKIX path building
  failed` even though browsers on the same machine work. The script sets
  `-Djavax.net.ssl.trustStoreType=WINDOWS-ROOT` for the Gradle JVM (that run
  only; `-NoWindowsCertStore` opts out). The same symptom makes `sdkmanager`
  report *"Failed to download any source lists"*.
- **Cleartext to the dev server.** `-Local` points the app at `http://10.0.2.2:8000`
  (the emulator's route to the host). Android forbids cleartext by default, so
  debug builds carry
  [network_security_config.xml](../flutter_client/android/app/src/debug/res/xml/network_security_config.xml),
  scoped to `10.0.2.2` and `localhost`. Release builds have no such exemption.

`-Local` needs the API server running on the host (`uvicorn api.router:app --port 8000`).

### SDK packages

The build needs `platforms;android-35` — `path_provider_android` pulls in the
`jni` module, which compiles against 35 regardless of what the app targets.
Gradle's error names it directly (*"Failed to find Platform SDK with path:
platforms;android-35"*); install with
`sdkmanager "platforms;android-35"`, or from Android Studio's SDK Manager if
`sdkmanager` cannot reach its manifests (see TLS trust, above).

## The signing key

The key **is** the app's identity. An update signed with a different key cannot
be installed over an existing copy, and there is no way to re-key a published
app — the only way out is a new applicationId, which orphans every install. Back
it up, and keep the passwords with it.

```powershell
.\scripts\new-android-keystore.ps1
```

It writes the keystore outside the repository (`~/.android-keys/` by default),
prints the SHA-1 and SHA-256 fingerprints, and dumps the base64 blob for CI.
`android/key.properties` and `*.jks` are gitignored.

Four places consume its output:

| Output | Goes to | Without it |
|---|---|---|
| base64 + passwords | GitHub secrets `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEY_ALIAS`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_PASSWORD` | the release workflow fails fast |
| SHA-1 | Google Cloud OAuth client (below) | Google Sign-In fails on device |
| SHA-256 | `ANDROID_CERT_FINGERPRINTS` on the server | App Links open in a browser |
| the file itself | `flutter_client/android/key.properties` | local release builds fall back to the debug key |

`key.properties`:

```properties
storeFile=C:\\Users\\you\\.android-keys\\traxjourney-release.jks
storePassword=...
keyAlias=traxjourney
keyPassword=...
```

## Google Sign-In

`main.dart` already passes a `serverClientId` for native platforms, but Google
also requires an **Android** OAuth client registered against the app's package
name and signing certificate. Create it once, in the Google Cloud console for
the project with id `viewtrip` (a Google Cloud project id can never be
changed, so it keeps the app's old name; only the display name can say
TraxJourney):

> Credentials → Create credentials → OAuth client ID → **Android**
> Package name: `com.traxjourney.app`
> SHA-1: the release fingerprint printed by the keystore script

No client secret is involved, and nothing in the app needs to change — Google
matches the installed app against the registration at sign-in time. Until it
exists, the Google button returns `sign_in_failed`; email/password login works
regardless.

Add the **debug** keystore's SHA-1 as a second Android client if you want Google
Sign-In to work in debug builds too (`keytool -list -v -keystore
~/.android/debug.keystore -alias androiddebugkey -storepass android`).

## Deep links (Android App Links)

Share, invite and email-verification links open directly in the app:

| Link | Route |
|---|---|
| `https://traxjourney.com/share/TOKEN` | shared trip |
| `https://traxjourney.com/join/TOKEN` | trip invite |
| `https://traxjourney.com/verify-email/TOKEN` | email verification |

Three pieces have to line up:

1. The `autoVerify` intent filter in
   [AndroidManifest.xml](../flutter_client/android/app/src/main/AndroidManifest.xml).
2. `flutter_deeplinking_enabled`, plus `initialLocationFor()` in
   [app_router.dart](../flutter_client/lib/src/core/app_router.dart) returning
   null on native so go_router starts from the intent's route rather than `/`.
3. `https://traxjourney.com/.well-known/assetlinks.json` listing the package and
   the release SHA-256. Served by `api/router.py` from `ANDROID_CERT_FINGERPRINTS`;
   it 404s when that is unset, because an empty statement is worse than none —
   Android caches a failed verification.

Set the variable in the server `.env`, redeploy, and check:

```bash
curl https://traxjourney.com/.well-known/assetlinks.json
```

On device, verification runs at install time:

```powershell
adb shell pm get-app-links com.traxjourney.app
```

`verified` means links open in the app. A **debug** build can never verify — its
fingerprint is the debug key's, which is not in the statement — so test routing
by naming the package explicitly, which `run-android.ps1 -DeepLink` does.

## Cutting a release

Nothing extra: `.\bump_version_and_release.ps1` as usual (see
[RELEASING.md](RELEASING.md)). The tag triggers the APK build, which attaches
`traxjourney-vX.Y.Z.apk` to the release once it finishes — a few minutes after
the release page appears.

The build targets `android-arm64` only (`--target-platform android-arm64` in
[android-release.yml](../.github/workflows/android-release.yml)) and runs with
R8 minification and resource shrinking on
([proguard-rules.pro](../flutter_client/android/app/proguard-rules.pro)). Every
phone sold since ~2017 is 64-bit, and this app is sideload-only, so there's no
Play Store to hand a 32-bit or x86_64 slice to anyone who'd need one — bundling
them would only bloat the one file everyone downloads. A 32-bit-only device
cannot install this APK.

To sanity-check a build after touching `build.gradle.kts` or
`proguard-rules.pro` — R8 stripping something it shouldn't is a runtime
failure, not a build failure — move the `validation` tag to trigger the same
workflow without cutting a real release, then install and smoke-test the
result before tagging for real.

`versionCode` comes from the tag via
[android_version_code.py](../scripts/android_version_code.py) (`v0.48.0` → 4800),
because Android identifies an upgrade by that number alone and pubspec's `+1`
never moves. Installing a *lower* versionCode over a higher one fails with
`INSTALL_FAILED_VERSION_DOWNGRADE`; uninstall first.

## Changing the icon

```powershell
python scripts/generate_icons.py          # redraw every raster from the mark
cd flutter_client; dart run flutter_launcher_icons
```

The source is the brand mark itself, not a PNG:
[generate_icons.py](../scripts/generate_icons.py) draws it and writes the flat
icon plus both adaptive layers (`assets/app_icon.png`,
`assets/app_icon_background.png`, `assets/app_icon_foreground.png`), which
`flutter_launcher_icons` then resizes into `mipmap-*`. The foreground is inset to
62%, since only the centre ~66% of that layer survives launcher masking.

The same run also writes the cold-start assets: `launch_image.png` per density
for the pre-Android-12 window (`drawable*/launch_background.xml`) and
`splash_icon.png` for the Android 12+ system splash (`values-v31/styles.xml`,
`values-night-v31/styles.xml`). Both are inset for their own masking rules — see
the constants at the top of the script.

The mark's geometry lives in three places that must move together:
`assets/mark.svg`, `flutter_client/lib/src/core/brand_mark.dart` and
`scripts/generate_icons.py`.

## Not yet done

- iOS is untouched by this work — same Flutter code, no signing or store setup.
- Poster PNG "download" and GPX export are web-only paths
  (`image_download_stub.dart`, `download_stub.dart` are no-ops on native);
  sharing works, saving to the device does not.
- No Play Store listing.
