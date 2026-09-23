// The *strict* direction of `_refetchIsCurrent` — issue #321 review.
//
// `_refetchGeoForZoomInner` used to guard on `_isCurrent(token, r)`, which
// compares against the ref the load *began* with. `load()` reassigns `ref` from
// the server's `caller_role`, so on any non-owner load (a share link renames
// token -> trip name; a companion's role placeholder is corrected) the guard was
// structurally false and the refetched geometry was thrown away. That is fixed
// by comparing the load token and the *current* ref instead.
//
// These pin the other side of that change: the looser guard must still discard a
// refetch that resolves after the notifier has moved on. Read that intent
// carefully before touching them —
//
//   * `geo_zoom_lod_test.dart` and `shared_geo_zoom_lod_test.dart` pin the
//     PERMISSIVE direction (a legitimate refetch is applied). Those fail against
//     the pre-fix notifier; they are the bug's reproduction.
//   * these pin the STRICT direction, and they pass against the pre-fix notifier
//     too, because the old guard was strictly stricter. They are a fence against
//     a future over-loosening of the guard, not a reproduction. A change that
//     makes them fail has widened it too far.
//
// The mock's asymmetry is load-bearing: `Trip` at zoom 15 is slow (500 ms) and
// everything else is fast, and `Other` returns a distinguishable point count. The
// `_api` helper in `geo_zoom_lod_test.dart` has neither property, so reusing it
// would let a stale refetch land unnoticed and these would pass vacuously.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

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

Map<String, dynamic> _meta(String name) => {
      'name': name,
      'lock_version': 1,
      'activities': [
        {'id': '1'}
      ],
      'items': [
        {'item_type': 'activity', 'activity_id': '1'}
      ],
      'people': <dynamic>[],
      'groups': <dynamic>[],
    };

int _pointsOf(ProjectNotifier n) {
  final features = n.geo?['features'] as List?;
  if (features == null || features.isEmpty) return 0;
  return (features.first['geometry']['coordinates'] as List?)?.length ?? 0;
}

Future<bool> _waitFor(bool Function() cond,
    {Duration timeout = const Duration(seconds: 5)}) async {
  final deadline = DateTime.now().add(timeout);
  while (DateTime.now().isBefore(deadline)) {
    if (cond()) return true;
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
  return cond();
}

/// `Trip` at zoom 15 takes 500 ms; every other level answers in 20 ms. `Trip`
/// returns `zoom.ceil()` points, `Other` returns `1000 + zoom.ceil()` — so which
/// project's geometry is on screen is visible from the point count alone.
ApiClient _api(List<Uri> calls) => ApiClient(
      baseUrl: '',
      httpClient: MockClient((req) async {
        final path = req.url.path;
        if (path == '/api/geo/project/simplified') {
          calls.add(req.url);
          final name = req.url.queryParameters['name']!;
          final zoom = double.parse(req.url.queryParameters['zoom']!).ceil();
          if (name == 'Trip' && zoom == 15) {
            await Future<void>.delayed(const Duration(milliseconds: 500));
          } else {
            await Future<void>.delayed(const Duration(milliseconds: 20));
          }
          return _json(_geoWith(name == 'Trip' ? zoom : 1000 + zoom));
        }
        if (path == '/api/geo/project/low-res') {
          return _json({'type': 'FeatureCollection', 'features': <dynamic>[]});
        }
        if (path == '/api/projects/Trip/meta') return _json(_meta('Trip'));
        if (path == '/api/projects/Other/meta') return _json(_meta('Other'));
        return _json({});
      }),
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => projectDataCache.resetForTest());

  test('a refetch resolving after a second load() for another project is discarded',
      () async {
    final calls = <Uri>[];
    api = _api(calls);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);

    await n.load(const ProjectRef(name: 'Trip'));
    expect(await _waitFor(() => _pointsOf(n) == 9), isTrue);

    n.setMapZoom(15); // schedules Trip@15, which will take 500 ms
    expect(
        await _waitFor(() => calls.any((u) =>
            u.queryParameters['name'] == 'Trip' &&
            u.queryParameters['zoom']!.startsWith('15'))),
        isTrue);

    await n.load(const ProjectRef(name: 'Other')); // supersedes mid-refetch
    expect(await _waitFor(() => _pointsOf(n) == 1015), isTrue,
        reason: 'Other at zoom 15 should land first');

    // Outlast the superseded Trip@15 response, then prove it was dropped rather
    // than merely late.
    await Future<void>.delayed(const Duration(milliseconds: 700));
    expect(_pointsOf(n), 1015,
        reason: 'the superseded Trip@15 refetch must not overwrite Other');
    expect(n.ref?.name, 'Other');
  });

  test('a refetch resolving after clear() does not repopulate geo', () async {
    final calls = <Uri>[];
    api = _api(calls);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);

    await n.load(const ProjectRef(name: 'Trip'));
    expect(await _waitFor(() => _pointsOf(n) == 9), isTrue);

    n.setMapZoom(15);
    expect(
        await _waitFor(() =>
            calls.any((u) => u.queryParameters['zoom']!.startsWith('15'))),
        isTrue);

    n.clear();
    await Future<void>.delayed(const Duration(milliseconds: 700));
    expect(n.geo, isNull,
        reason: 'a torn-down notifier must not be repopulated by a late fetch');
  });
}
