// Apple's sign-in script is loaded on demand (I1-3): web/index.html no longer
// loads it on every page, the login screen starts the load when it shows the
// Apple button, a tap waits for a load still running, and a failed load is
// reported and retried. The page is faked through apple_js_loader.dart's
// appleJsLoader and appleNeedsJs, since widget tests never run as web.

import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:sign_in_with_apple/sign_in_with_apple.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/apple_js_loader.dart';
import 'package:traxjourney_client/src/auth/apple_sign_in.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/auth/login_screen.dart';

const _credential = AuthorizationCredentialAppleID(
  userIdentifier: null,
  givenName: null,
  familyName: null,
  authorizationCode: 'the-code',
  email: null,
  identityToken: 'the-id-token',
  state: null,
);

/// A page whose script loads complete when the test says so.
class _FakePage {
  final injected = <String>[];
  final pending = <Completer<void>>[];

  Future<void> inject(String src) {
    injected.add(src);
    final c = Completer<void>();
    pending.add(c);
    return c.future;
  }
}

Future<void> _pumpLogin(WidgetTester tester) async {
  final router = GoRouter(
    initialLocation: '/login',
    routes: [
      GoRoute(path: '/login', builder: (_, __) => const LoginScreen()),
    ],
  );
  await tester.pumpWidget(
    ChangeNotifierProvider<AuthNotifier>(
      create: (_) => AuthNotifier(AuthService()),
      child: MaterialApp.router(routerConfig: router),
    ),
  );
  await tester.pumpAndSettle();
}

void _testOnAndroid(String description, WidgetTesterCallback body) =>
    testWidgets(description, body,
        variant: TargetPlatformVariant.only(TargetPlatform.android));

