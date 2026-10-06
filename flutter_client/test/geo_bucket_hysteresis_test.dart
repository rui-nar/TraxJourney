// Issue #401: a zoom oscillating around an integer boundary must not refetch
// geometry on every crossing. The loaded bucket B (zooms in (B-1, B]) stays
// fresh until the zoom is a margin past either edge.

import 'dart:convert';
import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

http.Response _json(Map<String, dynamic> body) =>
    http.Response(jsonEncode(body), 200);

Map<String, dynamic> _geoWith(int points) => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'properties': {'type': 'activity', 'activity_id': '1'},
          'geometry': {
            'type': 'LineString',
            'coordinates': [
              for (var i = 0; i < points; i++) [7.0 + i * 0.001, 45.0 + i * 0.001]
            ],
          },
        },
      ],
    };

ApiClient _api(List<String> zooms, {Duration lodDelay = Duration.zero}) =>
    ApiClient(
      baseUrl: '',
      httpClient: MockClient((req) async {
        final path = req.url.path;
        if (path == '/api/projects/Trip/meta') {
          return _json({
            'name': 'Trip',
            'lock_version': 1,
            'activities': [
              {'id': '1'}
            ],
            'items': [
              {'item_type': 'activity', 'activity_id': '1'}
            ],
            'people': <dynamic>[],
            'groups': <dynamic>[],
          });
        }
        if (path == '/api/geo/project/low-res') {
          return _json({'type': 'FeatureCollection', 'features': <dynamic>[]});
        }
        if (path == '/api/geo/project/simplified') {
          final z = req.url.queryParameters['zoom']!;
          zooms.add(z);
          if (lodDelay > Duration.zero) await Future<void>.delayed(lodDelay);
          return _json(_geoWith(double.parse(z).ceil()));
        }
        if (path == '/api/geo/project') return _json(_geoWith(999));
        if (path == '/api/projects/Trip/elevation') {
          return _json({'profiles': <String, dynamic>{}, 'encrypted': <String, dynamic>{}});
        }
        return _json({});
      }),
    );

int _points(ProjectNotifier n) {
  final features = n.geoFacet.geo?['features'] as List?;
  if (features == null || features.isEmpty) return 0;
  return (features.first['geometry']['coordinates'] as List?)?.length ?? 0;
}

Future<bool> _waitFor(bool Function() cond,
    {Duration timeout = const Duration(seconds: 2)}) async {
  final deadline = DateTime.now().add(timeout);
  while (DateTime.now().isBefore(deadline)) {
    if (cond()) return true;
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
  return cond();
}

void main() {
  setUp(() => projectDataCache.resetForTest());

  group('isZoomBucketStale', () {
    test('the margin is 0.3 of a level', () {
      expect(kZoomBucketMargin, 0.3);
    });

    test('a bucket is fresh up to a margin past either edge', () {
      expect(isZoomBucketStale(6, 6.3), isFalse);
      expect(isZoomBucketStale(6, 6.31), isTrue);
      expect(isZoomBucketStale(6, 5.0), isFalse);
      expect(isZoomBucketStale(6, 4.7), isTrue);
      expect(isZoomBucketStale(6, 4.71), isFalse);
    });

    test('a refetch at ceil(zoom) is never stale for that zoom', () {
      // The #332 lesson: the predicate must be satisfiable by the action that
      // satisfies it, or the app refetches forever.
      final rng = Random(401);
      for (var i = 0; i < 20000; i++) {
        final zoom = rng.nextDouble() * 22;
        expect(isZoomBucketStale(zoom.ceil(), zoom), isFalse,
            reason: 'zoom $zoom');
      }
      for (final zoom in [1.0, 6.0, 6.0000001, 5.9999999, 21.5, 22.0]) {
        expect(isZoomBucketStale(zoom.ceil(), zoom), isFalse);
      }
    });
  });

  test('wobbling 5.9 -> 6.1 -> 5.9 -> 6.1 costs only the load', () async {
    final zooms = <String>[];
    api = _api(zooms);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(5.9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);
    await n.load(_ref);
    expect(await _waitFor(() => _points(n) == 6), isTrue);

    for (final z in [6.1, 5.9, 6.1]) {
      n.setMapZoom(z);
    }
    await Future<void>.delayed(const Duration(milliseconds: 80));
    expect(zooms, hasLength(1), reason: 'bucket 6 covers 5.9 and 6.1');
  });

  test('a zoom past the margin refetches once, and a wobble back does not',
      () async {
    final zooms = <String>[];
    api = _api(zooms);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(5.9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);
    await n.load(_ref);
    expect(await _waitFor(() => _points(n) == 6), isTrue);

    n.setMapZoom(6.4);
    expect(await _waitFor(() => _points(n) == 7), isTrue);
    for (final z in [6.0, 6.4, 6.0]) {
      n.setMapZoom(z);
    }
    await Future<void>.delayed(const Duration(milliseconds: 80));
    expect(zooms, hasLength(2), reason: 'the load plus one refetch');
    expect(_points(n), 7);
  });

  test('a wobble during an in-flight fetch keeps a still-fresh result',
      () async {
    final zooms = <String>[];
    api = _api(zooms);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(5.9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);
    await n.load(_ref);
    expect(await _waitFor(() => _points(n) == 6), isTrue);

    api = _api(zooms, lodDelay: const Duration(milliseconds: 120));
    n.setMapZoom(7.0);
    expect(await _waitFor(() => zooms.length == 2), isTrue,
        reason: 'the refetch for bucket 7 is in flight');
    n.setMapZoom(6.0); // ceil 6, but still inside bucket 7's margin
    expect(await _waitFor(() => _points(n) == 7), isTrue,
        reason: 'the result is still fresh for 6.0 and must be kept');
  });
}
