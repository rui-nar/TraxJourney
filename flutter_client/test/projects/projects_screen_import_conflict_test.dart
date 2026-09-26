/// The import screen keeps asking while the name stays taken (issue #452).
///
/// Drives the real ProjectsScreen: the retried import can itself come back
/// with the name taken, and the screen must show the choice again rather
/// than stop silently.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_screen.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';

class _FakeProjectsService extends ProjectsService {
  @override
  Future<List<Map<String, dynamic>>> list() async => [];
}

/// Answers the first [conflicts] uploads with "name taken", then succeeds.
class _ConflictingNotifier extends ProjectsNotifier {
  _ConflictingNotifier(this.conflicts) : super(_FakeProjectsService());

  int conflicts;
  final sent = <ImportConflictChoice?>[];
  String? _taken;

  @override
  String? get nameConflict => _taken;

  @override
  void clearNameConflict() => _taken = null;

  @override
  Future<({List<int> bytes, String defaultName})?> pickProjectFile() async =>
      (bytes: <int>[1, 2, 3], defaultName: 'Alps');

  @override
  Future<String?> uploadProjectFile({
    required List<int> bytes,
    required String name,
    ImportConflictChoice? onConflict,
  }) async {
    sent.add(onConflict);
    if (conflicts > 0) {
      conflicts--;
      _taken = name;
      return null;
    }
    return '$name (2)';
  }
}

AuthNotifier _loggedInAuth() {
  final auth = AuthNotifier(AuthService());
  auth.updateUser({
    'id': 'user-1',
    'email': 'a@x.com',
    'display_name': 'A',
    'auth_provider': 'local',
  });
  return auth;
}

Widget _app(ProjectsNotifier notifier) {
  final router = GoRouter(routes: [
    GoRoute(path: '/', builder: (_, __) => const ProjectsScreen()),
    GoRoute(
      path: '/view',
      builder: (_, state) =>
          Text('viewing ${state.uri.queryParameters['project']}'),
    ),
  ]);
  return MultiProvider(
    providers: [
      ChangeNotifierProvider<AuthNotifier>.value(value: _loggedInAuth()),
      ChangeNotifierProvider<ProjectsNotifier>.value(value: notifier),
    ],
    child: MaterialApp.router(routerConfig: router),
  );
}

Future<void> _startImport(WidgetTester tester) async {
  await tester.ensureVisible(find.textContaining('Choose .'));
  await tester.tap(find.textContaining('Choose .'));
  await tester.pumpAndSettle();
  // The name dialog, prefilled with the file's name.
  await tester.tap(find.widgetWithText(ElevatedButton, 'Import'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('a retry that finds the name taken again asks again',
      (tester) async {
    tester.view.physicalSize = const Size(1200, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final notifier = _ConflictingNotifier(2);

    await tester.pumpWidget(_app(notifier));
    await tester.pumpAndSettle();
    await _startImport(tester);

    expect(find.text('Keep both'), findsOneWidget);
    await tester.tap(find.text('Replace'));
    await tester.pumpAndSettle();

    // Taken again: the choice is offered again.
    expect(find.text('Keep both'), findsOneWidget);
    await tester.tap(find.text('Keep both'));
    await tester.pumpAndSettle();

    expect(notifier.sent, [
      null,
      ImportConflictChoice.replace,
      ImportConflictChoice.keepBoth,
    ]);
    expect(find.text('viewing Alps (2)'), findsOneWidget);
  });

  testWidgets('cancelling the choice imports nothing more', (tester) async {
    tester.view.physicalSize = const Size(1200, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final notifier = _ConflictingNotifier(1);

    await tester.pumpWidget(_app(notifier));
    await tester.pumpAndSettle();
    await _startImport(tester);
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();

    expect(notifier.sent, [null]);
    expect(find.textContaining('viewing'), findsNothing);
  });
}
