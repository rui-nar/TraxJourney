// Regression test for a mutation-propagation audit finding in the
// group-add/edit flow, the same class of bug already fixed for
// memories/journal entries/encounters/people (see
// encounter_dialog_save_result_test.dart and
// person_form_dialog_save_result_test.dart):
//
//  - _GroupFormDialog._save() used to call createGroup/updateGroup/
//    setGroupMembers and pop the dialog unconditionally, even though all
//    three swallowed their own exceptions. A failed save looked successful
//    to the user. createGroup/updateGroup/setGroupMembers now return a
//    value the dialog checks — the dialog stays open and shows the error
//    inline (a SnackBar would render behind the modal barrier and never be
//    seen — see segment_dialog.dart's issue #20 fix, which this follows).
//
//  - A group create/update can succeed while the follow-up
//    setGroupMembers() call fails (the group row now exists, but its member
//    list may not reflect what the user picked). That must not be treated
//    as full success: the dialog stays open with a distinct error, and a
//    retry must update the already-created group rather than creating a
//    second one.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/group_form_dialog.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

// Opens the group dialog via a real showDialog(), like the app does —
// pushing an actual second route so Navigator.of(context).pop() inside
// _save() closes just the dialog, not the whole screen.
Widget _harness(ProjectNotifier notifier, {Map<String, dynamic>? group}) =>
    MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () => showGroupFormDialog(context, notifier,
                group: group),
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

    expect(find.byType(AlertDialog), findsOneWidget);
    final errorFinder = find.text('Server exploded');
    expect(errorFinder, findsOneWidget);
    expect(
      find.descendant(of: find.byType(AlertDialog), matching: errorFinder),
      findsOneWidget,
    );
    expect(find.widgetWithText(SnackBar, 'Server exploded'), findsNothing);
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

  testWidgets(
      'a successful create with no members closes the dialog and adds the '
      'real group', (tester) async {
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.method == 'POST' && req.url.path == '/api/groups/') {
            return http.Response('{"id": 5}', 200);
          }
          if (req.method == 'PUT' && req.url.path == '/api/groups/5/members') {
            return http.Response('{}', 200);
          }
          if (req.method == 'GET' &&
              req.url.path == '/api/projects/Trip/meta') {
            return http.Response(
                '{"name":"Trip","groups":[{"id":5,"name":"Squad"}]}', 200);
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
    expect(notifier.groups, hasLength(1));
    expect(notifier.groups.single['id'], 5);
  });

  testWidgets(
      'a group created but whose member list failed to save shows a '
      'distinct error, and retrying updates the existing group instead of '
      'creating a second one', (tester) async {
    var groupPostCount = 0;
    var membersPutCount = 0;
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.method == 'POST' && req.url.path == '/api/groups/') {
            groupPostCount++;
            return http.Response('{"id": 5}', 200);
          }
          if (req.method == 'PUT' && req.url.path == '/api/groups/5') {
            return http.Response('{}', 200);
          }
          if (req.method == 'PUT' && req.url.path == '/api/groups/5/members') {
            membersPutCount++;
            if (membersPutCount == 1) {
              return http.Response('{"detail":"members exploded"}', 500);
            }
            return http.Response('{}', 200);
          }
          if (req.method == 'GET' &&
              req.url.path == '/api/projects/Trip/meta') {
            return http.Response(
                '{"name":"Trip","groups":[{"id":5,"name":"Squad"}]}', 200);
          }
          return http.Response('{}', 404);
        }));
    final notifier = ProjectNotifier(ProjectService())..ref = _ref;

    await tester.pumpWidget(_harness(notifier));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Add'));
    await tester.pumpAndSettle();

    // The group itself was created, but its member list wasn't — this must
    // not read as full success: the dialog stays open with a distinct error.
    expect(find.byType(AlertDialog), findsOneWidget);
    expect(find.textContaining('Group saved'), findsOneWidget);
    expect(find.textContaining('members exploded'), findsOneWidget);
    expect(groupPostCount, 1);

    // Retrying must update the already-created group, not create a second
    // one, and this time the member save succeeds.
    await tester.tap(find.widgetWithText(FilledButton, 'Add'));
    await tester.pumpAndSettle();

    expect(find.byType(AlertDialog), findsNothing);
    expect(groupPostCount, 1,
        reason: 'retry must not create a second group');
    expect(membersPutCount, 2);
    expect(notifier.groups, hasLength(1));
    expect(notifier.groups.single['id'], 5);
  });
}
