// A refused share-link change says why (issue #480, review R1-1).
//
// The share endpoints need a co-owner, so an editor on a shared trip who
// pressed "Create link" got a 403 and saw nothing happen: the error left the
// button's callback uncaught. This drives the real Sharing section against a
// server that refuses, and checks the reader sees the server's message whole.

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/project_settings_screen.dart';

// A quote and non-ASCII text, which the old regex cut short.
const _refusal = 'Only a co-owner can share "Mon voyage" — désolé';

ApiClient _api() => ApiClient(
      httpClient: MockClient((req) async {
        if (req.method == 'POST' && req.url.path.endsWith('/share')) {
          return http.Response.bytes(
              utf8.encode(jsonEncode({'detail': _refusal})), 403,
              headers: {'content-type': 'application/json; charset=utf-8'});
        }
        if (req.method == 'GET' && req.url.path.endsWith('/share/visitors')) {
          return http.Response(
              jsonEncode({
                'full': {'anonymous_count': 0, 'registered': []},
                'no_memories': {'anonymous_count': 0, 'registered': []},
              }),
              200);
        }
        if (req.method == 'GET' && req.url.path.endsWith('/polarsteps/trips')) {
          return http.Response('[]', 200);
        }
        return http.Response('{}', 200);
      }),
    );

Future<void> _frames(WidgetTester tester, [int n = 8]) async {
  for (var i = 0; i < n; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

void main() {
  late ApiClient realApi;

  setUp(() {
    realApi = api;
    api = _api();
  });
  tearDown(() => api = realApi);

  testWidgets('a refused "Create link" shows the server\'s reason',
      (tester) async {
    tester.view.physicalSize = const Size(520, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final n = ProjectNotifier(ProjectService())
      ..ref = const ProjectRef(name: 'Trip');
    final router = GoRouter(routes: [
      GoRoute(
        path: '/',
        builder: (_, __) => const Scaffold(body: Text('home')),
        routes: [
          GoRoute(
            path: 'settings',
            builder: (_, __) => const ProjectSettingsScreen(projectName: 'Trip'),
          ),
        ],
      ),
    ]);
    await tester.pumpWidget(ChangeNotifierProvider<ProjectNotifier>.value(
      value: n,
      child: MaterialApp.router(routerConfig: router),
    ));
    router.go('/settings');
    await _frames(tester);
    await tester.tap(find.byIcon(Icons.share));
    await _frames(tester);

    await tester.tap(find.text('Create link').first);
    await _frames(tester);

    expect(tester.takeException(), isNull);
    expect(find.text(_refusal), findsOneWidget);
    expect(n.shareToken, isNull);
  });
}
