/// Backup management in Settings is admin-only.
///
/// Two layers: SettingsService.{listBackups,restoreBackup} against a mocked
/// HTTP client (a 403 must not surface as an error on list), and the Backups
/// card in SettingsScreen, which only admins see.
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
  late ApiClient savedApi;
  setUp(() => savedApi = api);
  tearDown(() => api = savedApi);

  group('SettingsService backups', () {
    test('listBackups returns an empty list on 403', () async {
      api = ApiClient(httpClient: MockClient((req) async {
        expect(req.url.path, '/api/backup/');
        return _json(403, {'detail': 'Admin access required'});
      }));

      expect(await SettingsService().listBackups(), isEmpty);
    });

    test('listBackups still surfaces other failures', () async {
      api = ApiClient(httpClient: MockClient(
          (req) async => _json(500, {'detail': 'boom'})));

      await expectLater(
          SettingsService().listBackups(), throwsA(isA<ApiException>()));
    });

    test('restoreBackup surfaces the 403 detail message', () async {
      api = ApiClient(httpClient: MockClient(
          (req) async => _json(403, {'detail': 'Admin access required'})));

      await expectLater(
        SettingsService().restoreBackup('2026-01-01'),
        throwsA(isA<Exception>().having((e) => e.toString(), 'message',
            contains('Admin access required'))),
      );
    });
  });

  group('SettingsScreen Backups card', () {
    setUp(() {
      SharedPreferences.setMockInitialValues({});
      // BillingSection talks to the global `api` directly; give it a stub.
      api = ApiClient(httpClient: MockClient((req) async {
        if (req.url.path == '/api/billing/me') {
          return _json(200, {'billing_enabled': false});
        }
        return _json(200, {});
      }));
    });

    Map<String, dynamic> userMap({required bool isAdmin}) => {
          'id': 1,
          'email': 'u@example.com',
          'display_name': 'U',
          'auth_provider': 'local',
          'is_admin': isAdmin,
        };

    Future<_FakeBackupSettingsService> pumpScreen(WidgetTester tester,
        {required bool isAdmin, AuthNotifier? authNotifier}) async {
      final svc = _FakeBackupSettingsService();
      final auth = authNotifier ?? AuthNotifier(AuthService())
        ..updateUser(userMap(isAdmin: isAdmin));
      await tester.pumpWidget(MultiProvider(
        providers: [
          ChangeNotifierProvider<AuthNotifier>.value(value: auth),
          ChangeNotifierProvider<ThemeNotifier>(create: (_) => ThemeNotifier()),
        ],
        child: MaterialApp(home: SettingsScreen(service: svc)),
      ));
      await tester.pumpAndSettle();
      return svc;
    }

    testWidgets('is hidden from non-admins and backups are never fetched',
        (tester) async {
      final svc = await pumpScreen(tester, isAdmin: false);

      expect(find.text('Backups'), findsNothing);
      expect(find.text('2026-01-01'), findsNothing);
      expect(find.widgetWithText(TextButton, 'Restore'), findsNothing);
      expect(svc.listCalls, 0);
    });

    testWidgets('is shown to admins with a Restore button per backup',
        (tester) async {
      final svc = await pumpScreen(tester, isAdmin: true);

      expect(find.text('Backups'), findsOneWidget);
      expect(find.text('2026-01-01'), findsOneWidget);
      expect(find.widgetWithText(TextButton, 'Restore'), findsOneWidget);
      expect(svc.listCalls, 1);
    });

    testWidgets(
        'loads backups when admin status arrives after the screen is up',
        (tester) async {
      // A restored session starts with a placeholder user (not admin) and
      // fills in the real profile in the background.
      final auth = AuthNotifier(AuthService());
      final svc = await pumpScreen(tester, isAdmin: false, authNotifier: auth);
      expect(find.text('Backups'), findsNothing);
      expect(svc.listCalls, 0);

      auth.updateUser(userMap(isAdmin: true));
      await tester.pumpAndSettle();

      expect(find.text('Backups'), findsOneWidget);
      expect(find.text('2026-01-01'), findsOneWidget);
      expect(svc.listCalls, 1);
    });
  });
}

class _FakeBackupSettingsService extends SettingsService {
  int listCalls = 0;

  @override
  Future<bool> getStravaStatus() async => false;

  @override
  Future<Map<String, dynamic>> getPolarstepsStatus() async =>
      {'connected': false};

  @override
  Future<Map<String, dynamic>> getImmichStatus() async =>
      {'connected': false};

  @override
  Future<Map<String, dynamic>> getProfile() async => {
        'display_name': 'U',
        'email': 'u@example.com',
        'auth_provider': 'local',
      };

  @override
  Future<List<Map<String, dynamic>>> listBackups() async {
    listCalls++;
    return [
      {'date': '2026-01-01', 'size_bytes': 2048},
    ];
  }
}
