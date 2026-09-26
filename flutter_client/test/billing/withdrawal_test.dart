/// Withdrawing inside the 14 days after a subscription starts (issue #441).
///
/// The action appears only while the server says the window is open, the
/// confirmation states the refund before anything happens, and a refusal
/// reaches the user in the server's own words.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/billing/billing_service.dart';
import 'package:traxjourney_client/src/billing/plan_screen.dart';
import 'package:traxjourney_client/src/billing/plan_widgets.dart';
import 'package:traxjourney_client/src/core/theme.dart';

const _mb = 1024 * 1024;

final _subscribed = <String, dynamic>{
  'billing_enabled': true,
  'quotas_enforced': true,
  'plan': 'tier_2',
  'plan_name': 'Tier 2',
  'status': 'active',
  'limits': {
    'max_projects': 10,
    'max_storage_bytes': 20 * 1024 * _mb,
    'max_trip_days': 365,
  },
  'usage': {'projects': 1, 'storage_bytes': 250 * _mb},
};

final _inWindow = <String, dynamic>{
  ..._subscribed,
  'withdrawal_open': true,
  'withdrawal_closes_at': 1790000000.0,
};

class _FakeBilling implements BillingService {
  final List<Map<String, dynamic>> payloads;
  final Money quote;
  final Object? withdrawError;
  final Money owed;
  int statusCalls = 0;
  int quoteCalls = 0;
  int withdrawCalls = 0;

  _FakeBilling(Map<String, dynamic> payload,
      {List<Map<String, dynamic>>? then,
      this.quote = const Money(266, 'eur'),
      this.withdrawError,
      this.owed = const Money(0, 'eur')})
      : payloads = [payload, ...?then];

  @override
  Future<BillingStatus> status() async {
    final i = statusCalls < payloads.length ? statusCalls : payloads.length - 1;
    statusCalls++;
    return BillingStatus.fromJson(payloads[i]);
  }

  @override
  Future<List<PlanInfo>> plans() async => const [
        PlanInfo(id: 'free', name: 'Free', priceLabel: 'Free', features: []),
        PlanInfo(id: 'tier_2', name: 'Tier 2', priceLabel: '€3.99 / month',
            features: ['10 trips']),
      ];

  @override
  Future<Money> withdrawalQuote() async {
    quoteCalls++;
    return quote;
  }

  @override
  Future<Withdrawal> withdraw() async {
    withdrawCalls++;
    if (withdrawError != null) throw withdrawError!;
    return Withdrawal(refunded: quote, owed: owed);
  }

  @override
  Future<String> checkoutUrl({String? plan, String returnPath = kPlanRoute}) async =>
      '';

  @override
  Future<String> planChangeUrl({required String plan, String returnPath = kPlanRoute}) async =>
      '';

  @override
  Future<String> urlToReach(
          {required String plan,
          required bool subscribed,
          String returnPath = kPlanRoute}) async =>
      '';

  @override
  Future<String> portalUrl({String returnPath = kPlanRoute}) async => '';
}

