import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/version_gate.dart';

void main() {
  group('isClientStale', () {
    test('two different real versions → stale', () {
      expect(isClientStale('v0.40.0', 'v0.41.0'), isTrue);
      expect(isClientStale('v0.41.0', 'v0.40.0'), isTrue);
    });

    test('same version → not stale', () {
      expect(isClientStale('v0.41.0', 'v0.41.0'), isFalse);
    });

    test('dev on either side → not stale (no local false positives)', () {
      expect(isClientStale('dev', 'v0.41.0'), isFalse);
      expect(isClientStale('v0.41.0', 'dev'), isFalse);
      expect(isClientStale('dev', 'dev'), isFalse);
    });

    test('empty on either side → not stale', () {
      expect(isClientStale('', 'v0.41.0'), isFalse);
      expect(isClientStale('v0.41.0', ''), isFalse);
    });
  });
}
