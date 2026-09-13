/// Named routes + auth guard for the TraxJourney Flutter client.
library;

import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../admin/admin_screen.dart';
import '../auth/auth_notifier.dart';
import '../auth/forced_change_password_screen.dart';
import '../auth/login_screen.dart';
import '../auth/onboarding_screen.dart';
import '../auth/register_screen.dart';
import '../auth/verify_email_screen.dart';
import '../auth/welcome_screen.dart';
import '../billing/billing_service.dart' show kPlanRoute;
import '../billing/plan_screen.dart';
import 'last_opened_project.dart';
import 'onboarding_notifier.dart';
import 'platform.dart';
import 'return_to.dart';
import 'splash_screen.dart' show kSplashBackground;
import '../projects/projects_screen.dart';
import '../projects/app_screen.dart';
import '../projects/join_trip_screen.dart';
import '../projects/poster_download_screen.dart';
import '../projects/view_screen.dart';
import '../projects/strava_import_screen.dart';
import '../projects/strava_import_notifier.dart';
import '../projects/polarsteps_import_screen.dart';
import '../projects/polarsteps_import_notifier.dart';
import '../projects/project_settings_screen.dart';
import '../projects/project_stats_screen.dart';
import '../settings/settings_screen.dart';
import '../shared/shared_project_screen.dart';

/// The router's `initialLocation` for [base] on this platform, or null to let
/// the platform choose.
///
/// On web, derive the starting location from the actual browser URL so that
/// deep links (e.g. /share/TOKEN) are honoured even before auth resolves.
///
/// On Android/iOS, null is the answer that makes App Links work: go_router then
/// starts from the platform's default route, which is the path of the intent
/// that launched the app (`https://traxjourney.com/share/TOKEN` arrives as
/// `/share/TOKEN`). Returning '/' here — as this did — silently discarded every
/// incoming deep link. With no intent, the platform default is '/' anyway, so
/// a normal launch is unaffected.
String? initialLocationFor({required bool isWeb, required Uri base}) {
  if (!isWeb) return null;
  final path = base.path;
  return (path.isEmpty || path == '/') ? '/' : path;
}

/// Reads the `owner` query param (issue #106 — shared-project addressing)
/// as an int, or null when absent/malformed (own project).
int? _ownerParam(GoRouterState state) =>
    int.tryParse(state.uri.queryParameters['owner'] ?? '');

/// The auth-guard decision for [uri] given the current [auth] state: the
/// location to redirect to, or null to stay. Extracted from buildRouter's
/// redirect callback so it can be unit-tested directly (like
/// rootRedirectTarget — see app_router_redirect_test.dart).
Future<String?> authRedirectTarget(
  AuthNotifier auth,
  Uri uri, {
  bool hasSeenOnboarding = true,
}) async {
  // Wait for restoreSession to complete before making routing decisions.
  if (auth.isLoading) return null;

  final isLoggedIn = auth.user != null;
  // Use the real browser URL path, not matchedLocation, so that timing
  // issues during auth init don't cause stale-route redirects.
  final loc = uri.path;

  // Shared-project links are accessible without login.
  if (loc.startsWith('/share/')) return null;

  // Email-verification links (issue #110) are accessible either way: the
  // recipient clicks from their inbox, which may be a different browser with
  // no session — and a signed-in user must not be bounced to /projects before
  // the token is consumed.
  if (loc.startsWith('/verify-email/')) return null;

  // Poster-ready/failed notification links (issue #14) — same reasoning as
  // /verify-email/ above: reached from an email, possibly with no session.
  if (loc.startsWith('/poster/')) return null;

  // Invite deep links (issue #106) require login. Send the visitor to
  // the login screen with the invite URL as return_to — the same
  // mechanism the shared-project screen's sign-in link uses (see
  // login_screen._returnTo) — so the token survives the login round-trip.
  if (!isLoggedIn && loc.startsWith('/join/')) {
    return '/login?return_to=${Uri.encodeComponent(loc)}';
  }

  // On a native Android/iOS build, bare root never shows the marketing
  // WelcomeScreen (its "Sign in" button sits under the status bar there
  // anyway — it's built for a browser chrome, not a phone). First launch
  // gets the onboarding carousel; every launch after gets the login screen
  // directly. Web/desktop keep the marketing page, untouched below.
  if (!isLoggedIn && isNativeMobile && loc == '/') {
    return hasSeenOnboarding ? '/login' : '/onboarding';
  }

  // Redirect unauthenticated users away from protected routes.
  final isPublicPage =
      loc == '/' || loc == '/login' || loc == '/register' || loc == '/onboarding';
  if (!isLoggedIn && !isPublicPage) return '/';

  // Redirect authenticated users away from public pages. Bare root goes
  // straight to the user's last-opened project (issue #93) instead of
  // /projects, if one was recorded; otherwise falls through to /projects
  // as before.
  if (isLoggedIn && isPublicPage) {
    // Honour a pending return_to (share links, /join invites) so the
    // deep link survives even when this redirect wins the race against
    // LoginScreen._navigateAfterLogin (which applies the same guard).
    final ret = safeReturnTo(uri.queryParameters['return_to']);
    if (ret != null) return ret;
    if (loc == '/') return rootRedirectTarget(auth.user?.id);
    return '/projects';
  }

  // Force a password change when required (seeded admin / admin-reset users).
  if (isLoggedIn && auth.user!.passwordChangeRequired) {
    return loc == '/change-password' ? null : '/change-password';
  }
  // Don't linger on the change-password page once it's no longer required.
  if (loc == '/change-password') return '/projects';

  // The admin route is admin-only (the server also enforces this with 403).
  if (loc == '/admin' && !(auth.user?.isAdmin ?? false)) return '/projects';

  return null;
}

