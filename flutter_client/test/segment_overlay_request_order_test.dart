// A resolved segment's patch gives way to a geo request started after it was
// applied, and only to such a request (I1-R2-2 of
// docs/reviews/CLIENT_STATE_MAP_PLAN.md) — whichever notifier started it, when
// a request joins another notifier's (I1-R3-3).
//
// The patch used to be kept whenever the server's route_hash differed from
// its own, so another writer's re-route — the hourly degraded-route sweep,
// another device's track edit — stayed hidden behind it until the trip was
// reopened. A request started before the patch can still carry the state the
// patch replaced (issue #278), so it keeps the patch unless its content
// already matches.

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import 'helpers/signed_in.dart';

const _trip = ProjectRef(name: 'Trip');

// Stored route_polyline strings and their CRC-32 (Python's zlib.crc32), as
// in segment_overlay_test.dart.
const _routeA = '[[0,0],[0.5,0.7],[1,1]]';
const _hashA = 2591404778;
const _hashB = 2401349885;

Map<String, dynamic> _collection(List<dynamic> features) =>
    {'type': 'FeatureCollection', 'features': features};

Map<String, dynamic> _serverSeg(List<List<num>> coords,
        {required String status, int? hash}) =>
    {
      'type': 'Feature',
      'geometry': {'type': 'LineString', 'coordinates': coords},
      'properties': {
        'type': 'segment',
        'segment_id': 's1',
        'route_mode': 'rail',
        'route_status': status,
        if (hash != null) 'route_hash': hash,
      },
    };

/// The great-circle placeholder a fetch from before the resolve still has.
Map<String, dynamic> get _pendingArc => _serverSeg([
      [0, 0],
      [1, 1],
    ], status: 'pending');

/// Route B, written by another writer after this client applied route A.
Map<String, dynamic> get _routeBFeature => _serverSeg([
      [0, 0],
      [0.4, 0.9],
      [1, 1],
    ], status: 'resolved', hash: _hashB);

/// One trip with one train segment. Geo endpoints answer [geo]; a simplified
/// geo request is held on [heldLod] when a test sets it. With [joinHeldLod],
/// every simplified request while it is held gets that one, the way the real
/// service hands a request in flight to any later caller.
class _Server extends ProjectService {
  Map<String, dynamic> geo = _collection([_pendingArc]);
  Completer<Map<String, dynamic>>? heldLod;
  bool joinHeldLod = false;
  int lodCalls = 0;

  Map<String, dynamic> _copy(Map<String, dynamic> m) =>
      jsonDecode(jsonEncode(m)) as Map<String, dynamic>;

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => {
        'name': ref.name,
        'activities': <dynamic>[],
        'items': [
          {
            'item_type': 'segment',
            'segment': {
              'id': 's1',
              'segment_type': 'train',
              'route_mode': 'rail',
              'start': {'lat': 0.0, 'lon': 0.0},
              'end': {'lat': 1.0, 'lon': 1.0},
            },
          },
        ],
      };

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      _collection([]);

  @override
  Future<Map<String, dynamic>> getSimplifiedGeo(ProjectRef ref, double zoom,
      {Object? bbox}) {
    lodCalls++;
    final held = heldLod;
    if (held != null) {
      if (!joinHeldLod) heldLod = null;
      return held.future;
    }
    return Future.value(_copy(geo));
  }

  /// The refresh after a write asks for its own request (issue #379).
  @override
  Future<Map<String, dynamic>> getSimplifiedGeoFresh(ProjectRef ref, double zoom,
          {Object? bbox}) async =>
      _copy(geo);

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _copy(geo);

  @override
  Future<Map<String, dynamic>> resetActivityTrack(
          ProjectRef ref, int activityId) async =>
      {};
}

ProjectNotifier _notifier(_Server server) => ProjectNotifier(server)
  ..loadRetryBackoff = const []
  ..zoomRefetchDebounce = Duration.zero;

/// The coordinates [features] draw for s1.
List<dynamic> _drawn(List<dynamic> features) {
  for (final f in features) {
    final props = (f as Map)['properties'] as Map;
    if (props['segment_id'] == 's1') {
      return (f['geometry'] as Map)['coordinates'] as List;
    }
  }
  fail('s1 is not drawn');
}

List<dynamic> _drawnOnMap(ProjectNotifier n) =>
    _drawn(n.geoFacet.geo!['features'] as List);

/// Loads the trip and applies route A the way the resolve poller does.
Future<(_Server, ProjectNotifier)> _loadedWithRouteA(
    WidgetTester tester) async {
  final server = _Server();
  return (server, await _loaded(tester, server));
}

