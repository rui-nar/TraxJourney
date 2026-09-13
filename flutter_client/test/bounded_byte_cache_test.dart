// Issue #276. map_panel.dart's marker-thumbnail cache was a
// `static final Map<String, Uint8List>` with no eviction: a long trip
// accumulated every thumbnail the session ever displayed and released none,
// which is also why hiding the memories layer freed nothing.
//
// The same mistake as the server payload cache in api/geo.py — bounding a
// cache of wildly non-uniform entries by entry count, or not at all, when the
// resource that actually runs out is bytes.

import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/bounded_byte_cache.dart';

Uint8List _bytes(int n) => Uint8List(n);

void main() {
  test('stores and returns what it was given', () {
    final c = BoundedByteCache(1000);
    final v = _bytes(10);
    c.put('a', v);
    expect(c['a'], same(v));
    expect(c.containsKey('a'), isTrue);
    expect(c.bytes, 10);
  });

  test('a missing key is null, not an error', () {
    expect(BoundedByteCache(100)['nope'], isNull);
  });

  test('evicts oldest-first once over budget', () {
    final c = BoundedByteCache(100);
    c.put('a', _bytes(60));
    c.put('b', _bytes(60)); // 120 > 100 -> 'a' goes
    expect(c.containsKey('a'), isFalse);
    expect(c.containsKey('b'), isTrue);
    expect(c.bytes, 60);
  });

  test('stays within budget across many writes', () {
    final c = BoundedByteCache(500);
    for (var i = 0; i < 50; i++) {
      c.put('k$i', _bytes(100));
    }
    expect(c.bytes, lessThanOrEqualTo(500));
    expect(c.length, greaterThan(0), reason: 'must not evict itself empty');
  });

  test('re-putting a key re-accounts rather than double-counting', () {
    // Without this the budget drifts upward on every refresh and the cache
    // silently stops being bounded at all.
    final c = BoundedByteCache(1000);
    c.put('a', _bytes(100));
    c.put('a', _bytes(300));
    expect(c.bytes, 300);
    expect(c.length, 1);
    expect(c['a']!.length, 300);
  });

  test('a value larger than the whole budget is still returned to its caller',
      () {
    // Dropping it would mean the marker that just fetched it cannot draw.
    final c = BoundedByteCache(100);
    c.put('big', _bytes(500));
    expect(c['big'], isNotNull);
    expect(c.length, 1);
  });

  test('an oversized entry is retired by the next write', () {
    final c = BoundedByteCache(100);
    c.put('big', _bytes(500));
    c.put('small', _bytes(50));
    expect(c.containsKey('big'), isFalse);
    expect(c.bytes, 50);
  });

  test('clear empties the cache and its accounting', () {
    final c = BoundedByteCache(1000);
    c.put('a', _bytes(100));
    c.clear();
    expect(c.length, 0);
    expect(c.bytes, 0);
  });
}
