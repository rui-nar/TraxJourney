import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/encounter_dialog.dart';
import 'package:traxjourney_client/src/projects/people_screen.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Guards issue #175: an encounter can be edited and deleted from the person and
/// group detail sheets, not only from the trip timeline.

/// Captures the CRUD calls the sheets make, so no network is involved (mirrors
/// encounter_dialog_test.dart's `_FakeNotifier`).
class _FakeNotifier extends ProjectNotifier {
  _FakeNotifier() : super(ProjectService());

  Map<String, dynamic>? updated;
  final deleted = <String>[];
  Map<String, dynamic>? personPayload;
  int fetchPersonCalls = 0;

  @override
  Future<bool> updateEncounter(
    String encounterId, {
    int? personId,
    int? groupId,
    required String date,
    required String geoMode,
    String? time,
    String? description,
    double? lat,
    double? lon,
  }) async {
    updated = {
      'id': encounterId,
      'personId': personId,
      'groupId': groupId,
      'date': date,
      'description': description,
    };
    return true;
  }

  @override
  Future<void> deleteEncounter(String encounterId) async {
    deleted.add(encounterId);
  }

  @override
  Future<Map<String, dynamic>?> fetchPerson(int personId) async {
    fetchPersonCalls++;
    return personPayload;
  }
}

_FakeNotifier _notifierWithGroupEncounter({String description = 'karaoke'}) {
  final n = _FakeNotifier()..ref = const ProjectRef(name: 'Trip');
  n.groups = [
    {'id': 5, 'name': 'Crew', 'nationalities': [], 'socials': []},
  ];
  n.items = [
    {
      'item_type': 'encounter',
      'encounter': {
        'id': 1,
        'person_id': null,
        'group_id': 5,
        'date': '2026-01-01',
        'time': '10:00',
        'description': description,
        'geo_mode': 'custom',
        'lat': 1.0,
        'lon': 2.0,
      },
    },
  ];
  return n;
}

/// The note text also renders on the sheet row behind the dialog, so assertions
/// about the dialog's prefilled field have to be scoped to the dialog.
Finder _inDialog(String text) => find.descendant(
    of: find.byType(EncounterDialog), matching: find.text(text));

Future<void> _openGroupSheet(WidgetTester tester, _FakeNotifier n) async {
  await tester.pumpWidget(MaterialApp(
    home: Builder(
      builder: (context) => ElevatedButton(
        onPressed: () => showGroupDetailSheet(context, n, n.groups.first),
        child: const Text('open'),
      ),
    ),
  ));
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('group sheet edit button opens the dialog and saves the encounter',
      (tester) async {
    final n = _notifierWithGroupEncounter();
    await _openGroupSheet(tester, n);

    await tester.tap(find.byKey(const Key('encounter-edit-1')));
    await tester.pumpAndSettle();

    // Edit mode, pre-filled from the row.
    expect(find.text('Edit encounter'), findsOneWidget);
    expect(_inDialog('karaoke'), findsOneWidget);

    await tester.tap(find.widgetWithText(ElevatedButton, 'Save'));
    await tester.pumpAndSettle();

    expect(n.updated, isNotNull);
    expect(n.updated!['id'], '1');
    expect(n.updated!['groupId'], 5);
    expect(n.updated!['personId'], isNull);
    expect(n.updated!['date'], '2026-01-01');
  });

  testWidgets('group sheet delete button deletes after confirmation',
      (tester) async {
    final n = _notifierWithGroupEncounter();
    await _openGroupSheet(tester, n);

    await tester.tap(find.byKey(const Key('encounter-delete-1')));
    await tester.pumpAndSettle();
    expect(find.text('Delete encounter?'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
    await tester.pumpAndSettle();

    expect(n.deleted, ['1']);
  });

  testWidgets('group sheet delete is a no-op when the confirmation is cancelled',
      (tester) async {
    final n = _notifierWithGroupEncounter();
    await _openGroupSheet(tester, n);

    await tester.tap(find.byKey(const Key('encounter-delete-1')));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
    await tester.pumpAndSettle();

    expect(n.deleted, isEmpty);
  });

  testWidgets('group sheet repaints its encounters when the notifier changes',
      (tester) async {
    // Without this the row keeps showing the pre-edit note until the sheet is
    // reopened — the sheet itself is stateless and reads notifier.items.
    final n = _notifierWithGroupEncounter(description: 'before');
    await _openGroupSheet(tester, n);
    expect(find.text('before'), findsOneWidget);

    n.items.first['encounter']['description'] = 'after';
    n.notifyListeners();
    await tester.pumpAndSettle();

    expect(find.text('after'), findsOneWidget);
    expect(find.text('before'), findsNothing);
  });

  testWidgets('person sheet edits an inherited group encounter as the group',
      (tester) async {
    // The person sheet's rows come from GET /api/people/{id}, which tags an
    // inherited encounter with group_id and a null person_id — the dialog has
    // to open on the group, and saving must keep it a group encounter.
    final n = _FakeNotifier()..ref = const ProjectRef(name: 'Trip');
    n.groups = [
      {'id': 5, 'name': 'Crew', 'nationalities': [], 'socials': []},
    ];
    n.people = [
      {'id': 9, 'name': 'Alice', 'group_id': 5, 'nationalities': [], 'socials': []},
    ];
    n.personPayload = {
      'id': 9,
      'name': 'Alice',
      'group_id': 5,
      'nationalities': [],
      'socials': [],
      'encounters': [
        {
          'id': 7,
          'person_id': null,
          'group_id': 5,
          'group_name': 'Crew',
          'source': 'group',
          'date': '2026-01-02',
          'time': null,
          'description': 'hostel night',
          'geo_mode': 'custom',
          'lat': 1.0,
          'lon': 2.0,
        },
      ],
    };

    await tester.pumpWidget(MaterialApp(
      home: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showPersonDetailSheet(context, n, n.people.first),
          child: const Text('open'),
        ),
      ),
    ));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('With Crew'), findsOneWidget);

    await tester.tap(find.byKey(const Key('encounter-edit-7')));
    await tester.pumpAndSettle();

    expect(find.text('Edit encounter'), findsOneWidget);
    expect(_inDialog('hostel night'), findsOneWidget);

    await tester.tap(find.widgetWithText(ElevatedButton, 'Save'));
    await tester.pumpAndSettle();

    expect(n.updated!['id'], '7');
    expect(n.updated!['groupId'], 5);
    expect(n.updated!['personId'], isNull);
  });
}
