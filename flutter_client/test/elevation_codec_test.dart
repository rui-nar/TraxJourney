// Issue #295. The client decoder must agree exactly with
// `src/project/elevation_codec.py`; a silent disagreement would show up as a
// subtly wrong elevation chart rather than as an error.
//
// The vectors here were produced by the Python encoder, so this is a genuine
// cross-language contract test rather than a round-trip against ourselves.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/elevation_codec.dart';

void main() {
  test('decodes a vector produced by the server encoder', () {
    // encode_profile_pairs([[0.0, 100.0], [0.01, 101.5], [0.025, 99.0]])
    const encoded = r'?o}@S]]p@';
    final decoded = decodeElevationProfile(encoded);
    expect(decoded, hasLength(3));
    expect(decoded[0][0], closeTo(0.0, 0.0006));
    expect(decoded[0][1], closeTo(100.0, 0.051));
    expect(decoded[1][0], closeTo(0.01, 0.0006));
    expect(decoded[1][1], closeTo(101.5, 0.051));
    expect(decoded[2][0], closeTo(0.025, 0.0006));
    expect(decoded[2][1], closeTo(99.0, 0.051));
  });

  test('an empty string decodes to nothing', () {
    expect(decodeElevationProfile(''), isEmpty);
  });

  test('a truncated payload yields what decoded cleanly', () {
    const encoded = r'?o}@S]]p@';
    final partial = decodeElevationProfile(encoded.substring(0, 5));
    expect(partial.length, lessThan(3));
    // Whatever survived must still be coherent, not garbage.
    for (final p in partial) {
      expect(p, hasLength(2));
      expect(p[0].isFinite, isTrue);
      expect(p[1].isFinite, isTrue);
    }
  });

  test('descending elevation and below-sea-level values survive', () {
    // encode_profile_pairs([[0.0, 5.0], [0.01, -3.5], [0.02, -12.0]])
    const encoded = r'?cBShDShD';
    final decoded = decodeElevationProfile(encoded);
    expect(decoded, hasLength(3));
    expect(decoded[1][1], closeTo(-3.5, 0.051));
    expect(decoded[2][1], closeTo(-12.0, 0.051));
  });

  group('decodeElevationProfiles', () {
    test('decodes each activity and drops empties', () {
      final out = decodeElevationProfiles({
        '1': r'?o}@S]]p@',
        '2': '',
        '3': 42, // not a string — a malformed response must not throw
      });
      expect(out.keys, ['1']);
      expect(out['1'], hasLength(3));
    });

    test('an empty map is an empty result', () {
      expect(decodeElevationProfiles(const {}), isEmpty);
    });
  });
}
