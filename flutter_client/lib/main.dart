import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:flutter_web_plugins/url_strategy.dart';
import 'package:go_router/go_router.dart';
import 'package:google_sign_in/google_sign_in.dart';
import 'package:provider/provider.dart';

import 'src/api/client.dart';
import 'src/auth/auth_service.dart';
import 'src/auth/auth_notifier.dart';
import 'src/projects/projects_service.dart';
import 'src/projects/projects_notifier.dart';
import 'src/projects/project_service.dart';
import 'src/projects/photo_thumb_cache.dart';
import 'src/projects/project_data_cache.dart';
import 'src/projects/facets/project_facet_providers.dart';
import 'src/projects/project_notifier.dart';
import 'src/settings/theme_notifier.dart';
import 'src/core/app_router.dart';
import 'src/core/brand.dart';
import 'src/core/last_opened_project.dart';
import 'src/core/onboarding_notifier.dart';
import 'src/core/perf_timing.dart';
import 'src/core/server_config.dart';
import 'src/core/splash_screen.dart';
import 'src/core/theme.dart';
import 'src/core/version_gate.dart';

// Server client ID used on Android/iOS to receive an idToken.
// On web, GIS reads the client ID from <meta name="google-signin-client_id">
// in web/index.html — so serverClientId must be null on web.
const _kGoogleServerClientId =
    '544571555396-gj0q3hndadfo00ifotme305jcf4ii5cc.apps.googleusercontent.com';

void main() async {
  if (kIsWeb) usePathUrlStrategy();
  WidgetsFlutterBinding.ensureInitialized();
  // No-op unless built with --dart-define=PERF_TIMING=true (dev measurement).
  PerfTiming.instance.start();
  // Event-loop stall watchdog — the only instrument that sees a freeze, since
  // a blocked main thread emits no frame timings at all (issue #276).
  perfSpans.start();
  // Fire-and-forget: login_screen.initState calls attemptLightweightAuthentication()
  // which handles ordering internally. Deferring unblocks the first frame.
  GoogleSignIn.instance.initialize(
    serverClientId: kIsWeb ? null : _kGoogleServerClientId,
  );
  // A saved self-hosting override (login screen's "Self-hosting?" link) takes
  // effect on the next cold start — swap the singleton before anything reads
  // it. Two fast local reads, awaited here rather than gated behind their own
  // isLoading flags like AuthNotifier's session restore.
  final customServerUrl = await readCustomServerUrl();
  if (customServerUrl != null) api = ApiClient(baseUrl: customServerUrl);
  final hasSeenOnboarding = await readHasSeenOnboarding();
  await projectDataCache.init();
  // State the empty user id wrote before ids were real (issue #418). Before
  // runApp, so before the router's first redirect or any load can read it.
  await purgeSharedLastOpenedProject();
  await projectDataCache.purgeUserZero();
  await photoThumbCache.init();
  runApp(
    // MultiProvider lives here — above TraxJourneyApp — so its providers are
    // never reconstructed by theme changes. Only the Builder inside
    // TraxJourneyApp (which watches ThemeNotifier) rebuilds on theme toggles.
    MultiProvider(
      providers: [
        ChangeNotifierProvider<ThemeNotifier>(
          create: (_) => ThemeNotifier(),
        ),
        ChangeNotifierProvider<AuthNotifier>(
          create: (_) => AuthNotifier(AuthService())..init(),
        ),
        ChangeNotifierProvider<OnboardingNotifier>(
          create: (_) => OnboardingNotifier(hasSeenOnboarding),
        ),
        ChangeNotifierProxyProvider<AuthNotifier, ProjectsNotifier>(
          create: (_) => ProjectsNotifier(ProjectsService()),
          update: (_, auth, previous) =>
              previous!..onAuthChanged(auth.user != null),
        ),
        accountScopedProjectNotifier(() => ProjectNotifier(ProjectService())),
      ],
      // The app-wide notifier's facets (#294), following it across account
      // changes.
      child: const ProjectFacetProviders<ProjectNotifier>(
        child: TraxJourneyApp(),
      ),
    ),
  );
}

/// The app-wide [ProjectNotifier], one per signed-in account (issue #418).
///
/// Whenever [ProjectNotifier.onAuthChanged] reports an account change —
/// including to or from no account — this hands out a fresh notifier, and
/// the provider disposes the one it replaces. A response the old account
/// started can only land in that discarded instance: trip checks compare name
/// and owner, and an own trip has no owner, so in a shared instance it passed
/// them on the next account's trip of the same name (I1-R4-1, I1-R4-2).
///
/// Not lazy, so every auth change reaches it — a lazy proxy updates only when
/// read, and would miss a logout followed by the same account signing back in
/// before anything read it.
ChangeNotifierProxyProvider<AuthNotifier, ProjectNotifier>
    accountScopedProjectNotifier(ProjectNotifier Function() create) =>
        ChangeNotifierProxyProvider<AuthNotifier, ProjectNotifier>(
          lazy: false,
          create: (_) => create(),
          update: (_, auth, previous) {
            final userId = auth.user?.id;
            if (!previous!
                .onAuthChanged(userId, restoring: auth.isRestoring)) {
              return previous;
            }
            // The new account is the first this notifier sees, not a change.
            return create()..onAuthChanged(userId);
          },
        );

class TraxJourneyApp extends StatefulWidget {
  const TraxJourneyApp({super.key});

  @override
  State<TraxJourneyApp> createState() => _TraxJourneyAppState();
}

class _TraxJourneyAppState extends State<TraxJourneyApp> {
  GoRouter? _router;

  @override
  Widget build(BuildContext context) {
    // Only the theme mode is watched here — this is the only widget that
    // rebuilds on theme change, and it only affects MaterialApp.router params.
    final themeMode = context.watch<ThemeNotifier>().mode;
    // Router is created once and reused — recreating it would destroy nav stack.
    _router ??= buildRouter(context);
    return MaterialApp.router(
      title: kAppName,
      theme: lightTheme,
      darkTheme: darkTheme,
      themeMode: themeMode,
      routerConfig: _router!,
      // Wraps every page: SplashGate holds the brand splash over the app until
      // the persisted session has been restored; VersionGate detects a stale
      // cached web bundle (client APP_VERSION != deployed /api/version) and
      // surfaces a Reload prompt.
      builder: (context, child) => SplashGate(
        child: VersionGate(child: child ?? const SizedBox.shrink()),
      ),
      // Visual UI/raster bars alongside the console percentiles — dev only.
      showPerformanceOverlay: kPerfTiming,
      debugShowCheckedModeBanner: false,
    );
  }
}
