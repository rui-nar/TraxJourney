/// A live filter whose last match has gone keeps the chip that turns it off
/// (#409).
///
/// Restore prunes a stale saved filter, but a filter that goes stale while the
/// trip is open never passes through restore: filter to hikes, delete the last
/// hike, open the sheet. It used to offer only what the trip holds, so there
/// was no 'Hike' chip — and on a trip with no transport at all, no
/// Transportation section — to untick the filter that was emptying the list.
/// The Source section got this in #406; these pin it for the rest.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// A trip with a ride, no transport, no tags, and a hotel night.
ProjectNotifier _notifier() {
  final n = ProjectNotifier(ProjectService());
  n.activities = [
    {
      'id': 1,
      'name': 'Morning ride',
      'type': 'Ride',
      'start_date_local': '2026-06-01T09:00:00',
    },
  ];
  n.items = [
    {'item_type': 'activity', 'activity_id': 1},
  ];
  n.dayMeta = {
    '2026-06-01': {'sleeping': 'Hotel'},
  };
  return n;
}

Future<void> _pumpSheet(WidgetTester tester, ProjectNotifier notifier) =>
    tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: FilterSheet(notifier: notifier, readOnly: false),
      ),
    ));

/// Asserts [section] is on screen with a selected [chip], taps it, and asserts
/// that left no filter behind.
Future<void> _untick(WidgetTester tester, ProjectNotifier notifier,
    {required String section, required String chip}) async {
  expect(find.text(section), findsOneWidget);
  final finder = find.widgetWithText(FilterChip, chip);
  expect(finder, findsOneWidget);
  expect(tester.widget<FilterChip>(finder).selected, isTrue);

  await tester.tap(finder);
  await tester.pumpAndSettle();

  expect(notifier.hasActiveFilter, isFalse);
}

void main() {
  testWidgets('an activity type the trip no longer holds keeps its chip',
      (tester) async {
    final notifier = _notifier()..setFilters(activityTypes: {'hike'});
    await _pumpSheet(tester, notifier);

    // Offered beside the types the trip does hold, not instead of them.
    expect(find.widgetWithText(FilterChip, 'Ride'), findsOneWidget);
    await _untick(tester, notifier, section: 'Activity type', chip: 'Hike');
  });

  testWidgets('a sleeping mode the trip no longer holds keeps its chip',
      (tester) async {
    final notifier = _notifier()..setFilters(sleeping: {'Camping'});
    await _pumpSheet(tester, notifier);

    expect(find.widgetWithText(FilterChip, 'Hotel'), findsOneWidget);
    await _untick(tester, notifier, section: 'Sleeping mode', chip: 'Camping');
  });

  testWidgets("a stale sleeping mode goes before 'No data', which stays last",
      (tester) async {
    List<String?> chipLabels() => tester
        .widgetList<FilterChip>(find.byType(FilterChip))
        .map((c) => (c.label as Text).data)
        .toList();

    // One hotel night and one unset: the trip offers [Hotel, No data].
    final notifier = _notifier()
      ..dayMeta['2026-06-02'] = <String, dynamic>{}
      ..setFilters(sleeping: {'Camping'});
    await _pumpSheet(tester, notifier);

    // Sleeping mode comes first on this trip (no tags); Ride is the type.
    expect(chipLabels(), ['Hotel', 'Camping', 'No data', 'Ride']);

    // And a stale 'No data' itself — every night now set — still goes last,
    // even after a stale mode that sorts after it: sorted with the other
    // stale values it would land before 'Shelter'.
    notifier
      ..dayMeta['2026-06-02'] = {'sleeping': 'Hostel'}
      ..setFilters(sleeping: {'Shelter', 'No data'});
    await tester.pumpAndSettle();

    expect(chipLabels(), ['Hostel', 'Hotel', 'Shelter', 'No data', 'Ride']);
  });

  testWidgets('a transport filter keeps its section on a trip with none left',
      (tester) async {
    // The trip holds no segments at all, so the section used to vanish whole.
    final notifier = _notifier()..setFilters(transport: {'flight'});
    await _pumpSheet(tester, notifier);

    await _untick(tester, notifier, section: 'Transportation', chip: 'Flight');
  });

  testWidgets('a tag filter keeps its section on a trip with no tags left',
      (tester) async {
    final notifier = _notifier()..setFilters(tags: {'museum'});
    await _pumpSheet(tester, notifier);

    await _untick(tester, notifier, section: 'Tags', chip: 'museum');
  });

  testWidgets('with nothing filtered, an empty dimension still has no section',
      (tester) async {
    await _pumpSheet(tester, _notifier());

    // The sheet did build: what the trip holds is on screen.
    expect(find.text('Filter'), findsOneWidget);
    expect(find.text('Sleeping mode'), findsOneWidget);
    expect(find.text('Activity type'), findsOneWidget);
    expect(find.text('Tags'), findsNothing);
    expect(find.text('Transportation'), findsNothing);
  });
}
