// Tests the non-web fallback for the Strava OAuth popup abstraction
// (issue #99). The real web variant (strava_oauth_popup_web.dart) opens an
// actual browser popup and listens for a postMessage — not practically
// testable under `flutter test` (same as image_download_web.dart /
// download_web.dart, which have no dedicated tests either, only their
// stub/wiring). This just proves the stub is safe to construct and dispose.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/settings/strava_oauth_popup_stub.dart';

void main() {
  test('dispose() with no prior connect() call does not throw', () {
    final popup = StravaOAuthPopup();
    expect(() => popup.dispose(), returnsNormally);
  });

  test('dispose() is safe to call more than once', () {
    final popup = StravaOAuthPopup();
    popup.dispose();
    expect(() => popup.dispose(), returnsNormally);
  });
}