Future<void> _pump(WidgetTester tester, _FakeBilling billing) async {
  tester.view.physicalSize = const Size(800, 2000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(MaterialApp(
    theme: lightTheme,
    home: PlanScreen(service: billing, launcher: (_) async {}),
  ));
  await tester.pumpAndSettle();
}

const _action = 'Withdraw and get a refund';

void main() {
  group('the action', () {
    testWidgets('is offered while the window is open', (tester) async {
      await _pump(tester, _FakeBilling(_inWindow));
      expect(find.text(_action), findsOneWidget);
      expect(find.textContaining('you can withdraw'), findsOneWidget);
    });

    testWidgets('is not offered outside it', (tester) async {
      await _pump(tester, _FakeBilling(_subscribed));
      expect(find.text(_action), findsNothing);
    });

    testWidgets('is not offered on the free plan', (tester) async {
      await _pump(tester, _FakeBilling({
        ..._subscribed,
        'plan': 'free',
        'plan_name': 'Free',
        'status': 'none',
      }));
      expect(find.text(_action), findsNothing);
    });
  });

  group('the confirmation', () {
    testWidgets('states the refund before anything is done', (tester) async {
      final billing = _FakeBilling(_inWindow, quote: const Money(266, 'eur'));
      await _pump(tester, billing);

      await tester.tap(find.text(_action));
      await tester.pumpAndSettle();

      expect(billing.quoteCalls, 1);
      expect(billing.withdrawCalls, 0);
      expect(find.text('Withdraw and get a refund?'), findsOneWidget);
      expect(find.textContaining('About €2.66'), findsOneWidget);
      expect(find.textContaining('ends immediately'), findsOneWidget);
    });

    testWidgets('keeping the plan withdraws nothing', (tester) async {
      final billing = _FakeBilling(_inWindow);
      await _pump(tester, billing);
      await tester.tap(find.text(_action));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Keep my plan'));
      await tester.pumpAndSettle();

      expect(billing.withdrawCalls, 0);
      expect(find.text(_action), findsOneWidget);
    });

    testWidgets('confirming withdraws, says what was refunded, and reloads',
        (tester) async {
      final billing = _FakeBilling(_inWindow, then: [_subscribed]);
      await _pump(tester, billing);
      await tester.tap(find.text(_action));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Withdraw'));
      await tester.pumpAndSettle();

      expect(billing.withdrawCalls, 1);
      expect(find.textContaining('€2.66 is on its way back'), findsOneWidget);
      expect(billing.statusCalls, greaterThan(1));
      expect(find.text(_action), findsNothing);
    });

    testWidgets('a refusal is shown in the server\'s words', (tester) async {
      const detail = 'Your subscription was cancelled, but the refund could '
          'not be issued yet. Please try again in a few minutes.';
      final billing = _FakeBilling(_inWindow,
          withdrawError: ApiException(
              502, jsonEncode({'detail': detail, 'code': 'refund_failed'})));
      await _pump(tester, billing);
      await tester.tap(find.text(_action));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Withdraw'));
      await tester.pumpAndSettle();

      expect(find.text(detail), findsOneWidget);
    });
  });

  group('an owed refund', () {
    testWidgets('is said, not passed off as the whole refund', (tester) async {
      final billing = _FakeBilling(_inWindow,
          quote: const Money(0, 'eur'), owed: const Money(266, 'eur'));
      await _pump(tester, billing);
      await tester.tap(find.text(_action));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Withdraw'));
      await tester.pumpAndSettle();

      expect(find.textContaining('€2.66 could not be refunded automatically'),
          findsOneWidget);
    });

    test('nothing owed says nothing about it', () {
      final text = withdrawalDone(const Withdrawal(
          refunded: Money(266, 'eur'), owed: Money(0, 'eur')));
      expect(text, contains('€2.66 is on its way back'));
      expect(text, isNot(contains('automatically')));
    });
  });

  group('wording', () {
    test('nothing left to refund says so rather than "€0.00"', () {
      final text = withdrawalConfirmation(const Money(0, 'eur'));
      expect(text, contains('Nothing is left to refund'));
      expect(text, isNot(contains('€0.00')));
    });

    test('money reads like the plan prices', () {
      expect(const Money(266, 'eur').label, '€2.66');
      expect(const Money(5, 'eur').label, '€0.05');
      expect(const Money(1999, 'usd').label, '19.99 USD');
    });

    test('the notice names the last day of the window, in UTC', () {
      // The server sends the first instant after the window: midnight UTC at
      // the start of 25 September 2026. The last day is the 24th, wherever
      // the device is.
      final closes =
          DateTime.utc(2026, 9, 25).millisecondsSinceEpoch / 1000;
      final status = BillingStatus.fromJson(
          {..._inWindow, 'withdrawal_closes_at': closes});
      expect(withdrawalNotice(status),
          startsWith('Until the end of 24 September (UTC), '));
    });

    test('without a known deadline it says 14 days', () {
      final unknown =
          BillingStatus.fromJson({..._inWindow, 'withdrawal_closes_at': 0});
      expect(withdrawalNotice(unknown), startsWith('Within 14 days'));
    });
  });

  group('BillingStatus', () {
    test('reads the window', () {
      final status = BillingStatus.fromJson(_inWindow);
      expect(status.withdrawalOpen, isTrue);
      expect(status.withdrawalClosesAt, 1790000000.0);
    });

    test('an older server that sends nothing means closed', () {
      final status = BillingStatus.fromJson(_subscribed);
      expect(status.withdrawalOpen, isFalse);
      expect(status.withdrawalClosesAt, 0);
    });
  });

  group('BillingService', () {
    test('asks for the quote and withdraws at the endpoints', () async {
      final requests = <String>[];
      final service = BillingService(ApiClient(
        baseUrl: '',
        httpClient: MockClient((req) async {
          requests.add('${req.method} ${req.url.path}');
          final body = req.method == 'GET'
              ? {'amount_cents': 266, 'currency': 'eur', 'closes_at': 1.0}
              : {'refunded_cents': 265, 'currency': 'eur', 'owed_cents': 1};
          return http.Response(jsonEncode(body), 200,
              headers: {'content-type': 'application/json'});
        }),
      ));

      final quote = await service.withdrawalQuote();
      final done = await service.withdraw();

      expect(requests,
          ['GET /api/billing/withdraw', 'POST /api/billing/withdraw']);
      expect(quote.cents, 266);
      expect(done.refunded.cents, 265);
      expect(done.owed.cents, 1);
      expect(done.refunded.currency, 'eur');
    });
  });
}
