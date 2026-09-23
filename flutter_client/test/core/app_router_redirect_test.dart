// Tests for the bare-root (`/`) redirect target used by app_router.dart's
// `redirect` callback (issue #93): a logged-in user hitting `/` should land
// on their last-opened project instead of /projects, when one is recorded.
//
// The `/`-specific decision app_router.dart's redirect defers to —
// rootRedirectTarget() in last_opened_project.dart — is tested directly here
// in isolation. The `/app` route group below additionally exercises a real
// GoRouter end-to-end (see its own comment for why it doesn't import
// app_router.dart / buildRouter() itself).

import 'dart:async' show unawaited;

import 'package:flutter/foundation.dart' show debugDefaultTargetPlatformOverride;
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/auth/welcome_screen.dart';
import 'package:traxjourney_client/src/core/app_router.dart';
import 'package:traxjourney_client/src/core/last_opened_project.dart';
import 'package:traxjourney_client/src/core/onboarding_notifier.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/app_screen.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';
import 'package:traxjourney_client/src/settings/settings_screen.dart';
import 'package:traxjourney_client/src/settings/theme_notifier.dart';

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

// Stands in for the real ProjectService so AppScreen's initState (it kicks
// off ProjectNotifier.load() via a postFrameCallback) never hits a real,
// unmocked api.get() call.
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

  // load() unconditionally kicks off a background elevation-data fetch via
  // getDetails() (not gated by loadOwnerExtras) — override it too so nothing
  // falls through to a real, unmocked api.get() call (see
  // project_stats_screen_test.dart for the same precedent).
  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async =>
      getDetailsMeta(ref);
}

/// Skips owner-only network calls (sync meta / share info) that
/// _FakeProjectService doesn't stub — irrelevant to this test.
class _TestProjectNotifier extends ProjectNotifier {
  _TestProjectNotifier(super.service);
  @override
  bool get loadOwnerExtras => false;
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  // The full auth-guard decision — authRedirectTarget() — is what
  // app_router.dart's redirect callback delegates to; tested directly here
  // (same approach as rootRedirectTarget below). Issue #106: a /join/{token}
  // invite deep link must survive the login round-trip via return_to.
  group('authRedirectTarget — /join invite deep links (issue #106)', () {
    test('unauthenticated /join/{token} redirects to /login with the invite '
        'URL as return_to', () async {
      final auth = AuthNotifier(AuthService()); // no user, not loading

      final target =
          await authRedirectTarget(auth, Uri.parse('/join/tok123'));

      expect(target, '/login?return_to=%2Fjoin%2Ftok123');
    });

    test('after login, /login?return_to=/join/{token} resolves back to the '
        'invite URL', () async {
      final auth = _loggedInAuth('user-1');

      final target = await authRedirectTarget(
          auth, Uri.parse('/login?return_to=%2Fjoin%2Ftok123'));

      expect(target, '/join/tok123');
    });

    test('logged-in /join/{token} passes through (no redirect)', () async {
      final auth = _loggedInAuth('user-1');

      final target =
          await authRedirectTarget(auth, Uri.parse('/join/tok123'));

      expect(target, isNull);
    });

    test('return_to is ignored unless it is a relative path', () async {
      final auth = _loggedInAuth('user-1');

      final target = await authRedirectTarget(
          auth, Uri.parse('/login?return_to=https%3A%2F%2Fevil.example'));

      expect(target, '/projects');
    });

    test('scheme-relative return_to (//host) is ignored too', () async {
      final auth = _loggedInAuth('user-1');

      final target = await authRedirectTarget(
          auth, Uri.parse('/login?return_to=%2F%2Fevil.example'));

      expect(target, '/projects');
    });

    test('other protected routes still redirect unauthenticated users to /',
        () async {
      final auth = AuthNotifier(AuthService());

      expect(await authRedirectTarget(auth, Uri.parse('/projects')), '/');
      // Share links stay public.
      expect(await authRedirectTarget(auth, Uri.parse('/share/tok')), isNull);
    });
  });

