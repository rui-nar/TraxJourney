// Issue #295, client half of zoom level of detail.
//
// The client used to hold full-resolution geometry regardless of zoom: a
// 219-activity trip carried 1,465,345 coordinates while the map rendered
// 6,051, the rest costing roughly 180 MB of a Dart heap that device profiling
// put at ~625 MB steady and ~804 MB during load, on a process Android kills
// above ~1.3 GB.
//
// It now asks the server for geometry simplified to about one screen pixel at
// the current zoom, and asks again when the zoom bucket changes — and, since
// issue #324, scoped to the camera's box, asking again when the camera leaves
// it.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/perf_timing.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/geo_viewport.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

http.Response _json(Map<String, dynamic> body) =>
    http.Response(jsonEncode(body), 200);

/// A feature whose point count encodes the zoom it was built for, so a test
/// can tell which level the notifier is holding.
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
  final zooms = <String>[];
  /// The `bbox` parameter of each simplified request, null when none was sent.
  final boxes = <String?>[];
  int fullGeo = 0;
  bool failLod = false;
  /// Makes the full-resolution endpoint fail, for the export path's fallback.
  bool failFullGeo = false;
  /// Highest number of simplified requests outstanding at once. The refetch
  /// awaits a fetch, a camera-idle wait and _buildFullTrack, and the last of
  /// those copies `geo` into an isolate — so overlapping them is what turned
  /// one unsatisfiable staleness check into gigabytes (issue #332).
  int inFlight = 0;
  int inFlightPeak = 0;
}

/// A camera box over the Alps, small enough that a tile-snapped fetch box
/// around it is nowhere near the far-away one the pan tests move to.
const _vp = GeoBox(7.30, 45.30, 7.50, 45.50);

/// [lodStatus] lets a test make the simplified endpoint fail, to exercise the
/// fallback an older server would take; [lodDelay] holds the response so a
/// test can watch what is on screen while a refetch is in flight.
ApiClient _api(_Calls calls,
        {int lodStatus = 200, Duration lodDelay = Duration.zero}) =>
    ApiClient(
      baseUrl: '',
      httpClient: MockClient((req) async {
        final path = req.url.path;
        if (path == '/api/projects/Trip/meta') return _json(_meta());
        if (path == '/api/geo/project/low-res') {
          return _json({'type': 'FeatureCollection', 'features': <dynamic>[]});
        }
        if (path == '/api/geo/project/simplified') {
          calls.inFlight++;
          if (calls.inFlight > calls.inFlightPeak) {
            calls.inFlightPeak = calls.inFlight;
          }
          // Long enough that a second refetch starting before this one
          // finishes is observable rather than a race.
          await Future<void>.delayed(const Duration(milliseconds: 15));
          calls.inFlight--;
          if (lodStatus != 200) return http.Response('nope', lodStatus);
          final z = req.url.queryParameters['zoom']!;
          calls.zooms.add(z);
          calls.boxes.add(req.url.queryParameters['bbox']);
          if (calls.failLod) return http.Response('nope', 500);
          if (lodDelay > Duration.zero) await Future<void>.delayed(lodDelay);
          // Point count scales with zoom, as real simplification does.
          return _json(_geoWith(double.parse(z).ceil()));
        }
        if (path == '/api/geo/project') {
          calls.fullGeo++;
          if (calls.failFullGeo) return http.Response('nope', 500);
          return _json(_geoWith(999));
        }
        if (path == '/api/projects/Trip/elevation') {
          return _json({'profiles': <String, dynamic>{}, 'encrypted': <String, dynamic>{}});
        }
        return _json({});
      }),
    );