void main() {
  late _FakePage page;
  late bool originalNeedsJs;
  late AppleJsLoader originalLoader;
  late String originalServicesId;
  late AppleCredentialRequester originalRequester;
  late int appleCalls;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    page = _FakePage();
    originalNeedsJs = appleNeedsJs;
    originalLoader = appleJsLoader;
    originalServicesId = appleServicesId;
    originalRequester = requestAppleCredential;
    appleJsLoader = AppleJsLoader(page.inject);
    appleCalls = 0;
    requestAppleCredential = (
        {required String nonce,
        WebAuthenticationOptions? webAuthenticationOptions}) async {
      appleCalls++;
      return _credential;
    };
  });

  tearDown(() {
    appleNeedsJs = originalNeedsJs;
    appleJsLoader = originalLoader;
    appleServicesId = originalServicesId;
    requestAppleCredential = originalRequester;
  });

  test('web/index.html loads neither Apple nor Google sign-in scripts', () {
    // `flutter test` runs from the package root.
    final html = File('web/index.html').readAsStringSync();
    expect(html, isNot(contains('appleid.cdn-apple.com')));
    expect(html, isNot(contains('accounts.google.com/gsi')));
  });

  group('AppleJsLoader', () {
    test('injects the script once, and is loaded only after its load event',
        () async {
      final first = appleJsLoader.load();
      final second = appleJsLoader.load();

      expect(page.injected, [appleJsUrl]);
      expect(appleJsLoader.isLoaded, isFalse);

      page.pending.single.complete();
      await Future.wait([first, second]);

      expect(appleJsLoader.isLoaded, isTrue);
      await appleJsLoader.load();
      expect(page.injected, hasLength(1));
    });

    test('a failed load throws, stays unloaded, and the next load retries',
        () async {
      final failed = appleJsLoader.load();
      page.pending.single.completeError(StateError('offline'));

      await expectLater(failed, throwsA(isA<AppleJsLoadException>()));
      expect(appleJsLoader.isLoaded, isFalse);

      final retry = appleJsLoader.load();
      expect(page.injected, hasLength(2));
      page.pending.last.complete();
      await retry;
      expect(appleJsLoader.isLoaded, isTrue);
    });

    test('preloading is a no-op where Apple JS is not used', () {
      appleNeedsJs = false;
      preloadAppleJs();
      expect(page.injected, isEmpty);
      expect(appleJsReady, isTrue);
    });

    test('a preload failure is swallowed, for the tap to report', () async {
      appleNeedsJs = true;
      preloadAppleJs();
      page.pending.single.completeError(StateError('blocked'));
      await pumpEventQueue();
      expect(appleJsReady, isFalse);
    });
  });

  group('signInWithApple on the web', () {
    late ApiClient originalApi;
    setUp(() {
      appleNeedsJs = true;
      // The redirect URI needs an http(s) base; tests run from file:.
      originalApi = api;
      api = ApiClient(baseUrl: 'https://traxjourney.com');
    });
    tearDown(() => api = originalApi);

    test('waits for a load still running before calling Apple', () async {
      final signIn = signInWithApple();
      await pumpEventQueue();

      expect(page.injected, [appleJsUrl]);
      expect(appleCalls, 0);

      page.pending.single.complete();
      await signIn;
      expect(appleCalls, 1);
    });

    test('calls Apple within the tap once the script is loaded', () async {
      final load = appleJsLoader.load();
      page.pending.single.complete();
      await load;

      final signIn = signInWithApple();
      // No await yet: the popup must open in the same turn as the click.
      expect(appleCalls, 1);
      await signIn;
    });

    test('a failed load fails the sign-in without calling Apple', () async {
      final signIn = signInWithApple();
      page.pending.single.completeError(StateError('offline'));

      await expectLater(signIn, throwsA(isA<AppleJsLoadException>()));
      expect(appleCalls, 0);
    });
  });

  group('LoginScreen', () {
    _testOnAndroid('loads Apple JS once when it shows the Apple button',
        (tester) async {
      appleNeedsJs = true;
      appleServicesId = 'com.traxjourney.web';
      await _pumpLogin(tester);

      expect(find.byType(SignInWithAppleButton), findsOneWidget);
      expect(page.injected, [appleJsUrl]);
    });

    _testOnAndroid('loads nothing when it shows no Apple button',
        (tester) async {
      appleNeedsJs = true;
      appleServicesId = '';
      await _pumpLogin(tester);

      expect(find.byType(SignInWithAppleButton), findsNothing);
      expect(page.injected, isEmpty);
    });

    _testOnAndroid('loads nothing on a custom server, which hides the button',
        (tester) async {
      SharedPreferences.setMockInitialValues(
          {'custom_server_url': 'https://trax.example.com'});
      appleNeedsJs = true;
      appleServicesId = 'com.traxjourney.web';
      await _pumpLogin(tester);

      expect(find.byType(SignInWithAppleButton), findsNothing);
      expect(page.injected, isEmpty);
    });

    _testOnAndroid('loads nothing where Apple JS is not used', (tester) async {
      appleNeedsJs = false;
      appleServicesId = 'com.traxjourney.web';
      await _pumpLogin(tester);

      expect(find.byType(SignInWithAppleButton), findsOneWidget);
      expect(page.injected, isEmpty);
    });

    _testOnAndroid('a tap after a failed load shows the error', (tester) async {
      appleNeedsJs = true;
      appleServicesId = 'com.traxjourney.web';
      await _pumpLogin(tester);
      page.pending.single.completeError(StateError('blocked'));
      await tester.pump();

      final button = find.byType(SignInWithAppleButton);
      await tester.ensureVisible(button);
      await tester.tap(button);
      await tester.pump();
      // The tap retries the load; it fails again.
      expect(page.injected, hasLength(2));
      page.pending.last.completeError(StateError('blocked'));
      await tester.pumpAndSettle();

      expect(appleCalls, 0);
      expect(
          find.text("Apple sign-in failed: Apple's sign-in script could not "
              'be loaded'),
          findsOneWidget);
    });
  });
}
