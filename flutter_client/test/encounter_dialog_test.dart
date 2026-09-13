import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/encounter_dialog.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Captures create/update payloads — so the dialog is tested without network
/// (mirrors person_form_dialog_test.dart's _FakeNotifier pattern).
class _FakeNotifier extends ProjectNotifier {
  _FakeNotifier() : super(ProjectService());

  Map<String, dynamic>? created;
  Map<String, dynamic>? updated;

  @override
  Future<bool> createEncounter({
    int? personId,
    int? groupId,
    required String date,
    required String geoMode,
    String? time,
    String? description,
    double? lat,
    double? lon,
    int? insertAfterIndex,
  }) async {
    created = {'personId': personId, 'groupId': groupId, 'date': date};
    return true;
  }

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
      'id': encounterId, 'personId': personId, 'groupId': groupId, 'date': date,
    };
    return true;
  }
}

Future<void> _pump(
  WidgetTester tester,
  _FakeNotifier notifier, {
  Map<String, dynamic>? editEntry,
}) async {
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Center(child: EncounterDialog(
        notifier: notifier,
        editEntry: editEntry,
        initialDate: '2024-06-01',
      )),
    ),
  ));
}

final _picker = find.byKey(const Key('encounter-person-group-picker'));
Finder get _save => find.widgetWithText(ElevatedButton, 'Save');

/// Bounded settle: `pumpAndSettle` never returns in "add" mode because the
/// device-location fetch shows an indeterminate (indefinitely-animating)
/// spinner while it awaits a platform channel with no test handler. Pump a
/// fixed number of frames instead — enough to cover the dropdown menu's
/// ~300ms open/close transition.
Future<void> _settle(WidgetTester tester) async {
  for (var i = 0; i < 10; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  Map<String, dynamic> person(int id, String name) => {'id': id, 'name': name};
  Map<String, dynamic> group(int id, String name) => {'id': id, 'name': name};

  testWidgets('selecting a person and saving sends personId, no groupId',
      (tester) async {
    final n = _FakeNotifier()..people = [person(1, 'Alice')];
    await _pump(tester, n);

    await tester.tap(_picker);
    await _settle(tester);
    await tester.tap(find.text('Alice').last);
    await _settle(tester);

    await tester.tap(_save);
    await _settle(tester);

    expect(n.created, isNotNull);
    expect(n.created!['personId'], 1);
    expect(n.created!['groupId'], isNull);
  });

  testWidgets('selecting a group and saving sends groupId, no personId',
      (tester) async {
    final n = _FakeNotifier()
      ..people = [person(1, 'Alice')]
      ..groups = [group(10, 'Crew')];
    await _pump(tester, n);

    await tester.tap(_picker);
    await _settle(tester);
    await tester.tap(find.text('Crew').last);
    await _settle(tester);

    await tester.tap(_save);
    await _settle(tester);

    expect(n.created, isNotNull);
    expect(n.created!['groupId'], 10);
    expect(n.created!['personId'], isNull);
  });

  testWidgets('section headers render but are not selectable', (tester) async {
    final n = _FakeNotifier()
      ..people = [person(1, 'Alice')]
      ..groups = [group(10, 'Crew')];
    await _pump(tester, n);

    await tester.tap(_picker);
    await _settle(tester);
    expect(find.text('GROUPS'), findsOneWidget);
    expect(find.text('PEOPLE'), findsOneWidget);

    // Tapping a disabled header item doesn't select anything — the menu
    // stays open (no selection made), so close it and confirm save is
    // still blocked (nothing picked).
    await tester.tap(find.text('GROUPS'));
    await _settle(tester);
    await tester.tapAt(const Offset(10, 10)); // dismiss the menu if still open
    await _settle(tester);

    await tester.tap(_save);
    await _settle(tester);
    expect(find.text('Pick a person or group'), findsOneWidget);
    expect(n.created, isNull);
  });

  testWidgets('editing an existing group-encounter prefills the group',
      (tester) async {
    final n = _FakeNotifier()
      ..people = [person(1, 'Alice')]
      ..groups = [group(10, 'Crew')];
    await _pump(tester, n, editEntry: {
      'id': 5,
      'group_id': 10,
      'date': '2024-06-01',
      'description': 'met the crew',
    });

    expect(find.text('Crew'), findsOneWidget);

    await tester.tap(_save);
    await _settle(tester);
    expect(n.updated, isNotNull);
    expect(n.updated!['id'], '5');
    expect(n.updated!['groupId'], 10);
    expect(n.updated!['personId'], isNull);
  });

  testWidgets(
      "new encounter defaults the pin to the selected day's activity location, "
      'not the device GPS', (tester) async {
    final n = _FakeNotifier()
      ..people = [person(1, 'Alice')]
      ..activities = [
        {
          'id': 1,
          'start_date_local': '2024-06-01T08:00:00',
          'start_latlng': [48.8566, 2.3522],
        },
      ];
    await _pump(tester, n);
    await _settle(tester);

    expect(find.textContaining('48.85660, 2.35220'), findsOneWidget);
  });

  testWidgets('save is blocked with a snackbar when nothing is selected',
      (tester) async {
    final n = _FakeNotifier()..people = [person(1, 'Alice')];
    await _pump(tester, n);

    await tester.tap(_save);
    await _settle(tester);

    expect(find.text('Pick a person or group'), findsOneWidget);
    expect(n.created, isNull);
  });
}
