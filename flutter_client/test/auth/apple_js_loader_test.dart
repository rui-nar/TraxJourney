// Apple's sign-in script is loaded on demand (I1-3): web/index.html no longer
// loads it on every page, and the login screen starts the load when it shows
// the Apple button. Apple's popup only opens inside the click (I2-1), so the
// button is disabled while the script loads, a tap calls Apple at once when
// it has loaded, and a tap after a failed load only retries the load. The
// page is faked through apple_js_loader.dart's appleJsLoader and
// appleNeedsJs, since widget tests never run as web.

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

bool _appleEnabled(WidgetTester tester) =>
    tester
        .widget<SignInWithAppleButton>(find.byType(SignInWithAppleButton))
        .onPressed !=
    null;

Future<void> _tapAppleButton(WidgetTester tester) async {
  final button = find.byType(SignInWithAppleButton);
  await tester.ensureVisible(button);
  await tester.tap(button);
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
      expect(appleJsLoader.state.value, AppleJsState.idle);
      final first = appleJsLoader.load();
      final second = appleJsLoader.load();

      expect(page.injected, [appleJsUrl]);
      expect(appleJsLoader.isLoaded, isFalse);
      expect(appleJsLoader.state.value, AppleJsState.loading);

      page.pending.single.complete();
      await Future.wait([first, second]);

      expect(appleJsLoader.isLoaded, isTrue);
      expect(appleJsLoader.state.value, AppleJsState.loaded);
      await appleJsLoader.load();
      expect(page.injected, hasLength(1));
    });

    test('a failed load throws, stays unloaded, and the next load retries',
        () async {
      final failed = appleJsLoader.load();
      page.pending.single.completeError(StateError('offline'));

      await expectLater(failed, throwsA(isA<AppleJsLoadException>()));
      expect(appleJsLoader.isLoaded, isFalse);
      expect(appleJsLoader.state.value, AppleJsState.failed);

      final retry = appleJsLoader.load();
      expect(page.injected, hasLength(2));
      expect(appleJsLoader.state.value, AppleJsState.loading);
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
      // Not stuck loading: the button comes back for the tap to retry.
      expect(appleJsState.value, AppleJsState.failed);
      expect(appleJsLoading, isFalse);
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

    test('never waits for the script: refuses to call Apple before it loaded',
        () async {
      // Calling Apple after a wait would open its popup outside the click
      // (I2-1); the login screen loads the script instead.
      await expectLater(signInWithApple(), throwsStateError);
      expect(page.injected, isEmpty);
      expect(appleCalls, 0);
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

    _testOnAndroid('the Apple button is disabled while the script loads',
        (tester) async {
      appleNeedsJs = true;
      appleServicesId = 'com.traxjourney.web';
      await _pumpLogin(tester);

      // A tap now could only open Apple's popup after the click (I2-1).
      expect(page.injected, [appleJsUrl]);
      expect(_appleEnabled(tester), isFalse);
    });

    _testOnAndroid('once loaded, a tap calls Apple straight away',
        (tester) async {
      appleNeedsJs = true;
      appleServicesId = 'com.traxjourney.web';
      requestAppleCredential = (
          {required String nonce,
          WebAuthenticationOptions? webAuthenticationOptions}) {
        appleCalls++;
        // Left pending: this test is about the call, not the server.
        return Completer<AuthorizationCredentialAppleID>().future;
      };
      // The redirect URI needs an http(s) base; tests run from file:.
      final originalApi = api;
      addTearDown(() => api = originalApi);
      api = ApiClient(baseUrl: 'https://traxjourney.com');
      await _pumpLogin(tester);
      page.pending.single.complete();
      await tester.pumpAndSettle();

      expect(_appleEnabled(tester), isTrue);
      await _tapAppleButton(tester);
      // No second load: the tap went straight to Apple. That the call comes
      // before any await is the 'calls Apple within the tap' test above.
      expect(appleCalls, 1);
      expect(page.injected, hasLength(1));
    });

    _testOnAndroid('after a failed load the button is enabled again',
        (tester) async {
      appleNeedsJs = true;
      appleServicesId = 'com.traxjourney.web';
      await _pumpLogin(tester);
      page.pending.single.completeError(StateError('blocked'));
      await tester.pumpAndSettle();

      expect(_appleEnabled(tester), isTrue);
    });

    _testOnAndroid('a tap after a failed load retries it, and shows the error',
        (tester) async {
      appleNeedsJs = true;
      appleServicesId = 'com.traxjourney.web';
      await _pumpLogin(tester);
      page.pending.single.completeError(StateError('blocked'));
      await tester.pumpAndSettle();

      await _tapAppleButton(tester);
      await tester.pump();
      // The tap retries the load, and the button waits for it.
      expect(page.injected, hasLength(2));
      expect(_appleEnabled(tester), isFalse);
      page.pending.last.completeError(StateError('blocked'));
      await tester.pumpAndSettle();

      expect(appleCalls, 0);
      expect(
          find.text("Apple sign-in failed: Apple's sign-in script could not "
              'be loaded'),
          findsOneWidget);
      expect(_appleEnabled(tester), isTrue);
    });

    _testOnAndroid(
        'a retried load that succeeds leaves Apple to the next tap',
        (tester) async {
      appleNeedsJs = true;
      appleServicesId = 'com.traxjourney.web';
      await _pumpLogin(tester);
      page.pending.single.completeError(StateError('blocked'));
      await tester.pumpAndSettle();

      await _tapAppleButton(tester);
      await tester.pump();
      page.pending.last.complete();
      await tester.pumpAndSettle();

      // Apple is not called after the wait: its popup would be blocked.
      expect(appleCalls, 0);
      expect(appleJsState.value, AppleJsState.loaded);
      expect(_appleEnabled(tester), isTrue);
      expect(
          find.text('Apple sign-in is ready. Tap Continue with Apple again.'),
          findsOneWidget);
    });

    _testOnAndroid('the button is enabled where Apple JS is not used',
        (tester) async {
      appleNeedsJs = false;
      appleServicesId = 'com.traxjourney.web';
      await _pumpLogin(tester);

      expect(_appleEnabled(tester), isTrue);
    });
  });
}
