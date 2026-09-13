import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/map/polyline_decoder.dart';

void main() {
  group('ProjectService.expandEncodedActivities', () {
    test('decodes an activity polyline into coordinates', () {
      final line = [(60.17, 24.94), (61.50, 23.77), (65.01, 25.48)];
      final encoded = _encode(line);

      final geo = {
        'type': 'FeatureCollection',
        'features': [
          {
            'type': 'Feature',
            'geometry': {'type': 'LineString', 'coordinates': []},
            'properties': {
              'type': 'activity',
              'activity_id': 111,
              'polyline': encoded,
            },
          },
        ],
      };

      final out = ProjectService.expandEncodedActivities(geo);
      final coords = out['features'][0]['geometry']['coordinates'] as List;

      expect(coords.length, line.length);
      // GeoJSON order is [lon, lat].
      expect((coords.first[0] as num).toDouble(), closeTo(24.94, 1e-4));
      expect((coords.first[1] as num).toDouble(), closeTo(60.17, 1e-4));
      expect((coords.last[0] as num).toDouble(), closeTo(25.48, 1e-4));
      expect((coords.last[1] as num).toDouble(), closeTo(65.01, 1e-4));
    });

    test('drops out-of-range decoded points (no flutter_map bounds crash)', () {
      // A validly-encoded polyline whose points are out of geographic range
      // (the web-decode failure mode) must never reach the map as coordinates.
      final encoded = _encode([(200.0, 0.0), (200.0, 5.0)]); // lat 200 = invalid
      final geo = {
        'type': 'FeatureCollection',
        'features': [
          {
            'type': 'Feature',
            'geometry': {'type': 'LineString', 'coordinates': []},
            'properties': {'type': 'activity', 'activity_id': 7, 'polyline': encoded},
          },
        ],
      };
      final out = ProjectService.expandEncodedActivities(geo);
      final coords = out['features'][0]['geometry']['coordinates'] as List;
      // Out-of-range points are dropped, so coordinates stay empty here.
      expect(coords, isEmpty);
    });

    test('leaves features that already have coordinates untouched', () {
      final geo = {
        'type': 'FeatureCollection',
        'features': [
          {
            'type': 'Feature',
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                [24.94, 60.17],
                [25.70, 66.50],
              ],
            },
            'properties': {'type': 'segment', 'segment_id': 's1'},
          },
          {
            'type': 'Feature',
            'geometry': {
              'type': 'LineString',
              'coordinates': [
                [1.0, 2.0],
                [3.0, 4.0],
              ],
            },
            'properties': {'type': 'activity', 'activity_id': 9},
          },
        ],
      };

      final out = ProjectService.expandEncodedActivities(geo);
      expect((out['features'][0]['geometry']['coordinates'] as List).length, 2);
      expect((out['features'][1]['geometry']['coordinates'] as List).length, 2);
    });
  });
}

/// Minimal Google-polyline encoder mirroring [decodePolyline] (lat, lon) input.
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
