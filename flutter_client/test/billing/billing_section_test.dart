/// The Plan summary in Settings (issues #121, #153).
///
/// The load-bearing case is the self-hosted one: a deployment that sells
/// nothing must render no billing UI at all.
///
/// Everything here pumps under the **real** app theme. The first version of this
/// file used a bare `MaterialApp`, and so never saw that the theme sets
/// `ElevatedButton.minimumSize` to `Size.fromHeight(44)` — infinite width — which
/// broke the "Change plan" button inside a `Row` in the actual app while every
/// test here passed (issue #153).
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/billing/billing_section.dart';
import 'package:traxjourney_client/src/billing/billing_service.dart';
import 'package:traxjourney_client/src/core/theme.dart';

class _FakeBilling implements BillingService {
  final Map<String, dynamic> payload;
  int statusCalls = 0;

  _FakeBilling(this.payload);

  @override
  Future<BillingStatus> status() async {
    statusCalls++;
    return BillingStatus.fromJson(payload);
  }

  @override
  Future<String> checkoutUrl({String? plan, String returnPath = kPlanRoute}) async =>
      'https://pay.test/checkout';

  @override
  Future<String> planChangeUrl(
          {required String plan, String returnPath = kPlanRoute}) async =>
      'https://pay.test/change';

  @override
  Future<String> urlToReach({
    required String plan,
    required bool subscribed,
    String returnPath = kPlanRoute,
  }) async =>
      subscribed ? 'https://pay.test/change' : 'https://pay.test/checkout';

  @override
  Future<String> portalUrl({String returnPath = kPlanRoute}) async =>
      'https://pay.test/portal';

  @override
  Future<List<PlanInfo>> plans() async => const [
        PlanInfo(id: 'free', name: 'Free', priceLabel: 'Free', features: []),
        PlanInfo(id: 'tier_1', name: 'Tier 1', priceLabel: '€1 / month',
            features: []),
      ];
}

Future<void> _pump(
  WidgetTester tester,
  BillingService billing, {
  List<String>? opened,
  Uri? currentUri,
  Size? surface,
}) async {
  if (surface != null) {
    tester.view.physicalSize = surface;
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
  }
  await tester.pumpWidget(MaterialApp(
    // The real theme, not the default one — see the library docstring.
    theme: lightTheme,
    home: Scaffold(
      body: BillingSection(
        service: billing,
        onOpen: () => opened?.add('plan-page'),
        currentUri: currentUri,
        retryDelay: Duration.zero,
      ),
    ),
  ));
  await tester.pumpAndSettle();
}

/// Returns each entry of [payloads] in turn, repeating the last one once
/// exhausted — for simulating a webhook that lands a few calls in.
class _SequenceBilling implements BillingService {
  final List<Map<String, dynamic>> payloads;
  int calls = 0;

  _SequenceBilling(this.payloads);

  @override
  Future<BillingStatus> status() async {
    final payload = payloads[calls < payloads.length ? calls : payloads.length - 1];
    calls++;
    return BillingStatus.fromJson(payload);
  }

  @override
  Future<String> checkoutUrl({String? plan, String returnPath = kPlanRoute}) async =>
      'https://pay.test/checkout';

  @override
  Future<String> planChangeUrl(
          {required String plan, String returnPath = kPlanRoute}) async =>
      'https://pay.test/change';

  @override
  Future<String> urlToReach({
    required String plan,
    required bool subscribed,
    String returnPath = kPlanRoute,
  }) async =>
      subscribed ? 'https://pay.test/change' : 'https://pay.test/checkout';

  @override
  Future<String> portalUrl({String returnPath = kPlanRoute}) async =>
      'https://pay.test/portal';

  @override
  Future<List<PlanInfo>> plans() async => const [];
}

/// A [BillingService] whose `status()` call doesn't resolve until
/// [complete] or [fail] is called — for observing the loading state
/// (issue #196), which a same-tick fake would resolve too fast to see.
class _PendingBilling implements BillingService {
  final _completer = Completer<BillingStatus>();

  void complete(Map<String, dynamic> payload) =>
      _completer.complete(BillingStatus.fromJson(payload));

  void fail() => _completer.completeError(Exception('network error'));

  @override
  Future<BillingStatus> status() => _completer.future;

