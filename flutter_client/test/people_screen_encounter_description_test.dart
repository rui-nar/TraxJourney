import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/people_screen.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _longNote =
    'We met at the hostel common room after a long day of hiking and ended up '
    'talking for hours about our respective trips, favorite trails, the gear '
    'we wished we had brought, and swapped recommendations for the next leg '
    'of the journey through the mountains.';

ProjectNotifier _notifierWithGroupEncounter(String description) {
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
        'description': description,
      },
    },
  ];
  return n;
}

/// Guards issue #73: a long encounter note clamps to 2 lines and shows a
/// "Read more" that opens the full text in a dialog; a short note shows no
/// "Read more" at all.
void main() {
  testWidgets('long description shows Read more and the dialog shows the full text',
      (tester) async {
    final notifier = _notifierWithGroupEncounter(_longNote);
    final group = notifier.groups.first;

    await tester.pumpWidget(MaterialApp(
      home: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showGroupDetailSheet(context, notifier, group),
          child: const Text('open'),
        ),
      ),
    ));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('Read more'), findsOneWidget);

    await tester.tap(find.text('Read more'));
    await tester.pumpAndSettle();

    // The dialog shows the full, untruncated text (the clamped tile behind
    // it also renders the same string data via maxLines/ellipsis, so scope
    // the search to the dialog itself).
    expect(
      find.descendant(
          of: find.byType(AlertDialog), matching: find.text(_longNote)),
      findsOneWidget,
    );
    expect(find.text('Close'), findsOneWidget);

    await tester.tap(find.text('Close'));
    await tester.pumpAndSettle();

    expect(find.text('Close'), findsNothing);
  });

  testWidgets('short description shows no Read more', (tester) async {
    final notifier = _notifierWithGroupEncounter('short note');
    final group = notifier.groups.first;

    await tester.pumpWidget(MaterialApp(
      home: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showGroupDetailSheet(context, notifier, group),
          child: const Text('open'),
        ),
      ),
    ));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('short note'), findsOneWidget);
    expect(find.text('Read more'), findsNothing);
  });
}