GoRouter buildRouter(BuildContext context) {
  final authNotifier = context.read<AuthNotifier>();
  final onboardingNotifier = context.read<OnboardingNotifier>();

  return GoRouter(
    initialLocation: initialLocationFor(isWeb: kIsWeb, base: Uri.base),
    // Re-evaluate redirect whenever auth state or the onboarding flag changes
    // (login / logout / init / onboarding markSeen()).
    refreshListenable: Listenable.merge([authNotifier, onboardingNotifier]),

    redirect: (BuildContext ctx, GoRouterState state) => authRedirectTarget(
      ctx.read<AuthNotifier>(),
      state.uri,
      hasSeenOnboarding: ctx.read<OnboardingNotifier>().hasSeenOnboarding,
    ),

    routes: [
      GoRoute(
        path: '/',
        // On native mobile the auth guard always redirects `/` away (see
        // authRedirectTarget above) — but that redirect is async, so a plain
        // `WelcomeScreen()` here would still get mounted and painted for a
        // frame first, flashing the marketing page in between the splash
        // disappearing and the redirect landing on /login. Painting the
        // splash's own background instead makes that frame invisible.
        builder: (context, state) => isNativeMobile
            ? const ColoredBox(color: kSplashBackground)
            : const WelcomeScreen(),
      ),
      GoRoute(
        path: '/onboarding',
        builder: (context, state) => const OnboardingScreen(),
      ),
      GoRoute(
        path: '/login',
        builder: (context, state) => const LoginScreen(),
      ),
      GoRoute(
        path: '/register',
        builder: (context, state) => const RegisterScreen(),
      ),
      GoRoute(
        path: '/change-password',
        builder: (context, state) => const ForcedChangePasswordScreen(),
      ),
      GoRoute(
        path: '/admin',
        builder: (context, state) => AdminScreen(),
      ),
      GoRoute(
        path: '/projects',
        builder: (context, state) => const ProjectsScreen(),
      ),
      GoRoute(
        path: '/app',
        builder: (context, state) {
          final projectName =
              state.uri.queryParameters['project'] ?? '';
          return AppScreen(
            projectName: projectName,
            ownerId: _ownerParam(state),
            initialLat: double.tryParse(state.uri.queryParameters['lat'] ?? ''),
            initialLng: double.tryParse(state.uri.queryParameters['lng'] ?? ''),
            initialZoom: double.tryParse(state.uri.queryParameters['zoom'] ?? ''),
          );
        },
      ),
      GoRoute(
        path: '/view',
        builder: (context, state) {
          final projectName =
              state.uri.queryParameters['project'] ?? '';
          return ViewScreen(
            projectName: projectName,
            ownerId: _ownerParam(state),
            initialLat: double.tryParse(state.uri.queryParameters['lat'] ?? ''),
            initialLng: double.tryParse(state.uri.queryParameters['lng'] ?? ''),
            initialZoom: double.tryParse(state.uri.queryParameters['zoom'] ?? ''),
          );
        },
      ),
      GoRoute(
        path: '/strava-import',
        builder: (context, state) {
          final projectName =
              state.uri.queryParameters['project'] ?? '';
          return ChangeNotifierProvider(
            create: (_) => StravaImportNotifier(),
            child: StravaImportScreen(
                projectName: projectName, ownerId: _ownerParam(state)),
          );
        },
      ),
      GoRoute(
        path: '/polarsteps-import',
        builder: (context, state) {
          final projectName =
              state.uri.queryParameters['project'] ?? '';
          return ChangeNotifierProvider(
            create: (_) => PolarstepsImportNotifier(),
            child: PolarstepsImportScreen(
                projectName: projectName, ownerId: _ownerParam(state)),
          );
        },
      ),
      GoRoute(
        path: '/stats',
        builder: (context, state) {
          final projectName =
              state.uri.queryParameters['project'] ?? '';
          // Tags/groups come from the ambient ProjectNotifier, not
          // GoRouterState.extra — extra isn't URL-encoded, so it's lost on a
          // forced reload (issue #76 follow-up). See ProjectStatsScreen.
          return ProjectStatsScreen(
              projectName: projectName, ownerId: _ownerParam(state));
        },
      ),
      GoRoute(
        path: '/project-settings',
        builder: (context, state) {
          final projectName = state.uri.queryParameters['project'] ?? '';
          return ProjectSettingsScreen(
              projectName: projectName, ownerId: _ownerParam(state));
        },
      ),
      GoRoute(
        path: '/settings',
        builder: (context, state) => const SettingsScreen(),
      ),
      GoRoute(
        // A real route rather than a pushed MaterialPageRoute: the payment
        // provider redirects the browser here by URL after checkout or a plan
        // change (issue #153), carrying ?checkout=success|cancelled.
        path: kPlanRoute,
        builder: (context, state) => PlanScreen(
          checkoutOutcome: state.uri.queryParameters['checkout'],
        ),
      ),
      GoRoute(
        path: '/join/:token',
        builder: (context, state) =>
            JoinTripScreen(token: state.pathParameters['token']!),
      ),
      GoRoute(
        path: '/verify-email/:token',
        builder: (context, state) =>
            VerifyEmailScreen(token: state.pathParameters['token']!),
      ),
      GoRoute(
        path: '/poster/:token',
        builder: (context, state) =>
            PosterDownloadScreen(token: state.pathParameters['token']!),
      ),
      GoRoute(
        path: '/share/:token',
        builder: (context, state) {
          final token = state.pathParameters['token']!;
          // Optional deep link to a specific memory by its stable public_id.
          final memoryPublicId = state.uri.queryParameters['memory'];
          return SharedProjectScreen(
            token: token,
            initialMemoryPublicId: memoryPublicId,
          );
        },
      ),
    ],
  );
}
