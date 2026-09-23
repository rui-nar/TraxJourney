import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/map/great_circle.dart';
import 'package:traxjourney_client/src/map/polyline_decoder.dart';
import 'package:traxjourney_client/src/projects/client_geo_builder.dart';

/// Minimal Google-polyline encoder mirroring [decodePolyline] (lat, lon) input
/// — same helper used by geo_encoded_test.dart.
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

void main() {
  group('activitiesById', () {
    test('indexes by string id, skipping activities without an id', () {
      final byId = activitiesById([
        {'id': 1, 'name': 'A'},
        {'id': 2, 'name': 'B'},
        {'name': 'no id'},
      ]);
      expect(byId.keys.toSet(), {'1', '2'});
      expect(byId['1']!['name'], 'A');
    });
  });

  group('buildFullGeo', () {
    test('decodes an activity polyline into expanded coordinates', () {
      final line = [(48.0, 2.0), (48.0, 2.01), (48.0, 2.02)];
      final encoded = _encode(line);
      final items = [
        {'item_type': 'activity', 'activity_id': 1},
      ];
      final byId = {
        '1': {
          'id': 1, 'name': 'Ride', 'type': 'Ride',
          'map': {'summary_polyline': encoded},
        },
      };

      final geo = buildFullGeo(items, byId);
      final features = geo['features'] as List;
      expect(features, hasLength(1));
      final props = features[0]['properties'] as Map;
      expect(props['type'], 'activity');
      expect(props['activity_id'], 1);
      expect(props['name'], 'Ride');
      expect(props['sport_type'], 'Ride');

      // Compare against decoding the same polyline directly (equivalence with
      // the existing primitive, per the task's suggested verification style).
      final expected = decodePolyline(encoded);
      final coords = features[0]['geometry']['coordinates'] as List;
      expect(coords.length, expected.length);
      for (var i = 0; i < expected.length; i++) {
        expect((coords[i][0] as num).toDouble(), closeTo(expected[i].lon, 1e-6));
        expect((coords[i][1] as num).toDouble(), closeTo(expected[i].lat, 1e-6));
      }
    });

    test('falls back to a straight line when there is no polyline', () {
      final items = [
        {'item_type': 'activity', 'activity_id': 1},
      ];
      final byId = {
        '1': {
          'id': 1, 'name': 'GPX Ride', 'type': 'Ride',
          'start_latlng': [48.0, 2.0],
          'end_latlng': [48.5, 2.5],
        },
      };
      final geo = buildFullGeo(items, byId);
      final coords =
          (geo['features'] as List)[0]['geometry']['coordinates'] as List;
      expect(coords, [
        [2.0, 48.0],
        [2.5, 48.5],
      ]);
    });

    test('skips an activity with no polyline and no start/end latlng '
        '(the encrypted-and-still-locked case)', () {
      final items = [
        {'item_type': 'activity', 'activity_id': 1},
        {'item_type': 'activity', 'activity_id': 2},
      ];
      final byId = {
        '1': {'id': 1, 'name': 'Encrypted, locked', 'type': 'Ride'},
        '2': {
          'id': 2, 'name': 'Plain', 'type': 'Ride',
          'start_latlng': [1.0, 1.0], 'end_latlng': [2.0, 2.0],
        },
      };
      final geo = buildFullGeo(items, byId);
      final features = geo['features'] as List;
      expect(features, hasLength(1));
      expect(features[0]['properties']['activity_id'], 2);
    });

    test('builds a great-circle arc for a plain segment, matching '
        'greatCirclePoints directly', () {
      final items = [
        {
          'item_type': 'segment',
          'segment': {
            'id': 's1', 'segment_type': 'flight', 'label': 'A -> B',
            'route_mode': 'great_circle',
            'start': {'lat': 10.0, 'lon': 20.0},
            'end': {'lat': 15.0, 'lon': 25.0},
          },
        },
      ];
      final geo = buildFullGeo(items, {});
      final features = geo['features'] as List;
      expect(features, hasLength(1));
      final props = features[0]['properties'] as Map;
      expect(props['type'], 'segment');
      expect(props['segment_id'], 's1');
      expect(props['route_mode'], 'great_circle');
      expect(props.containsKey('route_degraded'), isFalse);

      final expected = greatCirclePoints(10.0, 20.0, 15.0, 25.0, nPoints: 50);
      final coords = features[0]['geometry']['coordinates'] as List;
      expect(coords.length, expected.length);
      expect((coords.first[0] as num).toDouble(), closeTo(expected.first.lon, 1e-9));
      expect((coords.first[1] as num).toDouble(), closeTo(expected.first.lat, 1e-9));
    });

    test('uses the stored route_polyline for a rail segment', () {
      final items = [
        {
          'item_type': 'segment',
          'segment': {
            'id': 's2', 'segment_type': 'train', 'label': 'Train',
            'route_mode': 'rail',
            'route_polyline': '[[2.0,48.0],[2.5,48.5],[3.0,49.0]]',
            'start': {'lat': 48.0, 'lon': 2.0},
            'end': {'lat': 49.0, 'lon': 3.0},
          },
        },
      ];
      final geo = buildFullGeo(items, {});
      final coords =
          (geo['features'] as List)[0]['geometry']['coordinates'] as List;
      expect(coords, [
        [2.0, 48.0],
        [2.5, 48.5],
        [3.0, 49.0],
      ]);
    });
  });

  group('buildLowResGeo', () {
    test('represents every activity as a 2-point straight line, no polyline decode', () {
      final items = [
        {'item_type': 'activity', 'activity_id': 1},
      ];
      final byId = {
        '1': {
          'id': 1, 'name': 'Ride', 'type': 'Ride',
          // A polyline present here must be IGNORED by the low-res builder.
          'map': {'summary_polyline': _encode([(0, 0), (1, 1), (2, 2)])},
          'start_latlng': [48.0, 2.0],
          'end_latlng': [48.5, 2.5],
        },
      };
      final geo = buildLowResGeo(items, byId);
      final coords =
          (geo['features'] as List)[0]['geometry']['coordinates'] as List;
      expect(coords, [
        [2.0, 48.0],
        [2.5, 48.5],
      ]);
    });

    test('includes route_degraded on segment properties (unlike full-res)', () {
      final items = [
        {
          'item_type': 'segment',
          'segment': {
            'id': 's1', 'segment_type': 'flight', 'label': 'A -> B',
            'route_mode': 'great_circle', 'route_degraded': true,
            'start': {'lat': 10.0, 'lon': 20.0},
            'end': {'lat': 15.0, 'lon': 25.0},
          },
        },
      ];
      final geo = buildLowResGeo(items, {});
      final props = (geo['features'] as List)[0]['properties'] as Map;
      expect(props['route_degraded'], isTrue);
    });

    test('includes route_status on segment properties, so a failed resolve '
        'can be drawn distinctly on load', () {
      final items = [
        {
          'item_type': 'segment',
          'segment': {
            'id': 's1', 'segment_type': 'boat', 'label': 'A -> B',
            'route_mode': 'great_circle', 'route_status': 'failed',
            'start': {'lat': 10.0, 'lon': 20.0},
            'end': {'lat': 15.0, 'lon': 25.0},
          },
        },
      ];
      final geo = buildLowResGeo(items, {});
      final props = (geo['features'] as List)[0]['properties'] as Map;
      expect(props['route_status'], 'failed');
    });

    test('skips an activity with no start/end latlng at all', () {
      final items = [
        {'item_type': 'activity', 'activity_id': 1},
      ];
      final byId = {
        '1': {'id': 1, 'name': 'No geometry', 'type': 'Ride'},
      };
      final geo = buildLowResGeo(items, byId);
      expect(geo['features'], isEmpty);
    });
  });
}
