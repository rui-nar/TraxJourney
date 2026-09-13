// Issue #321, client half: zoom level of detail on a share link.
//
// The zoom LOD of #295 landed for the owner path only. A shared or public link
// declared there was no share-scoped endpoint and failed fast, taking the
// older-server fallback to /api/share/{token}/geo — full resolution, 4.5 MB and
// roughly 180 MB of heap on the trip it was measured against, on the device
// least likely to have room for it.
//
// There is one now. These pin that the shared screen actually uses it, that it
// asks the way the owner path asks, and that the fallback it replaced is still
// there for an older server.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/geo_viewport.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/shared/shared_project_screen.dart';

const _token = 'tok123';
const _ref = ProjectRef(name: _token);

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

Map<String, dynamic> _meta() => {
      'name': 'Trip',
      'owner_name': 'Owner',
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

class _Calls {
  /// Every simplified request's full URI, in order.
  final lod = <Uri>[];
  int fullGeo = 0;
  int lodInFlight = 0;
  int lodInFlightPeak = 0;
}

/// [lodStatus] lets a test make the share-scoped endpoint fail, to exercise
/// the fallback an older server — one deployed before this endpoint — takes.
ApiClient _api(_Calls calls,
        {int lodStatus = 200,
        Duration lodDelay = const Duration(milliseconds: 15)}) =>
    ApiClient(
      baseUrl: '',
      httpClient: MockClient((req) async {
        final path = req.url.path;
        if (path == '/api/share/$_token/geo/simplified') {
          calls.lodInFlight++;
          if (calls.lodInFlight > calls.lodInFlightPeak) {
            calls.lodInFlightPeak = calls.lodInFlight;
          }
          await Future<void>.delayed(lodDelay);
          calls.lodInFlight--;
          calls.lod.add(req.url);
          if (lodStatus != 200) return http.Response('nope', lodStatus);
          // Point count scales with zoom, as real simplification does.
          return _json(
              _geoWith(double.parse(req.url.queryParameters['zoom']!).ceil()));
        }
        if (path == '/api/share/$_token/geo') {
          calls.fullGeo++;
          return _json(_geoWith(999));
        }
        if (path == '/api/share/$_token/meta') return _json(_meta());
        if (path == '/api/share/$_token/geo/low-res') {
          return _json({'type': 'FeatureCollection', 'features': <dynamic>[]});
        }
        if (path == '/api/share/$_token') return _json(_meta());
        return _json({});
      }),
    );

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

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => projectDataCache.resetForTest());

  test('a shared load fetches geometry for the zoom, not full resolution',
      () async {
    final calls = _Calls();
    api = _api(calls);
    final n = SharedProjectNotifier(_token)..setMapZoom(9);

    await n.loadShared();
    expect(await _waitFor(() => _pointsOf(n) > 0), isTrue);

    expect(calls.lod.map((u) => u.queryParameters['zoom']), ['9.0']);
    expect(calls.fullGeo, 0,
        reason: 'the full-resolution payload is what this exists to avoid');
    expect(_pointsOf(n), 9);
  });

  test('the visitor id is not sent with a simplified request', () async {
    // aid is who is looking. It is recorded once per shared load by /meta;
    // sending it on a request that repeats on every zoom and every pan would
    // be a DB write per bucket for a number that cannot go up — and, if it
    // ever reached the server's cache key, an entry per visitor.
    final calls = _Calls();
    api = _api(calls);
    final n = SharedProjectNotifier(_token, anonymousId: 'visitor-a')
      ..setMapZoom(9);

    await n.loadShared();
    expect(await _waitFor(() => calls.lod.isNotEmpty), isTrue);
    expect(calls.lod.single.queryParameters.containsKey('aid'), isFalse);
  });

  test('the camera box is passed through', () async {
    final calls = _Calls();
    api = _api(calls);
    final n = SharedProjectNotifier(_token);

    await n.service.getSimplifiedGeo(_ref, 12,
        bbox: const GeoBox(7.3, 45.3, 7.5, 45.5));

    expect(calls.lod.single.queryParameters['bbox'],
        const GeoBox(7.3, 45.3, 7.5, 45.5).param);
  });

  test('two callers at the same level share one request', () async {
    // The shared getGeo override bypasses the base class's in-flight dedup;
    // this one must not. A mode toggle mid-load and a concurrent zoom refetch
    // would otherwise fire the same multi-MB request twice and decode both
    // back to back on the UI isolate — the ANR the dedup exists for.
    final calls = _Calls();
    api = _api(calls);
    final n = SharedProjectNotifier(_token);

    await Future.wait([
      n.service.getSimplifiedGeo(_ref, 12),
      n.service.getSimplifiedGeo(_ref, 12),
    ]);

    expect(calls.lod, hasLength(1));
    expect(calls.lodInFlightPeak, 1);
  });

  test('zooming in asks for more detail', () async {
    final calls = _Calls();
    api = _api(calls);
    final n = SharedProjectNotifier(_token)
      ..setMapZoom(9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);

    await n.loadShared();
    expect(await _waitFor(() => _pointsOf(n) == 9), isTrue);

    n.setMapZoom(15);
    expect(await _waitFor(() => _pointsOf(n) == 15), isTrue,
        reason: 'a new zoom bucket must bring finer geometry');
  });

  test('a companion whose role the server corrects also gets more detail',
      () async {
    // The same bug as the test above, on the owner-side path — which is why
    // it lives in this file. `ref` drifts from the ref load() began with as
    // soon as the server answers: the share screen's name goes from token to
    // trip name, and a companion's role goes from the "editor" placeholder to
    // whatever they actually are. A refetch reads `ref`, so on either path it
    // held a ref the load track could never match and its result was fetched
    // and then discarded.
    final calls = <Uri>[];
    api = ApiClient(
      baseUrl: '',
      httpClient: MockClient((req) async {
        final path = req.url.path;
        if (path == '/api/geo/project/simplified') {
          calls.add(req.url);
          await Future<void>.delayed(const Duration(milliseconds: 15));
          return _json(_geoWith(
              double.parse(req.url.queryParameters['zoom']!).ceil()));
        }
        if (path == '/api/geo/project/low-res') {
          return _json({'type': 'FeatureCollection', 'features': <dynamic>[]});
        }
        if (path == '/api/projects/Trip/meta') {
          // The correction: this caller is a viewer, not the "editor" the
          // URL-derived placeholder guessed.
          return _json({..._meta(), 'caller_role': 'viewer'});
        }
        return _json({});
      }),
    );
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);

    await n.load(const ProjectRef(name: 'Trip', ownerId: 7, role: 'editor'));
    expect(await _waitFor(() => _pointsOf(n) == 9), isTrue);

    n.setMapZoom(15);
    expect(await _waitFor(() => _pointsOf(n) == 15), isTrue,
        reason: 'a corrected role must not strand the viewer at one level');
  });

  test('an older server without the endpoint still serves the shared trip',
      () async {
    // The notifier's own fallback, unchanged — which is why the client needed
    // no new fallback logic for this.
    final calls = _Calls();
    api = _api(calls, lodStatus: 404);
    final n = SharedProjectNotifier(_token)..setMapZoom(9);

    await n.loadShared();
    expect(await _waitFor(() => _pointsOf(n) > 0), isTrue);
    expect(calls.fullGeo, greaterThan(0));
    expect(_pointsOf(n), 999);
  });
}
