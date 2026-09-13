/// The product's name, in one place (issue #151).
///
/// Web and platform files that cannot import Dart (web/index.html,
/// web/manifest.json, the iOS Info.plist, desktop runners) spell it out by
/// hand; test/brand/no_legacy_brand_test.dart keeps them in step.
library;

/// Product name, as shown on the Android launcher and traxjourney.com.
const kAppName = 'TraxJourney';
