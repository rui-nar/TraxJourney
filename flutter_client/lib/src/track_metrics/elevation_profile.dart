/// Dart port of `points_to_elevation_profile` and `interpolate_elevation_gaps`
/// in src/models/track_edit.py.
library;

import 'align.dart';
import 'haversine.dart';

/// Mirrors `points_to_elevation_profile`: the `(distances_km, elevations_m)` of
/// an ordered point list, holes interpolated by cumulative distance. Null when
/// there are no points or no point has an elevation.
ElevationProfile? pointsToElevationProfile(List<TrackPoint> points) {
  if (points.isEmpty || points.every((p) => p.elev == null)) return null;
  final distances = <double>[0.0];
  for (var i = 1; i < points.length; i++) {
    distances.add(distances.last +
        haversineKm(points[i - 1].lat, points[i - 1].lng, points[i].lat,
            points[i].lng));
  }
  return ElevationProfile(distances,
      interpolateElevationGaps(distances, [for (final p in points) p.elev]));
}

/// Mirrors `interpolate_elevation_gaps`: fills the null holes of an elevation
/// series by cumulative distance. Interior gaps are a straight line between the
/// bracketing known samples; a leading or trailing gap extends the nearest
/// known value. Throws [ArgumentError] (Python's `ValueError`) when no element
/// is known.
List<double> interpolateElevationGaps(
  List<double> distancesKm,
  List<double?> elevations,
) {
  final known = <int>[
    for (var i = 0; i < elevations.length; i++)
      if (elevations[i] != null) i,
  ];
  if (known.isEmpty) {
    throw ArgumentError('no point carries an elevation');
  }
  final filled = <double>[for (final e in elevations) e ?? 0.0];
  for (var i = 0; i < known.first; i++) {
    filled[i] = filled[known.first];
  }
  for (var i = known.last + 1; i < filled.length; i++) {
    filled[i] = filled[known.last];
  }
  for (var k = 0; k + 1 < known.length; k++) {
    final a = known[k], b = known[k + 1];
    if (b == a + 1) continue;
    final d0 = distancesKm[a], d1 = distancesKm[b];
    final e0 = filled[a], e1 = filled[b];
    final span = d1 - d0;
    for (var i = a + 1; i < b; i++) {
      final frac = span != 0 ? (distancesKm[i] - d0) / span : 0.0;
      filled[i] = e0 + frac * (e1 - e0);
    }
  }
  return filled;
}
