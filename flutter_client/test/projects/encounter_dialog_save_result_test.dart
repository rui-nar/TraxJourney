// Regression test for a mutation-propagation audit finding in the manual
// encounter-add flow, the same class of bug fixed for memories/journal
// entries (see memory_dialog_save_result_test.dart /
// memory_journal_create_rollback_test.dart):
//
//  - EncounterDialog._save() used to call createEncounter and pop the
//    dialog unconditionally, even though createEncounter swallowed its own
//    exceptions. A failed create looked successful to the user while a
//    phantom optimistic item (with the literal, non-unique id
//    '__optimistic__') lingered. createEncounter now returns a bool the
//    dialog checks and rolls back its own placeholder on failure — the
//    dialog stays open and shows the error inline (a SnackBar via the root
//    ScaffoldMessenger would render behind the modal barrier and never be
//    seen — see segment_dialog.dart's issue #20 fix, which this follows).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/encounter_dialog.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

// Opens EncounterDialog via a real showDialog(), like the app does — pushing
// an actual second route so Navigator.of(context).pop() inside _save()
// closes just the dialog, not the whole screen.
Widget _harness(ProjectNotifier notifier) => MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () => showDialog<void>(
              context: context,
              builder: (_) => EncounterDialog(
                notifier: notifier,
                initialDate: '2026-01-01',
              ),
            ),
            child: const Text('open'),
          ),
        ),
      ),
    );

Future<void> _settle(WidgetTester tester) async {
  for (var i = 0; i < 10; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  testWidgets(
      'a failed create keeps the dialog open, shows the error inline (not a '
      'SnackBar), and rolls back the optimistic placeholder', (tester) async {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient(
            (req) async => http.Response('{"detail":"Server exploded"}', 500)));
    final notifier = ProjectNotifier(ProjectService())
      ..ref = _ref
      ..people = [
        {'id': 1, 'name': 'Alice'},
      ];

    await tester.pumpWidget(_harness(notifier));
    await tester.tap(find.text('open'));
    await _settle(tester);

    await tester.tap(find.byKey(const Key('encounter-person-group-picker')));
    await _settle(tester);
    await tester.tap(find.text('Alice').last);
    await _settle(tester);

    await tester.tap(find.widgetWithText(ElevatedButton, 'Save'));
    await _settle(tester);

    // The dialog is still open — the failed create must not have popped it.
    expect(find.byType(AlertDialog), findsOneWidget);
    // The error is visible, inside the dialog...
    final errorFinder = find.text('Server exploded');
    expect(errorFinder, findsOneWidget);
    expect(
      find.descendant(of: find.byType(AlertDialog), matching: errorFinder),
      findsOneWidget,
    );
    // ...and not as a SnackBar (which would render behind the modal barrier).
    expect(find.widgetWithText(SnackBar, 'Server exploded'), findsNothing);
    // The optimistic placeholder must have been rolled back.
    expect(notifier.items, isEmpty);
    // Save/Cancel must be re-enabled so the user can actually retry.
    expect(
      tester
          .widget<ElevatedButton>(find.widgetWithText(ElevatedButton, 'Save'))
          .onPressed,
      isNotNull,
    );
  });

  testWidgets('a successful create closes the dialog and adds the real item',
      (tester) async {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.method == 'POST' && req.url.path == '/api/encounters/') {
            return http.Response('{}', 200);
          }
          if (req.method == 'GET' &&
              req.url.path == '/api/projects/Trip/meta') {
            return http.Response(
                '{"name":"Trip","items":[{"item_type":"encounter",'
                '"encounter":{"id":"enc-real-1","date":"2026-01-01"}}]}',
                200);
          }
          return http.Response('{}', 404);
        }));
    final notifier = ProjectNotifier(ProjectService())
      ..ref = _ref
      ..people = [
        {'id': 1, 'name': 'Alice'},
      ];

    await tester.pumpWidget(_harness(notifier));
    await tester.tap(find.text('open'));
    await _settle(tester);

    await tester.tap(find.byKey(const Key('encounter-person-group-picker')));
    await _settle(tester);
    await tester.tap(find.text('Alice').last);
    await _settle(tester);

    await tester.tap(find.widgetWithText(ElevatedButton, 'Save'));
    await _settle(tester);

    expect(find.byType(AlertDialog), findsNothing);
    expect(notifier.items, hasLength(1));
    expect(notifier.items.single['encounter']['id'], 'enc-real-1');
  });
}
