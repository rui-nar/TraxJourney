// Issue #467: renaming a trip to a name the owner already has — whether the
// server's check saw the other trip or a concurrent save took the name a
// moment earlier — is refused with 409. The settings screen used to close as
// if the rename had worked, the refusal visible nowhere; it now says why and
// stays open with the name as typed, having saved nothing else.

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

/// Every request other than the rename that changes something on the server.
late List<String> otherWrites;

/// Names the server refuses as taken.
const _taken = 'Taken';

ApiClient _api() => ApiClient(
      httpClient: MockClient((req) async {
        if (req.method == 'PUT' && req.url.path.endsWith('/projects/Trip')) {
          final body = jsonDecode(req.body) as Map<String, dynamic>;
          final newName = body['new_name'] as String?;
          if (newName == _taken) {
            return http.Response(
                jsonEncode({'detail': "Project '$_taken' already exists"}),
                409);
          }
          return http.Response(
              jsonEncode({'name': newName ?? 'Trip', 'trip_start': null}), 200);
        }
        if (req.method == 'GET' && req.url.path.endsWith('/polarsteps/trips')) {
          return http.Response('[]', 200);
        }
        if (req.method != 'GET') otherWrites.add('${req.method} ${req.url.path}');
        return http.Response('{}', 200);
      }),
    );

Future<void> _frames(WidgetTester tester, [int n = 8]) async {
  for (var i = 0; i < n; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

Future<void> _pumpSettings(WidgetTester tester, ProjectNotifier n) async {
  // Narrow, as in project_settings_trip_end_test.dart: the wide sidebar
  // overflows under the test font.
  tester.view.physicalSize = const Size(520, 1000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

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
}

Future<void> _renameAndSave(WidgetTester tester, String name) async {
  await tester.enterText(
    find.byWidgetPredicate(
        (w) => w is TextField && w.controller?.text == 'Trip'),
    name,
  );
  await tester.tap(find.byTooltip('Save'));
  await _frames(tester);
}

void main() {
  late ApiClient realApi;

  setUp(() {
    otherWrites = [];
    realApi = api;
    api = _api();
  });
  tearDown(() => api = realApi);

  ProjectNotifier notifier() => ProjectNotifier(ProjectService())
    ..ref = const ProjectRef(name: 'Trip')
    ..tripStart = '2026-06-01';

  testWidgets('a refused rename says why and keeps the screen open',
      (tester) async {
    final n = notifier();
    await _pumpSettings(tester, n);

    await _renameAndSave(tester, _taken);

    expect(find.text("Project '$_taken' already exists"), findsOneWidget);
    expect(find.text('home'), findsNothing);
    expect(find.byWidgetPredicate(
        (w) => w is TextField && w.controller?.text == _taken), findsOneWidget);
    expect(n.projectName, 'Trip');
    expect(otherWrites, isEmpty);
  });

  testWidgets('an accepted rename saves the rest and closes', (tester) async {
    final n = notifier();
    await _pumpSettings(tester, n);

    await _renameAndSave(tester, 'Alps');

    expect(n.projectName, 'Alps');
    expect(find.text('home'), findsOneWidget);
    expect(otherWrites, isNotEmpty);
  });
}