/// A notifier with the trip loaded from [server], showing the arc.
Future<ProjectNotifier> _loaded(WidgetTester tester, _Server server) async {
  final n = _notifier(server);
  await n.load(_trip);
  // The background geo phase, and the sync check load() schedules at 5 s.
  await tester.pump(const Duration(seconds: 6));
  expect(_drawnOnMap(n), hasLength(2), reason: 'the arc before the resolve');
  return n;
}

void _applyRouteA(ProjectNotifier n) => n.applyResolvedSegment('s1', {
      'route_mode': 'rail',
      'segment_type': 'train',
      'route_polyline': _routeA,
    });

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    projectDataCache.resetForTest();
    signInAs(1,
        httpClient: MockClient((req) async => http.Response('{}', 200)));
  });

  testWidgets('a zoom refetch started before the resolve keeps the route',
      (tester) async {
    final (server, n) = await _loadedWithRouteA(tester);
    final held = Completer<Map<String, dynamic>>();
    server.heldLod = held;
    final calls = server.lodCalls;
    n.setMapZoom(14);
    // The debounce fires; the refetch is now in flight.
    await tester.pump(const Duration(milliseconds: 1));
    expect(server.lodCalls, calls + 1);

    _applyRouteA(n);
    held.complete(_collection([_pendingArc]));
    await tester.pump(const Duration(seconds: 1));

    expect(_drawnOnMap(n), hasLength(3),
        reason: 'the resolved route, not the arc the refetch carried');
    n.dispose();
  });

  testWidgets("a refetch joining another notifier's earlier request keeps the "
      'route (I1-R3-3)', (tester) async {
    // The view-mode notifier and the app-wide one, on one service.
    final server = _Server();
    final view = await _loaded(tester, server);
    final manage = await _loaded(tester, server);
    final held = Completer<Map<String, dynamic>>();
    server
      ..heldLod = held
      ..joinHeldLod = true;
    final calls = server.lodCalls;
    view.setMapZoom(14);
    await tester.pump(const Duration(milliseconds: 1));
    expect(server.lodCalls, calls + 1, reason: "the view's request in flight");

    _applyRouteA(manage);
    manage.setMapZoom(14);
    await tester.pump(const Duration(milliseconds: 1));
    expect(server.lodCalls, calls + 2, reason: 'the manage refetch joins it');

    server.heldLod = null;
    held.complete(_collection([_pendingArc]));
    await tester.pump(const Duration(seconds: 1));

    expect(_drawnOnMap(manage), hasLength(3),
        reason: 'the answer is from before the resolve, so the route stays');
    view.dispose();
    manage.dispose();
  });

  testWidgets('a zoom refetch started after the resolve shows another '
      "writer's route", (tester) async {
    final (server, n) = await _loadedWithRouteA(tester);
    _applyRouteA(n);
    expect(_drawnOnMap(n)[1], [0.5, 0.7]);

    // The hourly sweep or another device re-routes the segment.
    server.geo = _collection([_routeBFeature]);
    n.setMapZoom(14);
    await tester.pump(const Duration(seconds: 1));

    expect(_drawnOnMap(n)[1], [0.4, 0.9],
        reason: "the server's route B, not the patch's route A");
    n.dispose();
  });

  testWidgets('a reload after the resolve leaves no patch to bring the old '
      'route back', (tester) async {
    final (server, n) = await _loadedWithRouteA(tester);
    _applyRouteA(n);

    server.geo = _collection([_routeBFeature]);
    await n.resetActivityTrack(1); // a full reload: details and geo
    expect(_drawnOnMap(n)[1], [0.4, 0.9]);

    // Every later rebuild re-applies the overlay over what the server sent.
    final rebuilt = n.mergePendingSegmentPatches(
        List<dynamic>.from(n.geoFacet.geo!['features'] as List));
    expect(_drawn(rebuilt)[1], [0.4, 0.9],
        reason: 'route A must not come back over the server route');
    n.dispose();
  });

  testWidgets('a reload that already has the resolved route drops the patch',
      (tester) async {
    final (server, n) = await _loadedWithRouteA(tester);
    _applyRouteA(n);

    server.geo = _collection([
      _serverSeg([
        [0, 0],
        [0.5, 0.7],
        [1, 1],
      ], status: 'resolved', hash: _hashA),
    ]);
    await n.resetActivityTrack(1);

    expect(_drawnOnMap(n)[1], [0.5, 0.7]);
    expect(_drawn(n.mergePendingSegmentPatches([_pendingArc])), hasLength(2),
        reason: 'no patch left over');
    n.dispose();
  });
}
