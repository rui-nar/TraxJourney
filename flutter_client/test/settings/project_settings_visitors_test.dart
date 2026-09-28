// Regression test for issue #431: the share-link visitor list used to show a
// signed-in visitor's email address to the trip owner — outright when the
// visitor had no display name, and it was in the payload either way.
//
// The server no longer sends the address; it sends a display name, an avatar
// and a pseudonymous `visitor_key` scoped to the owner. These tests drive the
// real Sharing section against a mocked visitors endpoint and check what the
// owner actually sees.

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

/// What GET /share/visitors answers.
late Map<String, dynamic> visitorsPayload;

ApiClient _api() => ApiClient(
      httpClient: MockClient((req) async {
        if (req.method == 'GET' && req.url.path.endsWith('/share/visitors')) {
          return http.Response(jsonEncode(visitorsPayload), 200);
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

/// Opens the settings screen on the Sharing tab. Narrow layout: the sidebar
/// is icon-only there, so the tab is found by its icon.
Future<void> _pumpSharing(WidgetTester tester) async {
  tester.view.physicalSize = const Size(520, 1000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  // No share token on purpose: the stats block renders whenever the visitors
  // call answered, and a token would make _ShareLinkCard build a share URL
  // from Uri.base, which has no origin under the test VM's file:// scheme.
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
}

Map<String, dynamic> _payload(List<Map<String, dynamic>> registered) => {
      'full': {'anonymous_count': 2, 'registered': registered},
      'no_memories': {'anonymous_count': 0, 'registered': const []},
    };

void main() {
  late ApiClient realApi;

  setUp(() {
    realApi = api;
    api = _api();
  });
  tearDown(() => api = realApi);

  testWidgets('a named visitor is listed by display name only', (tester) async {
    visitorsPayload = _payload([
      {
        'visitor_key': '0123456789abcdef',
        'display_name': 'Alice',
        'avatar_url': '',
        'last_seen_at': 1.0,
      },
    ]);
    await _pumpSharing(tester);

    expect(find.text('Alice'), findsOneWidget);
    expect(find.text('Anonymous: 2 unique visitors'), findsOneWidget);
    expect(find.textContaining('@'), findsNothing);
  });

  testWidgets('a nameless visitor is labelled by the pseudonymous key, never an email',
      (tester) async {
    // A stale or misbehaving server that still ships an address must not get
    // it onto the screen either: the old client fell back to `email` here.
    visitorsPayload = _payload([
      {
        'visitor_key': 'fedcba9876543210',
        'display_name': '',
        'avatar_url': '',
        'last_seen_at': 1.0,
        'email': 'secret@example.com',
      },
    ]);
    await _pumpSharing(tester);

    expect(find.text('Visitor fedcba'), findsOneWidget);
    expect(find.textContaining('secret@example.com'), findsNothing);
    expect(find.textContaining('@'), findsNothing);
  });

  testWidgets('a nameless visitor without a key still gets a neutral label',
      (tester) async {
    visitorsPayload = _payload([
      {'display_name': '', 'avatar_url': '', 'last_seen_at': 1.0},
    ]);
    await _pumpSharing(tester);

    expect(find.text('Signed-in visitor'), findsOneWidget);
  });
}
