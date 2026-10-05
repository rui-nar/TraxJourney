// Pins the Dart track-geometry port (haversine, polyline encoder, align_points,
// points_to_elevation_profile, interpolate_elevation_gaps) to the vectors
// produced by the Python in src/models/ (issue #366).

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/track_metrics/align.dart';
import 'package:traxjourney_client/src/track_metrics/elevation_profile.dart';
import 'package:traxjourney_client/src/track_metrics/haversine.dart';
import 'package:traxjourney_client/src/track_metrics/polyline_encoder.dart';

import 'vectors.dart';

void main() {
  group('haversine_km', () {
    checkSection(
      'haversine_km',
      (i) => haversineKm((i['lat1'] as num).toDouble(), (i['lng1'] as num).toDouble(),
          (i['lat2'] as num).toDouble(), (i['lng2'] as num).toDouble()),
    );
  });

  group('polyline_encode', () {
    checkSection('polyline_encode', (i) {
      final points = [
        for (final p in i['points'] as List)
          ((p[0] as num).toDouble(), (p[1] as num).toDouble()),
      ];
      // The Python returns None for no points (points_to_polyline).
      return points.isEmpty ? null : encodePolyline(points);
    });
  });

  group('align_points', () {
    checkSection('align_points', (i) {
      final profile = i['elevation_profile'] as Map<String, dynamic>?;
      return pointRows(alignPoints(
        i['summary_polyline'] as String?,
        profile == null
            ? null
            : ElevationProfile(
                doubles(profile['distances_km']), doubles(profile['elevations_m'])),
      ));
    });
  });

  group('points_to_elevation_profile', () {
    checkSection('points_to_elevation_profile',
        (i) => profileJson(pointsToElevationProfile(trackPoints(i['points']))));
  });

  group('interpolate_elevation_gaps', () {
    checkSection(
      'interpolate_elevation_gaps',
      (i) => interpolateElevationGaps(
          doubles(i['distances_km']), nullableDoubles(i['elevations'])),
    );
  });
}
