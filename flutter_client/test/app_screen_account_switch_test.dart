// AppScreen reuses the app-wide ProjectNotifier when it already holds the
// trip it is asked for (see app_screen_remount_load_guard_test.dart). Two ways
// that reuse handed over state that was not this screen's to keep (issue
// #418):
//
//  - Across accounts. Two accounts' own trips of the same name, deep-linked,
//    have equal refs: account B opening /app?project=Japan after A logged out
//    reused A's notifier — A's whole trip, and A's filter. Now the account
//    owns the notifier, through the same provider main.dart builds, and an
//    account change clears it, for an explicit logout and a 401 alike.
//  - Within one account. View mode changes the saved filter, and the reused
//    edit notifier kept its own copy, which the next tap saved over view
//    mode's. Now a reuse re-reads the saved state first.

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/main.dart' show accountScopedProjectNotifier;
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/app_screen.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import 'helpers/signed_in.dart';

const _day1 = '2024-04-01';
const _day2 = '2024-04-02';

/// Each account's own "Japan": same name, no owner in the deep link, and the
/// same nights — so a filter carried across would survive pruning.
class _Service extends ProjectService {
  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => {
        'name': ref.name,
        'activities': <dynamic>[],
        'items': <dynamic>[],
        'people': <dynamic>[],
        'groups': <dynamic>[],
        'trip_end': _day2,
        'day_meta': {
          _day1: {'sleeping': 'Hotel'},
          _day2: {'sleeping': 'Camping'},
        },
      };

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref,
          {bool bypassCache = false}) async =>
      getDetailsMeta(ref);
}

class _CountingProjectNotifier extends ProjectNotifier {
  _CountingProjectNotifier(super.service);

  int loadCallCount = 0;

  @override
  bool get loadOwnerExtras => false;

  @override
  Future<void> load(ProjectRef ref) {
    loadCallCount++;
    return super.load(ref);
  }
}

/// Signs in the account [loginWithPassword] is given the id of as its email;
/// restores account 1, finishing when [restoreGate] (if any) completes, with
/// a /me that answers when the test says.
class _AuthService extends AuthService {
  _AuthService({this.restoreGate});

  final Completer<void>? restoreGate;
  final me = Completer<Map<String, dynamic>>();

  @override
  Future<bool> restoreSession() async {
    api.setToken(_restorableJwt(1));
    await restoreGate?.future;
    return true;
  }

  @override
  Future<void> appOpened(String sessionState) async {}

  @override
  Future<Map<String, dynamic>> getMe() => me.future;

  @override
  Future<Map<String, dynamic>> loginWithPassword(String email, String pw) async {
    final id = int.parse(email);
    api.setToken(fakeJwt(sub: id));
    return {'id': id, 'email': '$id@example.com', 'auth_provider': 'local'};
  }

  @override
  Future<void> logout() async => api.clearToken();
}

/// A token for account [sub] with days left on it, so a restore trusts it
/// and checks /me in the background.
String _restorableJwt(int sub) {
  String seg(Object payload) =>
      base64Url.encode(utf8.encode(jsonEncode(payload))).replaceAll('=', '');
  final exp = DateTime.now().toUtc().add(const Duration(days: 3));
  return '${seg({'alg': 'none'})}.'
      '${seg({'sub': '$sub', 'exp': exp.millisecondsSinceEpoch ~/ 1000})}.sig';
}

String _japan() => '/app?project=${Uri.encodeComponent('Japan')}';

class _Harness {
  _Harness(this.tester, this.auth);

  final WidgetTester tester;
  final AuthNotifier auth;
  late final _CountingProjectNotifier notifier;
  late final GoRouter router;

