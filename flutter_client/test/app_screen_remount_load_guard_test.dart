// Regression test for a load-performance audit finding: AppScreen.initState()
// called notifier.load(projectRef) unconditionally on every mount, including
// toggling straight back from view mode to the same project a moment later —
// redundantly re-fetching everything the notifier already held. Fixed by a
// guard mirroring ProjectStatsScreen's identical singleton-reuse check
// (project_stats_screen.dart), adapted so a still-loading or errored notifier
// still goes through a real load().

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/app_screen.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

// Stands in for the real ProjectService so AppScreen's initState never hits a
// real, unmocked api.get() call — same precedent as app_router_redirect_test.dart
// / view_screen_test.dart.
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

class _CountingProjectNotifier extends ProjectNotifier {
  _CountingProjectNotifier(super.service);

  int loadCallCount = 0;

  @override
  bool get loadOwnerExtras => false; // irrelevant to this test

  @override
  Future<void> load(ProjectRef ref) {
    loadCallCount++;
    return super.load(ref);
  }
}

/// Pumps until [n]'s load() has settled (isLoading flips false) or a bound
/// is hit — AppScreen mounts a real map (tile fetches, animation
/// controllers) that never fully quiesces under flutter_test, so
/// pumpAndSettle() isn't usable here (same precedent as
/// app_router_redirect_test.dart / view_screen_test.dart).
Future<void> _pumpUntilLoaded(WidgetTester tester, ProjectNotifier n) async {
  for (var i = 0; i < 20 && n.isLoading; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets(
      'mounting AppScreen twice for the same project (manage → view → '
      'manage toggle) only calls load() once', (tester) async {
    final auth = AuthNotifier(AuthService());
    auth.updateUser(const {
      'id': 'user-1',
      'email': 'a@x.com',
      'display_name': 'A',
      'auth_provider': 'local',
    });
    final notifier = _CountingProjectNotifier(_FakeProjectService());

    final router = GoRouter(
      initialLocation: '/app?project=${Uri.encodeComponent('Trip')}',
      routes: [
        GoRoute(
          path: '/app',
          builder: (context, state) => AppScreen(
              projectName: state.uri.queryParameters['project'] ?? ''),
        ),
        // Stands in for view mode: any other route unmounts AppScreen so a
        // subsequent /app navigation is a genuine remount (initState reruns),
        // exactly like GoRouter swapping /view <-> /app on the real toggle.
        GoRoute(path: '/other', builder: (context, state) => const SizedBox()),
      ],
    );

    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<AuthNotifier>.value(value: auth),
        ChangeNotifierProvider<ProjectNotifier>.value(value: notifier),
      ],
      child: MaterialApp.router(routerConfig: router),
    ));
    await tester.pump();
    await _pumpUntilLoaded(tester, notifier);

    expect(notifier.loadCallCount, 1);
    expect(notifier.ref, const ProjectRef(name: 'Trip'));
    expect(notifier.error, isNull);
    expect(notifier.isLoading, isFalse);

    // Toggle away (view mode) then back (manage mode) — AppScreen unmounts
    // and remounts with a fresh State, so initState runs again.
    router.go('/other');
    await tester.pump();
    router.go('/app?project=${Uri.encodeComponent('Trip')}');
    await tester.pump();
    await _pumpUntilLoaded(tester, notifier);

    expect(notifier.loadCallCount, 1,
        reason: 'the already-loaded, error-free project must not be '
            're-fetched on a same-project remount');
  });
}
