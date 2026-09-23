/// Immich connection settings — issue #33.
///
/// Two layers: SettingsService.{getImmichStatus,saveImmichConfig,
/// disconnectImmich} against a mocked HTTP client (mirrors
/// polarsteps_token_expiry_test.dart), and the Immich card in SettingsScreen
/// against a fake SettingsService (mirrors
/// auth/forced_change_password_screen_test.dart's DI pattern).
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
import 'package:traxjourney_client/src/settings/settings_screen.dart';
import 'package:traxjourney_client/src/settings/settings_service.dart';
import 'package:traxjourney_client/src/settings/theme_notifier.dart';

http.Response _json(int status, Object body) =>
    http.Response(jsonEncode(body), status,
        headers: {'content-type': 'application/json'});

void main() {
  group('SettingsService.Immich', () {
    test('getImmichStatus returns connected + server_url', () async {
      final mock = MockClient((req) async {
        expect(req.url.path, '/api/immich/status');
        return _json(200, {
          'connected': true,
          'server_url': 'https://photos.example.com',
        });
      });
      final svc = SettingsService();
      api = ApiClient(httpClient: mock);

      final data = await svc.getImmichStatus();

      expect(data['connected'], isTrue);
      expect(data['server_url'], 'https://photos.example.com');
    });

    test('saveImmichConfig PUTs server_url + api_key', () async {
      final mock = MockClient((req) async {
        expect(req.method, 'PUT');
        expect(req.url.path, '/api/immich/config');
        final body = jsonDecode(req.body) as Map<String, dynamic>;
        expect(body['server_url'], 'https://photos.example.com');
        expect(body['api_key'], 'secret-key');
        return _json(200, {});
      });
      final svc = SettingsService();
      api = ApiClient(httpClient: mock);

      await svc.saveImmichConfig(
        serverUrl: 'https://photos.example.com',
        apiKey: 'secret-key',
      );
    });

    test('saveImmichConfig surfaces the 422 detail message', () async {
      final mock = MockClient((req) async =>
          _json(422, {'detail': 'Could not connect to Immich server'}));
      final svc = SettingsService();
      api = ApiClient(httpClient: mock);

      await expectLater(
        svc.saveImmichConfig(serverUrl: 'bad', apiKey: 'x'),
        throwsA(isA<Exception>().having((e) => e.toString(), 'message',
            contains('Could not connect to Immich server'))),
      );
    });

    test('disconnectImmich DELETEs the disconnect endpoint', () async {
      final mock = MockClient((req) async {
        expect(req.method, 'DELETE');
        expect(req.url.path, '/api/immich/disconnect');
        return http.Response('', 204);
      });
      final svc = SettingsService();
      api = ApiClient(httpClient: mock);

      await svc.disconnectImmich();
    });
  });

  group('SettingsScreen Immich card', () {
    setUp(() {
      SharedPreferences.setMockInitialValues({});
      // The screen also renders BillingSection, which hits the global `api`
      // singleton directly via its own BillingService() — not something this
      // card's fake SettingsService can stand in for. Point it at a stub
      // so that card resolves to "self-hosted, nothing to show" instead of
      // hitting the network.
      api = ApiClient(httpClient: MockClient((req) async {
        if (req.url.path == '/api/billing/me') {
          return _json(200, {'billing_enabled': false});
        }
        return _json(200, {});
      }));
    });

    Finder immichCard() =>
        find.ancestor(of: find.text('Immich'), matching: find.byType(Card));

    Future<void> pumpScreen(WidgetTester tester, SettingsService service) async {
      await tester.pumpWidget(MultiProvider(
        providers: [
          ChangeNotifierProvider<AuthNotifier>(
              create: (_) => AuthNotifier(AuthService())),
          ChangeNotifierProvider<ThemeNotifier>(create: (_) => ThemeNotifier()),
        ],
        child: MaterialApp(home: SettingsScreen(service: service)),
      ));
      await tester.pumpAndSettle();
    }

    testWidgets('connect flow: fills fields, taps Save, shows connected state',
        (tester) async {
      final svc = _FakeImmichSettingsService();
      await pumpScreen(tester, svc);

      expect(
        find.descendant(
          of: immichCard(),
          matching: find.textContaining('Connect your self-hosted Immich'),
        ),
        findsOneWidget,
      );

      await tester.enterText(
        find.widgetWithText(TextField, 'Server URL'),
        'https://photos.example.com',
      );
      await tester.enterText(
        find.widgetWithText(TextField, 'API key'),
        'secret-key',
      );
      final saveButton = find.descendant(
        of: immichCard(),
        matching: find.widgetWithText(FilledButton, 'Save'),
      );
      // The settings page is a single long scrollable, taller than the
      // default test viewport -- scroll the button into view before tapping,
      // or the tap silently misses (off-screen) instead of throwing.
      await tester.ensureVisible(saveButton);
      await tester.tap(saveButton);
      await tester.pumpAndSettle();

      expect(svc.saveCalled, isTrue);
      expect(svc.lastServerUrl, 'https://photos.example.com');
      expect(svc.lastApiKey, 'secret-key');
      expect(
        find.descendant(
          of: immichCard(),
          matching: find.textContaining('Connected to https://photos.example.com'),
        ),
        findsOneWidget,
      );
      expect(
        find.descendant(
            of: immichCard(), matching: find.widgetWithText(TextButton, 'Disconnect')),
        findsOneWidget,
      );
    });

    testWidgets('disconnect flow: tapping Disconnect returns to the form',
        (tester) async {
      final svc = _FakeImmichSettingsService(
        connected: true,
        serverUrl: 'https://photos.example.com',
      );
      await pumpScreen(tester, svc);

      expect(
        find.descendant(
          of: immichCard(),
          matching: find.textContaining('Connected to https://photos.example.com'),
        ),
        findsOneWidget,
      );

      final disconnectButton = find.descendant(
        of: immichCard(),
        matching: find.widgetWithText(TextButton, 'Disconnect'),
      );
      await tester.ensureVisible(disconnectButton);
      await tester.tap(disconnectButton);
      await tester.pumpAndSettle();

      expect(svc.disconnectCalled, isTrue);
      expect(
        find.descendant(
          of: immichCard(),
          matching: find.textContaining('Connect your self-hosted Immich'),
        ),
        findsOneWidget,
      );
      expect(
        find.descendant(
            of: immichCard(), matching: find.widgetWithText(FilledButton, 'Save')),
        findsOneWidget,
      );
    });

    testWidgets(
        'validation error: a failed Save surfaces the backend detail message',
        (tester) async {
      final svc = _FakeImmichSettingsService()
        ..saveError = Exception('Could not connect to Immich server');
      await pumpScreen(tester, svc);

      await tester.enterText(
        find.widgetWithText(TextField, 'Server URL'),
        'https://bad.example.com',
      );
      await tester.enterText(
        find.widgetWithText(TextField, 'API key'),
        'wrong-key',
      );
      final saveButton = find.descendant(
        of: immichCard(),
        matching: find.widgetWithText(FilledButton, 'Save'),
      );
      await tester.ensureVisible(saveButton);
      await tester.tap(saveButton);
      await tester.pumpAndSettle();

      expect(svc.saveCalled, isTrue);
      expect(find.text('Could not connect to Immich server'), findsOneWidget);
      // Stays disconnected — the form is still up, not the connected state.
      expect(
        find.descendant(
            of: immichCard(), matching: find.widgetWithText(FilledButton, 'Save')),
        findsOneWidget,
      );
    });
  });
}

class _FakeImmichSettingsService extends SettingsService {
  bool connected;
  String? serverUrl;
  Object? saveError;
  bool saveCalled = false;
  bool disconnectCalled = false;
  String? lastServerUrl;
  String? lastApiKey;

  _FakeImmichSettingsService({this.connected = false, this.serverUrl});

  // The screen also loads these on init — stub them so the card under test
  // isn't blocked behind unrelated sections' loading spinners.
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
  Future<Map<String, dynamic>> getImmichStatus() async =>
      {'connected': connected, 'server_url': serverUrl};

  @override
  Future<void> saveImmichConfig({
    required String serverUrl,
    required String apiKey,
  }) async {
    saveCalled = true;
    lastServerUrl = serverUrl;
    lastApiKey = apiKey;
    if (saveError != null) throw saveError!;
    connected = true;
    this.serverUrl = serverUrl;
  }

  @override
  Future<void> disconnectImmich() async {
    disconnectCalled = true;
    connected = false;
    serverUrl = null;
  }
}
