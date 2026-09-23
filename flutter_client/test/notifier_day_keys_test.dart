// Unit tests for ProjectNotifier.orderedDayKeys() and activeDayKey() — the
// day-resolution the add-FAB relies on. orderedDayKeys is the full-trip day
// list (union of day-meta days and every dated item's day, ascending, matching
// the activity panel's day headers — issue #370); activeDayKey picks the FAB's
// default day: today while the trip is active, else the last trip day.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

ProjectNotifier _notifier({
  Map<String, Map<String, dynamic>> dayMeta = const {},
  List<Map<String, dynamic>> activities = const [],
  List<Map<String, dynamic>> items = const [],
  String? tripEnd,
}) {
  return ProjectNotifier(ProjectService())
    ..dayMeta = {for (final e in dayMeta.entries) e.key: e.value}
    ..activities = List.of(activities)
    ..items = List.of(items)
    ..tripEnd = tripEnd;
}

String _ymd(DateTime d) =>
    '${d.year.toString().padLeft(4, '0')}-'
    '${d.month.toString().padLeft(2, '0')}-'
    '${d.day.toString().padLeft(2, '0')}';

void main() {
  group('orderedDayKeys', () {
    test('is empty when the project has no days', () {
      expect(_notifier().orderedDayKeys(), isEmpty);
    });

    test('unions day-meta, activity and memory dates, sorted ascending, deduped',
        () {
      final n = _notifier(
        dayMeta: {'2025-06-03': {}},
        activities: [
          {'start_date_local': '2025-06-01T08:30:00'},
          {'start_date_local': '2025-06-03T10:00:00'}, // dup with dayMeta day
        ],
        items: [
          {
            'item_type': 'memory',
            'memory': {'date': '2025-06-02'},
          },
          {'item_type': 'activity', 'activity_id': '1'}, // ignored (no memory)
        ],
      );
      expect(
        n.orderedDayKeys(),
        ['2025-06-01', '2025-06-02', '2025-06-03'],
      );
    });

    // Issue #370: the activity panel gives a day header to *every* dated
    // item, so a day whose only content is a journal/encounter/segment used to
    // render on screen while being absent from this list — the add-FAB, the
    // day carousel and day numbering all disagreed with the panel.
    test('includes a day whose only content is a journal entry', () {
      final n = _notifier(items: [
        {
          'item_type': 'journal',
          'journal': {'date': '2025-06-04'},
        },
      ]);
      expect(n.orderedDayKeys(), ['2025-06-04']);
    });

    test('includes a day whose only content is an encounter', () {
      final n = _notifier(items: [
        {
          'item_type': 'encounter',
          'encounter': {'date': '2025-06-05'},
        },
      ]);
      expect(n.orderedDayKeys(), ['2025-06-05']);
    });

    test('includes a day whose only content is a dated segment', () {
      final n = _notifier(items: [
        {
          'item_type': 'segment',
          'segment': {'date': '2025-06-06'},
        },
      ]);
      expect(n.orderedDayKeys(), ['2025-06-06']);
    });

    test('an undated item adds no day', () {
      final n = _notifier(items: [
        {'item_type': 'journal', 'journal': <String, dynamic>{}},
        {'item_type': 'segment', 'segment': null},
      ]);
      expect(n.orderedDayKeys(), isEmpty);
    });

    test('journal/encounter/segment days sort in with the activity, memory '
        'and day-meta days', () {
      final n = _notifier(
        dayMeta: {'2025-06-05': {}},
        activities: [
          {'start_date_local': '2025-06-01T08:30:00'},
        ],
        items: [
          {
            'item_type': 'segment',
            'segment': {'date': '2025-06-04'},
          },
          {
            'item_type': 'memory',
            'memory': {'date': '2025-06-02'},
          },
          {
            'item_type': 'journal',
            'journal': {'date': '2025-06-03'},
          },
          {
            'item_type': 'encounter',
            'encounter': {'date': '2025-06-03'}, // dup with the journal day
          },
        ],
      );
      expect(n.orderedDayKeys(), [
        '2025-06-01',
        '2025-06-02',
        '2025-06-03',
        '2025-06-04',
        '2025-06-05',
      ]);
    });

    test('reflects a later reassignment of activities/items/dayMeta — '
        'guards the identical()-based cache against staleness', () {
      final n = _notifier(activities: [
        {'start_date_local': '2025-06-01T08:30:00'},
      ]);
      expect(n.orderedDayKeys(), ['2025-06-01']);

      n.activities = [
        {'start_date_local': '2025-06-01T08:30:00'},
        {'start_date_local': '2025-06-09T08:30:00'},
      ];
      expect(n.orderedDayKeys(), ['2025-06-01', '2025-06-09']);

      n.dayMeta = {'2025-06-15': {}};
      expect(n.orderedDayKeys(), ['2025-06-01', '2025-06-09', '2025-06-15']);

      n.items = [
        {
          'item_type': 'memory',
          'memory': {'date': '2025-06-20'},
        },
      ];
      expect(n.orderedDayKeys(),
          ['2025-06-01', '2025-06-09', '2025-06-15', '2025-06-20']);
    });
  });

  group('activeDayKey', () {
    test('returns today when the trip is still active (no end date)', () {
      final n = _notifier(
        dayMeta: {'2020-01-01': {}}, // a stale past day exists
      );
      expect(n.activeDayKey(), _ymd(DateTime.now()));
    });

    test('returns the last trip day when the trip has ended', () {
      final n = _notifier(
        dayMeta: {'2025-06-01': {}, '2025-06-05': {}, '2025-06-03': {}},
        tripEnd: '2025-06-05',
      );
      expect(n.activeDayKey(), '2025-06-05');
    });

    test('returns null when the trip has ended and there are no days', () {
      final n = _notifier(tripEnd: '2025-06-05');
      expect(n.activeDayKey(), isNull);
    });

    // Issue #370: the FAB used to default to the last *activity* day even
    // though the panel was already showing a later journal-only day.
    test('returns a trailing journal-only day when the trip has ended', () {
      final n = _notifier(
        activities: [
          {'start_date_local': '2025-06-01T08:30:00'},
        ],
        items: [
          {
            'item_type': 'journal',
            'journal': {'date': '2025-06-03'},
          },
        ],
        tripEnd: '2025-06-05',
      );
      expect(n.activeDayKey(), '2025-06-03');
    });
  });

  group('dayTripNumbering over orderedDayKeys', () {
    // Issue #370: the panel numbers its headers over every dated item, so a
    // trailing journal day is "Day 3 of 3" there. Everything numbering off
    // orderedDayKeys (the carousel, the map selection overlay) used to call
    // the same trip 2 days long.
    test('counts a trailing journal-only day in the trip total', () {
      final n = _notifier(
        activities: [
          {'start_date_local': '2025-06-01T08:30:00'},
        ],
        items: [
          {
            'item_type': 'journal',
            'journal': {'date': '2025-06-03'},
          },
        ],
      );
      final keys = n.orderedDayKeys();
      expect(dayTripNumbering('2025-06-01', keys, null),
          (dayNumber: 1, totalDays: 3));
      expect(dayTripNumbering('2025-06-03', keys, null),
          (dayNumber: 3, totalDays: 3));
    });
  });
}