  // Android launch UX: bare root skips the marketing WelcomeScreen (its
  // "Sign in" button sits under the status bar there) on a native mobile
  // build, going to onboarding on first launch and straight to login after.
  // Web/desktop keep the marketing page unconditionally.
  group('authRedirectTarget — native-mobile root entry point', () {
    setUp(() => debugDefaultTargetPlatformOverride = TargetPlatform.android);
    tearDown(() => debugDefaultTargetPlatformOverride = null);

    test('unauthenticated + onboarding not yet seen goes to /onboarding',
        () async {
      final auth = AuthNotifier(AuthService());

      final target = await authRedirectTarget(auth, Uri.parse('/'),
          hasSeenOnboarding: false);

      expect(target, '/onboarding');
    });

    test('unauthenticated + onboarding already seen goes straight to /login',
        () async {
      final auth = AuthNotifier(AuthService());

      final target = await authRedirectTarget(auth, Uri.parse('/'),
          hasSeenOnboarding: true);

      expect(target, '/login');
    });

    test('logged-in users are unaffected (fall through to the usual root '
        'redirect)', () async {
      final auth = _loggedInAuth('user-1');

      final target = await authRedirectTarget(auth, Uri.parse('/'),
          hasSeenOnboarding: false);

      expect(target, isNot('/onboarding'));
      expect(target, isNot('/login'));
    });

    test('/onboarding itself stays public for an unauthenticated visitor',
        () async {
      final auth = AuthNotifier(AuthService());

      final target = await authRedirectTarget(auth, Uri.parse('/onboarding'),
          hasSeenOnboarding: false);

      expect(target, isNull);
    });

    test('on web, root keeps showing the marketing page regardless of '
        'onboarding state', () async {
      debugDefaultTargetPlatformOverride = null; // kIsWeb is what matters
      // flutter_test always runs on the VM (kIsWeb == false), so this proves
      // the non-mobile branch is reachable rather than simulating a browser —
      // the desktop/`TargetPlatform.linux` case below covers the same path.
      debugDefaultTargetPlatformOverride = TargetPlatform.linux;
      final auth = AuthNotifier(AuthService());

      final target = await authRedirectTarget(auth, Uri.parse('/'),
          hasSeenOnboarding: false);

      expect(target, isNull);
    });
  });

  test(
      'resolves to /view?project=<name> when a last-opened project pref is set',
      () async {
    final auth = _loggedInAuth('user-1');
    await saveLastOpenedProject(auth.user!.id, const ProjectRef(name: 'Trip A'));

    final target = await rootRedirectTarget(auth.user?.id);

    expect(target, '/view?project=Trip%20A');
  });

  test('resolves to /projects when no last-opened project pref is set',
      () async {
    final auth = _loggedInAuth('user-1');

    final target = await rootRedirectTarget(auth.user?.id);

    expect(target, '/projects');
  });

  test('resolves to /projects for a different user than the one who saved',
      () async {
    final saver = _loggedInAuth('user-1');
    await saveLastOpenedProject(saver.user!.id, const ProjectRef(name: 'Trip A'));
    final otherUser = _loggedInAuth('user-2');

    final target = await rootRedirectTarget(otherUser.user?.id);

    expect(target, '/projects');
  });

