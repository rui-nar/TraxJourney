/// Importing a trip under a name the user already has (issue #452).
///
/// The server refuses with 409 `name_conflict` and the name; the import
/// screen then asks "Keep both" / "Replace" / "Cancel" and retries with the
/// choice as `on_conflict`.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/projects/import_conflict_dialog.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';

http.Response _conflict(String name) => http.Response(
      jsonEncode({
        'detail': 'You already have a trip with this name.',
        'code': 'name_conflict',
        'name': name,
        'request_id': 'abcd1234',
      }),
      409,
      headers: {'content-type': 'application/json'},
    );

void main() {
  group('ProjectsNotifier.uploadProjectFile', () {
    test('a name conflict is surfaced as a choice, not an error', () async {
      final notifier = ProjectsNotifier(ProjectsService());

      final saved = await http.runWithClient(
        () => notifier.uploadProjectFile(bytes: [1], name: 'Alps'),
        () => MockClient((_) async => _conflict('Alps')),
      );

      expect(saved, isNull);
      expect(notifier.nameConflict, 'Alps');
      expect(notifier.error, isNull);
      expect(notifier.quotaError, isNull);
      expect(notifier.isLoading, isFalse);

      notifier.clearNameConflict();
      expect(notifier.nameConflict, isNull);
    });

    for (final (choice, sent) in [
      (ImportConflictChoice.keepBoth, 'copy'),
      (ImportConflictChoice.replace, 'replace'),
    ]) {
      test('$choice is sent as on_conflict=$sent', () async {
        final notifier = ProjectsNotifier(ProjectsService());
        Uri? url;

        final saved = await http.runWithClient(
          () => notifier.uploadProjectFile(
              bytes: [1], name: 'Alps', onConflict: choice),
          () => MockClient((req) async {
            if (req.method == 'POST') {
              url = req.url;
              return http.Response(
                  jsonEncode({'name': 'Alps (2)', 'outcome': 'copied'}), 201);
            }
            return http.Response('[]', 200); // the reload of the list
          }),
        );

        expect(url!.path, '/api/projects/import');
        expect(url!.queryParameters['on_conflict'], sent);
        expect(saved, 'Alps (2)');
        expect(notifier.nameConflict, isNull);
      });
    }

    test('no choice sends no on_conflict', () async {
      final notifier = ProjectsNotifier(ProjectsService());
      Uri? url;

      await http.runWithClient(
        () => notifier.uploadProjectFile(bytes: [1], name: 'Alps'),
        () => MockClient((req) async {
          if (req.method == 'POST') url = req.url;
          return req.method == 'POST'
              ? http.Response(jsonEncode({'name': 'Alps', 'outcome': 'created'}), 201)
              : http.Response('[]', 200);
        }),
      );

      expect(url!.queryParameters.containsKey('on_conflict'), isFalse);
    });
  });

  group('showImportConflictDialog', () {
    Future<ImportConflictChoice?> pickWith(
        WidgetTester tester, String? buttonText) async {
      ImportConflictChoice? picked;
      var closed = false;
      await tester.pumpWidget(MaterialApp(
        home: Builder(
          builder: (context) => TextButton(
            onPressed: () async {
              picked = await showImportConflictDialog(context, 'Alps 2025');
              closed = true;
            },
            child: const Text('open'),
          ),
        ),
      ));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Alps 2025'), findsWidgets);
      expect(find.text('Keep both'), findsOneWidget);
      expect(find.text('Replace'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
      // Replacing is destructive: the dialog says what is overwritten and
      // what is kept before the user can pick it.
      expect(find.textContaining('overwrites'), findsOneWidget);
      expect(find.textContaining('deleted'), findsOneWidget);
      expect(find.textContaining('share links'), findsOneWidget);
      // An older file can lack photos a memory it keeps has since gained.
      expect(find.textContaining('Photos that aren’t in the file are removed too'),
          findsOneWidget);
      expect(find.textContaining(
              'People the file doesn’t have are deleted with their avatars'),
          findsOneWidget);

      if (buttonText == null) {
        await tester.tapAt(const Offset(5, 5)); // outside: dismissed
      } else {
        await tester.tap(find.text(buttonText));
      }
      await tester.pumpAndSettle();
      expect(closed, isTrue);
      return picked;
    }

    testWidgets('Keep both', (tester) async {
      expect(await pickWith(tester, 'Keep both'), ImportConflictChoice.keepBoth);
    });

    testWidgets('Replace', (tester) async {
      expect(await pickWith(tester, 'Replace'), ImportConflictChoice.replace);
    });

    testWidgets('Cancel', (tester) async {
      expect(await pickWith(tester, 'Cancel'), isNull);
    });

    testWidgets('dismissing is a cancel', (tester) async {
      expect(await pickWith(tester, null), isNull);
    });
  });

  group('importResolvingNameConflicts', () {
    test('asks again when the retried import also finds the name taken',
        () async {
      final sent = <ImportConflictChoice?>[];
      final answers = [null, null, 'Alps (2)'];
      final conflicts = ['Alps', 'Alps'];
      final asked = <String>[];

      final result = await importResolvingNameConflicts(
        upload: (choice) async {
          sent.add(choice);
          return answers[sent.length - 1];
        },
        takeNameConflict: () => conflicts.isEmpty ? null : conflicts.removeAt(0),
        ask: (name) async {
          asked.add(name);
          return asked.length == 1
              ? ImportConflictChoice.replace
              : ImportConflictChoice.keepBoth;
        },
      );

      expect(result, 'Alps (2)');
      expect(asked, ['Alps', 'Alps']);
      expect(sent, [
        null,
        ImportConflictChoice.replace,
        ImportConflictChoice.keepBoth,
      ]);
    });

    test('stops when the user cancels', () async {
      var uploads = 0;
      final result = await importResolvingNameConflicts(
        upload: (_) async {
          uploads++;
          return null;
        },
        takeNameConflict: () => 'Alps',
        ask: (_) async => null,
      );

      expect(result, isNull);
      expect(uploads, 1);
    });

    test('a failure that is not a name conflict ends it', () async {
      var asked = false;
      final result = await importResolvingNameConflicts(
        upload: (_) async => null,
        takeNameConflict: () => null,
        ask: (_) async {
          asked = true;
          return ImportConflictChoice.keepBoth;
        },
      );

      expect(result, isNull);
      expect(asked, isFalse);
    });
  });
}
