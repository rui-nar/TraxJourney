// Issue #379: the refresh after a write keeps the map's level of detail.
//
// A track save, a remove, a split, a Strava refresh and a segment 409 used to
// reload the trip's geometry at full resolution — the ~180 MB the zoom level
// of detail exists to avoid (#295) — write it into L1 for the rest of the
// session, and leave it under a zoom bucket that no longer described it. They
// now fetch simplified geometry at the camera's level and box, from a request
// of their own, and an older zoom or load answer still in flight cannot put
// the pre-write geometry back (Decisions 10, 20 and 24 of
// docs/CLIENT_STATE_MAP_PLAN.md).

import 'dart:async';
import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/crypto/encryption.dart';
import 'package:traxjourney_client/src/crypto/encryption_service.dart';
import 'package:traxjourney_client/src/projects/facets/project_facet.dart';
import 'package:traxjourney_client/src/projects/geo_viewport.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

/// A camera box over the Alps.
const _vp = GeoBox(7.30, 45.30, 7.50, 45.50);

http.Response _json(Object body) => http.Response(jsonEncode(body), 200);

/// One trip: one activity and one segment. Every write bumps [version], which
/// the geometry endpoints stamp on what they send, so a test can tell the
/// geometry from before a write from the geometry after it.
class _Server {
  int version = 1;

  /// The segment's label as the server holds it.
  String segmentLabel = 'server label';

  /// Each simplified request: its zoom and box parameters.
  final simplified = <({String zoom, String? bbox})>[];

  /// Requests for full-resolution geometry.
  int fullGeo = 0;

  /// Full-resolution fetches through the map's path ([ProjectService.getGeo]),
  /// which keep the payload in L1, and through the offline seed's
  /// ([ProjectService.fetchFullGeoUncached]), which does not (see [_Service]).
  int mapPathFullGeo = 0;
  int seedFullGeo = 0;

  /// While true, each simplified request waits on its own entry in [held].
  bool hold = false;
  final held = <Completer<void>>[];

  /// While true, a segment PUT answers 409, as if another device had written.
  bool segmentConflict = false;

  Map<String, dynamic> _meta() => {
        'name': 'Trip',
        'lock_version': version,
        'activities': [
          {
            'id': 1,
            'type': 'Ride',
            'name': 'Ride',
            'start_date_local': '2026-06-01T08:00:00',
            'refresh_status': 'resolved',
          },
        ],
        'items': [
          {'item_type': 'activity', 'activity_id': 1},
          {
            'item_type': 'segment',
            'segment': {
              'id': 's1',
              'segment_type': 'flight',
              'label': segmentLabel,
              'start': {'lat': 45.0, 'lon': 7.0},
              'end': {'lat': 46.0, 'lon': 8.0},
            },
          },
        ],
        'people': <dynamic>[],
        'groups': <dynamic>[],
      };

  Map<String, dynamic> _geo(int points, int v) => {
        'type': 'FeatureCollection',
        'features': [
          {
            'type': 'Feature',
            'properties': {'type': 'activity', 'activity_id': '1', 'v': v},
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                for (var i = 0; i < points; i++)
                  [7.0 + i * 0.001, 45.0 + i * 0.001]
              ],
            },
          },
        ],
      };

  ApiClient api() => ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          final path = req.url.path;
          if (path == '/api/projects/Trip/meta' || path == '/api/projects/Trip') {
            return _json(_meta());
          }
          if (path == '/api/geo/project/low-res') {
            return _json({'type': 'FeatureCollection', 'features': <dynamic>[]});
          }
          if (path == '/api/geo/project/simplified') {
            // The server's state when the request arrives is what it answers.
            final v = version;
            final zoom = req.url.queryParameters['zoom']!;
            simplified.add((zoom: zoom, bbox: req.url.queryParameters['bbox']));
            if (hold) {
              final gate = Completer<void>();
              held.add(gate);
              await gate.future;
            }
            return _json(_geo(double.parse(zoom).ceil(), v));
          }
          if (path == '/api/geo/project') {
            fullGeo++;
            return _json(_geo(999, version));
          }
          if (path.startsWith('/api/projects/Trip/segments/') &&
              req.method == 'PUT' &&
              segmentConflict) {
            version++; // another device's write
            segmentLabel = 'other device';
            return http.Response(jsonEncode({'detail': 'conflict'}), 409);
          }
          if (path == '/api/projects/Trip/activities/1/refresh') {
            version++;
            return _json({'status': 'pending', 'refresh_status': 'pending'});
          }
          if (req.method != 'GET') {
            version++; // a write
            return _json(<String, dynamic>{});
          }
          return _json(<String, dynamic>{});
        }),
      );
}

