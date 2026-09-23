// Guard for the app's platform identity (issue #151). The same id is spelled
// out in several build files that cannot read Dart; a mismatch builds fine
// and only surfaces later, as a Google sign-in that rejects the iOS bundle or
// a macOS test target pointing at an app that no longer exists.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/brand.dart';

// `flutter test` runs from the package root.
String _read(String path) => File(path).readAsStringSync();

List<String> _all(String pattern, String text) =>
    RegExp(pattern).allMatches(text).map((m) => m.group(1)!).toList();

void main() {
  test('Android applicationId is kAppPackageId', () {
    expect(_all(r'applicationId\s*=\s*"([^"]+)"',
        _read('android/app/build.gradle.kts')), [kAppPackageId]);
  });

  test('iOS bundle ids are kAppPackageId, tests under it', () {
    final ids = _all(r'PRODUCT_BUNDLE_IDENTIFIER = ([^;]+);',
        _read('ios/Runner.xcodeproj/project.pbxproj'));
    final app = ids.where((id) => !id.endsWith('.RunnerTests')).toSet();
    final tests = ids.where((id) => id.endsWith('.RunnerTests')).toSet();

    expect(app, {kAppPackageId});
    expect(tests, {'$kAppPackageId.RunnerTests'});
  });

  test('GoogleService-Info.plist BUNDLE_ID matches the iOS bundle id', () {
    expect(
        _all(r'<key>BUNDLE_ID</key>\s*<string>([^<]*)</string>',
            _read('ios/Runner/GoogleService-Info.plist')),
        [kAppPackageId]);
  });

  test('macOS bundle id is kAppPackageId, tests under it', () {
    expect(
        _all(r'PRODUCT_BUNDLE_IDENTIFIER = (.+)',
            _read('macos/Runner/Configs/AppInfo.xcconfig')),
        [kAppPackageId]);
    expect(
        _all(r'PRODUCT_BUNDLE_IDENTIFIER = ([^;]+);',
                _read('macos/Runner.xcodeproj/project.pbxproj'))
            .toSet(),
        {'$kAppPackageId.RunnerTests'});
  });

  test('macOS product name agrees with every .app reference', () {
    final name = _all(r'PRODUCT_NAME = (.+)',
            _read('macos/Runner/Configs/AppInfo.xcconfig'))
        .single
        .trim();
    final pbxproj = _read('macos/Runner.xcodeproj/project.pbxproj');
    final scheme = _read(
        'macos/Runner.xcodeproj/xcshareddata/xcschemes/Runner.xcscheme');

    // Built-product names only: skip `$PRODUCT_NAME.app` in the embed script
    // and the `.app` inside the tests' bundle id.
    expect(_all(r'(?<![\w$.])([\w-]+\.app)\b', pbxproj).toSet(),
        {'$name.app'});
    expect(_all(r'BuildableName = "([^"]+\.app)"', scheme).toSet(),
        {'$name.app'});
    expect(
        _all(r'TEST_HOST = "[^"]*/([^/"]+)";', pbxproj).toSet(), {name});
  });

  test('Linux APPLICATION_ID is kAppPackageId', () {
    expect(_all(r'set\(APPLICATION_ID "([^"]+)"\)',
        _read('linux/CMakeLists.txt')), [kAppPackageId]);
  });
}
