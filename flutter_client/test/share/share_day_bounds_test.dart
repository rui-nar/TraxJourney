import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';
import 'package:traxjourney_client/src/share/share_day_bounds.dart';

void main() {
  Map<String, dynamic> activityFeature(String id, List<List<double>> lonLat) => {
        'properties': {'type': 'activity', 'activity_id': id},
        'geometry': {'type': 'LineString', 'coordinates': lonLat},
      };

  Map<String, dynamic> segmentFeature(String id, List<List<double>> lonLat) => {
        'properties': {'type': 'segment', 'segment_id': id},
        'geometry': {'type': 'LineString', 'coordinates': lonLat},
      };

  group('dayRoutePoints', () {
    test('returns empty when geo is null', () {
      expect(
        dayRoutePoints(geo: null, items: const [], activities: const [], date: '2026-05-29'),
        isEmpty,
      );
    });

    test('collects only the matching day activity points', () {
      final geo = {
        'features': [
          activityFeature('1', [
            [10.0, 50.0],
            [11.0, 51.0],
          ]),
          activityFeature('2', [
            [20.0, 60.0],
          ]),
        ],
      };
      final activities = [
        {'id': 1, 'start_date_local': '2026-05-29T09:00:00'},
        {'id': 2, 'start_date_local': '2026-05-30T09:00:00'},
      ];
      final items = [
        {'item_type': 'activity', 'activity_id': 1},
        {'item_type': 'activity', 'activity_id': 2},
      ];

      final pts = dayRoutePoints(
        geo: geo, items: items, activities: activities, date: '2026-05-29');

      expect(pts, [const LatLng(50.0, 10.0), const LatLng(51.0, 11.0)]);
    });

    test('includes segments dated to the day (and inherits prior activity date)', () {
      final geo = {
        'features': [
          activityFeature('1', [
            [1.0, 2.0],
          ]),
          segmentFeature('9', [
            [3.0, 4.0],
            [5.0, 6.0],
          ]),
        ],
      };
      final activities = [
        {'id': 1, 'start_date_local': '2026-05-29T09:00:00'},
      ];
      final items = [
        {'item_type': 'activity', 'activity_id': 1},
        // Segment without explicit date → inherits 2026-05-29 from activity.
        {'item_type': 'segment', 'segment': {'id': 9}},
      ];

      final pts = dayRoutePoints(
        geo: geo, items: items, activities: activities, date: '2026-05-29');

      expect(pts, contains(const LatLng(2.0, 1.0)));
      expect(pts, contains(const LatLng(4.0, 3.0)));
      expect(pts, contains(const LatLng(6.0, 5.0)));
    });

    test('returns empty when nothing matches the date', () {
      final geo = {
        'features': [
          activityFeature('1', [
            [1.0, 2.0],
          ]),
        ],
      };
      final activities = [
        {'id': 1, 'start_date_local': '2026-05-29T09:00:00'},
      ];
      final items = [
        {'item_type': 'activity', 'activity_id': 1},
      ];

      expect(
        dayRoutePoints(
            geo: geo, items: items, activities: activities, date: '2026-12-31'),
        isEmpty,
      );
    });
  });
}
