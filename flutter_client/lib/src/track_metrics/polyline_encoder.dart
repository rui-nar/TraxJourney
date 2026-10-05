/// Google polyline encoder, moved out of the video module so the track maths
/// and the video request share one implementation.
library;

/// Google-encodes [points] (`(lat, lon)`) at precision 5, the format the
/// server decodes consent geometry with.
///
/// Each coordinate is scaled by 1e5 and rounded half away from zero (Dart's
/// `num.round`, which is also what the Python `polyline` package does), then
/// delta-encoded.
///
/// Arithmetic only, no shifts or bitwise ops on accumulated values, for the
/// same reason as `decodePolyline`: on the web those run as 32-bit ops.
String encodePolyline(List<(double, double)> points) {
  final out = StringBuffer();
  void write(int delta) {
    var v = delta < 0 ? -delta * 2 - 1 : delta * 2;
    while (v >= 32) {
      out.writeCharCode(32 + v % 32 + 63);
      v = v ~/ 32;
    }
    out.writeCharCode(v + 63);
  }

  var lat = 0, lon = 0;
  for (final (pLat, pLon) in points) {
    final eLat = (pLat * 1e5).round();
    final eLon = (pLon * 1e5).round();
    write(eLat - lat);
    write(eLon - lon);
    lat = eLat;
    lon = eLon;
  }
  return out.toString();
}
