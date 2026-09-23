// Regression test for a subclass-override bug found by a load-performance
// audit: ProjectNotifier._loadElevationData() calls _service.getDetails(ref),
// which the base ProjectService contract means "fetch the full payload with
// elevation_profile". _ViewProjectService and _SharedProjectService used to
// override getDetails() to instead return the lightweight /meta response (for
// a purpose that, by the time of this fix, no longer existed — see
// view_screen.dart/shared_project_screen.dart) — so Dart's virtual dispatch
// meant _loadElevationData() silently got meta-only data in View/Shared mode,
// firing a third, redundant /meta-shaped request that raced the screen's own
// deliberate full-details fetch.
//
// Fixed by no longer overriding getDetails() at all in _ViewProjectService
// (its override was vestigial dead code) and by _SharedProjectService
// overriding getDetails() to mean the real full payload (delegating to its
// existing fetchFullDetails()) while moving the meta-only behaviour to a
// correct override of getDetailsMeta() — which also fixes a separate bug this
// investigation surfaced: getDetailsMeta() was never overridden for shared
// mode at all, so load()'s Phase 1 built /api/projects/{token}/meta (an
// authenticated, owner-scoped endpoint) instead of /api/share/{token}/meta,
// and failed on every shared-project load.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/view_screen.dart';
import 'package:traxjourney_client/src/shared/shared_project_screen.dart';

http.Response _json(Map<String, dynamic> body) =>
    http.Response(jsonEncode(body), 200);

void main() {
  setUp(() => projectDataCache.resetForTest());

  test(
      'ViewProjectNotifier: the background elevation-data fetch hits the '
      'elevation endpoint, not a second /meta call', () async {
    final paths = <String>[];
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          paths.add(req.url.path);
          if (req.url.path == '/api/projects/Trip/meta') {
            // No auto-sync/active-trip background check side effects.
            return _json({'name': 'Trip', 'activities': [], 'items': [],
              'people': [], 'groups': []});
          }
          if (req.url.path == '/api/projects/Trip/sync-meta') {
            return _json({'auto_sync_enabled': false});
          }
          if (req.url.path == '/api/geo/project/low-res' ||
              req.url.path == '/api/geo/project') {
            return _json({'type': 'FeatureCollection', 'features': []});
          }
          if (req.url.path == '/api/projects/Trip/elevation') {
            // Since issue #295 this is where elevation comes from — 2.8 MB
            // rather than the 33 MB details payload. encode_profile_pairs(
            // [[0.0, 10.0], [1.0, 12.0]])
            return _json({
              'profiles': {'1': r'?gEo}@g@'},
              'encrypted': <String, dynamic>{},
            });
          }
          if (req.url.path == '/api/projects/Trip') {
            return _json({
              'name': 'Trip',
              'activities': [
                {'id': '1', 'elevation_profile': [[0.0, 10.0]]},
              ],
              'items': [],
            });
          }
          return _json({});
        }));

    final notifier = ViewProjectNotifier();
    await notifier.loadView(const ProjectRef(name: 'Trip'));
    // Let the background _loadFullGeoProgressively().whenComplete(
    // _loadElevationData) chain (fired unawaited from load()) settle.
    await Future<void>.delayed(const Duration(milliseconds: 100));

    final metaCalls = paths.where((p) => p == '/api/projects/Trip/meta').length;
    expect(metaCalls, 1,
        reason: 'Phase 1 is the only call that should ever hit /meta — '
            '_loadElevationData() must not silently get redirected there '
            'by a getDetails() override');

    expect(paths.where((p) => p == '/api/projects/Trip/elevation').length, 1,
        reason: 'elevation must come from its own endpoint');
    // And exactly once: view mode used to run its own Phase 2 details fetch
    // *in addition* to the inherited upgrade, so a 180-day trip fetched
    // elevation twice — once compactly and once as 33 MB (issue #295).
    expect(paths.where((p) => p == '/api/projects/Trip').length, 0,
        reason: 'the 33 MB details payload must not be fetched for elevation');
  });

  test(
      'SharedProjectNotifier: the background elevation-data fetch hits the '
      'full-details share endpoint, not a second /meta call — and Phase 1 '
      'now reaches the share /meta endpoint at all', () async {
    final paths = <String>[];
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          paths.add(req.url.path);
          if (req.url.path == '/api/share/tok123/meta') {
            return _json({'name': 'Trip', 'activities': [], 'items': [],
              'people': [], 'groups': [], 'owner_name': 'Alice'});
          }
          if (req.url.path == '/api/share/tok123/geo/low-res' ||
              req.url.path == '/api/share/tok123/geo') {
            return _json({'type': 'FeatureCollection', 'features': []});
          }
          if (req.url.path == '/api/share/tok123') {
            return _json({
              'name': 'Trip',
              'owner_name': 'Alice',
              'activities': [
                {'id': '1', 'elevation_profile': [[0.0, 10.0]]},
              ],
              'items': [],
            });
          }
          return _json({});
        }));

    final notifier = SharedProjectNotifier('tok123');
    await notifier.loadShared();
    await Future<void>.delayed(const Duration(milliseconds: 100));

    // Regression guard for the separate bug this investigation surfaced:
    // before the fix, getDetailsMeta() was never overridden for shared mode,
    // so Phase 1 built /api/projects/tok123/meta instead and every
    // shared-project load failed outright.
    expect(paths.any((p) => p == '/api/projects/tok123/meta'), isFalse,
        reason: 'shared mode must never hit the authenticated /api/projects '
            'endpoint');

    final metaCalls =
        paths.where((p) => p == '/api/share/tok123/meta').length;
    expect(metaCalls, 1,
        reason: '_loadElevationData() must not silently re-hit /meta');

    final fullCalls = paths.where((p) => p == '/api/share/tok123').length;
    expect(fullCalls, greaterThanOrEqualTo(1),
        reason: 'the full-details share endpoint must actually be reached');

    expect(notifier.error, isNull);
  });
}
