import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';

/// Issue #19: a day breakpoint node is drawn at the start of each day. This
/// covers the pure helper that picks which activities begin a new day.
void main() {
  Map<String, dynamic> act(int id, String date) =>
      {'id': id, 'start_date_local': '${date}T08:00:00'};
  Map<String, dynamic> actItem(int id) =>
      {'item_type': 'activity', 'activity_id': id};

  group('dayStartActivityIds', () {
    test('first activity of each distinct day is a day-start', () {
      final byId = {
        1: act(1, '2026-06-01'),
        2: act(2, '2026-06-01'),
        3: act(3, '2026-06-02'),
      };
      final items = [actItem(1), actItem(2), actItem(3)];
      expect(dayStartActivityIds(items, byId), {'1', '3'});
    });

    test('non-activity items do not advance the day', () {
      final byId = {1: act(1, '2026-06-01'), 2: act(2, '2026-06-01')};
      final items = [
        actItem(1),
        {'item_type': 'memory', 'memory': {'id': 'm'}},
        actItem(2),
      ];
      expect(dayStartActivityIds(items, byId), {'1'});
    });

    test('dateless activities are skipped', () {
      final byId = {1: {'id': 1}, 2: act(2, '2026-06-02')};
      final items = [actItem(1), actItem(2)];
      expect(dayStartActivityIds(items, byId), {'2'});
    });

    test('empty input yields no day starts', () {
      expect(dayStartActivityIds(const [], const {}), isEmpty);
    });
  });

  group('buildDayBreakpointMarkers', () {
    Map<String, dynamic> feat(int id) => {
          'properties': {'type': 'activity', 'activity_id': id},
          'geometry': {
            'coordinates': [
              [10.0, 60.0],
              [10.1, 60.1],
            ],
          },
        };

    test('one marker per day-start activity', () {
      final geo = {
        'features': [feat(1), feat(2), feat(3)]
      };
      final markers =
          buildDayBreakpointMarkers(geo, {'1', '3'}, const Color(0xFF000000));
      expect(markers.length, 2);
    });

    test('empty day-start set yields no markers', () {
      final geo = {
        'features': [feat(1)]
      };
      expect(buildDayBreakpointMarkers(geo, const {}, const Color(0xFF000000)),
          isEmpty);
    });
  });
}