  // Regression for the Android "website flash" bug: the `/` route's redirect
  // is async, so between the splash disappearing (auth.isRestoring flips
  // false) and the redirect actually landing on /login, the router is
  // briefly still parked on `/` and its builder runs. On native mobile that
  // builder must never construct the marketing WelcomeScreen — see the
  // `isNativeMobile` branch in app_router.dart's `/` GoRoute.
  testWidgets(
      'native mobile: WelcomeScreen never mounts at "/", even mid-redirect',
      (tester) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    // Reset synchronously before this function returns rather than via
    // addTearDown: flutter_test's post-test invariant check (which asserts
    // this is back to null) runs before package:test flushes addTearDown
    // callbacks, so an addTearDown reset lands one beat too late and trips
    // "The value of a foundation debug variable was changed by the test."
    try {
      final auth = AuthNotifier(AuthService());

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<AuthNotifier>.value(value: auth),
            ChangeNotifierProvider<OnboardingNotifier>(
                create: (_) => OnboardingNotifier(true)),
            ChangeNotifierProvider<ThemeNotifier>(
                create: (_) => ThemeNotifier()),
          ],
          child: Builder(
            builder: (context) {
              final router = buildRouter(context);
              return MaterialApp.router(routerConfig: router);
            },
          ),
        ),
      );

      // Mirrors main.dart's `..init()` cascade: kicks off session restore
      // (isLoading/isRestoring true, so the redirect defers and `/` builds)
      // and lets it settle (isLoading/isRestoring flip false, triggering the
      // redirect's async re-evaluation) — WelcomeScreen must not appear at
      // any point in between.
      unawaited(auth.init());
      await tester.pump();
      expect(find.byType(WelcomeScreen), findsNothing);

      await tester.pumpAndSettle();
      expect(find.byType(WelcomeScreen), findsNothing);
    } finally {
      debugDefaultTargetPlatformOverride = null;
    }
  });

  // `app_screen.dart` used to import `dart:html` directly, which made it (and
  // anything that imports it, including app_router.dart's `/app` route)
  // uncompilable under flutter_test's default VM platform. That's now fixed
  // by extracting the browser-download call into a conditional-import pair
  // (see app_screen.dart / download_web.dart / download_stub.dart). This
  // proves the fix: a real GoRouter's `/app` route builder — using the exact
  // same builder body as app_router.dart's `/app` GoRoute — resolves to a
  // mounted AppScreen with no compile/platform error.
  //
  // Historically this group did NOT import app_router.dart / call
  // buildRouter() itself: `lib/src/settings/settings_screen.dart` (reachable
  // from buildRouter() via its `/settings` route) unconditionally imported
  // `dart:js_interop` / `package:web` for a Strava OAuth popup flow
  // (`window.open` + a `postMessage` listener stored as a `JSFunction` state
  // field) — a second, separate VM-compile blocker (issue #99) the original
  // dart:html investigation didn't catch. That's now fixed too (the popup +
  // postMessage handshake moved into a conditional-import pair,
  // strava_oauth_popup_stub.dart / strava_oauth_popup_web.dart), so
  // buildRouter() itself is exercised end-to-end below in the
  // "buildRouter() end-to-end" group.
  group('/app route (GoRouter, mirroring app_router.dart)', () {
    testWidgets('/app?project=<name> resolves to a mounted AppScreen',
        (tester) async {
      final auth = _loggedInAuth('user-1');
      final router = GoRouter(
        initialLocation: '/app?project=${Uri.encodeComponent('Trip A')}',
        routes: [
          GoRoute(
            path: '/app',
            builder: (context, state) {
              final projectName = state.uri.queryParameters['project'] ?? '';
              return AppScreen(projectName: projectName);
            },
          ),
        ],
      );

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<AuthNotifier>.value(value: auth),
            ChangeNotifierProvider<ProjectNotifier>(
                create: (_) => _TestProjectNotifier(_FakeProjectService())),
          ],
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      // Deliberately not pumpAndSettle(): AppScreen mounts a real map (tile
      // fetches, animation controllers) that never fully quiesces under
      // flutter_test. A couple of plain pumps is enough to mount the route
      // and run its first postFrameCallback.
      await tester.pump();
      await tester.pump();

      expect(find.byType(AppScreen), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  });

  // Proves issue #99 is closed: the real buildRouter() from app_router.dart
  // — including its `/settings` route, which used to drag in
  // settings_screen.dart's unconditional dart:js_interop/package:web imports
  // — now compiles and mounts under flutter_test's VM platform, for both the
  // `/app` and `/settings` routes.
  group('buildRouter() end-to-end (real router)', () {
    testWidgets('/app and /settings both mount with no compile/platform error',
        (tester) async {
      final auth = _loggedInAuth('user-1');
      late GoRouter router;

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider<AuthNotifier>.value(value: auth),
            ChangeNotifierProvider<OnboardingNotifier>(
                create: (_) => OnboardingNotifier(true)),
            ChangeNotifierProvider<ProjectNotifier>(
                create: (_) => _TestProjectNotifier(_FakeProjectService())),
            ChangeNotifierProvider<ThemeNotifier>(
                create: (_) => ThemeNotifier()),
            // No last-opened-project pref is saved for this user, so the
            // initial `/` redirect (rootRedirectTarget) lands on /projects
            // before we navigate away below — ProjectsScreen needs a
            // ProjectsNotifier in the tree to mount. Its .load() is never
            // triggered here (that's normally wired via a
            // ChangeNotifierProxyProvider in main.dart, not present in this
            // test), so ProjectsService.list() is never actually called.
            ChangeNotifierProvider<ProjectsNotifier>(
                create: (_) => ProjectsNotifier(ProjectsService())),
          ],
          child: Builder(
            builder: (context) {
              router = buildRouter(context);
              return MaterialApp.router(routerConfig: router);
            },
          ),
        ),
      );
      // Let the initial `/` redirect (rootRedirectTarget) settle.
      await tester.pump();
      await tester.pump();

      router.go('/app?project=${Uri.encodeComponent('Trip A')}');
      // Deliberately not pumpAndSettle(): AppScreen mounts a real map (tile
      // fetches, animation controllers) that never fully quiesces under
      // flutter_test — same reasoning as the /app group above. Unlike that
      // group (whose initialLocation is /app with nothing to transition
      // from), this navigates away from the /projects page the initial `/`
      // redirect landed on, so it's a real push transition — pump with a
      // duration a few times so it progresses far enough to mount the page.
      await tester.pump(const Duration(milliseconds: 50));
      await tester.pump(const Duration(milliseconds: 50));
      await tester.pump(const Duration(milliseconds: 50));
      expect(find.byType(AppScreen), findsOneWidget);
      expect(tester.takeException(), isNull);

      router.go('/settings');
      await tester.pump(const Duration(milliseconds: 50));
      await tester.pump(const Duration(milliseconds: 50));
      await tester.pump(const Duration(milliseconds: 50));
      expect(find.byType(SettingsScreen), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  });
}
