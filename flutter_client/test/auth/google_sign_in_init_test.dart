// Google Sign-In is initialised by the login screen, not by main() (#611). On
// the web, initialising is what loads Google's script (the vendored
// google_sign_in_web no longer loads it at plugin registration; its own
// browser test pins that), so this keeps the script off every page but the
// sign-in page. The platform is faked, since widget tests never run as web.

import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_sign_in_platform_interface/google_sign_in_platform_interface.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/auth/google_sign_in_init.dart';
import 'package:traxjourney_client/src/auth/login_screen.dart';

/// Records init() and lightweight-authentication calls; init() completes
/// when the test says so.
class _FakeGoogleSignIn extends GoogleSignInPlatform {
  int initCalls = 0;
  int lightweightCalls = 0;

  /// Created by init(), so inside the widget test's zone: one made in setUp
  /// would complete outside it, where tester.pump() never runs its listeners.
  late Completer<void> initDone;

  @override
  Future<void> init(InitParameters params) {
    initCalls++;
    initDone = Completer<void>();
    return initDone.future;
  }

  @override
  Future<AuthenticationResults?>? attemptLightweightAuthentication(
    AttemptLightweightAuthenticationParameters params,
  ) {
    lightweightCalls++;
    return null;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

Future<void> _pumpLogin(WidgetTester tester) async {
  await tester.pumpWidget(
    ChangeNotifierProvider<AuthNotifier>(
      create: (_) => AuthNotifier(AuthService()),
      child: const MaterialApp(home: LoginScreen()),
    ),
  );
  await tester.pump();
}

void main() {
  late _FakeGoogleSignIn platform;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    resetGoogleSignInInitialized();
    platform = _FakeGoogleSignIn();
    GoogleSignInPlatform.instance = platform;
  });

  testWidgets(
      'the login screen initialises Google Sign-In once, then tries a '
      'lightweight sign-in', (tester) async {
    expect(platform.initCalls, 0);

    await _pumpLogin(tester);
    expect(platform.initCalls, 1);
    // Not before initialize() has finished: the platform would not be ready.
    expect(platform.lightweightCalls, 0);

    platform.initDone.complete();
    await tester.pump();
    expect(platform.lightweightCalls, 1);

    // A second visit to the login screen does not initialise again.
    await tester.pumpWidget(const SizedBox());
    await _pumpLogin(tester);
    expect(platform.initCalls, 1);
    expect(platform.lightweightCalls, 2);
  });

  testWidgets('a login screen closed before initialize() finishes does nothing',
      (tester) async {
    await _pumpLogin(tester);
    await tester.pumpWidget(const SizedBox());

    platform.initDone.complete();
    await tester.pump();
    expect(platform.lightweightCalls, 0);
  });

  testWidgets('a failed initialize() leaves the login screen usable',
      (tester) async {
    await _pumpLogin(tester);
    platform.initDone.completeError(StateError('no client id'));
    await tester.pump();

    expect(tester.takeException(), isNull);
    expect(platform.lightweightCalls, 0);
    expect(find.byType(LoginScreen), findsOneWidget);
  });

  // Anything else that initialised Google Sign-In, such as main() as it used
  // to, would load Google's script on every page of the web app again.
  test('only the login screen initialises Google Sign-In', () {
    final initializers = <String>[];
    final callers = <String>[];
    for (final f in Directory('lib').listSync(recursive: true)) {
      if (f is! File || !f.path.endsWith('.dart')) continue;
      final src = f.readAsStringSync();
      final path = f.path.replaceAll(r'\', '/');
      if (src.contains('GoogleSignIn.instance.initialize(')) {
        initializers.add(path);
      }
      if (src.contains('ensureGoogleSignInInitialized()') &&
          !path.endsWith('/google_sign_in_init.dart')) {
        callers.add(path);
      }
    }
    expect(initializers, ['lib/src/auth/google_sign_in_init.dart']);
    expect(callers, ['lib/src/auth/login_screen.dart']);
  });
}
