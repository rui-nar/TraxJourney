import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import 'package:traxjourney_client/src/api/client.dart' show ApiException;
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/app_screen.dart';
import 'package:traxjourney_client/src/projects/people_screen.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/view_screen.dart';

// Stands in for the real ProjectService so AppScreen's initState (reached via
// the mode-toggle test below) never hits a real, unmocked api.get() call —
// same precedent as app_router_redirect_test.dart.
class _FakeProjectService extends ProjectService {
  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => {
        'name': ref.name,
        'activities': <dynamic>[],
        'items': <dynamic>[],
        'people': <dynamic>[],
        'groups': <dynamic>[],
      };

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async =>
      getDetailsMeta(ref);
}

class _TestProjectNotifier extends ProjectNotifier {
  _TestProjectNotifier(super.service);
  @override
  bool get loadOwnerExtras => false;
}

/// Guards the fix where view mode's AppBar was missing the People/Encounters
/// directory entry point that manage mode already had (issue #40 follow-up).
void main() {
  testWidgets('view mode AppBar has an Encounters button that opens PeopleScreen',
      (tester) async {
    // ViewScreen loads project data over the network on init; in the test
    // sandbox every request fails with a fake 400, and ProjectNotifier.load()
    // fires two requests concurrently but only awaits one before its catch
    // block returns — the other's later rejection is an orphaned Future error
    // unrelated to the button/navigation under test here, so ignore just that.
    final previousReporter = reportTestException;
    reportTestException = (details, description) {
      if (details.exception is! ApiException) previousReporter(details, description);
    };

    // ViewScreen resolves the project ref's role against the signed-in user
    // on load (issue #106), so an AuthNotifier must be in the tree.
    final auth = AuthNotifier(AuthService());
    auth.updateUser({
      'id': 'user-1',
      'email': 'a@x.com',
      'display_name': 'A',
      'auth_provider': 'local',
    });
    await tester.pumpWidget(MaterialApp(
      home: ChangeNotifierProvider<AuthNotifier>.value(
        value: auth,
        child: const ViewScreen(projectName: 'Trip'),
      ),
    ));
    await tester.pump();
    reportTestException = previousReporter;

    expect(find.byTooltip('Encounters'), findsOneWidget);

    await tester.tap(find.byTooltip('Encounters'));
    await tester.pumpAndSettle();

    expect(find.byType(PeopleScreen), findsOneWidget);

    // Flush every retry/one-shot timer the failed load schedules, so none
    // remain pending at teardown: the initial fetch pair now backs off 2 s then
    // 6 s before giving up (issue #178), and only then does the background geo
    // fetch start its own 2 s retry, ahead of the 5 s background-sync check.
    await tester.pump(const Duration(seconds: 20));
  });

  // Regression test for issue #219: switching from view mode to manage mode
  // failed on the first tap and only worked on a retry. Root cause — the
  // mode toggle read the map camera to carry the viewport over, but the map
  // widget isn't mounted (and its MapController isn't attached) until the
  // project's meta load finishes; tapping the toggle before then threw and
  // silently dropped the navigation. The toggle must win regardless.
  testWidgets(
      'tapping "Manage mode" navigates to /app on the first tap, before '
      'the project has finished loading (issue #219)', (tester) async {
    final previousReporter = reportTestException;
    reportTestException = (details, description) {
      if (details.exception is! ApiException) previousReporter(details, description);
    };

    final auth = AuthNotifier(AuthService());
    auth.updateUser({
      'id': 'user-1',
      'email': 'a@x.com',
      'display_name': 'A',
      'auth_provider': 'local',
    });

    final router = GoRouter(
      initialLocation: '/view?project=${Uri.encodeComponent('Trip')}',
      routes: [
        GoRoute(
          path: '/view',
          builder: (context, state) => ViewScreen(
              projectName: state.uri.queryParameters['project'] ?? ''),
        ),
        GoRoute(
          path: '/app',
          builder: (context, state) => AppScreen(
              projectName: state.uri.queryParameters['project'] ?? ''),
        ),
      ],
    );

    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<AuthNotifier>.value(value: auth),
        ChangeNotifierProvider<ProjectNotifier>(
            create: (_) => _TestProjectNotifier(_FakeProjectService())),
      ],
      child: MaterialApp.router(routerConfig: router),
    ));

    // Deliberately a single pump, not pumpAndSettle(): ViewScreen's project
    // load is still in flight (it fails against the test sandbox's network
    // stub, same as the sibling test above), so the map has not mounted.
    // Tapping now reproduces "the user is faster than the loading of the
    // view screen".
    await tester.pump();

    expect(find.byTooltip('Manage mode'), findsOneWidget);
    await tester.tap(find.byTooltip('Manage mode'));
    await tester.pump();
    await tester.pump();

    expect(router.state.uri.path, '/app');
    expect(find.byType(AppScreen), findsOneWidget);
    expect(tester.takeException(), isNull);

    reportTestException = previousReporter;
    await tester.pump(const Duration(seconds: 20));
  });
}
