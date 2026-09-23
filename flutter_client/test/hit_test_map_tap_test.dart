// hitTestMapTap / totalMapTapPoints — the map-tap hit-test map_panel.dart
// runs via `compute()` on a background isolate once a trip has enough
// points to make it worth it. Every tap on the map (not just a marker tap)
// used to run this synchronously on the UI isolate: a full scan of every
// activity/segment coordinate, and — if nothing was hit — a second full
// scan of the whole elevation track. On a large trip that's the same
// "big computation on the UI isolate" mistake the day-carousel ANR fix
// addressed elsewhere in this file (see buildDayIndex's doc comment), just
// triggered by every tap instead of every selection. This only exercises
// the pure computation itself, same scope as build_day_index_test.dart and
// compute_elevation_spots_test.dart.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/map/geo_point.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';

void main() {
  Map<String, dynamic> feature(String type, dynamic id, List<List<double>> coords) => {
        'properties': {
          'type': type,
          if (type == 'activity') 'activity_id': id,
          if (type == 'segment') 'segment_id': id,
        },
        'geometry': {'coordinates': coords},
      };

  Map<String, dynamic> geoOf(List<Map<String, dynamic>> features) =>
      {'type': 'FeatureCollection', 'features': features};

  const threshold = 0.0001; // squared-degrees

  test('hits an activity feature within threshold and returns its id', () {
    final geo = geoOf([
      feature('activity', 42, [
        [11.0, 47.0],
        [11.001, 47.001],
      ]),
    ]);
    final result = hitTestMapTap((
      geo: geo,
      tapLat: 47.0,
      tapLon: 11.0,
      thresholdSq: threshold,
      track: const <(double, GeoPoint)>[],
    ));
    expect(result.hitActivityId, 42);
    expect(result.hitSegmentId, isNull);
    expect(result.cursorPoint, isNull);
  });

  test('hits a segment feature within threshold and returns its id', () {
    final geo = geoOf([
      feature('segment', 7, [
        [11.0, 47.0],
      ]),
    ]);
    final result = hitTestMapTap((
      geo: geo,
      tapLat: 47.0,
      tapLon: 11.0,
      thresholdSq: threshold,
      track: const <(double, GeoPoint)>[],
    ));
    expect(result.hitSegmentId, '7');
    expect(result.hitActivityId, isNull);
  });

  test('prefers the nearest candidate among several within threshold', () {
    final geo = geoOf([
      feature('activity', 'far', [
        [11.0002, 47.0002], // farther
      ]),
      feature('activity', 'near', [
        [11.00005, 47.00005], // closer
      ]),
    ]);
    final result = hitTestMapTap((
      geo: geo,
      tapLat: 47.0,
      tapLon: 11.0,
      thresholdSq: threshold,
      track: const <(double, GeoPoint)>[],
    ));
    expect(result.hitActivityId, 'near');
  });

  test('a tap outside threshold and outside geo entirely falls back to the '
      'nearest track point for the elevation cursor', () {
    final geo = geoOf([
      feature('activity', 1, [
        [50.0, 50.0], // nowhere near the tap
      ]),
    ]);
    final track = <(double, GeoPoint)>[
      (0.0, (lat: 47.0, lon: 11.0)),
      (1.0, (lat: 48.0, lon: 12.0)), // nearest to the tap below
      (2.0, (lat: 60.0, lon: 20.0)),
    ];
    final result = hitTestMapTap((
      geo: geo,
      tapLat: 48.0,
      tapLon: 12.0,
      thresholdSq: threshold,
      track: track,
    ));
    expect(result.hitActivityId, isNull);
    expect(result.hitSegmentId, isNull);
    expect(result.cursorPoint, (lat: 48.0, lon: 12.0));
    expect(result.cursorDist, 1.0);
  });

  test('null geo and empty track yields no hit and no cursor', () {
    final result = hitTestMapTap((
      geo: null,
      tapLat: 47.0,
      tapLon: 11.0,
      thresholdSq: threshold,
      track: const <(double, GeoPoint)>[],
    ));
    expect(result.hitActivityId, isNull);
    expect(result.hitSegmentId, isNull);
    expect(result.cursorPoint, isNull);
  });

  test('non-activity/segment features (e.g. already-filtered) are ignored', () {
    final geo = geoOf([
      {
        'properties': {'type': 'other'},
        'geometry': {
          'coordinates': [
            [11.0, 47.0],
          ],
        },
      },
    ]);
    final track = <(double, GeoPoint)>[(0.0, (lat: 47.0, lon: 11.0))];
    final result = hitTestMapTap((
      geo: geo,
      tapLat: 47.0,
      tapLon: 11.0,
      thresholdSq: threshold,
      track: track,
    ));
    // Falls through to the track fallback since the 'other' feature never matches.
    expect(result.hitActivityId, isNull);
    expect(result.hitSegmentId, isNull);
    expect(result.cursorPoint, (lat: 47.0, lon: 11.0));
  });

  group('totalMapTapPoints', () {
    test('sums coordinate counts across features plus the track length', () {
      final geo = geoOf([
        feature('activity', 1, [
          [11.0, 47.0],
          [11.1, 47.1],
          [11.2, 47.2],
        ]),
        feature('segment', 2, [
          [12.0, 48.0],
        ]),
      ]);
      final track = <(double, GeoPoint)>[
        (0.0, (lat: 47.0, lon: 11.0)),
        (1.0, (lat: 48.0, lon: 12.0)),
      ];
      expect(totalMapTapPoints(geo, track), 3 + 1 + 2);
    });

    test('null geo counts only the track', () {
      final track = <(double, GeoPoint)>[(0.0, (lat: 47.0, lon: 11.0))];
      expect(totalMapTapPoints(null, track), 1);
    });

    test('empty geo and track is zero', () {
      expect(totalMapTapPoints(geoOf(const []), const []), 0);
    });
  });
}
