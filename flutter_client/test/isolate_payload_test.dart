// Issue #276. `compute()` copies its argument.
//
// Two per-trip computations were handed the whole `activities` list, which
// carries every activity's `elevation_profile` — `[[distKm, elevM], …]`, one
// Dart list object per sample. On a 219-activity trip that is ~700k small
// objects, and serialising them measured as a **2437 ms frame**: the cost
// landing on the UI isolate that the hop existed to keep it off. It went
// unnoticed because the copy happens *outside* the span wrapping the work,
// and because the perf report counted tracks and geometry but never
// elevation samples.
//
// `buildFullTrackFromTotals` needs one double per activity, so it is now
// given exactly that. `computeElevationSpotsFlat` genuinely needs the
// samples, so it is given them as a `Float64List` — which copies as one
// buffer rather than a million objects.
//
// These pin the payload shapes and prove the refactor left both computations'
// output identical, comparing against a reference implementation of the
// original nested walk (the nested entry points now delegate to the flat
// ones, so comparing those two directly would be tautological).

import 'dart:typed_data';

import 'package:fl_chart/fl_chart.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/elevation_chart.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';

List<Map<String, dynamic>> _acts(int n, int samples) => [
      for (var a = 0; a < n; a++)
        {
          'id': '$a',
          'elevation_profile': [
            for (var i = 0; i < samples; i++) [i * 0.1, 100.0 + i]
          ],
        }
    ];

Map<String, dynamic> _geo(int n, int coords) => {
      'type': 'FeatureCollection',
      'features': [
        for (var a = 0; a < n; a++)
          {
            'type': 'Feature',
            'properties': {'type': 'activity', 'activity_id': '$a'},
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                for (var i = 0; i < coords; i++)
                  [7.0 + i * 0.001, 45.0 + i * 0.001]
              ],
            },
          }
      ],
    };

/// The original nested walk, verbatim in behaviour — the contract the flat
/// implementation must still satisfy.
List<FlSpot> _referenceSpots(
    List<Map<String, dynamic>> activities, dynamic selectedId) {
  final source = selectedId == null
      ? activities
      : activities
          .where((a) => a['id']?.toString() == selectedId.toString())
          .toList();
  final spots = <FlSpot>[];
  double offsetKm = 0;
  for (final a in source) {
    final profile = a['elevation_profile'];
    if (profile is! List || profile.isEmpty) continue;
    final lastPt = profile.last;
    final elevTotalKm = (lastPt is List && lastPt.isNotEmpty)
        ? (lastPt[0] as num).toDouble()
        : 0.0;
    for (int i = 0; i < profile.length; i++) {
      final point = profile[i];
      if (point is! List || point.length < 2) continue;
      spots.add(FlSpot((point[0] as num).toDouble() + offsetKm,
          (point[1] as num).toDouble()));
    }
    if (elevTotalKm > 0) offsetKm += elevTotalKm;
  }
  return spots;
}

