/// Settings > About links out to the source code, the privacy policy and the
/// terms of service (issue #421).
///
/// The app is AGPL-3.0: someone using the hosted service over a network must
/// be offered the source, and a signed-in user's way to find it is here. The
/// legal pages live on the server the app is connected to.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/brand.dart';
import 'package:traxjourney_client/src/settings/settings_screen.dart';
import 'package:traxjourney_client/src/settings/settings_service.dart';
import 'package:traxjourney_client/src/settings/theme_notifier.dart';

import '../helpers/fake_url_launcher.dart';

Future<void> _pumpSettings(WidgetTester tester) async {
  await tester.pumpWidget(MultiProvider(
    providers: [
      ChangeNotifierProvider<AuthNotifier>(
          create: (_) => AuthNotifier(AuthService())),
      ChangeNotifierProvider<ThemeNotifier>(create: (_) => ThemeNotifier()),
    ],
    child: MaterialApp(home: SettingsScreen(service: SettingsService())),
  ));
  await tester.pumpAndSettle();
}

Future<void> _tapAboutLink(WidgetTester tester, String label) async {
  final link = find.widgetWithText(TextButton, label);
  await tester.scrollUntilVisible(link, 300,
      scrollable: find.byType(Scrollable).first);
  await tester.tap(link);
  await tester.pump();
}

void main() {
  late ApiClient previousApi;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    previousApi = api;
    // A server of its own, so the URLs the links build are unambiguous; the
    // stub keeps the screen's other cards off the network.
    api = ApiClient(
      baseUrl: 'https://trax.example.com',
      httpClient: MockClient((req) async {
        if (req.url.path == '/api/billing/me') {
          return http.Response(jsonEncode({'billing_enabled': false}), 200,
              headers: {'content-type': 'application/json'});
        }
        return http.Response('{}', 200,
            headers: {'content-type': 'application/json'});
      }),
    );
  });
  tearDown(() => api = previousApi);

  testWidgets('Source code opens the repository', (tester) async {
    final launcher = installFakeUrlLauncher();
    await _pumpSettings(tester);

    await _tapAboutLink(tester, 'Source code');

    expect(launcher.launched, [kRepoUrl]);
  });

  testWidgets("Privacy Policy opens the server's policy", (tester) async {
    final launcher = installFakeUrlLauncher();
    await _pumpSettings(tester);

    await _tapAboutLink(tester, 'Privacy Policy');

    expect(launcher.launched, ['https://trax.example.com/privacy']);
  });

  testWidgets("Terms of Service opens the server's terms", (tester) async {
    final launcher = installFakeUrlLauncher();
    await _pumpSettings(tester);

    await _tapAboutLink(tester, 'Terms of Service');

    expect(launcher.launched, ['https://trax.example.com/terms']);
  });

  testWidgets('names the licence instead of reserving all rights',
      (tester) async {
    installFakeUrlLauncher();
    await _pumpSettings(tester);

    expect(find.textContaining('AGPL-3.0'), findsOneWidget);
    expect(find.textContaining('All rights reserved'), findsNothing);
  });
}
