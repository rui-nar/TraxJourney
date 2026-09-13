/// The Settings > About panel is where a user is asked to read their versions
/// out for a bug report, so it names both halves — the app's own build and the
/// server's — and says so even when the server never answered (issue #275).
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
import 'package:traxjourney_client/src/core/app_version.dart';
import 'package:traxjourney_client/src/settings/settings_screen.dart';
import 'package:traxjourney_client/src/settings/settings_service.dart';
import 'package:traxjourney_client/src/settings/theme_notifier.dart';

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

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    serverVersion.value = null;
    // The screen's other cards reach for the global singleton; a stub keeps
    // them off the network.
    api = ApiClient(httpClient: MockClient((req) async {
      if (req.url.path == '/api/billing/me') {
        return http.Response(jsonEncode({'billing_enabled': false}), 200,
            headers: {'content-type': 'application/json'});
      }
      return http.Response('{}', 200,
          headers: {'content-type': 'application/json'});
    }));
  });
  tearDown(() => serverVersion.value = null);

  testWidgets('names both versions once the server version is known',
      (tester) async {
    serverVersion.value = 'v9.9.9';

    await _pumpSettings(tester);

    expect(find.text('app dev · server v9.9.9'), findsOneWidget);
  });

  testWidgets('says the server version is unknown rather than showing nothing',
      (tester) async {
    await _pumpSettings(tester);

    expect(find.text('app dev · server unknown'), findsOneWidget);
  });
}
