// Tests for safeReturnTo (issue #111): the single guard shared by
// app_router.dart's redirect and the login/register screens' post-auth
// navigation, so all return_to consumers enforce the same
// relative-path-only rule.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/return_to.dart';

void main() {
  group('safeReturnTo', () {
    test('accepts a relative path', () {
      expect(safeReturnTo('/join/tok123'), '/join/tok123');
      expect(safeReturnTo('/view?project=Trip%20A&owner=7'),
          '/view?project=Trip%20A&owner=7');
    });

    test('rejects null and empty', () {
      expect(safeReturnTo(null), isNull);
      expect(safeReturnTo(''), isNull);
    });

    test('rejects absolute URLs', () {
      expect(safeReturnTo('https://evil.example'), isNull);
      expect(safeReturnTo('javascript:alert(1)'), isNull);
    });

    test('rejects scheme-relative //host', () {
      expect(safeReturnTo('//evil.example'), isNull);
      expect(safeReturnTo('//evil.example/join/tok'), isNull);
    });

    test('rejects paths without a leading slash', () {
      expect(safeReturnTo('join/tok123'), isNull);
    });
  });
}