  @override
  Future<String> checkoutUrl({String? plan, String returnPath = kPlanRoute}) async =>
      'https://pay.test/checkout';

  @override
  Future<String> planChangeUrl(
          {required String plan, String returnPath = kPlanRoute}) async =>
      'https://pay.test/change';

  @override
  Future<String> urlToReach({
    required String plan,
    required bool subscribed,
    String returnPath = kPlanRoute,
  }) async =>
      subscribed ? 'https://pay.test/change' : 'https://pay.test/checkout';

  @override
  Future<String> portalUrl({String returnPath = kPlanRoute}) async =>
      'https://pay.test/portal';

  @override
  Future<List<PlanInfo>> plans() async => const [];
}

void main() {
  group('self-hosted', () {
    testWidgets('renders nothing when billing is disabled', (tester) async {
      await _pump(tester, _FakeBilling({'billing_enabled': false}));
      expect(find.text('Plan'), findsNothing);
      expect(find.byType(Card), findsNothing);
    });
  });

  group('loading (issue #196)', () {
    testWidgets('reserves a fixed-position card with a spinner', (tester) async {
      final billing = _PendingBilling();
      await tester.pumpWidget(MaterialApp(
        theme: lightTheme,
        home: Scaffold(body: BillingSection(service: billing)),
      ));
      // A frame with the status request still in flight.
      await tester.pump();

      expect(find.text('Plan'), findsOneWidget);
      expect(find.byType(Card), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsOneWidget);

      // Let the pending future resolve so the test doesn't leak a timer.
      billing.complete({'billing_enabled': true, 'plan': 'free'});
      await tester.pumpAndSettle();
    });

    testWidgets('settles into the real card once the status arrives',
        (tester) async {
      final billing = _PendingBilling();
      await tester.pumpWidget(MaterialApp(
        theme: lightTheme,
        home: Scaffold(body: BillingSection(service: billing)),
      ));
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsOneWidget);

      billing.complete({
        'billing_enabled': true,
        'plan': 'free',
        'plan_name': 'Free',
        'status': 'none',
        'limits': {'max_projects': 1},
        'usage': {'projects': 1},
      });
      await tester.pumpAndSettle();
      expect(find.byType(CircularProgressIndicator), findsNothing);
      expect(find.text('Free'), findsOneWidget);
    });

    testWidgets('a failed request hides the section rather than getting stuck',
        (tester) async {
      final billing = _PendingBilling();
      await tester.pumpWidget(MaterialApp(
        theme: lightTheme,
        home: Scaffold(body: BillingSection(service: billing)),
      ));
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsOneWidget);

      billing.fail();
      await tester.pumpAndSettle();
      expect(find.text('Plan'), findsNothing);
      expect(find.byType(Card), findsNothing);
    });
  });

  group('free plan', () {
    final free = {
      'billing_enabled': true,
      'plan': 'free',
      'plan_name': 'Free',
      'status': 'none',
      'limits': {
        'max_projects': 1,
        'max_storage_bytes': 500 * 1024 * 1024,
        'max_trip_days': 10,
      },
      'usage': {'projects': 1, 'storage_bytes': 250 * 1024 * 1024},
    };

    testWidgets('shows the plan and its usage', (tester) async {
      await _pump(tester, _FakeBilling(free));
      expect(find.text('Plan'), findsOneWidget);
      expect(find.text('Free'), findsOneWidget);
      expect(find.text('1 / 1'), findsOneWidget);
      expect(find.text('250 MB / 500 MB'), findsOneWidget);
    });

    testWidgets('states the trip-length limit', (tester) async {
      await _pump(tester, _FakeBilling(free));
      expect(find.text('Trip length'), findsOneWidget);
      expect(find.text('up to 10 days'), findsOneWidget);
    });

    testWidgets('invites the user to the plan page', (tester) async {
      await _pump(tester, _FakeBilling(free));
      expect(find.text('See plans & upgrade'), findsOneWidget);
    });
  });

  group('paid plan', () {
    final cloud = {
      'billing_enabled': true,
      'plan': 'tier_3',
      'plan_name': 'Tier 3',
      'status': 'active',
      'limits': {
        'max_projects': null,
        'max_storage_bytes': 1024 * 1024 * 1024,
        'max_trip_days': null,
      },
      'usage': {'projects': 7, 'storage_bytes': 1024 * 1024},
    };

    testWidgets('names the tier and shows what is unlimited', (tester) async {
      await _pump(tester, _FakeBilling(cloud));
      expect(find.text('Tier 3'), findsOneWidget);
      expect(find.text('7 / unlimited'), findsOneWidget);
      expect(find.text('any length'), findsOneWidget);
    });

    testWidgets('offers management rather than an upgrade', (tester) async {
      await _pump(tester, _FakeBilling(cloud));
      expect(find.text('Manage or change plan'), findsOneWidget);
    });

    testWidgets('flags a subscription that is ending', (tester) async {
      await _pump(tester, _FakeBilling({...cloud, 'cancel_at_period_end': true}));
      expect(find.text('Ends soon'), findsOneWidget);
    });

    testWidgets('flags a failed payment', (tester) async {
      await _pump(tester, _FakeBilling({...cloud, 'status': 'past_due'}));
      expect(find.text('Payment failed'), findsOneWidget);
    });

    testWidgets('flags a comped account', (tester) async {
      await _pump(tester, _FakeBilling({...cloud, 'admin_override': true}));
      expect(find.text('Granted'), findsOneWidget);
    });
  });

  group('opening the plan page', () {
    final free = {
      'billing_enabled': true,
      'plan': 'free',
      'plan_name': 'Free',
      'status': 'none',
      'limits': {'max_projects': 1},
      'usage': {'projects': 1},
    };

    testWidgets('the whole card is the way in', (tester) async {
      final opened = <String>[];
      await _pump(tester, _FakeBilling(free), opened: opened);
      await tester.tap(find.text('Free'));
      await tester.pumpAndSettle();
      expect(opened, ['plan-page']);
    });

    // Issue #153: the section laid out under the app theme, which forces every
    // ElevatedButton to infinite width unless the button overrides it. This is
    // the narrowest the card ever gets.
    testWidgets('lays out without overflowing a phone', (tester) async {
      await _pump(tester, _FakeBilling(free), surface: const Size(360, 800));
      expect(tester.takeException(), isNull);
      expect(find.text('See plans & upgrade'), findsOneWidget);
    });
  });

  group('returning from checkout (issue #192)', () {
    final free = {
      'billing_enabled': true,
      'plan': 'free',
      'plan_name': 'Free',
      'status': 'none',
      'limits': {'max_projects': 1, 'max_storage_bytes': 1, 'max_trip_days': 1},
      'usage': {'projects': 0, 'storage_bytes': 0},
    };
    final paid = {
      'billing_enabled': true,
      'plan': 'tier_1',
      'plan_name': 'Tier 1',
      'status': 'active',
      'limits': {'max_projects': 2, 'max_storage_bytes': 1, 'max_trip_days': 1},
      'usage': {'projects': 0, 'storage_bytes': 0},
    };

    testWidgets('without ?checkout=success, a free read is not retried',
        (tester) async {
      final billing = _SequenceBilling([free]);
      await _pump(tester, billing, currentUri: Uri.parse('https://x/settings'));
      expect(billing.calls, 1);
      expect(find.text('Free'), findsOneWidget);
    });

    testWidgets('?checkout=success but already paid needs no retry',
        (tester) async {
      final billing = _SequenceBilling([paid]);
      await _pump(tester,
          billing, currentUri: Uri.parse('https://x/settings?checkout=success'));
      expect(billing.calls, 1);
      expect(find.text('Tier 1'), findsOneWidget);
    });

    testWidgets(
        '?checkout=success retries until the webhook-updated plan shows',
        (tester) async {
      final billing = _SequenceBilling([free, free, paid]);
      await _pump(tester,
          billing, currentUri: Uri.parse('https://x/settings?checkout=success'));
      expect(billing.calls, 3);
      expect(find.text('Tier 1'), findsOneWidget);
    });

    testWidgets('gives up after 6 reads rather than retrying forever',
        (tester) async {
      final billing = _SequenceBilling([free]);
      await _pump(tester,
          billing, currentUri: Uri.parse('https://x/settings?checkout=success'));
      expect(billing.calls, 6); // 1 initial + 5 retries
      expect(find.text('Free'), findsOneWidget);
    });
  });
}
