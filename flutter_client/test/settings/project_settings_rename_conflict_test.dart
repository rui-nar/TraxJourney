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

/// Every day-meta map PUT during a test, in order.
late List<Map<String, dynamic>> putDayMeta;

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
        if (req.method == 'PUT' && req.url.path.endsWith('/day-meta')) {
          final body = jsonDecode(req.body) as Map<String, dynamic>;
          putDayMeta.add(body['day_meta'] as Map<String, dynamic>);
          otherWrites.add('${req.method} ${req.url.path}');
          return http.Response('', 204);
        }
        if (req.method == 'GET' && req.url.path.endsWith('/content-days')) {
          return http.Response(jsonEncode({'days': <String>[]}), 200);
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

Finder _nameField() => find.byWidgetPredicate((w) =>
    w is TextField &&
    w.controller != null &&
    const {'Trip', _taken, 'Alps'}.contains(w.controller!.text));

Future<void> _renameAndSave(WidgetTester tester, String name) async {
  await tester.enterText(_nameField(), name);
  await tester.tap(find.byTooltip('Save'));
  await _frames(tester);
}

/// Moves the trip-end chip labelled [from] to [day] of the month it opens on.
Future<void> _moveEndDate(WidgetTester tester, String from, String day) async {
  await tester.tap(find.text(from));
  await _frames(tester);
  await tester.tap(find.descendant(
    of: find.byType(DatePickerDialog),
    matching: find.text(day),
  ));
  await _frames(tester);
  await tester.tap(find.text('OK'));
  await _frames(tester);
}

void main() {
  late ApiClient realApi;

  setUp(() {
    otherWrites = [];
    putDayMeta = [];
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

  testWidgets(
      'a refused rename leaves no confirmed day prune behind for the next save',
      (tester) async {
    // Review R1-1: the prune the owner confirmed used to be applied to the
    // screen's own day-meta before the rename was refused. With the screen
    // kept open, moving the end date back and saving again raised no dialog
    // and sent the pruned map: the notes on those days were deleted.
    final n = notifier()
      ..tripEnd = '2026-06-14'
      ..dayMeta = {
        for (final k in ['2026-06-13', '2026-06-14', '2026-06-15', '2026-06-16'])
          k: <String, dynamic>{'note': 'note for $k'},
      };
    await _pumpSettings(tester, n);

    await tester.enterText(_nameField(), _taken);
    await tester.tap(find.byTooltip('Save'));
    await _frames(tester);
    expect(find.text('Remove days after the end date?'), findsOneWidget);
    await tester.tap(find.text('Delete'));
    await _frames(tester);

    expect(find.text("Project '$_taken' already exists"), findsOneWidget);
    expect(putDayMeta, isEmpty);
    // Let the message time out: at this size it covers the Save button.
    await _frames(tester, 60);

    // The owner keeps the trip's name and moves the end date back over the
    // days they had agreed to drop.
    await tester.enterText(_nameField(), 'Trip');
    await _moveEndDate(tester, 'Jun 14, 2026', '16');
    await tester.tap(find.byTooltip('Save'));
    await _frames(tester);

    expect(find.text('home'), findsOneWidget);
    expect(putDayMeta, isNotEmpty);
    expect(putDayMeta.last.keys.toSet(),
        {'2026-06-13', '2026-06-14', '2026-06-15', '2026-06-16'});
    expect(n.dayMeta['2026-06-15']?['note'], 'note for 2026-06-15');
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
