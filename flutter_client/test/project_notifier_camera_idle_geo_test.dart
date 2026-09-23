// Regression test for a pan-jerkiness/ANR finding on the main trip map:
// ProjectNotifier._loadFullGeoProgressively() used to call notifyListeners()
// unconditionally on every full-res geo batch/final-pass update, regardless
// of what the map was doing. MapPanel subscribes to the notifier via its own
// Consumer, so a batch landing mid-drag forced a full rebuild of every
// polyline and marker on top of the pan gesture's own per-frame work — a
// large trip's rebuild cost stacked onto active panning was enough to ANR.
// Fixed by having the map screens report camera activity via
// ProjectNotifier.setMapCameraActive(); the progressive-geo apply now waits
// for the camera to go idle (capped at 2s so a user who never stops panning
// still eventually gets the full-res geo) before committing each update.

import 'dart:convert';

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

Map<String, dynamic> _fullGeo() => {
      'type': 'FeatureCollection',
      'features': [
        {
          'type': 'Feature',
          'properties': {'type': 'activity', 'activity_id': '1'},
          'geometry': {
            'type': 'LineString',
            'coordinates': [
              [7.0, 45.0],
              [7.01, 45.01],
            ],
          },
        },
      ],
    };

ApiClient _mockedApi() => ApiClient(
    baseUrl: '',
    httpClient: MockClient((req) async {
      final path = req.url.path;
      if (path == '/api/projects/Trip/meta') {
        return _json({
          'name': 'Trip',
          'activities': [
            {'id': '1'}
          ],
          'items': [
            {'item_type': 'activity', 'activity_id': '1'}
          ],
          'people': [],
          'groups': [],
        });
      }
      if (path == '/api/geo/project/low-res') {
        return _json({'type': 'FeatureCollection', 'features': []});
      }
      if (path == '/api/geo/project') {
        return _json(_fullGeo());
      }
      return _json({});
    }));

int _geoFeatureCount(ProjectNotifier n) =>
    (n.geo?['features'] as List?)?.length ?? 0;

void main() {
  setUp(() => projectDataCache.resetForTest());

  test(
      'the full-res geo upgrade waits for the camera to go idle before '
      'committing, instead of landing mid-pan', () async {
    api = _mockedApi();
    final notifier = ProjectNotifier(ProjectService());

    notifier.setMapCameraActive(true);
    await notifier.load(_ref);

    // Phase 1 (low-res, 0 features per the mock) is in; Phase 2 (full-res,
    // 1 feature) is backgrounded and must stay blocked on the camera.
    expect(_geoFeatureCount(notifier), 0);
    await Future<void>.delayed(const Duration(milliseconds: 300));
    expect(_geoFeatureCount(notifier), 0,
        reason: 'the full-res upgrade must not commit while the camera is '
            'still reported as actively moving');

    notifier.setMapCameraActive(false);
    for (var i = 0; i < 20 && _geoFeatureCount(notifier) == 0; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 50));
    }

    expect(_geoFeatureCount(notifier), 1,
        reason: 'once the camera goes idle the deferred upgrade still lands');
  });

  test(
      'a camera that is never marked active does not block the upgrade',
      () async {
    api = _mockedApi();
    final notifier = ProjectNotifier(ProjectService());

    await notifier.load(_ref);
    for (var i = 0; i < 20 && _geoFeatureCount(notifier) == 0; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 50));
    }

    expect(_geoFeatureCount(notifier), 1);
  });
}
