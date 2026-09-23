import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/map/polyline_decoder.dart';

/// These tests must be run on the web (`flutter test --platform chrome`) to be
/// meaningful: the decoder's previous bug only manifested when compiled to
/// JavaScript (`~` returns an unsigned 32-bit complement there). A VM-only run
/// passed even with the broken decoder.
void main() {
  group('decodePolyline (web-safe)', () {
    test('decodes the classic Google example exactly', () {
      // From Google's polyline algorithm docs: "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
      // → (38.5, -120.2), (40.7, -120.95), (43.252, -126.453)
      final pts = decodePolyline('_p~iF~ps|U_ulLnnqC_mqNvxq`@');
      expect(pts.length, 3);
      expect(pts[0].lat, closeTo(38.5, 1e-5));
      expect(pts[0].lon, closeTo(-120.2, 1e-5));
      expect(pts[1].lat, closeTo(40.7, 1e-5));
      expect(pts[1].lon, closeTo(-120.95, 1e-5));
      expect(pts[2].lat, closeTo(43.252, 1e-5));
      expect(pts[2].lon, closeTo(-126.453, 1e-5));
    });

    test('handles negative deltas without exploding (the web bug)', () {
      // A track that goes north then south then west — exercises negative
      // deltas in both lat and lon, which the old `~(r >> 1)` decoder turned
      // into ~4.29e9 values on the web.
      final encoded = _encode(const [
        (60.1700, 24.9400),
        (61.5000, 23.7700), // lon delta negative
        (60.0000, 25.0000), // lat delta negative
        (59.5000, 22.0000), // both negative
      ]);
      final pts = decodePolyline(encoded);
      expect(pts.length, 4);
      for (final p in pts) {
        expect(p.lat, inInclusiveRange(-90, 90));
        expect(p.lon, inInclusiveRange(-180, 180));
      }
      expect(pts[1].lat, closeTo(61.5, 1e-4));
      expect(pts[1].lon, closeTo(23.77, 1e-4));
      expect(pts[3].lat, closeTo(59.5, 1e-4));
      expect(pts[3].lon, closeTo(22.0, 1e-4));
    });
  });
}

String _encode(List<(double, double)> points) {
  final sb = StringBuffer();
  int prevLat = 0, prevLon = 0;
  for (final p in points) {
    final lat = (p.$1 * 1e5).round();
    final lon = (p.$2 * 1e5).round();
    _encodeValue(sb, lat - prevLat);
    _encodeValue(sb, lon - prevLon);
    prevLat = lat;
    prevLon = lon;
  }
  return sb.toString();
}

void _encodeValue(StringBuffer sb, int value) {
  int v = value < 0 ? ~(value << 1) : (value << 1);
  while (v >= 0x20) {
    sb.writeCharCode((0x20 | (v & 0x1f)) + 63);
    v >>= 5;
  }
  sb.writeCharCode(v + 63);
}
