/// Deleting the account cancels a paid plan immediately — issue #429.
///
/// The confirmation must say so before the user commits, and a refusal from the
/// server (the plan could not be cancelled, so nothing was deleted) must reach
/// the user in the server's own words.
library;

import 'dart:async';
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
import 'package:traxjourney_client/src/billing/billing_service.dart';
import 'package:traxjourney_client/src/settings/settings_screen.dart';
import 'package:traxjourney_client/src/settings/settings_service.dart';
import 'package:traxjourney_client/src/settings/theme_notifier.dart';

http.Response _json(int status, Object body) =>
    http.Response(jsonEncode(body), status,
        headers: {'content-type': 'application/json'});

Map<String, dynamic> _billingMe({required String status, String plan = 'tier_2'}) => {
      'billing_enabled': true,
      'plan': plan,
      'plan_name': plan == 'free' ? 'Free' : 'Explorer',
      'status': status,
    };

BillingStatus _billing(String status) =>
    BillingStatus.fromJson(_billingMe(status: status));

const _refusal = 'The paid plan on this account could not be cancelled, so the '
    'account was not deleted and nothing was removed. Please try again in a '
    'few minutes.';

void main() {
  group('deleteAccountWarning', () {
    test('names the plan that will be cancelled', () {
      final text = deleteAccountWarning(_billing('active'));
      expect(text, contains('Your Explorer plan will be cancelled immediately'));
      expect(text, contains('cannot be undone'));
    });

    test('covers every status the server would cancel, not just live ones', () {
      for (final status in ['trialing', 'past_due', 'unpaid', 'incomplete', 'paused']) {
        expect(deleteAccountWarning(_billing(status)),
            contains('will be cancelled immediately'),
            reason: status);
      }
    });

    test('says nothing about a plan when nothing can bill', () {
      for (final status in ['none', 'canceled', 'incomplete_expired']) {
        final text = deleteAccountWarning(_billing(status));
        expect(text, isNot(contains('cancelled')), reason: status);
        expect(text, contains('cannot be undone'));
      }
    });

    test('never names the free plan as the one being cancelled', () {
      for (final status in ['unpaid', 'paused', 'incomplete']) {
        final text = deleteAccountWarning(
            BillingStatus.fromJson(_billingMe(status: status, plan: 'free')));
        expect(text, contains('Any active paid plan will be cancelled immediately'),
            reason: status);
        expect(text, isNot(contains('Free plan')), reason: status);
      }
    });

    test('falls back to the general sentence when the plan is unknown', () {
      expect(deleteAccountWarning(null),
          contains('Any active paid plan will be cancelled immediately'));
    });
  });

  group('SettingsService.deleteAccount', () {
    test('surfaces the server message of a billing refusal', () async {
      api = ApiClient(httpClient: MockClient((req) async {
        expect(req.method, 'DELETE');
        expect(req.url.path, '/api/auth/me');
        return _json(502, {
          'detail': _refusal,
          'code': 'subscription_cancel_failed',
        });
      }));

      await expectLater(
        SettingsService().deleteAccount(),
        throwsA(isA<Exception>()
            .having((e) => e.toString(), 'message', contains(_refusal))),
      );
    });
  });

  group('SettingsScreen delete account', () {
    late FutureOr<http.Response> Function() billingMe;
    late int billingCalls;

    setUp(() {
      SharedPreferences.setMockInitialValues({});
      billingCalls = 0;
      billingMe = () => _json(200, {'billing_enabled': false});
      api = ApiClient(httpClient: MockClient((req) async {
        if (req.url.path == '/api/billing/me') {
          billingCalls++;
          return billingMe();
        }
        return _json(200, {});
      }));
    });

    Future<void> pumpScreen(WidgetTester tester, SettingsService service) async {
      final router = GoRouter(routes: [
        GoRoute(path: '/', builder: (_, __) => SettingsScreen(service: service)),
        GoRoute(path: '/login', builder: (_, __) => const Text('login page')),
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

    Future<void> openConfirmation(WidgetTester tester) async {
      final button = find.widgetWithText(OutlinedButton, 'Delete my account');
      await tester.ensureVisible(button);
      await tester.tap(button);
      await tester.pumpAndSettle();
      expect(find.text('Delete account?'), findsOneWidget);
    }

    testWidgets('a paying user is told their plan is cancelled immediately',
        (tester) async {
      billingMe = () => _json(200, _billingMe(status: 'active'));
      await pumpScreen(tester, _FakeSettingsService());

      await openConfirmation(tester);

      expect(find.textContaining('Your Explorer plan will be cancelled immediately'),
          findsOneWidget);
    });

    testWidgets('a billing subscription on the free plan is not called "Free"',
        (tester) async {
      // unpaid / paused / incomplete: can still bill, grants nothing.
      billingMe = () => _json(200, _billingMe(status: 'unpaid', plan: 'free'));
      await pumpScreen(tester, _FakeSettingsService());

      await openConfirmation(tester);

      expect(find.textContaining('Any active paid plan will be cancelled immediately'),
          findsOneWidget);
      expect(find.textContaining('Free plan'), findsNothing);
    });

    testWidgets('reuses the plan the Plan card already loaded', (tester) async {
      billingMe = () => _json(200, _billingMe(status: 'active'));
      await pumpScreen(tester, _FakeSettingsService());
      final loaded = billingCalls;

      await openConfirmation(tester);

      expect(billingCalls, loaded, reason: 'asked the server again');
      expect(find.textContaining('Your Explorer plan will be cancelled immediately'),
          findsOneWidget);
    });

    testWidgets('shows it is busy while the plan loads, and ignores a second tap',
        (tester) async {
      // The Plan card's own load fails, so the tap has to fetch — slowly.
      final slow = Completer<http.Response>();
      var first = true;
      billingMe = () {
        if (first) {
          first = false;
          return _json(500, {'detail': 'boom'});
        }
        return slow.future;
      };
      await pumpScreen(tester, _FakeSettingsService());
      final before = billingCalls;

      final button = find.widgetWithText(OutlinedButton, 'Delete my account');
      await tester.ensureVisible(button);
      await tester.tap(button);
      await tester.pump();

      expect(tester.widget<OutlinedButton>(button).onPressed, isNull);
      expect(find.descendant(of: button, matching: find.byType(CircularProgressIndicator)),
          findsOneWidget);
      await tester.tap(button, warnIfMissed: false);
      await tester.pump();

      slow.complete(_json(200, _billingMe(status: 'active')));
      await tester.pumpAndSettle();

      expect(find.text('Delete account?'), findsOneWidget);
      expect(billingCalls, before + 1);
    });

    testWidgets('a free user is not warned about a plan', (tester) async {
      billingMe = () => _json(200, _billingMe(status: 'none', plan: 'free'));
      await pumpScreen(tester, _FakeSettingsService());

      await openConfirmation(tester);

      expect(find.textContaining('will be cancelled immediately'), findsNothing);
    });

    testWidgets('an unknown plan state gets the general sentence', (tester) async {
      billingMe = () => _json(500, {'detail': 'boom'});
      await pumpScreen(tester, _FakeSettingsService());

      await openConfirmation(tester);

      expect(find.textContaining('Any active paid plan will be cancelled immediately'),
          findsOneWidget);
    });

    testWidgets('a refused deletion shows the server message and stays put',
        (tester) async {
      billingMe = () => _json(200, _billingMe(status: 'active'));
      final svc = _FakeSettingsService()..deleteError = Exception(_refusal);
      await pumpScreen(tester, svc);

      await openConfirmation(tester);
      await tester.tap(find.widgetWithText(TextButton, 'Delete'));
      await tester.pumpAndSettle();

      expect(svc.deleteCalled, isTrue);
      expect(find.text('Account not deleted'), findsOneWidget);
      expect(find.text(_refusal), findsOneWidget);
      expect(find.text('login page'), findsNothing);

      await tester.tap(find.widgetWithText(TextButton, 'OK'));
      await tester.pumpAndSettle();
      expect(find.text(_refusal), findsNothing);
      expect(find.widgetWithText(OutlinedButton, 'Delete my account'),
          findsOneWidget);
    });
  });
}

class _FakeSettingsService extends SettingsService {
  Object? deleteError;
  bool deleteCalled = false;

  @override
  Future<void> deleteAccount() async {
    deleteCalled = true;
    if (deleteError != null) throw deleteError!;
  }

  // The screen also loads these on init — stub them so nothing hits the network.
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
