// Regression test for issue #276: the ANR fix in
// project_notifier_camera_idle_geo_test.dart covered the full-res GEO
// upgrade landing mid-pan, but a SIBLING background load — the elevation-data
// upgrade (ProjectNotifier._loadElevationData / applyFullActivities, which
// rebuild fullTrack/perActivityTracks via _buildFullTrack) — still called an
// ungated notifyListeners() regardless of what the map camera was doing.
// Fixed the same way as the geo upgrade: _loadElevationData now awaits
// ProjectNotifier._waitForCameraIdle() before committing/notifying.
//
// Since #295 that upgrade fetches the compact /elevation endpoint rather than
// the 33 MB details payload, so the mock serves it here — the gate being
// tested is the same one either way.

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

Map<String, dynamic> _fullDetails() => {
      'name': 'Trip',
      'activities': [
        {
          'id': '1',
          'elevation_profile': [
            [0.0, 100],
            [1.0, 110],
          ],
        }
      ],
      'items': [
        {'item_type': 'activity', 'activity_id': '1'}
      ],
      'people': [],
      'groups': [],
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
      if (path == '/api/projects/Trip/elevation') {
        // The compact endpoint the elevation upgrade now uses (issue #295).
        // encode_profile_pairs([[0.0, 100.0], [1.0, 110.0]])
        return _json({
          'profiles': {'1': r'?o}@o}@gE'},
          'encrypted': <String, dynamic>{},
        });
      }
      if (path == '/api/projects/Trip') {
        return _json(_fullDetails());
      }
      return _json({});
    }));

void main() {
  setUp(() => projectDataCache.resetForTest());

  test(
      'the elevation-data upgrade waits for the camera to go idle before '
      'landing fullTrack/perActivityTracks, instead of committing mid-pan',
      () async {
    api = _mockedApi();
    final notifier = ProjectNotifier(ProjectService());

    notifier.setMapCameraActive(true);
    await notifier.load(_ref);

    // Phase 1 (meta, no elevation_profile) is in; phase 2 (full-res geo, then
    // the full elevation-carrying details fetch) is backgrounded and must
    // stay blocked on the camera the whole time.
    expect(notifier.fullTrack, isEmpty);
    await Future<void>.delayed(const Duration(milliseconds: 300));
    expect(notifier.fullTrack, isEmpty,
        reason: 'the elevation-data upgrade must not commit fullTrack while '
            'the camera is still reported as actively moving');

    notifier.setMapCameraActive(false);
    for (var i = 0; i < 20 && notifier.fullTrack.isEmpty; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 50));
    }

    expect(notifier.fullTrack, isNotEmpty,
        reason: 'once the camera goes idle the deferred elevation upgrade '
            'still lands');
    expect(notifier.perActivityTracks.containsKey('1'), isTrue);
  });

  test('a camera that is never marked active does not block the upgrade',
      () async {
    api = _mockedApi();
    final notifier = ProjectNotifier(ProjectService());

    await notifier.load(_ref);
    for (var i = 0; i < 20 && notifier.fullTrack.isEmpty; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 50));
    }

    expect(notifier.fullTrack, isNotEmpty);
  });
}
