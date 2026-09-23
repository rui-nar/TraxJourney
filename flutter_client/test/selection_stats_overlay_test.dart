import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Drives the map selection-stats overlay (issue #74) without real network
/// calls — mirrors encounter_dialog_test.dart's `_FakeNotifier` pattern.
class _FakeNotifier extends ProjectNotifier {
  _FakeNotifier() : super(ProjectService());
}

Map<String, dynamic> _activity(int id, String startDateLocal, double distanceM, double elevM) => {
      'id': id,
      'start_date_local': startDateLocal,
      'distance': distanceM,
      'total_elevation_gain': elevM,
    };

/// Three activities, one per day, so orderedDayKeys() = [06-01, 06-02, 06-03].
_FakeNotifier _tripNotifier() => _FakeNotifier()
  ..activities = [
    _activity(1, '2024-06-01T08:00:00', 10000, 100),
    _activity(2, '2024-06-02T08:00:00', 20000, 200),
    _activity(3, '2024-06-03T08:00:00', 5000, 50),
  ]
  ..items = [
    {'item_type': 'activity', 'activity_id': 1},
    {'item_type': 'activity', 'activity_id': 2},
    {'item_type': 'activity', 'activity_id': 3},
  ];

Future<void> _pump(WidgetTester tester, ProjectNotifier notifier) async {
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(body: SelectionStatsOverlay(notifier: notifier)),
  ));
}

void main() {
  group('computeSelectionStats', () {
    test('no selection returns null', () {
      final n = _tripNotifier();
      expect(computeSelectionStats(n), isNull);
    });

    test('single activity selected reports its own distance/climb/day', () {
      final n = _tripNotifier()..selectedActivityId = 1;
      final s = computeSelectionStats(n)!;
      expect(s.distanceKm, 10);
      expect(s.elevationM, 100);
      expect(s.dayLabel, 'Day 1');
    });

    test('single day selected uses dayStats and its day number', () {
      final n = _tripNotifier()..selectedDays = {'2024-06-02'};
      final s = computeSelectionStats(n)!;
      expect(s.distanceKm, 20);
      expect(s.elevationM, 200);
      expect(s.dayLabel, 'Day 2');
    });

    test('single day selected via plain click (selectedDay, not selectedDays)',
        () {
      // A plain click on a day header sets the singular `selectedDay` field
      // (see activity_panel.dart), distinct from the multi-select `selectedDays`
      // set. The overlay must honour both (issue #74 reopened).
      final n = _tripNotifier()..selectedDay = '2024-06-02';
      final s = computeSelectionStats(n)!;
      expect(s.distanceKm, 20);
      expect(s.elevationM, 200);
      expect(s.dayLabel, 'Day 2');
    });

    test('contiguous multi-day selection sums stats and shows a range', () {
      final n = _tripNotifier()..selectedDays = {'2024-06-01', '2024-06-02'};
      final s = computeSelectionStats(n)!;
      expect(s.distanceKm, 30);
      expect(s.elevationM, 300);
      expect(s.dayLabel, 'Days 1–2');
    });

    test('non-contiguous multi-day selection falls back to a count', () {
      final n = _tripNotifier()..selectedDays = {'2024-06-01', '2024-06-03'};
      final s = computeSelectionStats(n)!;
      expect(s.distanceKm, 15);
      expect(s.elevationM, 150);
      expect(s.dayLabel, '2 days selected');
    });
  });

  group('SelectionStatsOverlay widget', () {
    testWidgets('renders nothing when no selection is active', (tester) async {
      await _pump(tester, _tripNotifier());
      expect(find.text('DIST'), findsNothing);
      expect(find.text('CLIMB'), findsNothing);
    });

    testWidgets('shows distance, climb and day for a selected activity',
        (tester) async {
      await _pump(tester, _tripNotifier()..selectedActivityId = 1);
      expect(find.text('DIST'), findsOneWidget);
      expect(find.text('10'), findsOneWidget);
      expect(find.text('CLIMB'), findsOneWidget);
      expect(find.text('100'), findsOneWidget);
      expect(find.text('Day 1'), findsOneWidget);
    });

    testWidgets('shows distance, climb and day for a selected day',
        (tester) async {
      await _pump(tester, _tripNotifier()..selectedDays = {'2024-06-02'});
      expect(find.text('20'), findsOneWidget);
      expect(find.text('200'), findsOneWidget);
      expect(find.text('Day 2'), findsOneWidget);
    });

    testWidgets('shows distance, climb and day for a plain single-day click',
        (tester) async {
      await _pump(tester, _tripNotifier()..selectedDay = '2024-06-02');
      expect(find.text('20'), findsOneWidget);
      expect(find.text('200'), findsOneWidget);
      expect(find.text('Day 2'), findsOneWidget);
    });

    testWidgets('shows summed distance/climb and a range for a multi-day selection',
        (tester) async {
      await _pump(
          tester, _tripNotifier()..selectedDays = {'2024-06-01', '2024-06-02'});
      expect(find.text('30'), findsOneWidget);
      expect(find.text('300'), findsOneWidget);
      expect(find.text('Days 1–2'), findsOneWidget);
    });
  });
}
