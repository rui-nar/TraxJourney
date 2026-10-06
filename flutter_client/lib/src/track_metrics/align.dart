/// Dart port of `TrackPoint` and `align_points` in src/models/track_edit.py.
library;

import '../map/polyline_decoder.dart';
import 'haversine.dart';

/// Mirrors `ELEVATION_MIN_M` / `ELEVATION_MAX_M` in src/models/value_bounds.py.
const double _elevationMinM = -20000.0;
const double _elevationMaxM = 20000.0;

/// Mirrors `plausible_elevation`: [value] if it is a finite reading within
/// ±20 km, else null (a device's 65535, NaN and ±Infinity are missing).
double? plausibleElevation(double? value) {
  if (value == null || !value.isFinite) return null;
  if (value < _elevationMinM || value > _elevationMaxM) return null;
  return value;
}

/// One point of a track. Mirrors `TrackPoint`: an implausible elevation is
/// stored as a missing one.
class TrackPoint {
  TrackPoint(this.lat, this.lng, [double? elev])
      : elev = plausibleElevation(elev);

  final double lat;
  final double lng;
  final double? elev;
}

/// A stored elevation profile: `distances_km` and `elevations_m`, parallel.
class ElevationProfile {
  const ElevationProfile(this.distancesKm, this.elevationsM);

  final List<double> distancesKm;
  final List<double> elevationsM;
}

/// Mirrors `align_points`: aligns a polyline and an elevation profile into one
/// ordered point list. Elevation is interpolated onto the decoded points by
/// cumulative haversine distance; without a usable profile every point has no
/// elevation.
List<TrackPoint> alignPoints(
  String? summaryPolyline,
  ElevationProfile? elevationProfile,
) {
  if (summaryPolyline == null || summaryPolyline.isEmpty) return [];
  final decoded = decodePolyline(summaryPolyline);
  if (decoded.isEmpty) return [];

  final distKm = elevationProfile?.distancesKm ?? const <double>[];
  final elevM = elevationProfile?.elevationsM ?? const <double>[];

  if (distKm.isEmpty || elevM.isEmpty || distKm.length != elevM.length) {
    return [for (final p in decoded) TrackPoint(p.lat, p.lon)];
  }

  // Cumulative distance (km) along the decoded polyline.
  final cum = <double>[0.0];
  for (var i = 1; i < decoded.length; i++) {
    cum.add(cum.last +
        haversineKm(decoded[i - 1].lat, decoded[i - 1].lon, decoded[i].lat,
            decoded[i].lon));
  }

  return [
    for (var i = 0; i < decoded.length; i++)
      TrackPoint(
          decoded[i].lat, decoded[i].lon, _interpElev(cum[i], distKm, elevM)),
  ];
}

/// Mirrors `_interp_elev`: linear interpolation of elevation at cumulative
/// distance [d] (km), by binary search (`bisect_left`).
double _interpElev(double d, List<double> distKm, List<double> elevM) {
  if (d <= distKm.first) return elevM.first;
  if (d >= distKm.last) return elevM.last;
  var lo = 0, hi = distKm.length;
  while (lo < hi) {
    final mid = (lo + hi) ~/ 2;
    if (distKm[mid] < d) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  final i = lo;
  final d0 = distKm[i - 1], d1 = distKm[i];
  final e0 = elevM[i - 1], e1 = elevM[i];
  if (d1 == d0) return e0;
  final frac = (d - d0) / (d1 - d0);
  return e0 + frac * (e1 - e0);
}
