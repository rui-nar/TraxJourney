/// Dart port of `haversine_km` in src/models/great_circle.py.
library;

import 'dart:math' as math;

/// Great-circle distance in kilometres (Haversine formula), R = 6371.0.
///
/// Mirrors src/models/great_circle.py, including `math.radians` as
/// `x * (pi / 180)`, so the two agree to the last bit where libm does.
double haversineKm(
  double lat1Deg,
  double lon1Deg,
  double lat2Deg,
  double lon2Deg,
) {
  const double r = 6371.0;
  const double degToRad = math.pi / 180.0;
  final phi1 = lat1Deg * degToRad;
  final phi2 = lat2Deg * degToRad;
  final dphi = (lat2Deg - lat1Deg) * degToRad;
  final dlam = (lon2Deg - lon1Deg) * degToRad;
  final sinDphi = math.sin(dphi / 2);
  final sinDlam = math.sin(dlam / 2);
  final a =
      sinDphi * sinDphi + math.cos(phi1) * math.cos(phi2) * sinDlam * sinDlam;
  return 2.0 * r * math.asin(math.sqrt(a));
}
