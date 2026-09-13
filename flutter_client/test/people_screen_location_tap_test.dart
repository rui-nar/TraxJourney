import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/people_screen.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

ProjectNotifier _notifierWithGroupEncounters() {
  final n = ProjectNotifier(ProjectService())..ref = const ProjectRef(name: 'Trip');
  n.groups = [
    {'id': 5, 'name': 'Crew', 'nationalities': [], 'socials': []},
  ];
  n.items = [
    {
      'item_type': 'encounter',
      'encounter': {
        'id': 1,
        'group_id': 5,
        'date': '2026-01-01',
        'lat': 10.0,
        'lon': 20.0,
        'description': 'has coords',
      },
    },
    {
      'item_type': 'encounter',
      'encounter': {
        'id': 2,
        'group_id': 5,
        'date': '2026-01-02',
        'description': 'no coords',
      },
    },
  ];
  return n;
}

/// Guards issue #72: tapping an encounter's place icon on the person/group
/// detail sheet should invoke `onLocationTap` (only when the encounter has
/// coordinates), and PeopleScreen's own entry points must close their route
/// with the picked point so the caller (map/activity panel) can focus it.
void main() {
  testWidgets(
      'group encounter place icon is tappable only when lat/lon exist, and '
      'invokes onLocationTap with the right values', (tester) async {
    final notifier = _notifierWithGroupEncounters();
    final group = notifier.groups.first;
    (double, double)? tapped;

    await tester.pumpWidget(MaterialApp(
      home: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showGroupDetailSheet(context, notifier, group,
              onLocationTap: (lat, lon) => tapped = (lat, lon)),
          child: const Text('open'),
        ),
      ),
    ));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    final placeIcons = find.byIcon(Icons.place_outlined);
    expect(placeIcons, findsNWidgets(2));

    // The coord-less encounter's icon is inert: tapping it does nothing (it
    // sits inside ListTile's own always-present, but onTap:null, InkWell —
    // there is no *functional* tap handler on it, only on the icon for the
    // encounter that has coordinates).
    await tester.tap(placeIcons.at(1));
    await tester.pump();
    expect(tapped, isNull);

    // The icon for the encounter with coordinates is tappable and reports
    // its exact lat/lon.
    await tester.tap(placeIcons.at(0));
    await tester.pump();
    expect(tapped, (10.0, 20.0));
  });

  testWidgets(
      "PeopleScreen's own group entry point pops the route with the picked "
      'location (issue #72)', (tester) async {
    final notifier = _notifierWithGroupEncounters();
    dynamic poppedResult = 'not-popped-yet';

    await tester.pumpWidget(MaterialApp(
      home: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () async {
            poppedResult = await Navigator.of(context).push(MaterialPageRoute(
              builder: (_) => PeopleScreen(notifier: notifier),
            ));
          },
          child: const Text('open people'),
        ),
      ),
    ));
    await tester.tap(find.text('open people'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Groups'));
    await tester.pump();
    await tester.tap(find.text('Crew'));
    await tester.pumpAndSettle();

    await tester.tap(find.byIcon(Icons.place_outlined).first);
    await tester.pumpAndSettle();

    expect(poppedResult, {'lat': 10.0, 'lon': 20.0});
  });
}
