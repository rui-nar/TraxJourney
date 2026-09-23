// Regression test for a mutation-propagation audit finding in the
// person-add/edit flow, the same class of bug already fixed for
// memories/journal entries/encounters (see encounter_dialog_save_result_test.dart):
//
//  - PersonFormDialog._save() used to call createPerson/updatePerson and pop
//    the dialog unconditionally, even though both swallowed their own
//    exceptions. A failed save looked successful to the user, and `id: null`
//    was ambiguous with "user cancelled" to whatever code awaited the
//    dialog's result. createPerson/updatePerson now return a value the
//    dialog checks — the dialog stays open and shows the error inline (a
//    SnackBar would render behind the modal barrier and never be seen — see
//    segment_dialog.dart's issue #20 fix, which this follows).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/person_form_dialog.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

// Opens PersonFormDialog via a real showDialog(), like the app does — pushing
// an actual second route so Navigator.of(context).pop() inside _save() closes
// just the dialog, not the whole screen.
Widget _harness(ProjectNotifier notifier, {Map<String, dynamic>? person}) =>
    MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () => showPersonFormDialog(context, notifier,
                person: person),
            child: const Text('open'),
          ),
        ),
      ),
    );

void main() {
  testWidgets(
      'a failed create keeps the dialog open, shows the error inline (not a '
      'SnackBar), and re-enables the buttons', (tester) async {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient(
            (req) async => http.Response('{"detail":"Server exploded"}', 500)));
    final notifier = ProjectNotifier(ProjectService())..ref = _ref;

    await tester.pumpWidget(_harness(notifier));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Add'));
    await tester.pumpAndSettle();

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
    // Save/Cancel must be re-enabled so the user can actually retry.
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(FilledButton, 'Add'))
          .onPressed,
      isNotNull,
    );
    expect(
      tester
          .widget<TextButton>(find.widgetWithText(TextButton, 'Cancel'))
          .onPressed,
      isNotNull,
    );
  });

  testWidgets('a successful create closes the dialog and adds the real person',
      (tester) async {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.method == 'POST' && req.url.path == '/api/people/') {
            return http.Response('{"id": 42}', 200);
          }
          if (req.method == 'GET' &&
              req.url.path == '/api/projects/Trip/meta') {
            return http.Response(
                '{"name":"Trip","people":[{"id":42,"name":"Alice"}]}', 200);
          }
          return http.Response('{}', 404);
        }));
    final notifier = ProjectNotifier(ProjectService())..ref = _ref;

    await tester.pumpWidget(_harness(notifier));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Add'));
    await tester.pumpAndSettle();

    expect(find.byType(AlertDialog), findsNothing);
    expect(notifier.people, hasLength(1));
    expect(notifier.people.single['id'], 42);
  });

  testWidgets(
      'a failed update keeps the dialog open, shows the error inline, and '
      're-enables the buttons', (tester) async {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient(
            (req) async => http.Response('{"detail":"Server exploded"}', 500)));
    final notifier = ProjectNotifier(ProjectService())
      ..ref = _ref
      ..people = [
        {'id': 7, 'name': 'Alice'},
      ];

    await tester.pumpWidget(
        _harness(notifier, person: {'id': 7, 'name': 'Alice'}));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    expect(find.byType(AlertDialog), findsOneWidget);
    expect(find.text('Server exploded'), findsOneWidget);
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(FilledButton, 'Save'))
          .onPressed,
      isNotNull,
    );
  });
}
