/// The Settings display name can't be saved blank — issue #507.
///
/// Others see a blank name as "Traveller", so Save refuses it inline without
/// asking the server, and the server's own refusal (422) shows in the same
/// place.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/settings/settings_screen.dart';
import 'package:traxjourney_client/src/settings/settings_service.dart';
import 'package:traxjourney_client/src/settings/theme_notifier.dart';

http.Response _json(int status, Object body) =>
    http.Response(jsonEncode(body), status,
        headers: {'content-type': 'application/json'});

/// What FastAPI sends when `UpdateProfileRequest`'s validator refuses.
final _serverRefusal = {
  'detail': [
    {
      'type': 'value_error',
      'loc': ['body', 'display_name'],
      'msg': 'Value error, Display name must not be empty',
      'input': '',
    }
  ],
};

void main() {
  late List<http.Request> profilePuts;
  late http.Response Function() putResponse;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    profilePuts = [];
    putResponse = () => _json(200, {'access_token': null, 'user': {}});
    api = ApiClient(httpClient: MockClient((req) async {
      if (req.method == 'PUT' && req.url.path == '/api/auth/me') {
        profilePuts.add(req);
        return putResponse();
      }
      if (req.url.path == '/api/billing/me') {
        return _json(200, {'billing_enabled': false});
      }
      return _json(200, {});
    }));
  });

  Future<void> pumpScreen(WidgetTester tester) async {
    // Tall enough that the Account card never scrolls under the app bar,
    // where a tap on Save would miss.
    tester.view.physicalSize = const Size(800, 4000);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final router = GoRouter(routes: [
      GoRoute(
          path: '/',
          builder: (_, __) => SettingsScreen(service: _FakeSettingsService())),
    ]);
    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<AuthNotifier>(
            create: (_) => AuthNotifier(AuthService())),
        ChangeNotifierProvider<ThemeNotifier>(create: (_) => ThemeNotifier()),
      ],
      child: MaterialApp.router(routerConfig: router),
    ));
    await tester.pumpAndSettle();
  }

  Finder nameField() => find.widgetWithText(TextField, 'Display name');

  Future<void> save(WidgetTester tester) async {
    // The Account card's Save comes before Immich's.
    final button = find.widgetWithText(FilledButton, 'Save').first;
    await tester.ensureVisible(button);
    await tester.tap(button);
    // Narrow pumps: the saving spinner never settles while a request is out.
    for (var i = 0; i < 3; i++) {
      await tester.pump();
    }
  }

  testWidgets('Save with a blank name shows the message and sends nothing',
      (tester) async {
    await pumpScreen(tester);
    expect(find.text('Tester'), findsOneWidget);

    await tester.enterText(nameField(), '   ');
    await save(tester);

    expect(find.text(kBlankDisplayNameMessage), findsOneWidget);
    expect(profilePuts, isEmpty);

    // Typing a name clears the message, and that name is saved.
    await tester.enterText(nameField(), 'Ada');
    await tester.pump();
    expect(find.text(kBlankDisplayNameMessage), findsNothing);
    await save(tester);
    expect(profilePuts, hasLength(1));
    expect(jsonDecode(profilePuts.single.body), {'display_name': 'Ada'});
  });

  testWidgets("the server's refusal shows under the field", (tester) async {
    // Whatever the server refuses, its reason is shown, not a raw body.
    putResponse = () => _json(422, _serverRefusal);
    await pumpScreen(tester);

    await tester.enterText(nameField(), 'Ada');
    await save(tester);

    expect(profilePuts, hasLength(1));
    expect(find.text('Display name must not be empty'), findsOneWidget);
    expect(find.textContaining('Save failed'), findsNothing);
  });

  test('validationErrorMessage reads only FastAPI validation bodies', () {
    expect(validationErrorMessage(jsonEncode(_serverRefusal)),
        'Display name must not be empty');
    expect(validationErrorMessage(jsonEncode({'detail': 'User not found'})),
        isNull);
    expect(validationErrorMessage('<html>Bad gateway</html>'), isNull);
  });
}

class _FakeSettingsService extends SettingsService {
  // The screen loads these on init — stub them so nothing hits the network.
  // updateProfile is left real, so a save goes through the swapped `api`.
  @override
  Future<bool> getStravaStatus() async => false;

  @override
  Future<Map<String, dynamic>> getPolarstepsStatus() async =>
      {'connected': false};

  @override
  Future<Map<String, dynamic>> getProfile() async => {
        'display_name': 'Tester',
        'email': 'tester@example.com',
        'auth_provider': 'local',
      };

  @override
  Future<List<Map<String, dynamic>>> listBackups() async => [];

  @override
  Future<Map<String, dynamic>> getImmichStatus() async => {'connected': false};
}
