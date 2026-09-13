/// The product's name, in one place (issue #151).
///
/// Web and platform files that cannot import Dart (web/index.html,
/// web/manifest.json, the iOS Info.plist, desktop runners) spell it out by
/// hand; test/brand/no_legacy_brand_test.dart keeps them in step.
library;

/// Product name, as shown on the Android launcher and traxjourney.com.
const kAppName = 'TraxJourney';

/// The app's platform identity: Android applicationId, iOS/macOS bundle id and
/// Linux application id. Map tile requests also name the app by it in their
/// User-Agent, as tile providers' usage policies ask.
const kAppPackageId = 'com.traxjourney.app';
