// gzEncode/gzDecode — the pure gzip+JSON codec project_cache_store_native.dart
// runs via `compute()` on a background isolate instead of the UI isolate.
// Regression coverage for the ANR this fixed: a multi-MB full-res geo/details
// payload used to be jsonEncode'd + gzipped synchronously on the UI isolate
// as a "fire-and-forget" cache write landing right when the user started
// interacting with the map. See project_cache_store_native.dart for the full
// story; this only exercises the codec itself (round trip + corrupt input),
// same scope as photo_thumb_cache_test.dart, since a real sqflite backend
// isn't available under plain `flutter test`.

import 'dart:convert' show jsonEncode;

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/project_cache_store_native.dart';

void main() {
  test('gzEncode/gzDecode round-trips a typical payload', () {
    final value = {
      'type': 'FeatureCollection',
      'features': [
        {'geometry': {'coordinates': [1.0, 2.0]}},
      ],
    };

    final encoded = gzEncode(value);
    expect(encoded, isNotNull);
    expect(gzDecode(encoded!), value);
  });

  test('gzEncode/gzDecode round-trips a large payload', () {
    final value = {
      'type': 'FeatureCollection',
      'features': List.generate(
        50000,
        (i) => {'id': i, 'coordinates': [i * 0.001, i * 0.002]},
      ),
    };

    final encoded = gzEncode(value);
    expect(jsonEncode(value).length, greaterThan(1000000),
        reason: 'this case is meant to exercise a multi-MB-scale payload');
    expect(gzDecode(encoded!), value);
  });

  test('gzEncode returns null for a null value', () {
    expect(gzEncode(null), isNull);
  });

  test('gzDecode returns null for empty bytes', () {
    expect(gzDecode(const []), isNull);
  });

  test('gzDecode returns null for corrupt (non-gzip) bytes instead of throwing', () {
    expect(gzDecode([1, 2, 3, 4]), isNull);
  });
}
