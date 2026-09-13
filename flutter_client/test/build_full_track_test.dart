// buildFullTrackResult — the elevation-indexed full-track computation
// ProjectNotifier._buildFullTrack runs via `compute()` on a background
// isolate when the raw elevation_profile point count is large. See
// project_notifier.dart's doc comment on buildFullTrackResult for why: this
// used to run synchronously on the UI isolate with NO size threshold at all
// (issue #276), unlike every other per-trip-size computation in this
// codebase (computeElevationSpots, hitTestMapTap, decimatePolylinePoints).
// This only exercises the pure computation itself, same scope as
// compute_elevation_spots_test.dart / decimate_polyline_points_test.dart.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';

void main() {
  Map<String, dynamic> activity(
    dynamic id,
    List<List<num>>? profile,
  ) =>
      {
        'id': id,
        if (profile != null) 'elevation_profile': profile,
      };

  Map<String, dynamic> geoFeature(dynamic activityId, List<List<num>> coords) => {
        'type': 'Feature',
        'properties': {'type': 'activity', 'activity_id': activityId},
        'geometry': {'type': 'LineString', 'coordinates': coords},
      };

  Map<String, dynamic> geo(List<Map<String, dynamic>> features) =>
      {'type': 'FeatureCollection', 'features': features};

  group('totalElevationProfilePoints', () {
    test('sums elevation_profile length across activities', () {
      expect(
        totalElevationProfilePoints([
          activity(1, [
            [0.0, 100],
            [1.0, 110]
          ]),
          activity(2, [
            [0.0, 50]
          ]),
        ]),
        3,
      );
    });

    test('an activity with no elevation_profile contributes zero', () {
      expect(totalElevationProfilePoints([activity(1, null)]), 0);
    });

    test('empty activities is zero', () {
      expect(totalElevationProfilePoints(const []), 0);
    });
  });

  group('buildFullTrackResult', () {
    test('fast path: coords.length >= profile.length pairs each profile '
        'sample with the coordinate at the same index', () {
      final result = buildFullTrackResult((
        geo: geo([
          geoFeature(1, [
            [7.0, 45.0], // [lon, lat]
            [7.1, 45.1],
          ]),
        ]),
        activities: [
          activity(1, [
            [0.0, 100],
            [5.0, 200],
          ]),
        ],
      ));

      expect(result.fullTrack, [
        (0.0, (lat: 45.0, lon: 7.0)),
        (5.0, (lat: 45.1, lon: 7.1)),
      ]);
      expect(result.perActivityTracks['1'], result.fullTrack);
    });

    test('haversine fallback: fewer coords than profile samples still spans '
        '0..elevTotalKm using the geometry between the coordinates', () {
      final result = buildFullTrackResult((
        geo: geo([
          geoFeature(1, [
            [7.0, 45.0],
            [7.0, 45.1],
          ]),
        ]),
        activities: [
          activity(1, [
            [0.0, 100],
            [5.0, 150],
            [10.0, 200], // elevTotalKm = 10.0 (last sample's distance)
          ]),
        ],
      ));

      // buildTrackFromPolyline rescales the haversine distance between the 2
      // coordinates so the fallback track's endpoints land exactly on 0 and
      // elevTotalKm, regardless of the true geographic distance.
      final track = result.perActivityTracks['1']!;
      expect(track.length, 2);
      expect(track.first.$1, 0.0);
      expect(track.first.$2, (lat: 45.0, lon: 7.0));
      expect(track.last.$1, closeTo(10.0, 1e-9));
      expect(track.last.$2, (lat: 45.1, lon: 7.0));
    });

    test('multi-activity: each activity\'s fullTrack distances are offset by '
        'the running total of every prior activity\'s elevTotalKm', () {
      final result = buildFullTrackResult((
        geo: geo([
          geoFeature(1, [
            [7.0, 45.0],
            [7.1, 45.1],
          ]),
          geoFeature(2, [
            [8.0, 46.0],
            [8.1, 46.1],
          ]),
        ]),
        activities: [
          activity(1, [
            [0.0, 100],
            [5.0, 200], // elevTotalKm = 5.0
          ]),
          activity(2, [
            [0.0, 300],
            [3.0, 400],
          ]),
        ],
      ));

      expect(result.fullTrack, [
        (0.0, (lat: 45.0, lon: 7.0)),
        (5.0, (lat: 45.1, lon: 7.1)),
        (5.0, (lat: 46.0, lon: 8.0)), // second activity's 0.0 + first's 5.0 total
        (8.0, (lat: 46.1, lon: 8.1)),
      ]);
      // Per-activity tracks stay 0-based — not offset like the combined track.
      expect(result.perActivityTracks['2'], [
        (0.0, (lat: 46.0, lon: 8.0)),
        (3.0, (lat: 46.1, lon: 8.1)),
      ]);
    });

    test('an activity with no elevation_profile contributes nothing and does '
        'not offset the activities after it', () {
      final result = buildFullTrackResult((
        geo: geo([
          geoFeature(1, [
            [7.0, 45.0],
            [7.1, 45.1],
          ]),
          geoFeature(2, [
            [8.0, 46.0],
            [8.1, 46.1],
          ]),
        ]),
        activities: [
          activity(1, null), // no elevation_profile at all
          activity(2, [
            [0.0, 300],
            [3.0, 400],
          ]),
        ],
      ));

      expect(result.perActivityTracks.containsKey('1'), isFalse);
      expect(result.fullTrack, [
        (0.0, (lat: 46.0, lon: 8.0)), // not offset by the skipped activity
        (3.0, (lat: 46.1, lon: 8.1)),
      ]);
    });

    test('an activity with an empty elevation_profile list is likewise '
        'skipped', () {
      final result = buildFullTrackResult((
        geo: geo([geoFeature(1, [
              [7.0, 45.0],
              [7.1, 45.1],
            ])]),
        activities: [activity(1, [])],
      ));

      expect(result.fullTrack, isEmpty);
      expect(result.perActivityTracks, isEmpty);
    });

    test('empty geo/activities yields empty results', () {
      final result = buildFullTrackResult((geo: null, activities: const []));
      expect(result.fullTrack, isEmpty);
      expect(result.perActivityTracks, isEmpty);
    });
  });
}
