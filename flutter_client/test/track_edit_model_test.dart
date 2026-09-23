import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/map/geo_point.dart';
import 'package:traxjourney_client/src/map/polyline_decoder.dart';
import 'package:traxjourney_client/src/projects/track_edit_model.dart';

void main() {
  // Four collinear points ~roughly eastward, plus their elevations.
  final track = [
    (lat: 48.0, lon: 2.0),
    (lat: 48.0, lon: 2.01),
    (lat: 48.0, lon: 2.02),
    (lat: 48.0, lon: 2.03),
  ];
  final encoded = _encode(track);
  // Elevation-profile distances must match the track's real haversine cumulative
  // (as produced by enrichment), so build them from the decoded geometry.
  final cum = buildTrackFromPolyline(track);
  final elevValues = <double>[100.0, 120.0, 110.0, 140.0];
  final elevPairs = <List<double>>[
    for (var i = 0; i < cum.length; i++) [cum[i].$1, elevValues[i]],
  ];

  group('fromEncoded', () {
    test('decodes points and aligns elevation onto them', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      expect(m.length, 4);
      expect(m.points.first.lat, closeTo(48.0, 1e-5));
      expect(m.points.first.elev, closeTo(100.0, 1.0));
      expect(m.points.last.elev, closeTo(140.0, 1.0));
    });

    test('empty polyline yields an empty, invalid model', () {
      final m = TrackEditModel.fromEncoded(null, null);
      expect(m.length, 0);
      expect(m.isValid, isFalse);
    });

    test('no elevation pairs leaves elev null', () {
      final m = TrackEditModel.fromEncoded(encoded, null);
      expect(m.points.every((p) => p.elev == null), isTrue);
    });
  });

  group('fromLonLat', () {
    test('decodes [lon,lat] pairs with no elevation', () {
      final m = TrackEditModel.fromLonLat(const [
        [24.9414, 60.1719],
        [24.8, 59.9],
        [24.7536, 59.4370],
      ]);
      expect(m.length, 3);
      expect(m.points.first.lat, closeTo(60.1719, 1e-6));
      expect(m.points.first.lng, closeTo(24.9414, 1e-6));
      expect(m.points.every((p) => p.elev == null), isTrue);
    });

    test('null or empty coords yield an empty, invalid model', () {
      expect(TrackEditModel.fromLonLat(null).length, 0);
      expect(TrackEditModel.fromLonLat(const []).isValid, isFalse);
    });
  });

  group('dirty tracking', () {
    test('pristine model is not dirty', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      expect(m.isDirty, isFalse);
    });

    test('an edit marks it dirty', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      m.removePoint(1);
      expect(m.isDirty, isTrue);
    });
  });

  group('edit operations produce the expected point list', () {
    test('trim keeps only the inclusive range', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      m.trim(1, 2);
      expect(m.length, 2);
      expect(m.points[0].lng, closeTo(2.01, 1e-5));
      expect(m.points[1].lng, closeTo(2.02, 1e-5));
    });

    test('addPointAfter inserts into the chosen segment', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      m.addPointAfter(0, const EditPoint(48.5, 2.005, 105.0));
      expect(m.length, 5);
      expect(m.points[1], const EditPoint(48.5, 2.005, 105.0));
    });

    test('removePoint drops the vertex', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      m.removePoint(0);
      expect(m.length, 3);
      expect(m.points.first.lng, closeTo(2.01, 1e-5));
    });

    test('movePoint reshapes but preserves elevation', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      final elevBefore = m.points[2].elev;
      m.movePoint(2, 48.9, 2.9);
      expect(m.points[2].lat, 48.9);
      expect(m.points[2].lng, 2.9);
      expect(m.points[2].elev, elevBefore);
    });

    test('out-of-range edits throw', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      expect(() => m.trim(0, 99), throwsRangeError);
      expect(() => m.removePoint(99), throwsRangeError);
      expect(() => m.addPointAfter(99, const EditPoint(0, 0)), throwsRangeError);
    });
  });

  group('previewSplit', () {
    test('shares the boundary point between head and tail', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      final r = m.previewSplit(2);
      expect(r.head.length, 3); // [0,1,2]
      expect(r.tail.length, 2); // [2,3]
      expect(r.head.last, r.tail.first); // shared boundary
      expect(m.isDirty, isFalse); // preview does not mutate
    });

    test('rejects trivial split indices', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      expect(() => m.previewSplit(0), throwsRangeError);
      expect(() => m.previewSplit(3), throwsRangeError);
    });
  });

  group('toSavePayload', () {
    test('serialises the current points with lat/lng/elev', () {
      final m = TrackEditModel.fromPoints(const [
        EditPoint(48.0, 2.0, 100.0),
        EditPoint(48.1, 2.1, 110.0),
      ]);
      final payload = m.toSavePayload();
      expect(payload['points'], [
        {'lat': 48.0, 'lng': 2.0, 'elev': 100.0},
        {'lat': 48.1, 'lng': 2.1, 'elev': 110.0},
      ]);
    });

    test('omits elev when null', () {
      final m = TrackEditModel.fromPoints(const [
        EditPoint(48.0, 2.0),
        EditPoint(48.1, 2.1),
      ]);
      final points = m.toSavePayload()['points'] as List;
      expect(points.first.containsKey('elev'), isFalse);
    });

    test('reflects an edit in the produced payload', () {
      final m = TrackEditModel.fromEncoded(encoded, elevPairs);
      m.trim(0, 1);
      final points = m.toSavePayload()['points'] as List;
      expect(points.length, 2);
    });
  });
}

/// Minimal Google-polyline encoder for building test fixtures (mirror of the
/// decoder in polyline_decoder.dart).
String _encode(List<GeoPoint> pts) {
  final sb = StringBuffer();
  int lastLat = 0, lastLng = 0;
  for (final p in pts) {
    final lat = (p.lat * 1e5).round();
    final lng = (p.lon * 1e5).round();
    _encodeDelta(sb, lat - lastLat);
    _encodeDelta(sb, lng - lastLng);
    lastLat = lat;
    lastLng = lng;
  }
  return sb.toString();
}

void _encodeDelta(StringBuffer sb, int v) {
  int zig = v < 0 ? ~(v << 1) : (v << 1);
  while (zig >= 0x20) {
    sb.writeCharCode((0x20 | (zig & 0x1f)) + 63);
    zig >>= 5;
  }
  sb.writeCharCode(zig + 63);
}
