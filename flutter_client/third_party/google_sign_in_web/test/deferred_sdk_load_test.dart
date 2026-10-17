// The TraxJourney patch (#611): constructing the plugin, which the web plugin
// registrant does before main(), must not fetch Google's script; init() does.
// Run with `flutter test --platform chrome`.

@TestOn('browser')
library;

import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:google_sign_in_platform_interface/google_sign_in_platform_interface.dart';
import 'package:google_sign_in_web/google_sign_in_web.dart';
import 'package:web/web.dart' as web;

bool gsiScriptOnPage() => web.document
    .querySelectorAll('script[src^="https://accounts.google.com/gsi"]')
    .length > 0;

void main() {
  test('the constructor does not load the GIS SDK; init() does', () {
    // One plugin: each construction registers the same button view factory.
    final plugin = GoogleSignInPlugin();
    expect(gsiScriptOnPage(), isFalse);

    // Not awaited: init() waits for the script, which a test should not
    // fetch. The script element is added synchronously.
    unawaited(plugin.init(const InitParameters(clientId: 'test-client-id')));
    expect(gsiScriptOnPage(), isTrue);
  });
}