/// Point count of the first feature, or 0 while geo is still the empty
/// low-res placeholder the load starts from.
int _points(ProjectNotifier n) {
  final features = n.geo?['features'] as List?;
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

  test('the load fetches geometry for the current zoom, not full resolution',
      () async {
    final calls = _Calls();
    api = _api(calls);
    final n = ProjectNotifier(ProjectService())..setMapZoom(9);

    await n.load(_ref);
    expect(await _waitFor(() => _points(n) > 0), isTrue);

    expect(calls.zooms, ['9.0']);
    expect(_points(n), 9,
        reason: 'what the map holds is what the zoom asked for');
    // The full-resolution payload is fetched once afterwards, in the
    // background, purely to seed the offline cache (issue #317) — see the
    // group below. What matters here is that it never becomes the geometry
    // this notifier holds.
    await Future<void>.delayed(const Duration(milliseconds: 30));
    expect(_points(n), 9,
        reason: 'the offline seed must not replace what is on screen');
  });

  test('zooming in asks for more detail', () async {
    final calls = _Calls();
    api = _api(calls);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);

    await n.load(_ref);
    expect(await _waitFor(() => _points(n) == 9), isTrue);

    n.setMapZoom(15);
    expect(await _waitFor(() => _points(n) == 15), isTrue,
        reason: 'a new zoom bucket must bring finer geometry');
  });

  test('panning within a zoom bucket refetches nothing', () async {
    // The camera fires events continuously; only a bucket change is a reason
    // to go back to the server.
    final calls = _Calls();
    api = _api(calls);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);

    await n.load(_ref);
    expect(await _waitFor(() => _points(n) == 9), isTrue);

    for (final z in [8.2, 8.5, 8.9, 9.0]) {
      n.setMapZoom(z);
    }
    await Future<void>.delayed(const Duration(milliseconds: 80));
    expect(calls.zooms, hasLength(1),
        reason: 'ceil() puts all of those in bucket 9, already loaded');
  });

  test('a pinch through several levels causes one refetch', () async {
    final calls = _Calls();
    api = _api(calls);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 40);

    await n.load(_ref);
    expect(await _waitFor(() => _points(n) == 9), isTrue);

    for (final z in [10.0, 11.0, 12.0, 13.0, 14.0]) {
      n.setMapZoom(z);
    }
    expect(await _waitFor(() => _points(n) == 14), isTrue);
    expect(calls.zooms, hasLength(2),
        reason: 'the initial load plus one settled refetch, not five');
  });

  test('opening a second project does not carry the first zoom bucket over',
      () async {
    // ProjectNotifier is a single app-wide provider. A bucket left from the
    // previous trip arms refetching for geometry it has nothing to do with.
    final calls = _Calls();
    api = _api(calls);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);

    await n.load(_ref);
    expect(await _waitFor(() => _points(n) == 9), isTrue);

    await n.load(const ProjectRef(name: 'Other'));
    // A zoom event arriving before the second load's geometry has landed must
    // not fire a refetch against a stale bucket.
    n.setMapZoom(15);
    await Future<void>.delayed(const Duration(milliseconds: 60));
    expect(n.geo, isNotNull);
  });

  test('clear() disarms zoom refetching', () async {
    final calls = _Calls();
    api = _api(calls);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);

    await n.load(_ref);
    expect(await _waitFor(() => _points(n) == 9), isTrue);
    final before = calls.zooms.length;

    n.clear();
    n.setMapZoom(16);
    await Future<void>.delayed(const Duration(milliseconds: 60));
    expect(calls.zooms, hasLength(before),
        reason: 'nothing is on screen to refetch geometry for');
  });

  test('the bucket recorded is the one that was requested', () async {
    // The fit-bounds animation moves the camera during the fetch and the
    // camera-idle wait. Stamping whatever _mapZoom reads afterwards would
    // record a level never fetched — and then never refetch it.
    final calls = _Calls();
    api = _api(calls);
    final n = ProjectNotifier(ProjectService())
      ..setMapZoom(9)
      ..zoomRefetchDebounce = const Duration(milliseconds: 10);

    final loading = n.load(_ref);
    n.setMapZoom(14); // the camera moves mid-load
    await loading;
    expect(await _waitFor(() => _points(n) == 14), isTrue);
  });

  test('an older server without the endpoint falls back to full resolution',
      () async {
    final calls = _Calls();
    api = _api(calls, lodStatus: 404);
    final n = ProjectNotifier(ProjectService())..setMapZoom(9);

    await n.load(_ref);
    expect(await _waitFor(() => _points(n) > 0), isTrue);

    expect(calls.fullGeo, greaterThanOrEqualTo(1),
        reason: 'a 404 must not leave the map without geometry');
    expect(_points(n), 999);
  });

  // ── Viewport bounding box (issue #324) ───────────────────────────────────
  //
  // Zoom bounds the DETAIL of the geometry, not its EXTENT, so a deep zoom
  // still fetched — and made the server simplify — the whole trip: measured
  // at 4.0 s per request for a 219-activity trip at zoom 15 against 0.26 s at
  // zoom 9. The camera's box is now sent too, and leaving it is a reason to
  // refetch exactly as changing level is.

  group('viewport scoping', () {
    test('the initial load sends no box', () async {
      // The notifier has no camera box until the map emits its first event,
      // and the whole-trip answer is what fit-to-bounds and the whole-trip
      // elevation cursor are built from. This falls out rather than being a
      // special case, and it is asserted so it stays that way.
      final calls = _Calls();
      api = _api(calls);
      final n = ProjectNotifier(ProjectService())..setMapZoom(9);

      await n.load(_ref);
      expect(await _waitFor(() => _points(n) > 0), isTrue);
      expect(calls.boxes, [null]);
    });

    test('a zoom change carries a box that contains the camera', () async {
      // Issue #332. Scoping is back on, and the property that makes it safe is
      // that the box contains the viewport it was built from — see the
      // postcondition tests in geo_viewport_test.dart. Without it,
      // _geoIsStaleForCamera stays true after a successful refetch and every
      // camera event schedules another one: 2.6 GB of Dart heap and an ANR.
      final calls = _Calls();
      api = _api(calls);
      final n = ProjectNotifier(ProjectService())
        ..setMapZoom(9, viewport: _vp)
        ..zoomRefetchDebounce = const Duration(milliseconds: 10);

      await n.load(_ref);
      expect(await _waitFor(() => _points(n) == 9), isTrue);

      n.setMapZoom(15, viewport: _vp);
      expect(await _waitFor(() => _points(n) == 15), isTrue);
      final sent = calls.boxes.where((b) => b != null).toList();
      expect(sent, isNotEmpty, reason: 'the refetch must scope to the camera');
      expect(fetchBoxFor(_vp, 15).contains(_vp), isTrue,
          reason: 'the box sent is one the camera fits inside');
    });

    test('a settled camera stops refetching once its box is loaded', () async {
      // The runaway, from the outside: after a successful scoped refetch the
      // camera is no longer stale, so further camera events at the same place
      // must not go back to the server.
      final calls = _Calls();
      api = _api(calls);
      final n = ProjectNotifier(ProjectService())
        ..setMapZoom(9, viewport: _vp)
        ..zoomRefetchDebounce = const Duration(milliseconds: 10);

      await n.load(_ref);
      expect(await _waitFor(() => _points(n) == 9), isTrue);
      n.setMapZoom(15, viewport: _vp);
      expect(await _waitFor(() => _points(n) == 15), isTrue);
      await Future<void>.delayed(const Duration(milliseconds: 60));
      final settled = calls.zooms.length;

      for (var i = 0; i < 25; i++) {
        n.setMapZoom(15, viewport: _vp);
      }
      await Future<void>.delayed(const Duration(milliseconds: 150));
      expect(calls.zooms, hasLength(settled),
          reason: 'a refetch that satisfied the camera must not repeat');
    });

    test('a camera that never leaves its level refetches once, not forever',
        () async {
      // The runaway, from the outside: with scoping off the geometry is never
      // stale for the camera, so repeated camera events at one level must not
      // keep going back to the server.
      final calls = _Calls();
      api = _api(calls);
      final n = ProjectNotifier(ProjectService())
        ..setMapZoom(9, viewport: _vp)
        ..zoomRefetchDebounce = const Duration(milliseconds: 10);

      await n.load(_ref);
      expect(await _waitFor(() => _points(n) == 9), isTrue);
      final before = calls.zooms.length;

      for (var i = 0; i < 25; i++) {
        n.setMapZoom(9, viewport: _vp);
      }
      await Future<void>.delayed(const Duration(milliseconds: 120));
      expect(calls.zooms, hasLength(before),
          reason: 'nothing about the camera invalidates whole-trip geometry');
    });

    test('only one refetch runs at a time', () async {
      // Each refetch awaits a fetch, a camera-idle wait and _buildFullTrack,
      // and _buildFullTrack copies `geo` into an isolate. Overlapping them is
      // how one stale predicate became gigabytes.
      final calls = _Calls();
      api = _api(calls);
      final n = ProjectNotifier(ProjectService())
        ..setMapZoom(9, viewport: _vp)
        ..zoomRefetchDebounce = const Duration(milliseconds: 1);

      await n.load(_ref);
      expect(await _waitFor(() => _points(n) == 9), isTrue);

      // Walk the zoom up fast: every step is a new bucket, so every step
      // would schedule a refetch if nothing serialised them.
      for (var z = 10; z <= 20; z++) {
        n.setMapZoom(z.toDouble(), viewport: _vp);
        await Future<void>.delayed(const Duration(milliseconds: 2));
      }
      await Future<void>.delayed(const Duration(milliseconds: 200));
      expect(calls.inFlightPeak, lessThanOrEqualTo(1),
          reason: 'refetches must not overlap');
    });
  });

  // ── Offline cache seeding (issue #317) ───────────────────────────────────
  //
  // ProjectService.getGeo is the only thing that ever wrote the full-res row
  // the offline fallback reads, and the level-of-detail path returns before
  // reaching it. So a trip first opened on a device never got one, and a
  // later offline open fell back to low-res straight lines instead of the
  // detailed track it used to show.
  //
  // The seed runs in the background, and the row it writes is a *fallback* —
  // the naive version of this fix put the cached branch ahead of the
  // simplified fetch, which would have meant the first open used LOD and
  // every open after it rendered full resolution again.

  group('offline seeding', () {
    test('the load seeds the offline cache without holding the payload',
        () async {
      final calls = _Calls();
      api = _api(calls);
      final n = ProjectNotifier(ProjectService())..setMapZoom(9);

      await n.load(_ref);
      expect(await _waitFor(() => _points(n) == 9), isTrue);
      expect(await _waitFor(() => calls.fullGeo == 1), isTrue,
          reason: 'a trip with no full-res row on file must get one');

      expect(_points(n), 9, reason: 'the map keeps the simplified geometry');
      expect(await projectDataCache.readFullGeo(_ref), isNull,
          reason: 'the seed goes to disk only — an L1 copy would be the '
              '~180 MB the level of detail exists not to hold, and would '
              'become the answer to every read for the rest of the session');
    });

    test('a second open of a seeded trip still asks for simplified geometry',
        () async {
      // The regression the naive fix would have caused. A full-res row on
      // file is an offline fallback, not a shortcut: with it present the load
      // must still ask the server for the zoom it is showing.
      final calls = _Calls();
      api = _api(calls);
      projectDataCache.onMetaFetched(_ref, {'lock_version': 1, 'name': 'Trip'});
      projectDataCache.writeFullGeo(_ref, _geoWith(999));

      final n = ProjectNotifier(ProjectService())..setMapZoom(9);
      await n.load(_ref);
      expect(await _waitFor(() => _points(n) > 0), isTrue);

      expect(calls.zooms, ['9.0'],
          reason: 'a seeded trip must not skip the simplified endpoint');
      expect(_points(n), 9,
          reason: 'the second open renders the zoom it asked for, not the '
              'full-resolution row on file');
    });

    test('the cached row is what an offline open falls back to', () async {
      // The behaviour the seed exists to restore: the simplified fetch fails
      // (no network), and the detailed track comes off the device rather than
      // the low-res straight lines.
      final calls = _Calls();
      api = _api(calls, lodStatus: 503);
      projectDataCache.onMetaFetched(_ref, {'lock_version': 1, 'name': 'Trip'});
      projectDataCache.writeFullGeo(_ref, _geoWith(999));

      final n = ProjectNotifier(ProjectService())..setMapZoom(9);
      await n.load(_ref);
      expect(await _waitFor(() => _points(n) > 0), isTrue);

      expect(_points(n), 999, reason: 'the detailed track, from the device');
      expect(calls.fullGeo, 0,
          reason: 'offline is exactly when the network cannot answer');
    });

    test('a shared viewer never seeds', () async {
      // SharedProjectNotifier extends ProjectNotifier and does not override
      // the load path, so once a share-scoped simplified endpoint exists
      // (issue #321) shared viewers take the branch above. loadOwnerExtras is
      // what keeps the seed — a multi-MB owner-scoped fetch, for a screen
      // with no offline story — from firing for them. This stands in for it:
      // that getter is the only thing SharedProjectNotifier changes here.
      final calls = _Calls();
      api = _api(calls);
      final n = _ViewerNotifier()..setMapZoom(9);

      await n.load(_ref);
      expect(await _waitFor(() => _points(n) == 9), isTrue);
      await Future<void>.delayed(const Duration(milliseconds: 50));

      expect(calls.fullGeo, 0,
          reason: 'no full-resolution fetch may follow a successful LOD for '
              'a viewer who is not the owner');
    });

    test('a trip past the coordinate ceiling is not seeded', () async {
      final calls = _Calls();
      api = _api(calls);
      perfSpans.enabled = true;
      addTearDown(() => perfSpans.enabled = false);
      final n = ProjectNotifier(ProjectService())
        ..setMapZoom(9)
        ..offlineSeedCoordinateCeiling = 5;

      await n.load(_ref);
      expect(await _waitFor(() => calls.fullGeo == 1), isTrue);
      expect(
          await _waitFor(
              () => perfSpans.notes['geo_offline_seed'] != null),
          isTrue);

      expect(perfSpans.notes['geo_offline_seed'], 'skipped, 999 coords',
          reason: 'a trip whose geometry is too large to expand safely keeps '
              'the low-res offline map it has today');
    });

    test('a trip under the ceiling records the seed', () async {
      final calls = _Calls();
      api = _api(calls);
      perfSpans.enabled = true;
      addTearDown(() => perfSpans.enabled = false);
      final n = ProjectNotifier(ProjectService())..setMapZoom(9);

      await n.load(_ref);
      expect(
          await _waitFor(
              () => perfSpans.notes['geo_offline_seed'] != null),
          isTrue);
      expect(perfSpans.notes['geo_offline_seed'], '999 coords');
    });
  });

  // ── Full-resolution geometry for exports (issue #317) ────────────────────
  //
  // image_export.dart used to read notifier.geo, which is simplified for the
  // zoom the map is showing. An export fits its own camera — a day-scoped one
  // much tighter than the whole trip — so tracks rendered visibly angular.

  group('geometry for exports', () {
    test('an export gets full resolution, not the map\'s zoom level',
        () async {
      final calls = _Calls();
      api = _api(calls);
      final n = ProjectNotifier(ProjectService())..setMapZoom(9);

      await n.load(_ref);
      expect(await _waitFor(() => _points(n) == 9), isTrue);

      final exportGeo = await n.fullResGeoForExport();
      expect((exportGeo!['features'] as List).first['geometry']['coordinates'],
          hasLength(999));
      expect(_points(n), 9,
          reason: 'fetching for the export must not swap what the map holds');
    });

    test('geometry that is already full resolution is not refetched',
        () async {
      // The offline and older-server fallbacks apply the full payload, and an
      // E2EE trip builds it client-side; there is nothing to upgrade.
      final calls = _Calls();
      api = _api(calls, lodStatus: 503);
      final n = ProjectNotifier(ProjectService())..setMapZoom(9);

      await n.load(_ref);
      expect(await _waitFor(() => _points(n) == 999), isTrue);
      final before = calls.fullGeo;

      expect(await n.fullResGeoForExport(), same(n.geo));
      expect(calls.fullGeo, before, reason: 'no second request');
    });

    test('a failed fetch falls back to what is on screen', () async {
      // An angular export beats no export.
      final calls = _Calls();
      api = _api(calls);
      final n = ProjectNotifier(ProjectService())..setMapZoom(9);

      await n.load(_ref);
      expect(await _waitFor(() => _points(n) == 9), isTrue);
      calls.failFullGeo = true;

      expect(await n.fullResGeoForExport(), same(n.geo));
    });
  });
}

/// Stands in for `SharedProjectNotifier`, which differs from
/// `ProjectNotifier` in exactly this getter as far as the geo load path is
/// concerned — it does not override `_loadFullGeoProgressively`.
class _ViewerNotifier extends ProjectNotifier {
  _ViewerNotifier() : super(ProjectService());

  @override
  bool get loadOwnerExtras => false;
}
