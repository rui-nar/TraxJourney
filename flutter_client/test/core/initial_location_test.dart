// Tests for initialLocationFor() in app_router.dart — the router's starting
// location per platform (issue #136).
//
// On web it must keep honouring the browser URL, so a deep link survives the
// auth round-trip. On Android/iOS it must return *null*, which is what lets
// go_router start from the platform's default route: the URL of the App Link
// intent that launched the app. The previous '/' silently discarded every
// incoming link, and nothing in the app surfaced that — the app just opened on
// the welcome screen as if launched from the icon.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/app_router.dart';

void main() {
  group('native', () {
    test('defers to the platform route so App Links are honoured', () {
      expect(
        initialLocationFor(
          isWeb: false,
          base: Uri.parse('https://traxjourney.com/share/TOKEN'),
        ),
        isNull,
      );
    });

    test('defers on a plain launch too', () {
      // Uri.base on the VM/Android is a file: URL, and reading a path out of it
      // would produce nonsense like /data/user/0/... as a route.
      expect(
        initialLocationFor(isWeb: false, base: Uri.parse('file:///data/app/')),
        isNull,
      );
    });
  });

  group('web', () {
    test('keeps a deep-linked path', () {
      expect(
        initialLocationFor(
          isWeb: true,
          base: Uri.parse('https://traxjourney.com/share/TOKEN'),
        ),
        '/share/TOKEN',
      );
    });

    test('keeps the other out-of-band link shapes', () {
      expect(
        initialLocationFor(
          isWeb: true,
          base: Uri.parse('https://traxjourney.com/join/abc'),
        ),
        '/join/abc',
      );
      expect(
        initialLocationFor(
          isWeb: true,
          base: Uri.parse('https://traxjourney.com/verify-email/abc'),
        ),
        '/verify-email/abc',
      );
    });

    test('starts at the root for the bare origin', () {
      expect(
        initialLocationFor(isWeb: true, base: Uri.parse('https://traxjourney.com/')),
        '/',
      );
      expect(
        initialLocationFor(isWeb: true, base: Uri.parse('https://traxjourney.com')),
        '/',
      );
    });

    test('ignores the query string — GoRouter re-reads it from the URL', () {
      expect(
        initialLocationFor(
          isWeb: true,
          base: Uri.parse('https://traxjourney.com/app?owner=3'),
        ),
        '/app',
      );
    });
  });
}
