import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/people_screen.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_people_crud_mixin.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// A notifier whose Polarsteps calls resolve locally — the real ones go through
/// the global `api` singleton, which no widget test should reach.
class _FakeNotifier extends ProjectNotifier {
  _FakeNotifier() : super(ProjectService());

  int shownTripId = 0;

  @override
  Future<Map<String, dynamic>?> fetchPerson(int personId) async =>
      people.firstWhere((p) => p['id'] == personId);

  @override
  Future<List<Map<String, dynamic>>?> fetchPersonPolarstepsTrips(
          int personId) async =>
      [
        {'id': 7, 'name': 'Asia 2024', 'start_date': '2024-01-01', 'steps_count': 2},
      ];

  @override
  Future<bool> showPersonPolarstepsTrip(
      int personId, int tripId, String label) async {
    shownTripId = tripId;
    polarstepsOverlaySteps = [
      {'lat': 1.0, 'lon': 2.0},
      {'lat': 3.0, 'lon': 4.0},
    ];
    polarstepsOverlayLabel = label;
    notifyListeners();
    return true;
  }
}

_FakeNotifier _notifier() {
  final n = _FakeNotifier()..ref = const ProjectRef(name: 'Trip');
  n.people = [
    {'id': 1, 'name': 'Alice', 'polarsteps': 'alice'},
  ];
  return n;
}

/// Opens PeopleScreen, drills into Alice and loads her trip list.
Future<void> _openAliceTrips(WidgetTester tester, ProjectNotifier notifier,
    void Function(dynamic result) onPopped) async {
  await tester.pumpWidget(MaterialApp(
    home: Builder(
      builder: (context) => ElevatedButton(
        onPressed: () async {
          onPopped(await Navigator.of(context).push(MaterialPageRoute(
            builder: (_) => PeopleScreen(notifier: notifier),
          )));
        },
        child: const Text('open people'),
      ),
    ),
  ));
  await tester.tap(find.text('open people'));
  await tester.pumpAndSettle();

  await tester.tap(find.text('Alice'));
  await tester.pumpAndSettle();

  await tester.tap(find.text('Show trips'));
  await tester.pumpAndSettle();
}

/// Guards issue #123: picking one of a person's Polarsteps trips left the
/// full-screen People directory covering the map the overlay had just been
/// drawn on, so the tap looked like it did nothing.
void main() {
  testWidgets('picking a trip from PeopleScreen closes the sheet AND the route',
      (tester) async {
    final notifier = _notifier();
    dynamic popped = 'not-popped-yet';

    await _openAliceTrips(tester, notifier, (r) => popped = r);
    expect(find.text('Asia 2024'), findsOneWidget);

    await tester.tap(find.text('Asia 2024'));
    await tester.pumpAndSettle();

    expect(notifier.shownTripId, 7);
    expect(notifier.polarstepsOverlaySteps, hasLength(2));
    // Both the sheet and PeopleScreen are gone — the map is visible again.
    expect(find.byType(PeopleScreen), findsNothing);
    expect(find.text('open people'), findsOneWidget);
    expect(popped, isNull);
  });

  testWidgets(
      'a sheet opened over the map (no onTripShown) only closes itself',
      (tester) async {
    final notifier = _notifier();

    await tester.pumpWidget(MaterialApp(
      home: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () =>
              showPersonDetailSheet(context, notifier, notifier.people.first),
          child: const Text('map pin'),
        ),
      ),
    ));
    await tester.tap(find.text('map pin'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Show trips'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Asia 2024'));
    await tester.pumpAndSettle();

    // The sheet is gone; the underlying screen was never popped.
    expect(find.text('Asia 2024'), findsNothing);
    expect(find.text('map pin'), findsOneWidget);
  });

  group('mappablePolarstepsSteps', () {
    test('keeps only steps that carry coordinates, in order', () {
      final steps = mappablePolarstepsSteps([
        {'id': 1, 'lat': 1.0, 'lon': 2.0},
        {'id': 2, 'lat': null, 'lon': 4.0},
        {'id': 3},
        {'id': 4, 'lat': 5.0, 'lon': 6.0},
      ]);
      expect(steps.map((s) => s['id']), [1, 4]);
    });

    test('a trip with no mapped steps yields an empty list, not an overlay', () {
      expect(mappablePolarstepsSteps([{'id': 1}, {'id': 2, 'lon': 4.0}]), isEmpty);
      expect(mappablePolarstepsSteps(const []), isEmpty);
    });
  });
}
