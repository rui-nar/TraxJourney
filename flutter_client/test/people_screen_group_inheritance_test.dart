// Guards issue #124: a person inherits the encounters logged against the group
// they belong to — visible both in the People list (encounter count, search)
// and in their detail sheet, where inherited rows are labelled with the group.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/people_screen.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// A notifier whose `fetchPerson` is answered locally, so the person detail
/// sheet can be pumped without a server (the API client is app-wide).
class _StubNotifier extends ProjectNotifier {
  final Map<String, dynamic> personPayload;
  _StubNotifier(this.personPayload) : super(ProjectService());

  @override
  Future<Map<String, dynamic>?> fetchPerson(int personId) async =>
      personPayload;
}

ProjectNotifier _listNotifier() {
  final n = ProjectNotifier(ProjectService())..ref = const ProjectRef(name: 'Trip');
  n.people = [
    {'id': 1, 'name': 'Alice', 'group_id': 5},
    {'id': 2, 'name': 'Bob'}, // ungrouped
  ];
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
        'description': 'karaoke night',
      },
    },
  ];
  return n;
}

void main() {
  testWidgets('a group member\'s encounter count includes the group\'s',
      (tester) async {
    await tester
        .pumpWidget(MaterialApp(home: PeopleScreen(notifier: _listNotifier())));
    await tester.pump();

    // Alice belongs to the group that was met once; Bob has nothing.
    expect(find.text('1 encounter'), findsOneWidget);
    expect(
      find.descendant(
          of: find.widgetWithText(ListTile, 'Alice'),
          matching: find.text('1 encounter')),
      findsOneWidget,
    );
  });

  testWidgets('search matches a note on the group\'s encounter',
      (tester) async {
    await tester
        .pumpWidget(MaterialApp(home: PeopleScreen(notifier: _listNotifier())));
    await tester.pump();

    await tester.enterText(find.byType(TextField).first, 'karaoke');
    await tester.pump();

    expect(find.text('Alice'), findsOneWidget);
    expect(find.text('Bob'), findsNothing);
  });

  testWidgets('person detail sheet lists inherited encounters, labelled '
      'with the group', (tester) async {
    final notifier = _StubNotifier({
      'id': 1,
      'name': 'Alice',
      'group_id': 5,
      'encounters': [
        {
          'id': 9,
          'date': '2026-01-01',
          'description': 'coffee',
          'source': 'person',
          'group_id': null,
          'group_name': null,
        },
        {
          'id': 1,
          'date': '2026-01-02',
          'description': 'karaoke night',
          'source': 'group',
          'group_id': 5,
          'group_name': 'Crew',
        },
      ],
    });

    await tester.pumpWidget(MaterialApp(
      home: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showPersonDetailSheet(
              context, notifier, {'id': 1, 'name': 'Alice'}),
          child: const Text('open'),
        ),
      ),
    ));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    // Both dates are listed…
    expect(find.text('2026-01-01'), findsOneWidget);
    expect(find.text('2026-01-02'), findsOneWidget);
    // …with their details, and only the inherited one names the group.
    expect(find.text('coffee'), findsOneWidget);
    expect(find.text('karaoke night'), findsOneWidget);
    expect(find.text('With Crew'), findsOneWidget);
  });
}
