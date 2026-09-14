/// The product's name, in one place (issue #151).
///
/// Web and platform files that cannot import Dart (web/index.html,
/// web/manifest.json, the iOS Info.plist, desktop runners) spell it out by
/// hand; test/brand/no_legacy_brand_test.dart keeps them in step.
library;

/// Product name, as shown on the Android launcher and traxjourney.com.
const kAppName = 'TraxJourney';

/// Where the source code lives. The app is AGPL-3.0: people using the hosted
/// service over a network must be offered the source, and Settings → About
/// links here for that. `tests/test_license.py` keeps it equal to the
/// server's `src/brand.py` REPO_URL.
const kRepoUrl = 'https://github.com/rui-nar/TraxJourney';

/// The licence text, on the default branch.
const kLicenseUrl = '$kRepoUrl/blob/main/LICENSE';

/// The app's platform identity: Android applicationId, iOS/macOS bundle id and
/// Linux application id. Map tile requests also name the app by it in their
/// User-Agent, as tile providers' usage policies ask.
const kAppPackageId = 'com.traxjourney.app';
