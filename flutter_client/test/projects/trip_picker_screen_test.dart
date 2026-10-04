/// The trip picker a .gpx opened from another app leads to (issue #368).
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/incoming_gpx.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';
import 'package:traxjourney_client/src/projects/trip_picker_screen.dart';

class _FakeProjectsService extends ProjectsService {
  @override
  Future<List<Map<String, dynamic>>> list() async => [
        {'name': 'Alps', 'role': 'owner'},
        {'name': 'Lisbon', 'role': 'editor', 'owner_id': 7, 'owner_name': 'Bea'},
        {'name': 'Peeked', 'role': 'viewer', 'owner_id': 8, 'owner_name': 'Cy'},
      ];
}

class _EmptyProjectsService extends ProjectsService {
  @override
  Future<List<Map<String, dynamic>>> list() async => [];
}

class _GatedProjectsService extends ProjectsService {
  _GatedProjectsService(this._gate);
  final Future<List<Map<String, dynamic>>> _gate;

  @override
  Future<List<Map<String, dynamic>>> list() => _gate;
}

/// A session restore that stays pending until [finish] is called.
class _PendingAuthService extends AuthService {
  final _restore = Completer<bool>();

  void finish() => _restore.complete(false);

  @override
  Future<bool> restoreSession() => _restore.future;

  @override
  Future<void> appOpened(String sessionState) async {}
}

Future<({IncomingGpx holder, GoRouter router})> _pump(
  WidgetTester tester,
  IncomingFileRead read, {
  AuthNotifier? auth,
  ProjectsNotifier? projects,
}) async {
  final holder = IncomingGpx();
  await holder.receive(Future.value(read));
  auth ??= AuthNotifier(AuthService());
  if (projects == null) {
    projects = ProjectsNotifier(_FakeProjectsService());
    await projects.load();
  }
  final router = GoRouter(initialLocation: kIncomingGpxRoute, routes: [
    GoRoute(
        path: kIncomingGpxRoute,
        builder: (_, __) => TripPickerScreen(incoming: holder)),
    GoRoute(
        path: '/app',
        builder: (_, state) => Text('trip ${state.uri.query}')),
    GoRoute(path: '/projects', builder: (_, __) => const Text('trips')),
  ]);
  await tester.pumpWidget(MultiProvider(
    providers: [
      ChangeNotifierProvider<AuthNotifier>.value(value: auth),
      ChangeNotifierProvider<ProjectsNotifier>.value(value: projects),
    ],
    child: MaterialApp.router(routerConfig: router),
  ));
  // pump, not pumpAndSettle: a loading indicator never settles.
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 500));
  return (holder: holder, router: router);
}

final _ride = IncomingFileRead.file(
    IncomingGpxFile('ride.gpx', utf8.encode('<gpx></gpx>')));

void main() {
  testWidgets('lists the trips the user can add to, not the viewed ones',
      (tester) async {
    await _pump(tester, _ride);

    expect(find.textContaining('ride.gpx'), findsOneWidget);
    expect(find.text('Alps'), findsOneWidget);
    expect(find.text('Lisbon'), findsOneWidget);
    expect(find.text('Shared by Bea'), findsOneWidget);
    expect(find.text('Peeked'), findsNothing);
  });

  testWidgets('while the session is being restored it loads, not "no trip"',
      (tester) async {
    final service = _PendingAuthService();
    final auth = AuthNotifier(service);
    unawaited(auth.init());
    final projects = ProjectsNotifier(_EmptyProjectsService());

    await _pump(tester, _ride, auth: auth, projects: projects);

    expect(auth.isRestoring, isTrue);
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    expect(find.textContaining('no trip'), findsNothing);

    service.finish();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 500));
    expect(auth.isRestoring, isFalse);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    expect(find.textContaining('no trip'), findsOneWidget);
  });

  testWidgets('while the trips are loading it shows progress', (tester) async {
    final gate = Completer<List<Map<String, dynamic>>>();
    final projects = ProjectsNotifier(_GatedProjectsService(gate.future));
    unawaited(projects.load());

    await _pump(tester, _ride, projects: projects);

    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    expect(find.textContaining('no trip'), findsNothing);

    gate.complete([]);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 500));
    expect(find.byType(CircularProgressIndicator), findsNothing);
    expect(find.textContaining('no trip'), findsOneWidget);
  });

  testWidgets('loaded with no trips, it says there is none', (tester) async {
    final projects = ProjectsNotifier(_EmptyProjectsService());
    await projects.load();

    await _pump(tester, _ride, projects: projects);

    expect(find.byType(CircularProgressIndicator), findsNothing);
    expect(find.textContaining('no trip'), findsOneWidget);
  });

  testWidgets('choosing a trip opens it, and the file is waiting for it',
      (tester) async {
    final (:holder, router: _) = await _pump(tester, _ride);

    await tester.tap(find.text('Lisbon'));
    await tester.pumpAndSettle();

    expect(find.text('trip project=Lisbon&owner=7'), findsOneWidget);
    const lisbon = ProjectRef(name: 'Lisbon', ownerId: 7, role: 'editor');
    expect(holder.takeFor(lisbon)!.name, 'ride.gpx');
  });

  testWidgets('a refused file is explained, and nothing is offered',
      (tester) async {
    await _pump(
        tester, const IncomingFileRead.refused(kGpxTooLargeMessage));

    expect(find.text(kGpxTooLargeMessage), findsOneWidget);
    expect(find.text('Alps'), findsNothing);

    await tester.tap(find.text('Back to trips'));
    await tester.pumpAndSettle();
    expect(find.text('trips'), findsOneWidget);
  });

  testWidgets('cancelling lets the file go', (tester) async {
    final (:holder, router: _) = await _pump(tester, _ride);

    await tester.tap(find.byTooltip('Cancel'));
    await tester.pumpAndSettle();

    expect(holder.file, isNull);
    expect(find.text('trips'), findsOneWidget);
  });
}