void main() {
  group('the track builder is handed one double per activity', () {
    test('the payload is O(activities), not O(samples)', () {
      // The whole point: 5 activities x 1000 samples used to cross the isolate
      // boundary as 5000 two-element lists, to read 5 numbers.
      final totals = activityElevationTotals(_acts(5, 1000));
      expect(totals, hasLength(5));
      expect(totals.first.id, '0');
      expect(totals.first.elevTotalKm, closeTo(99.9, 1e-9));
    });

    test('activities the builder used to skip inline are still skipped', () {
      final totals = activityElevationTotals([
        {'id': 'a'},
        {'id': 'b', 'elevation_profile': <dynamic>[]},
        {
          'id': 'c',
          'elevation_profile': [
            [0.0, 1.0]
          ]
        },
      ]);
      expect(totals.map((t) => t.id).toList(), ['c'],
          reason: 'order and membership must be what the inline walk produced');
    });

    test('a malformed last entry yields a zero total rather than throwing', () {
      final totals = activityElevationTotals([
        {
          'id': 'a',
          'elevation_profile': [
            [0.0, 1.0],
            'junk',
          ]
        },
      ]);
      expect(totals.single.elevTotalKm, 0.0);
    });

    test('the built track is unchanged', () {
      final acts = _acts(3, 50);
      final geo = _geo(3, 80);
      final viaTotals = buildFullTrackFromTotals((
        coords: flattenGeoCoords(geo),
        totals: activityElevationTotals(acts),
      ));
      // buildFullTrackResult is the nested contract the existing suite pins;
      // it must keep agreeing with the flat form it now delegates to.
      final viaMaps = buildFullTrackResult((geo: geo, activities: acts));
      expect(viaTotals.fullTrack, viaMaps.fullTrack);
      expect(viaTotals.perActivityTracks.keys.toList(),
          viaMaps.perActivityTracks.keys.toList());
    });
  });

  group('the chart is handed typed buffers', () {
    test('samples become one Float64List per activity', () {
      final flat = flattenProfiles(_acts(1, 10));
      expect(flat.single.points, isA<Float64List>(),
          reason: 'typed data copies as a buffer, not object by object');
      expect(flat.single.points, hasLength(20),
          reason: '10 (distance, elevation) pairs');
    });

    test('activities with no usable profile are dropped', () {
      expect(
          flattenProfiles([
            {'id': 'x'},
            {'id': 'y', 'elevation_profile': <dynamic>[]},
          ]),
          isEmpty);
    });

    test('the spot series is identical to the nested walk', () {
      final acts = _acts(4, 60);
      final flat = computeElevationSpotsFlat(flattenProfiles(acts));
      final reference = _referenceSpots(acts, null);
      expect(flat.spots, hasLength(reference.length));
      for (var i = 0; i < reference.length; i++) {
        expect(flat.spots[i].x, closeTo(reference[i].x, 1e-9));
        expect(flat.spots[i].y, closeTo(reference[i].y, 1e-9));
      }
    });

    test('a selected activity still filters to just that activity', () {
      final acts = _acts(4, 20);
      final got = computeElevationSpots((activities: acts, selectedId: '2'));
      expect(got.spots, hasLength(20));
      expect(got.spots.first.x, closeTo(0.0, 1e-9),
          reason: 'a single activity starts at its own zero, with no offset');
      final reference = _referenceSpots(acts, '2');
      for (var i = 0; i < reference.length; i++) {
        expect(got.spots[i].x, closeTo(reference[i].x, 1e-9));
        expect(got.spots[i].y, closeTo(reference[i].y, 1e-9));
      }
    });

    test('selecting an activity does not read any other activity samples', () {
      // The ordering that matters. Flattening before filtering would walk the
      // whole trip to build a series for one activity — turning the cheap
      // bounded path (inline, and with no size threshold in front of it) into
      // the most expensive one there is.
      //
      // A poisoned neighbour makes that observable rather than merely slow:
      // the filtered walk this replaces never looked at it, so neither may
      // this one.
      final acts = <Map<String, dynamic>>[
        {
          'id': 'wanted',
          'elevation_profile': [
            [0.0, 10.0],
            [1.0, 20.0],
          ]
        },
        {
          'id': 'poison',
          'elevation_profile': [
            [0.0, 1.0],
            ['not a number', 1.0],
          ]
        },
      ];
      expect(_referenceSpots(acts, 'wanted').map((s) => s.y).toList(),
          [10.0, 20.0],
          reason: 'the walk being preserved returns cleanly on this input');
      final got =
          computeElevationSpots((activities: acts, selectedId: 'wanted'));
      expect(got.spots.map((s) => s.y).toList(), [10.0, 20.0]);
    });

    test('a malformed sample is skipped without shifting the offsets', () {
      // The subtle one: an activity whose samples are partly unusable still
      // advances the running distance offset by its raw last entry, so the
      // activities after it stay where they were.
      final acts = <Map<String, dynamic>>[
        {
          'id': 'a',
          'elevation_profile': [
            [0.0, 10.0],
            'junk',
            [2.0, 30.0],
          ]
        },
        {
          'id': 'b',
          'elevation_profile': [
            [0.0, 5.0],
            [1.0, 6.0],
          ]
        },
      ];
      final flat = computeElevationSpotsFlat(flattenProfiles(acts));
      expect(flat.spots.map((s) => s.x).toList(), [0.0, 2.0, 2.0, 3.0]);
      expect(flat.spots.map((s) => s.y).toList(), [10.0, 30.0, 5.0, 6.0]);
      expect(flat.spots.map((s) => s.x).toList(),
          _referenceSpots(acts, null).map((s) => s.x).toList());
    });

    test('min and max still come from the full series', () {
      final acts = _acts(2, 40);
      final flat = computeElevationSpotsFlat(flattenProfiles(acts));
      final reference = _referenceSpots(acts, null);
      expect(
          flat.minY, reference.map((s) => s.y).reduce((a, b) => a < b ? a : b));
      expect(
          flat.maxY, reference.map((s) => s.y).reduce((a, b) => a > b ? a : b));
    });
  });

  group('geometry crosses the isolate boundary as typed buffers', () {
    // The third instance of compute() copying its argument in this codebase,
    // and the one that amplified #332's refetch loop into a 2.6 GB Dart heap:
    // GeoJSON coordinates are one Dart list object per point, ~1.47 M of them
    // on the trip measured.
    test('coordinates become one Float64List per activity', () {
      final flat = flattenGeoCoords(_geo(2, 5));
      expect(flat, hasLength(2));
      expect(flat['0'], isA<Float64List>(),
          reason: 'typed data copies as a buffer, not object by object');
      expect(flat['0'], hasLength(10), reason: '5 interleaved lon/lat pairs');
    });

    test('the interleaving is lon, lat — GeoJSON order', () {
      final flat = flattenGeoCoords(_geo(1, 2));
      expect(flat['0']![0], closeTo(7.0, 1e-9));
      expect(flat['0']![1], closeTo(45.0, 1e-9));
    });

    test('segments are skipped, as the nested walk skipped them', () {
      final geo = {
        'type': 'FeatureCollection',
        'features': [
          {
            'type': 'Feature',
            'properties': {'type': 'segment', 'activity_id': 'seg'},
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                [7.0, 45.0],
                [7.1, 45.1]
              ]
            },
          },
        ],
      };
      expect(flattenGeoCoords(geo), isEmpty);
    });

    test('features with no id, no coordinates or empty ones are skipped', () {
      final geo = {
        'type': 'FeatureCollection',
        'features': [
          {'type': 'Feature', 'properties': <String, dynamic>{}, 'geometry': <String, dynamic>{}},
          {
            'type': 'Feature',
            'properties': {'activity_id': 'empty'},
            'geometry': {'type': 'LineString', 'coordinates': <dynamic>[]},
          },
        ],
      };
      expect(flattenGeoCoords(geo), isEmpty);
    });

    test('a malformed point is dropped, leaving the rest intact', () {
      final geo = {
        'type': 'FeatureCollection',
        'features': [
          {
            'type': 'Feature',
            'properties': {'activity_id': 'a'},
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                [7.0, 45.0],
                'junk',
                [7.2, 45.2],
              ]
            },
          },
        ],
      };
      final flat = flattenGeoCoords(geo);
      expect(flat['a'], hasLength(4), reason: 'two usable pairs');
      expect(flat['a']![2], closeTo(7.2, 1e-9));
    });

    test('a null geo yields no coordinates rather than throwing', () {
      expect(flattenGeoCoords(null), isEmpty);
    });

    test('the built track is the same as the nested walk produced', () {
      final acts = _acts(3, 50);
      final geo = _geo(3, 80);
      final viaFlat = buildFullTrackFromTotals((
        coords: flattenGeoCoords(geo),
        totals: activityElevationTotals(acts),
      ));
      final viaMaps = buildFullTrackResult((geo: geo, activities: acts));
      expect(viaFlat.fullTrack, viaMaps.fullTrack);
      expect(viaFlat.perActivityTracks.keys.toList(),
          viaMaps.perActivityTracks.keys.toList());
    });
  });

}
