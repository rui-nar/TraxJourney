// Pins the Dart recompute_track_metrics and _apportion_gain to the vectors
// produced by the Python (issue #366): src/models/track_edit.py and
// src/project/repo_activities.py.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/track_metrics/track_metrics.dart';

import 'vectors.dart';

void main() {
  group('recompute_track_metrics', () {
    checkSection('recompute_track_metrics', (i) {
      final m = recomputeTrackMetrics(
        trackPoints(i['points']),
        originalDistanceM: (i['original_distance_m'] as num).toDouble(),
        originalMovingTime: i['original_moving_time'] as int,
        originalElapsedTime: i['original_elapsed_time'] as int,
      );
      return {
        'distance': m.distance,
        'total_elevation_gain': m.totalElevationGain,
        'elev_high': m.elevHigh,
        'elev_low': m.elevLow,
        'start_latlng': m.startLatLng,
        'end_latlng': m.endLatLng,
        'average_speed': m.averageSpeed,
        'moving_time': m.movingTime,
        'elapsed_time': m.elapsedTime,
      };
    });
  });

  group('apportion_gain', () {
    checkSection(
      'apportion_gain',
      (i) => apportionGain((i['stored'] as num?)?.toDouble(),
          (i['before'] as num).toDouble(), (i['after'] as num).toDouble()),
    );
  });
}
