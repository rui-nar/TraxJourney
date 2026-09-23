// Tests for stale shared-project ref recovery (issue #111): after an owner
// renames a shared trip, a companion's stored/URL `{name, ownerId}` ref 404s.
// The client refreshes the projects list and re-matches the trip by owner
// (memberships survive renames server-side), reopening it under its new name;
// when that fails it clears the last-opened pref and lands on /projects.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/last_opened_project.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/core/stale_shared_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';

Map<String, dynamic> _entry(String name, int ownerId, String role) => {
      'name': name,
      'filename': '$name.traxj',
      'owner_id': ownerId,
      'owner_name': role == 'editor' ? 'Owner $ownerId' : '',
      'role': role,
    };

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  group('recoverStaleSharedRef (pure matcher)', () {
    const stale = ProjectRef(name: 'Old Name', ownerId: 7, role: 'editor');

    test('re-matches the renamed trip by owner when unambiguous', () {
      final entries = [
        _entry('My Own Trip', 1, 'owner'),
        _entry('New Name', 7, 'editor'),
      ];

      final ref = recoverStaleSharedRef(stale, entries);

      expect(ref, const ProjectRef(name: 'New Name', ownerId: 7, role: 'editor'));
    });

    test('returns null when the membership is gone', () {
      final entries = [_entry('My Own Trip', 1, 'owner')];

      expect(recoverStaleSharedRef(stale, entries), isNull);
    });

    test('returns null when several shared trips from the same owner make '
        'the match ambiguous', () {
      final entries = [
        _entry('New Name', 7, 'editor'),
        _entry('Another Trip', 7, 'editor'),
      ];

      expect(recoverStaleSharedRef(stale, entries), isNull);
    });

    test('never matches own-project refs or own entries', () {
      const ownStale = ProjectRef(name: 'Old Name');
      final entries = [_entry('New Name', 7, 'editor')];

      expect(recoverStaleSharedRef(ownStale, entries), isNull);
      // An own entry from the same user id must not be mistaken for a share.
      expect(
        recoverStaleSharedRef(stale, [_entry('New Name', 7, 'owner')]),
        isNull,
      );
    });
  });

  group('ProjectNotifier.loadErrorStatus', () {
    test('records 404 from a failed load and resets on the next one', () async {
      final service = _NotFoundProjectService();
      final notifier = _TestProjectNotifier(service);
      const ref = ProjectRef(name: 'Old Name', ownerId: 7, role: 'editor');

      await notifier.load(ref);
      expect(notifier.error, isNotNull);
      expect(notifier.loadErrorStatus, 404);

      service.fail = false;
      await notifier.load(const ProjectRef(name: 'New Name', ownerId: 7));
      expect(notifier.error, isNull);
      expect(notifier.loadErrorStatus, isNull);
    });

    test('stays null on a successful load', () async {
      final notifier = _TestProjectNotifier(_OkProjectService());

      await notifier.load(const ProjectRef(name: 'Trip A'));

      expect(notifier.error, isNull);
      expect(notifier.loadErrorStatus, isNull);
    });
  });

  group('recoverFromStaleSharedRef (navigation)', () {
    testWidgets('reopens the renamed trip in place and updates last-opened',
        (tester) async {
      const stale = ProjectRef(name: 'Old Name', ownerId: 7, role: 'editor');
      final auth = _loggedInAuth('user-1');
      late GoRouter router;
      late BuildContext screenCtx;

      router = GoRouter(initialLocation: '/view', routes: [
        GoRoute(
          path: '/view',
          builder: (context, state) => Scaffold(
            body: Builder(builder: (ctx) {
              screenCtx = ctx;
              return const Text('view');
            }),
          ),
        ),
        GoRoute(
            path: '/projects',
            builder: (_, __) => const Scaffold(body: Text('projects'))),
      ]);

      await tester.pumpWidget(
        ChangeNotifierProvider<AuthNotifier>.value(
          value: auth,
          child: MaterialApp.router(routerConfig: router),
        ),
      );

      await recoverFromStaleSharedRef(
        screenCtx,
        staleRef: stale,
        routePath: '/view',
        service: _FakeProjectsService([
          _entry('My Own Trip', 1, 'owner'),
          _entry('New Name', 7, 'editor'),
        ]),
      );
      await tester.pumpAndSettle();

      // Uri() encodes a query-param space as '+' (go_router decodes both
      // '+' and '%20' back to a space when reading queryParameters).
      expect(router.routeInformationProvider.value.uri.toString(),
          '/view?project=New+Name&owner=7');
      final saved = await readLastOpenedProject('user-1');
      expect(saved,
          const ProjectRef(name: 'New Name', ownerId: 7, role: 'editor'));
    });

    testWidgets(
        'lands on /projects with a notice and clears last-opened when the '
        'membership is gone', (tester) async {
      const stale = ProjectRef(name: 'Old Name', ownerId: 7, role: 'editor');
      final auth = _loggedInAuth('user-1');
      await saveLastOpenedProject('user-1', stale);
      late GoRouter router;
      late BuildContext screenCtx;

      router = GoRouter(initialLocation: '/view', routes: [
        GoRoute(
          path: '/view',
          builder: (context, state) => Scaffold(
            body: Builder(builder: (ctx) {
              screenCtx = ctx;
              return const Text('view');
            }),
          ),
        ),
        GoRoute(
            path: '/projects',
            builder: (_, __) => const Scaffold(body: Text('projects'))),
      ]);

      await tester.pumpWidget(
        ChangeNotifierProvider<AuthNotifier>.value(
          value: auth,
          child: MaterialApp.router(routerConfig: router),
        ),
      );

      await recoverFromStaleSharedRef(
        screenCtx,
        staleRef: stale,
        routePath: '/view',
        service: _FakeProjectsService([_entry('My Own Trip', 1, 'owner')]),
      );
      await tester.pumpAndSettle();

      expect(router.routeInformationProvider.value.uri.toString(), '/projects');
      expect(find.byType(SnackBar), findsOneWidget);
      expect(await readLastOpenedProject('user-1'), isNull);
    });
  });
}

AuthNotifier _loggedInAuth(String userId) {
  final auth = AuthNotifier(AuthService());
  auth.updateUser({
    'id': userId,
    'email': 'a@x.com',
    'display_name': 'A',
    'auth_provider': 'local',
  });
  return auth;
}

class _FakeProjectsService extends ProjectsService {
  final List<Map<String, dynamic>> entries;
  _FakeProjectsService(this.entries);

  @override
  Future<List<Map<String, dynamic>>> list() async => entries;
}

/// Succeeds with a minimal project payload — mirrors the fake in
/// app_router_redirect_test.dart so load() never hits a real api.get().
class _OkProjectService extends ProjectService {
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

/// 404s the meta fetch while [fail] is true — what a stale `{name, ownerId}`
/// ref produces after the owner renamed the trip.
class _NotFoundProjectService extends _OkProjectService {
  bool fail = true;

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    if (fail) throw ApiException(404, '{"detail":"Not found"}');
    return super.getDetailsMeta(ref);
  }
}

/// Skips owner-only network calls (sync meta / share info) that the fakes
/// don't stub — same as app_router_redirect_test.dart.
class _TestProjectNotifier extends ProjectNotifier {
  _TestProjectNotifier(super.service);
  @override
  bool get loadOwnerExtras => false;
}
