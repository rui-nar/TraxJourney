import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';

/// Incremental map updates: per-feature geometry work (coordinate→LatLng
/// conversion + arc-midpoint) is memoized by the identity of the raw coords
/// list, so unchanged features are not re-processed on every map rebuild.
/// These tests pin (a) correctness vs a from-scratch conversion and (b) that
/// the cache actually hits for the same coords object and misses for a distinct
/// (even if equal) one.
void main() {
  // GeoJSON coords are [lon, lat]; LatLng is (lat, lon).
  List coords(List<List<double>> pts) => [for (final p in pts) p];

  group('memoCoordsToLatLng', () {
    test('converts [lon,lat] pairs to LatLng (lat,lon), order preserved', () {
      final c = coords([[10.0, 60.0], [10.5, 61.0], [11.0, 62.0]]);
      expect(memoCoordsToLatLng(c), [
        const LatLng(60.0, 10.0),
        const LatLng(61.0, 10.5),
        const LatLng(62.0, 11.0),
      ]);
    });

    test('skips malformed coordinate entries', () {
      final c = [[10.0, 60.0], [99.0], 'x', [10.1, 60.1]];
      expect(memoCoordsToLatLng(c), [
        const LatLng(60.0, 10.0),
        const LatLng(60.1, 10.1),
      ]);
    });

    test('same coords object → cache hit (identical list returned)', () {
      final c = coords([[1.0, 2.0], [3.0, 4.0]]);
      final first = memoCoordsToLatLng(c);
      final second = memoCoordsToLatLng(c);
      expect(identical(first, second), isTrue,
          reason: 'unchanged feature must not be re-converted');
    });

    test('distinct equal-content objects → separate entries, equal output', () {
      final a = coords([[1.0, 2.0], [3.0, 4.0]]);
      final b = coords([[1.0, 2.0], [3.0, 4.0]]);
      final ra = memoCoordsToLatLng(a);
      final rb = memoCoordsToLatLng(b);
      expect(identical(ra, rb), isFalse); // different cache keys
      expect(ra, rb); // but equal content (correctness preserved)
    });

    test('memoized output equals a from-scratch conversion', () {
      final c = coords([[8.9, 48.0], [9.0, 48.1], [9.2, 48.3]]);
      final scratch = [
        for (final p in c)
          LatLng((p[1] as num).toDouble(), (p[0] as num).toDouble())
      ];
      expect(memoCoordsToLatLng(c), scratch);
    });
  });

  group('memoArcMidpoint', () {
    test('returns the chord midpoint of a straight 2-point line', () {
      final c = coords([[0.0, 0.0], [0.0, 10.0]]);
      expect(memoArcMidpoint(c), const LatLng(5.0, 0.0));
    });

    test('same coords object → cache hit (identical result)', () {
      final c = coords([[0.0, 0.0], [2.0, 0.0], [4.0, 0.0]]);
      final first = memoArcMidpoint(c);
      final second = memoArcMidpoint(c);
      expect(first, isNotNull);
      expect(identical(first, second), isTrue);
    });

    test('empty coords → null (and does not throw)', () {
      expect(memoArcMidpoint(const []), isNull);
    });
  });
}
