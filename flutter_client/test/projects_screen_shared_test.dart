// Widget tests for the "My Trips" / "Shared With Me" split on ProjectsScreen
// (issue #106 — travel companion). Shared entries come from a
// `/api/projects/` list entry carrying `owner_id`/`owner_name`/`role: "editor"`
// (see ProjectListEntry in core/project_ref.dart); the "Shared With Me"
// section only renders when at least one such entry is present.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_screen.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';

class _FakeProjectsService extends ProjectsService {
  final List<Map<String, dynamic>> entries;
  _FakeProjectsService(this.entries);

  @override
  Future<List<Map<String, dynamic>>> list() async => entries;
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

Future<ProjectsNotifier> _loadedNotifier(List<Map<String, dynamic>> entries) async {
  final notifier = ProjectsNotifier(_FakeProjectsService(entries));
  await notifier.load();
  return notifier;
}

Widget _harness(ProjectsNotifier notifier) => MaterialApp(
      home: MultiProvider(
        providers: [
          ChangeNotifierProvider<AuthNotifier>.value(value: _loggedInAuth()),
          ChangeNotifierProvider<ProjectsNotifier>.value(value: notifier),
        ],
        child: const ProjectsScreen(),
      ),
    );

void main() {
  testWidgets(
      'renders My Trips and Shared With Me as separate sections, with the '
      "owner's name shown on the shared tile", (tester) async {
    final notifier = await _loadedNotifier([
      {'name': 'Own Trip', 'filename': 'own.traxj', 'role': 'owner'},
      {
        'name': 'Friend Trip',
        'filename': 'friend.traxj',
        'owner_id': 7,
        'owner_name': 'Bob',
        'role': 'editor',
      },
    ]);

    await tester.pumpWidget(_harness(notifier));
    await tester.pump();

    expect(find.text('My Trips'), findsOneWidget);
    expect(find.text('Shared With Me'), findsOneWidget);
    expect(find.text('Own Trip'), findsOneWidget);
    expect(find.text('Friend Trip'), findsOneWidget);
    expect(find.textContaining('Bob'), findsOneWidget);

    // Delete is owner-only — the shared tile must not offer it. Own Trip
    // still gets exactly one delete button.
    expect(find.byIcon(Icons.delete_outline), findsOneWidget);
    // Both tiles still get an Open button.
    expect(find.widgetWithText(ElevatedButton, 'Open'), findsNWidgets(2));
  });

  testWidgets('Shared With Me is hidden entirely when nothing is shared',
      (tester) async {
    final notifier = await _loadedNotifier([
      {'name': 'Own Trip', 'filename': 'own.traxj', 'role': 'owner'},
    ]);

    await tester.pumpWidget(_harness(notifier));
    await tester.pump();

    expect(find.text('My Trips'), findsOneWidget);
    expect(find.text('Shared With Me'), findsNothing);
  });

  testWidgets(
      'backward compat: entries with no owner_id/role (older server) are '
      'all treated as My Trips', (tester) async {
    final notifier = await _loadedNotifier([
      {'name': 'Legacy Trip', 'filename': 'legacy.traxj'},
    ]);

    await tester.pumpWidget(_harness(notifier));
    await tester.pump();

    expect(find.text('My Trips'), findsOneWidget);
    expect(find.text('Legacy Trip'), findsOneWidget);
    expect(find.text('Shared With Me'), findsNothing);
    expect(find.byIcon(Icons.delete_outline), findsOneWidget);
  });

  testWidgets(
      'the app bar shows the product name and the import card asks for a '
      '.traxj file (issue #151)', (tester) async {
    final notifier = await _loadedNotifier([]);

    await tester.pumpWidget(_harness(notifier));
    await tester.pump();

    expect(find.descendant(of: find.byType(AppBar), matching: find.text('TraxJourney')),
        findsOneWidget);
    expect(find.text('Import a .traxj project file.'), findsOneWidget);
    expect(find.text('Choose .traxj file'), findsOneWidget);
  });
}
