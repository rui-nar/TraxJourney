// Loads test/fixtures/track_metrics_vectors.json (generated from the Python
// track maths by scripts/gen_track_metrics_vectors.py) and compares results to
// it: floats within 1e-6, ints, strings, booleans and nulls exactly.

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/track_metrics/align.dart';

const double vectorTolerance = 1e-6;

final Map<String, dynamic> _vectors =
    jsonDecode(File('test/fixtures/track_metrics_vectors.json').readAsStringSync())
        as Map<String, dynamic>;

/// The cases of one section of the vectors file.
List<Map<String, dynamic>> vectorCases(String section) =>
    (_vectors[section] as List).cast<Map<String, dynamic>>();

/// The `constants` section.
Map<String, dynamic> vectorConstants() =>
    _vectors['constants'] as Map<String, dynamic>;

/// Runs [run] on the `input` of every case of [section] and compares the
/// result, converted by [run] to JSON-shaped data, with `expected`; a case with
/// `expected_error` must throw instead.
void checkSection(
  String section,
  Object? Function(Map<String, dynamic> input) run,
) {
  final cases = vectorCases(section);
  test('$section has cases', () => expect(cases, isNotEmpty));
  for (final c in cases) {
    test('$section: ${c['name']}', () {
      final input = c['input'] as Map<String, dynamic>;
      if (c.containsKey('expected_error')) {
        expect(() => run(input), throwsA(isA<Error>()),
            reason: 'Python raised ${c['expected_error']}');
      } else {
        expectSame(run(input), c['expected'], r'$');
      }
    });
  }
}

/// Deep comparison: doubles within [vectorTolerance], everything else exact
/// (an int must be an int, a double a double).
void expectSame(Object? actual, Object? expected, String path) {
  if (expected is double) {
    expect(actual, isA<double>(), reason: '$path: type');
    if (expected.isInfinite || expected.isNaN) {
      expect(actual, expected, reason: path);
    } else {
      expect(actual as double, closeTo(expected, vectorTolerance), reason: path);
    }
  } else if (expected is List) {
    expect(actual, isA<List>(), reason: '$path: type');
    final list = actual as List;
    expect(list.length, expected.length, reason: '$path: length');
    for (var i = 0; i < expected.length; i++) {
      expectSame(list[i], expected[i], '$path[$i]');
    }
  } else if (expected is Map) {
    expect(actual, isA<Map>(), reason: '$path: type');
    final map = actual as Map;
    expect(map.keys.toSet(), expected.keys.toSet(), reason: '$path: keys');
    for (final key in expected.keys) {
      expectSame(map[key], expected[key], '$path.$key');
    }
  } else {
    expect(actual, expected, reason: path);
  }
}

// ── JSON <-> port types ──────────────────────────────────────────────────────

List<double> doubles(Object? json) =>
    [for (final v in json as List) (v as num).toDouble()];

List<double?> nullableDoubles(Object? json) =>
    [for (final v in json as List) (v as num?)?.toDouble()];

/// `[lat, lng, elev|null]` rows to points.
List<TrackPoint> trackPoints(Object? json) => [
      for (final p in json as List)
        TrackPoint((p[0] as num).toDouble(), (p[1] as num).toDouble(),
            (p[2] as num?)?.toDouble()),
    ];

List<List<Object?>> pointRows(List<TrackPoint> points) =>
    [for (final p in points) [p.lat, p.lng, p.elev]];

Map<String, Object>? profileJson(ElevationProfile? p) => p == null
    ? null
    : {'distances_km': p.distancesKm, 'elevations_m': p.elevationsM};