/// Tells the two ways to full-resolution geometry apart, which share one
/// endpoint: the map's, and the offline seed's disk-only one (I2-R1-1).
class _Service extends ProjectService {
  final _Server server;
  _Service(this.server);

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
      {bool bypassCache = false}) {
    server.mapPathFullGeo++;
    return super.getGeo(ref, bypassCache: bypassCache);
  }

  @override
  Future<Map<String, dynamic>> fetchFullGeoUncached(ProjectRef ref) {
    server.seedFullGeo++;
    return super.fetchFullGeoUncached(ref);
  }
}

/// The write stamp of the geometry on screen.
int? _v(ProjectNotifier n) {
  final features = n.geoFacet.geo?['features'] as List?;
  for (final f in features ?? const []) {
    final v = (f as Map)['properties']['v'];
    if (v != null) return v as int;
  }
  return null;
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

/// Lets in-flight work that has nothing to wait for run to the end.
Future<void> _settle() => Future<void>.delayed(const Duration(milliseconds: 30));

/// A notifier with the trip loaded at zoom 9 and the camera on [_vp].
Future<ProjectNotifier> _loaded(_Server server) async {
  api = server.api();
  final n = ProjectNotifier(_Service(server))
    ..loadRetryBackoff = const []
    ..zoomRefetchDebounce = const Duration(milliseconds: 10)
    ..setMapZoom(9);
  await n.load(_ref);
  expect(await _waitFor(() => n.geoFacet.lod.kind == GeoLodKind.level), isTrue);
  await _settle(); // the offline seed
  expect(n.geoFacet.lod, const GeoLod.level(9),
      reason: 'the load fetches the whole trip at the camera\'s level');
  // The map's next camera event. Whole-trip geometry contains any viewport,
  // so it refetches nothing.
  final requests = server.simplified.length;
  n.setMapZoom(9, viewport: _vp);
  await _settle();
  expect(server.simplified, hasLength(requests));
  return n;
}

/// The writes whose refresh is under test, by name.
final _writes = <String, Future<void> Function(ProjectNotifier)>{
  'a track save': (n) => n.saveActivityTrack(1, {'points': <dynamic>[]}),
  'a remove': (n) => n.removeItem(0),
  'a split': (n) => n.splitActivity(1, 1),
  'a Strava refresh': (n) => n.refreshActivity(1,
      pollInterval: const Duration(milliseconds: 1),
      pollTimeout: const Duration(seconds: 2)),
  'a segment 409': (n) => n.updateSegment('s1',
      segmentType: 'flight',
      label: 'optimistic label',
      startLat: 45.0,
      startLon: 7.0,
      endLat: 46.0,
      endLon: 8.0),
};

void main() {
  setUp(() => projectDataCache.resetForTest());

  group('after a write the map holds simplified geometry at the camera\'s '
      'level and box', () {
    for (final e in _writes.entries) {
      test(e.key, () async {
        final server = _Server();
        final n = await _loaded(server);
        if (e.key == 'a segment 409') server.segmentConflict = true;
        final before = server.simplified.length;
        final mapPathBefore = server.mapPathFullGeo;
        final seedsBefore = server.seedFullGeo;

        await e.value(n);
        await _settle();

        final box = fetchBoxFor(_vp, 9);
        expect(n.geoFacet.lod, GeoLod.level(9, box: box));
        expect(server.simplified.sublist(before),
            [(zoom: '9.0', bbox: box.param)],
            reason: 'one simplified request, at the level and box on screen');
        expect(_v(n), server.version, reason: 'the server\'s state after it');
        expect(server.mapPathFullGeo, mapPathBefore,
            reason: "no full-resolution fetch through the map's path");
        expect(server.seedFullGeo - seedsBefore, lessThanOrEqualTo(1),
            reason: 'the only full-resolution request is the offline seed '
                '(I2-R1-1), at most one per edit');
        expect(await projectDataCache.readFullGeo(_ref), isNull,
            reason: 'and no full-resolution geometry in L1');
        n.dispose();
      });
    }

    test('the 409 path shows the server\'s state, not the optimistic edit',
        () async {
      final server = _Server()..segmentConflict = true;
      final n = await _loaded(server);

      await _writes['a segment 409']!(n);
      await _settle();

      final segment = n.itemsFacet.items
          .firstWhere((i) => i['item_type'] == 'segment')['segment'] as Map;
      expect(segment['label'], 'other device');
      expect(_v(n), server.version);
      expect(n.error, contains('changed elsewhere'));
      n.dispose();
    });
  });

  group('an E2EE trip stays at full resolution', () {
    setUp(() async {
      api = _Server().api();
      FlutterSecureStorage.setMockInitialValues({});
      await encryption.enable(const RecoveryKeyChoice());
      expect(encryption.isUnlocked, isTrue);
    });
    tearDown(() => encryption.lock());

    for (final e in _writes.entries) {
      test(e.key, () async {
        final server = _Server();
        api = server.api();
        final n = ProjectNotifier(_Service(server))
          ..loadRetryBackoff = const []
          ..setMapZoom(9, viewport: _vp);
        await n.load(_ref);
        expect(await _waitFor(() => n.geoFacet.lod == GeoLod.full), isTrue);
        if (e.key == 'a segment 409') server.segmentConflict = true;
        final v = n.geoFacet.version;

        await e.value(n);
        await _settle();

        expect(n.geoFacet.version, greaterThan(v), reason: 'rebuilt');
        expect(n.geoFacet.lod, GeoLod.full);
        expect(server.simplified, isEmpty,
            reason: 'the server cannot build an E2EE trip\'s geometry');
        expect(server.fullGeo, 0);
        expect(server.seedFullGeo, 0, reason: 'no offline seed for E2EE');
        n.dispose();
      });
    }
  });

  group('an older answer still in flight cannot put back the pre-write '
      'geometry (Decision 24)', () {
    /// Loads at zoom 9 with no camera box, zooms to 12 and holds the zoom
    /// refetch, then writes and holds the refresh. Returns the notifier, the
    /// write, and the two held requests: the zoom refetch's and the refresh's.
    Future<(ProjectNotifier, Future<void>, Completer<void>, Completer<void>)>
        zoomThenWrite(_Server server) async {
      api = server.api();
      final n = ProjectNotifier(_Service(server))
        ..loadRetryBackoff = const []
        ..zoomRefetchDebounce = const Duration(milliseconds: 10)
        ..setMapZoom(9);
      await n.load(_ref);
      expect(await _waitFor(() => n.geoFacet.lod.kind == GeoLodKind.level),
          isTrue);
      await _settle();

      server.hold = true;
      n.setMapZoom(12);
      expect(await _waitFor(() => server.held.length == 1), isTrue,
          reason: 'the zoom refetch is in flight');

      final write = n.resetActivityTrack(1);
      expect(await _waitFor(() => server.held.length == 2), isTrue,
          reason: 'the refresh is in flight beside it');
      expect(server.simplified.reversed.take(2).toList(),
          [(zoom: '12.0', bbox: null), (zoom: '12.0', bbox: null)],
          reason: 'the same level and box: the refresh did not join the '
              'zoom request, which started before the write');
      return (n, write, server.held[0], server.held[1]);
    }

    test('the zoom answer landing after the refresh is ignored', () async {
      final server = _Server();
      final (n, write, zoom, refresh) = await zoomThenWrite(server);

      refresh.complete();
      await write;
      expect(_v(n), 2);
      zoom.complete();
      await _settle();

      expect(_v(n), 2, reason: 'the pre-write zoom answer must not land');
      expect(n.geoFacet.lod, const GeoLod.level(12));
      n.dispose();
    });

    test('the zoom answer landing first is replaced by the refresh', () async {
      final server = _Server();
      final (n, write, zoom, refresh) = await zoomThenWrite(server);

      zoom.complete();
      expect(await _waitFor(() => n.geoFacet.lod == const GeoLod.level(12)),
          isTrue);
      expect(_v(n), 1, reason: 'the zoom answer is from before the write');
      refresh.complete();
      await write;
      await _settle();

      expect(_v(n), 2);
      n.dispose();
    });

    /// Holds the load's own simplified request, writes once the trip is open
    /// and holds the refresh too.
    Future<(ProjectNotifier, Future<void>, Completer<void>, Completer<void>)>
        loadThenWrite(_Server server) async {
      api = server.api();
      server.hold = true;
      final n = ProjectNotifier(_Service(server))
        ..loadRetryBackoff = const []
        ..setMapZoom(9);
      await n.load(_ref);
      expect(await _waitFor(() => server.held.length == 1), isTrue,
          reason: "the load's level of detail is in flight");

      final write = n.resetActivityTrack(1);
      expect(await _waitFor(() => server.held.length == 2), isTrue);
      expect(server.simplified, [
        (zoom: '9.0', bbox: null),
        (zoom: '9.0', bbox: null),
      ]);
      return (n, write, server.held[0], server.held[1]);
    }

    test("the load's answer landing after the refresh is ignored", () async {
      final server = _Server();
      final (n, write, load, refresh) = await loadThenWrite(server);

      refresh.complete();
      await write;
      expect(_v(n), 2);
      load.complete();
      await _settle();

      expect(_v(n), 2);
      expect(n.geoFacet.lod, const GeoLod.level(9));
      expect(n.geoFacet.isLoaded, isTrue,
          reason: 'the load still finishes its geometry phase');
      n.dispose();
    });

    test("the load's answer landing first is replaced by the refresh",
        () async {
      final server = _Server();
      final (n, write, load, refresh) = await loadThenWrite(server);

      load.complete();
      expect(await _waitFor(() => _v(n) == 1), isTrue);
      refresh.complete();
      await write;
      await _settle();

      expect(_v(n), 2);
      n.dispose();
    });
  });
}
