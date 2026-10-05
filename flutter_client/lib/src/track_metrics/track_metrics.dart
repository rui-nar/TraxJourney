/// Dart port of `recompute_track_metrics` in src/models/track_edit.py and of
/// `_apportion_gain` in src/project/repo_activities.py.
library;

import 'dart:math' as math;

import 'align.dart';
import 'elevation_gain.dart';
import 'haversine.dart';

/// Mirrors `TrackMetrics`.
class TrackMetrics {
  const TrackMetrics({
    required this.distance,
    required this.totalElevationGain,
    required this.elevHigh,
    required this.elevLow,
    required this.startLatLng,
    required this.endLatLng,
    required this.averageSpeed,
    required this.movingTime,
    required this.elapsedTime,
  });

  /// Metres.
  final double distance;

  /// Metres.
  final double totalElevationGain;
  final double? elevHigh;
  final double? elevLow;
  final List<double>? startLatLng;
  final List<double>? endLatLng;

  /// Metres per second.
  final double averageSpeed;

  /// Seconds (apportioned).
  final int movingTime;

  /// Seconds (apportioned).
  final int elapsedTime;
}

/// Python's `round()` for a non-negative value: half to even (2.5 -> 2,
/// 3.5 -> 4), unlike Dart's `num.round`, which rounds half away from zero.
int _roundHalfEven(double x) {
  final floor = x.floorToDouble();
  final diff = x - floor;
  if (diff < 0.5) return floor.toInt();
  if (diff > 0.5) return floor.toInt() + 1;
  return floor.toInt().isEven ? floor.toInt() : floor.toInt() + 1;
}

/// Mirrors `recompute_track_metrics`: an activity's scalar metrics from an
/// edited point list. `original*` are the pre-edit distance and times; moving
/// and elapsed times are apportioned to the fraction of the original distance
/// retained. Fewer than two points give all-zero metrics.
TrackMetrics recomputeTrackMetrics(
  List<TrackPoint> points, {
  double originalDistanceM = 0.0,
  int originalMovingTime = 0,
  int originalElapsedTime = 0,
}) {
  if (points.length < 2) {
    final start =
        points.isEmpty ? null : [points[0].lat, points[0].lng];
    final elev = points.isEmpty ? null : points[0].elev;
    return TrackMetrics(
      distance: 0.0,
      totalElevationGain: 0.0,
      elevHigh: elev,
      elevLow: elev,
      startLatLng: start,
      endLatLng: start,
      averageSpeed: 0.0,
      movingTime: 0,
      elapsedTime: 0,
    );
  }

  // Cumulative distance is accumulated over EVERY point, but only recorded
  // alongside points that carry an elevation, so a stretch recorded without
  // one leaves a real gap in the series the smoothing window can see.
  var distanceKm = 0.0;
  final elevs = <double>[];
  final elevDistKm = <double>[];
  if (points[0].elev != null) {
    elevs.add(points[0].elev!);
    elevDistKm.add(0.0);
  }
  for (var i = 1; i < points.length; i++) {
    distanceKm += haversineKm(
        points[i - 1].lat, points[i - 1].lng, points[i].lat, points[i].lng);
    final elev = points[i].elev;
    if (elev != null) {
      elevs.add(elev);
      elevDistKm.add(distanceKm);
    }
  }
  final distanceM = distanceKm * 1000.0;

  final gain = elevationGain(elevs, elevDistKm);
  final elevHigh = elevs.isEmpty ? null : elevs.reduce(math.max);
  final elevLow = elevs.isEmpty ? null : elevs.reduce(math.min);

  // Apportion times proportionally to retained distance.
  final frac = originalDistanceM > 0
      ? math.max(0.0, math.min(1.0, distanceM / originalDistanceM))
      : 1.0;
  final movingTime = _roundHalfEven(originalMovingTime * frac);
  final elapsedTime = _roundHalfEven(originalElapsedTime * frac);

  final averageSpeed = movingTime > 0 ? distanceM / movingTime : 0.0;

  return TrackMetrics(
    distance: distanceM,
    totalElevationGain: gain,
    elevHigh: elevHigh,
    elevLow: elevLow,
    startLatLng: [points.first.lat, points.first.lng],
    endLatLng: [points.last.lat, points.last.lng],
    averageSpeed: averageSpeed,
    movingTime: movingTime,
    elapsedTime: elapsedTime,
  );
}

/// Mirrors `_apportion_gain`: scales a stored elevation gain to the share
/// [after] accounts for, [before] and [after] being our own measure of the same
/// two geometries. Falls back to [after] when there is nothing to scale: no
/// stored value, or a [before] of zero.
double apportionGain(double? stored, double before, double after) {
  if (stored == null || before <= 0) return after;
  return stored * (after / before);
}
