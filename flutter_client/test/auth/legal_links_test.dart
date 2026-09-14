/// Sign-in, registration and the landing page link to the privacy policy and
/// terms of service (issue #421). Google's OAuth verification and app stores
/// expect both to be reachable before an account exists.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/auth/login_screen.dart';
import 'package:traxjourney_client/src/auth/register_screen.dart';
import 'package:traxjourney_client/src/auth/welcome_screen.dart';

import '../helpers/fake_url_launcher.dart';

const _server = 'https://trax.example.com';

Future<void> _pumpAuthScreen(WidgetTester tester, Widget screen) async {
  await tester.pumpWidget(
    ChangeNotifierProvider<AuthNotifier>(
      create: (_) => AuthNotifier(AuthService()),
      child: MaterialApp(home: screen),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _tap(WidgetTester tester, Finder finder) async {
  await tester.ensureVisible(finder);
  await tester.pump();
  await tester.tap(finder);
  await tester.pump();
}

void main() {
  late ApiClient previousApi;
  late FakeUrlLauncher launcher;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    previousApi = api;
    api = ApiClient(baseUrl: _server);
  });
  tearDown(() => api = previousApi);

  for (final (name, screen) in [
    ('sign-in', const LoginScreen()),
    ('registration', const RegisterScreen()),
  ]) {
    group('$name screen', () {
      testWidgets('Terms of Service opens the server\'s terms', (tester) async {
        launcher = installFakeUrlLauncher();
        await _pumpAuthScreen(tester, screen);

        await _tap(tester, find.widgetWithText(TextButton, 'Terms of Service'));

        expect(launcher.launched, ['$_server/terms']);
      });

      testWidgets('Privacy Policy opens the server\'s policy', (tester) async {
        launcher = installFakeUrlLauncher();
        await _pumpAuthScreen(tester, screen);

        await _tap(tester, find.widgetWithText(TextButton, 'Privacy Policy'));

        expect(launcher.launched, ['$_server/privacy']);
      });
    });
  }

  group('landing page footer', () {
    // The page overflows in widget tests for reasons unrelated to links (see
    // welcome_screen_version_test.dart), so overflow reports are swallowed.
    Future<void> pumpWelcome(WidgetTester tester, Size size) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final previous = FlutterError.onError;
      FlutterError.onError = (details) {
        if (details.exceptionAsString().contains('overflowed')) return;
        previous?.call(details);
      };
      addTearDown(() => FlutterError.onError = previous);
      await tester.pumpWidget(const MaterialApp(home: WelcomeScreen()));
      await tester.pump();
    }

    for (final (layout, size) in [
      ('desktop', const Size(1280, 900)),
      ('phone', const Size(390, 844)),
    ]) {
      testWidgets('links Privacy and Terms on $layout', (tester) async {
        launcher = installFakeUrlLauncher();
        await pumpWelcome(tester, size);

        await _tap(tester, find.text('Privacy'));
        await _tap(tester, find.text('Terms'));

        expect(launcher.launched, ['$_server/privacy', '$_server/terms']);
      });
    }
  });
}
