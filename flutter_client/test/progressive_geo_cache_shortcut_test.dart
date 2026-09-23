// Regression test for the Android view/manage mode-toggle ANR.
//
// _loadFullGeoProgressively() used to unconditionally re-fetch and re-apply
// the full-res geo on every load(), even when it was already sitting in
// projectDataCache from the mode just left. Toggling between view and manage
// mode re-ran that whole path every single time, even though the data was
// already complete in memory. On a large trip that was enough back-to-back
// full-map rebuild work to trip Android's ANR watchdog.
//
// The fix: when projectDataCache already has full geo for the ref, apply it
// directly instead of going back to the network. This test asserts getGeo()
// is never called when the cache is already warm.
//
// (The batched low-res -> full-res reveal this originally guarded against
// replaying was deleted outright in issue #293 — see
// _loadFullGeoProgressively. The warm-cache shortcut it pins is independent
// of that and still load-bearing.)
//
// Re-derived for issue #317, which moved that branch. It used to run *ahead*
// of the zoom level-of-detail fetch; now the fetch goes first and the cache
// is the fallback behind it. What the branch protects is unchanged and is
// still asserted below: a load that cannot get geometry from the server must
// apply the cached payload in one shot, and must never re-fetch the multi-MB
// full-resolution payload for data the device already holds. What is
// deliberately different is the second test: with the server reachable, the
// warm cache must NOT win, because taking it would put full-resolution
// geometry back in the heap on every open after the first — the ~180 MB the
// level of detail exists to avoid.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/geo_viewport.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

Map<String, dynamic> _details() => {
      'name': 'Trip',
      'lock_version': 1,
      'activities': [
        {'id': 111, 'name': 'Ride', 'type': 'Ride'},
      ],
      'items': [
        {'item_type': 'activity', 'activity_id': 111},
      ],
    };

Map<String, dynamic> _geo(String activityId) => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'properties': {'activity_id': activityId},
          'geometry': {
            'type': 'LineString',
            'coordinates': [
              [1.0, 2.0],
              [1.1, 2.1],
            ],
          },
        },
      ],
    };

Map<String, dynamic> _fullGeo() => _geo('111');

/// [lodAvailable] false stands in for the two cases the cached branch exists
/// for: no network, and a server too old to have the simplified endpoint.
class _CountingService extends ProjectService {
  _CountingService({this.lodAvailable = false});

  final bool lodAvailable;
  int getGeoCalls = 0;
  int simplifiedCalls = 0;
  int seedCalls = 0;

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async =>
      {'lock_version': 1, 'name': 'Trip'};

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async =>
      _details();

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async {
    getGeoCalls++;
    return _fullGeo();
  }

  @override
  Future<Map<String, dynamic>> getSimplifiedGeo(ProjectRef ref, double zoom,
      {GeoBox? bbox}) async {
    simplifiedCalls++;
    if (!lodAvailable) throw StateError('offline');
    // A different activity_id, so a test can tell which payload was applied.
    return _geo('222');
  }

  /// The offline seed's fetch (issue #317). Counted rather than left to the
  /// base class, which would go to the network.
  @override
  Future<Map<String, dynamic>> fetchFullGeoUncached(ProjectRef ref) async {
    seedCalls++;
    return _fullGeo();
  }
}

void main() {
  setUp(() => projectDataCache.resetForTest());

  test(
      'a mode toggle with a warm full-geo cache applies it in one shot '
      'instead of replaying the batched network-reveal path', () async {
    // Seed the cache exactly as the mode just left would have: a live /meta
    // establishing lock_version 1, then a full geo fetch.
    projectDataCache.onMetaFetched(_ref, {'lock_version': 1, 'name': 'Trip'});
    projectDataCache.writeFullGeo(_ref, _fullGeo());

    final service = _CountingService();
    final notifier = ProjectNotifier(service);

    await notifier.load(_ref);
    // _loadFullGeoProgressively runs unawaited in the background; give it a
    // moment to finish.
    await Future<void>.delayed(const Duration(milliseconds: 30));

    expect(service.getGeoCalls, 0,
        reason: 'a warm cache must never fall through to the network path '
            '(_loadFullGeoProgressively is the only caller of getGeo())');
    expect(service.seedCalls, 0,
        reason: 'nothing to seed: the device already has the full payload, '
            'and the seed only follows a successful simplified fetch');
    expect(notifier.isGeoLoaded, isTrue);
    expect(notifier.geo?['features'], hasLength(1));
    expect((notifier.geo?['features'] as List).first['properties']['activity_id'], '111');
  });

  test('a warm full-geo cache is a fallback, not a shortcut past the load',
      () async {
    // The other half of the same ordering. Applying the cached full payload
    // when the server can answer would mean the first open of a trip used the
    // level of detail and every open after it rendered full resolution — the
    // heap the LOD path removed, back on every repeat visit.
    projectDataCache.onMetaFetched(_ref, {'lock_version': 1, 'name': 'Trip'});
    projectDataCache.writeFullGeo(_ref, _fullGeo());

    final service = _CountingService(lodAvailable: true);
    final notifier = ProjectNotifier(service);

    await notifier.load(_ref);
    await Future<void>.delayed(const Duration(milliseconds: 30));

    expect(service.simplifiedCalls, 1,
        reason: 'the load asks for the zoom on screen even when a '
            'full-resolution row is on file');
    expect((notifier.geo?['features'] as List).first['properties']['activity_id'], '222',
        reason: 'the simplified payload is what gets rendered');
    expect(service.getGeoCalls, 0,
        reason: 'the cached branch is skipped, not replaced by a fetch');
  });
}
