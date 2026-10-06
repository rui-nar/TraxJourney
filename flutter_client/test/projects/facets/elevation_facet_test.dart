// The elevation facet holds the full track, the per-activity tracks and the
// totals (issue #294, Decisions 17 and 19 of docs/CLIENT_STATE_MAP_PLAN.md):
// a rebuild bumps only its version, a dropped rebuild still notifies only when
// the caller does, and clear() puts it all back.

import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

class _Service extends ProjectService {
  Map<String, dynamic> _payload() => {
        'name': 'Trip',
        'lock_version': 1,
        'activities': <dynamic>[
          {
            'id': '1',
            'distance': 1500.0,
            'moving_time': 600,
            'total_elevation_gain': 40.0,
            'elevation_profile': [
              [0.0, 100],
              [1.0, 110],
            ],
          },
        ],
        'items': <dynamic>[
          {'item_type': 'activity', 'activity_id': '1'},
        ],
        'people': <dynamic>[],
        'groups': <dynamic>[],
      };

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async =>
      _payload();

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _payload();

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async => _geo();

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _geo();

  Map<String, dynamic> _geo() => {
        'type': 'FeatureCollection',
        'features': [
          {
            'type': 'Feature',
            'properties': {'type': 'activity', 'activity_id': '1'},
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                [2.0, 48.0],
                [2.01, 48.01],
                [2.02, 48.02],
              ],
            },
          },
        ],
      };
}

Future<ProjectNotifier> _loaded() async {
  final n = ProjectNotifier(_Service())
    ..loadRetryBackoff = const [Duration(milliseconds: 1)];
  await n.load(_ref);
  for (var i = 0; i < 40 && n.elevationFacet.fullTrack.isEmpty; i++) {
    await pumpEventQueue();
  }
  // Let the load's later phases finish, so none is left to write mid-test.
  for (var i = 0; i < 20; i++) {
    await pumpEventQueue();
  }
  return n;
}

void main() {
  test('a rebuild bumps only the elevation version', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    expect(n.elevationFacet.fullTrack, isNotEmpty);

    final elevation = n.elevationFacet.version;
    final geo = n.geoFacet.version;
    final selection = n.selectionFacet.version;
    final style = n.styleFacet.version;
    final items = n.itemsFacet.version;

    await n.buildFullTrack();

    expect(n.elevationFacet.version, greaterThan(elevation));
    expect(n.geoFacet.version, geo);
    expect(n.selectionFacet.version, selection);
    expect(n.styleFacet.version, style);
    expect(n.itemsFacet.version, items);
  });

  test('a rebuild notifies the facet only when the caller notifies', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    n.notifyListeners(); // flush what the load left marked
    var heard = 0;
    n.elevationFacet.addListener(() => heard++);

    await n.buildFullTrack();
    expect(heard, 0, reason: 'a facet write never notifies by itself');

    n.notifyListeners();
    expect(heard, 1);
    n.notifyListeners();
    expect(heard, 1, reason: 'nothing changed since the last flush');
  });

  test('load fills the tracks and the totals; clear() puts them back',
      () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    final e = n.elevationFacet;
    expect(e.fullTrack, isNotEmpty);
    expect(e.perActivityTracks.containsKey('1'), isTrue);
    expect(e.totalDistanceM, 1500.0);
    expect(e.totalMovingSeconds, 600);
    expect(e.totalElevationGainM, 40.0);

    n.clear();

    expect(e.fullTrack, isEmpty);
    expect(e.perActivityTracks, isEmpty);
    expect(e.totalDistanceM, 0);
    expect(e.totalMovingSeconds, 0);
    expect(e.totalElevationGainM, 0);
  });
}