  Future<void> pump() async {
    router = GoRouter(
      initialLocation: '/other',
      routes: [
        GoRoute(
          path: '/app',
          builder: (context, state) => AppScreen(
              projectName: state.uri.queryParameters['project'] ?? ''),
        ),
        // Stands in for /login and for view mode: anything that unmounts
        // AppScreen, so the next /app is a genuine remount.
        GoRoute(path: '/other', builder: (context, state) => const SizedBox()),
      ],
    );
    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<AuthNotifier>.value(value: auth),
        accountScopedProjectNotifier(
            () => notifier = _CountingProjectNotifier(_Service())),
      ],
      child: MaterialApp.router(routerConfig: router),
    ));
    await tester.pump();
  }

  Future<void> go(String location) async {
    router.go(location);
    await tester.pump();
    await tester.pump();
    // AppScreen mounts a real map that never quiesces, so no pumpAndSettle.
    for (var i = 0; i < 20 && notifier.isLoading; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    await tester.pump();
  }

  Future<void> settle() async {
    await tester.runAsync(pumpEventQueue);
    await tester.pump();
  }
}

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    projectDataCache.resetForTest();
    api = ApiClient();
  });

  testWidgets(
      "after a logout, the next account's deep link to a trip of the same "
      'name loads its own, with no filter', (tester) async {
    final auth = AuthNotifier(_AuthService());
    final h = _Harness(tester, auth);
    await h.pump();

    await tester.runAsync(() => auth.loginWithPassword('1', 'pw'));
    await h.go(_japan());
    expect(h.notifier.loadCallCount, 1);
    h.notifier.setFilters(sleeping: {'Hotel'});
    await h.settle();
    expect(h.notifier.sleepingFilter, {'Hotel'});

    await tester.runAsync(auth.logout);
    await h.go('/other');
    await tester.runAsync(() => auth.loginWithPassword('2', 'pw'));
    await h.go(_japan());

    expect(h.notifier.loadCallCount, 2,
        reason: "account 2 must get its own trip, not account 1's");
    expect(h.notifier.sleepingFilter, isEmpty);
    expect(h.notifier.hasActiveFilter, isFalse);
  });

  testWidgets('the same holds after a 401 forces the logout', (tester) async {
    final service = _AuthService();
    final auth = AuthNotifier(service);
    await tester.runAsync(auth.init);
    expect(auth.user?.id, '1', reason: 'restored, /me still out');
    final h = _Harness(tester, auth);
    await h.pump();

    await h.go(_japan());
    expect(h.notifier.loadCallCount, 1);
    h.notifier.setFilters(sleeping: {'Hotel'});
    await h.settle();

    service.me.completeError(ApiException(401, '{"detail":"expired"}'));
    await h.settle();
    expect(auth.user, isNull);
    await h.go('/other');
    await tester.runAsync(() => auth.loginWithPassword('2', 'pw'));
    await h.go(_japan());

    expect(h.notifier.loadCallCount, 2);
    expect(h.notifier.sleepingFilter, isEmpty);
    expect(h.notifier.hasActiveFilter, isFalse);
  });

  testWidgets(
      'neither the end of a session restore nor /me landing clears the trip '
      'the restored account is opening', (tester) async {
    final restoreGate = Completer<void>();
    final service = _AuthService(restoreGate: restoreGate);
    // As main.dart's create does: restoring from before the first frame.
    final auth = AuthNotifier(service);
    unawaited(auth.init());
    final h = _Harness(tester, auth);
    await h.pump();

    // A deep link opened under the splash: the trip loads while the session
    // is still being restored, with the restored token.
    await h.go(_japan());
    expect(auth.isRestoring, isTrue);
    expect(h.notifier.ref?.name, 'Japan');

    restoreGate.complete();
    await h.settle();
    expect(auth.isRestoring, isFalse);
    expect(auth.user?.id, '1');
    expect(h.notifier.ref?.name, 'Japan',
        reason: 'the restore naming its account is not an account change');

    service.me.complete(
        {'sub': '1', 'email': '1@example.com', 'auth_provider': 'local'});
    await h.settle();

    expect(auth.user?.email, '1@example.com');
    expect(h.notifier.ref?.name, 'Japan', reason: 'same account, same trip');
    expect(h.notifier.loadCallCount, 1);
  });

  testWidgets(
      'a filter view mode changed survives returning to the reused edit '
      'notifier, and the next tap', (tester) async {
    final auth = AuthNotifier(_AuthService());
    final h = _Harness(tester, auth);
    await h.pump();
    await tester.runAsync(() => auth.loginWithPassword('1', 'pw'));
    await h.go(_japan());
    expect(h.notifier.sleepingFilter, isEmpty);

    // View mode: its own notifier, the same account's saved state.
    await h.go('/other');
    final view = ProjectNotifier(_Service());
    await tester.runAsync(() async {
      await view.load(const ProjectRef(name: 'Japan'));
      view.setFilters(sleeping: {'Hotel'});
      await pumpEventQueue();
    });

    await h.go(_japan());
    await h.settle();
    expect(h.notifier.loadCallCount, 1, reason: 'still reused, not reloaded');
    expect(h.notifier.sleepingFilter, {'Hotel'});

    h.notifier.selectDay(_day1);
    await h.settle();
    final prefs = await tester.runAsync(SharedPreferences.getInstance);
    final saved = jsonDecode(prefs!.getString('project_ui_state_1:1:Japan')!)
        as Map<String, dynamic>;
    expect(saved['sleeping'], ['Hotel'],
        reason: "the tap must not save the edit notifier's stale filter");
  });
}
