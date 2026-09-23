// Issue #317, second half: an export must not borrow the map's geometry.
//
// image_export.dart read `notifier.geo`, which since issue #295 is simplified
// for the zoom the map is showing. A day-scoped export fits a far tighter
// camera than that zoom was simplified for, so its track rendered visibly
// angular — and the day's *bounds* were computed from the same coarse
// geometry (share_asset_source_impl.dart, the social share of issue #15).
//
// The export path now fetches full resolution itself. Two things have to hold
// for that to be safe: the day's points come from the detailed track, and a
// shared viewer's fetch goes to the share endpoint rather than the
// owner-scoped one, which would 401.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/share/share_day_bounds.dart';
import 'package:traxjourney_client/src/shared/shared_project_screen.dart';

const _ref = ProjectRef(name: 'Trip');
const _day = '2026-06-01';

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
              for (var i = 0; i < points; i++)
                [7.0 + i * 0.001, 45.0 + i * 0.001]
            ],
          },
        },
      ],
    };

Map<String, dynamic> _meta() => {
      'name': 'Trip',
      'lock_version': 1,
      'activities': [
        {'id': '1', 'start_date_local': '${_day}T08:00:00'}
      ],
      'items': [
        {'item_type': 'activity', 'activity_id': '1'}
      ],
      'people': <dynamic>[],
      'groups': <dynamic>[],
    };

void main() {
  setUp(() => projectDataCache.resetForTest());

  test('a day-scoped export frames the full-resolution track', () async {
    final paths = <String>[];
    api = ApiClient(
      baseUrl: '',
      httpClient: MockClient((req) async {
        paths.add(req.url.path);
        switch (req.url.path) {
          case '/api/projects/Trip/meta':
            return _json(_meta());
          case '/api/geo/project/low-res':
            return _json({'type': 'FeatureCollection', 'features': <dynamic>[]});
          case '/api/geo/project/simplified':
            return _json(_geoWith(4));
          case '/api/geo/project':
            return _json(_geoWith(400));
        }
        return _json({});
      }),
    );

    final n = ProjectNotifier(ProjectService())..setMapZoom(9);
    await n.load(_ref);
    while ((n.geo?['features'] as List?)?.isNotEmpty != true) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }

    // What the share card does: resolve the day's points, then hand the same
    // geometry to the exporter.
    final onScreen = dayRoutePoints(
      geo: n.geo,
      items: n.items,
      activities: n.activities,
      date: _day,
    );
    final forExport = dayRoutePoints(
      geo: await n.fullResGeoForExport(),
      items: n.items,
      activities: n.activities,
      date: _day,
    );

    expect(onScreen, hasLength(4),
        reason: 'the map holds geometry simplified for its zoom');
    expect(forExport, hasLength(400),
        reason: 'the export frames — and draws — the detailed track');
  });

  test('a shared viewer\'s export fetch is share-scoped', () async {
    // SharedProjectNotifier inherits fullResGeoForExport. Without an override
    // of the fetch it uses, a shared viewer sharing a trip card would call
    // the owner-scoped /api/geo/project and get a 401.
    final paths = <String>[];
    api = ApiClient(
      baseUrl: '',
      httpClient: MockClient((req) async {
        paths.add(req.url.path);
        return _json({'type': 'FeatureCollection', 'features': <dynamic>[]});
      }),
    );

    final n = SharedProjectNotifier('tok123');
    await n.service.fetchFullGeoUncached(const ProjectRef(name: 'tok123'));

    expect(paths, ['/api/share/tok123/geo']);
    expect(paths.any((p) => p.startsWith('/api/geo/project')), isFalse,
        reason: 'the owner-scoped geo endpoint 401s for a share viewer');
  });
}
