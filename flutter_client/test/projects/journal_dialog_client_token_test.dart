// Regression tests for the journal create idempotency guard (mutation-
// propagation audit): journal entries had no server-side dedupe, so a
// client-perceived timeout followed by a manual retry created a genuine
// duplicate row. JournalDialog now generates one client_token per save
// action and resends it on every retry of that same action, while a fresh
// dialog instance (a separate entry) gets its own new token.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/journal_dialog.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_journal_crud_mixin.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _ref = ProjectRef(name: 'Trip');

Widget _harness(ProjectNotifier notifier) => MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () => showDialog<void>(
              context: context,
              builder: (_) =>
                  JournalDialog(notifier: notifier, initialDate: '2026-01-01'),
            ),
            child: const Text('open'),
          ),
        ),
      ),
    );

void main() {
  setUp(() => projectDataCache.resetForTest());

  test('generateJournalClientToken produces distinct, non-empty values', () {
    final a = generateJournalClientToken();
    final b = generateJournalClientToken();
    expect(a, isNotEmpty);
    expect(a, isNot(equals(b)));
  });

  testWidgets('retrying Save after a failed create resends the same client_token',
      (tester) async {
    final tokensSent = <String?>[];
    var attempt = 0;
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.method == 'POST' && req.url.path == '/api/journal/') {
            attempt++;
            final body = jsonDecode(req.body) as Map<String, dynamic>;
            tokensSent.add(body['client_token'] as String?);
            // First tap fails (simulating a client-perceived timeout); the
            // second tap — same dialog, same draft — must retry with the
            // same token.
            if (attempt == 1) {
              return http.Response('{"detail":"boom"}', 500);
            }
            return http.Response('{}', 200);
          }
          if (req.method == 'GET' && req.url.path == '/api/projects/Trip/meta') {
            return http.Response(
                jsonEncode({
                  'name': 'Trip',
                  'items': [
                    {
                      'item_type': 'journal',
                      'journal': {
                        'id': 'journal-real-1',
                        'date': '2026-01-01',
                        'photos': <String>[],
                      },
                    },
                  ],
                }),
                200);
          }
          return http.Response('{}', 404);
        }));

    final notifier = ProjectNotifier(ProjectService())..ref = _ref;

    await tester.pumpWidget(_harness(notifier));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();
    // The dialog stays open after the failure (existing behaviour).
    expect(find.byType(AlertDialog), findsOneWidget);

    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();
    // The second attempt succeeded and closed the dialog.
    expect(find.byType(AlertDialog), findsNothing);

    expect(tokensSent, hasLength(2));
    expect(tokensSent[0], isNotNull);
    expect(tokensSent[1], equals(tokensSent[0]),
        reason: 'a retry of the same save action must resend the same '
            'idempotency token so the server can dedupe it');
  });

  testWidgets('two separate dialog instances use two different client_tokens',
      (tester) async {
    final tokensSent = <String?>[];
    api = ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          if (req.method == 'POST' && req.url.path == '/api/journal/') {
            final body = jsonDecode(req.body) as Map<String, dynamic>;
            tokensSent.add(body['client_token'] as String?);
            return http.Response('{}', 200);
          }
          if (req.method == 'GET' && req.url.path == '/api/projects/Trip/meta') {
            return http.Response(
                jsonEncode({'name': 'Trip', 'items': <dynamic>[]}), 200);
          }
          return http.Response('{}', 404);
        }));

    final notifier = ProjectNotifier(ProjectService())..ref = _ref;

    await tester.pumpWidget(_harness(notifier));

    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(tokensSent, hasLength(2));
    expect(tokensSent[0], isNotNull);
    expect(tokensSent[1], isNotNull);
    expect(tokensSent[1], isNot(equals(tokensSent[0])),
        reason: 'a genuinely new entry (a fresh dialog) must get its own '
            'token, or a second entry would become impossible to create');
  });
}
