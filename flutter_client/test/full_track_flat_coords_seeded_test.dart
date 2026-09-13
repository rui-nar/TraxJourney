// Issue #337. `_buildFullTrack` flattened the trip's GeoJSON coordinates into
// Float64List buffers on the UI isolate, immediately before handing them to
// the compute() hop that consumes them — measured at 22.6 ms on a real trip,
// the only span left over the 16.7 ms frame budget after #335.
//
// The buffers are now produced by the decode worker that is already holding
// those coordinates (`decodeGeoOffIsolate`) and come back on its zero-copy
// return, so the UI isolate never walks them. These tests pin the two halves
// that matter: the seeded path does *zero* flattening on the UI isolate, and
// a geo that never went through that worker still builds the same track
// rather than an empty one.
//
// heavy_decode_test.dart covers the seeding itself at the cache level; this
// file covers it end-to-end through ProjectNotifier, which is where the stall
// was measured.

import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/heavy_decode.dart';
import 'package:traxjourney_client/src/projects/map_geometry_memo.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

Map<String, dynamic> _geo(int activities, int pointsPer) => {
      'type': 'FeatureCollection',
      'features': [
        for (var a = 0; a < activities; a++)
          {
            'type': 'Feature',
            'properties': {'type': 'activity', 'activity_id': 'a$a'},
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                for (var i = 0; i < pointsPer; i++)
                  [7.0 + a * 0.01 + i * 0.00002, 45.0 + a * 0.01 + i * 0.00001]
              ],
            },
          }
      ],
    };

List<Map<String, dynamic>> _activities(int n, int samples) => [
      for (var a = 0; a < n; a++)
        {
          'id': 'a$a',
          'elevation_profile': [
            for (var i = 0; i < samples; i++) [i * 0.01, 100.0 + i]
          ],
        }
    ];

Uint8List _bytes(Object o) => Uint8List.fromList(utf8.encode(jsonEncode(o)));

void main() {
  // 20 x 600 = 12,000 coordinates: over kInlineFullTrackThreshold, so
  // _buildFullTrack takes the background-isolate branch — the one that used
  // to flatten inline first — and over kInlineDecodeThresholdBytes, so the
  // decode really hops too.
  const acts = 20;
  const pointsPer = 600;
  late Uint8List geoBytes;

  setUpAll(() {
    geoBytes = _bytes(_geo(acts, pointsPer));
    expect(geoBytes.length, greaterThan(kInlineDecodeThresholdBytes));
    expect(acts * pointsPer, greaterThan(kInlineFullTrackThreshold));
  });

  test('a geo that came through the decode worker costs the UI isolate no '
      'flattening at all', () async {
    final geo = await decodeGeoOffIsolate(geoBytes);
    final activities = _activities(acts, 50);

    final notifier = ProjectNotifier(ProjectService());
    notifier.activities = activities;
    notifier.geo = geo;

    flatCoordsConversionCount = 0;
    await notifier.buildFullTrack();
    expect(flatCoordsConversionCount, 0,
        reason: 'the decode worker already produced these buffers — walking '
            'the coordinates again is exactly the 22.6 ms stall #337 removes');

    // And the track is real, not an empty one bought by skipping the work.
    expect(notifier.fullTrack, isNotEmpty);
    expect(notifier.perActivityTracks, hasLength(acts));
    final expected = buildFullTrackResult((geo: geo, activities: activities));
    expect(notifier.fullTrack, expected.fullTrack);
  });

  test('a geo built on the client, which no worker ever saw, still builds the '
      'same track', () async {
    // The E2EE path (client_geo_builder) and locally patched features produce
    // coordinate lists no decode hop has seen. Falling back to flattening on
    // demand is the acceptable outcome; silently building an empty track is
    // not.
    final geo = _geo(acts, pointsPer);
    final activities = _activities(acts, 50);

    final notifier = ProjectNotifier(ProjectService());
    notifier.activities = activities;
    notifier.geo = geo;

    flatCoordsConversionCount = 0;
    await notifier.buildFullTrack();
    expect(flatCoordsConversionCount, acts,
        reason: 'unseeded geometry must flatten on demand, once per activity');
    expect(notifier.fullTrack, isNotEmpty);
    expect(notifier.perActivityTracks, hasLength(acts));

    final viaWorker = await decodeGeoOffIsolate(_bytes(_geo(acts, pointsPer)));
    final expected = buildFullTrackResult((geo: viaWorker, activities: activities));
    expect(notifier.fullTrack, expected.fullTrack,
        reason: 'seeded and unseeded geometry must produce identical tracks');
  });

  test('the inline branch reads the same seeds', () async {
    // Small trips (and every trip on web) never hop, but they run the same
    // flattenGeoCoords — so a seeded geo must cost them nothing either.
    final small = _bytes(_geo(2, 10));
    final geo = await decodeGeoOffIsolate(small);
    final notifier = ProjectNotifier(ProjectService());
    notifier.activities = _activities(2, 5);
    notifier.geo = geo;

    flatCoordsConversionCount = 0;
    await notifier.buildFullTrack();
    expect(flatCoordsConversionCount, 0);
    expect(notifier.fullTrack, isNotEmpty);
  });
}
