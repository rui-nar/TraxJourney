/// The plan page (issue #153).
///
/// The cases that matter are the ones the old Settings section got wrong: a
/// subscriber must be sent to the *change* flow rather than to checkout (which
/// would bill them twice), choosing Free must mean cancelling, and coming back
/// from the payment provider must not look like nothing happened.
///
/// Pumped under the real app theme throughout — see billing_section_test.dart.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/billing/billing_service.dart';
import 'package:traxjourney_client/src/billing/plan_screen.dart';
import 'package:traxjourney_client/src/core/theme.dart';

const _mb = 1024 * 1024;

class _FakeBilling implements BillingService {
  /// Successive `/api/billing/me` payloads; the last one repeats.
  final List<Map<String, dynamic>> payloads;
  final bool subscribedOnServer;
  final bool failChange;

  int statusCalls = 0;
  final List<String> checkouts = [];
  final List<String> changes = [];
  int portalCalls = 0;

  _FakeBilling(
    Map<String, dynamic> payload, {
    List<Map<String, dynamic>>? then,
    this.subscribedOnServer = true,
    this.failChange = false,
  }) : payloads = [payload, ...?then];

  @override
  Future<BillingStatus> status() async {
    final index = statusCalls < payloads.length ? statusCalls : payloads.length - 1;
    statusCalls++;
    return BillingStatus.fromJson(payloads[index]);
  }

  @override
  Future<List<PlanInfo>> plans() async => const [
        PlanInfo(id: 'free', name: 'Free', priceLabel: 'Free', features: []),
        PlanInfo(id: 'tier_1', name: 'Tier 1', priceLabel: '€0.99 / month',
            features: ['2 trips']),
        PlanInfo(id: 'tier_2', name: 'Tier 2', priceLabel: '€3.99 / month',
            features: ['10 trips']),
      ];

  @override
  Future<String> checkoutUrl({String? plan, String returnPath = kPlanRoute}) async {
    checkouts.add(plan ?? '');
    return 'https://pay.test/checkout/${plan ?? ''}';
  }

  @override
  Future<String> planChangeUrl({
    required String plan,
    String returnPath = kPlanRoute,
  }) async {
    changes.add(plan);
    if (failChange) throw Exception('provider down');
    if (!subscribedOnServer) throw const NotSubscribed();
    return 'https://pay.test/change/$plan';
  }

  @override
  Future<String> urlToReach({
    required String plan,
    required bool subscribed,
    String returnPath = kPlanRoute,
  }) async {
    // The real implementation, so the fallback is exercised rather than faked.
    if (subscribed) {
      try {
        return await planChangeUrl(plan: plan, returnPath: returnPath);
      } on NotSubscribed {
        // fall through
      }
    }
    return checkoutUrl(plan: plan, returnPath: returnPath);
  }

  @override
  Future<String> portalUrl({String returnPath = kPlanRoute}) async {
    portalCalls++;
    return 'https://pay.test/portal';
  }
}

final _free = <String, dynamic>{
  'billing_enabled': true,
  'quotas_enforced': true,
  'plan': 'free',
  'plan_name': 'Free',
  'status': 'none',
  'limits': {
    'max_projects': 1,
    'max_storage_bytes': 500 * _mb,
    'max_trip_days': 10,
  },
  'usage': {'projects': 1, 'storage_bytes': 250 * _mb},
};

final _subscribed = <String, dynamic>{
  ..._free,
  'plan': 'tier_1',
  'plan_name': 'Tier 1',
  'status': 'active',
  'limits': {
    'max_projects': 2,
    'max_storage_bytes': 5 * 1024 * _mb,
    'max_trip_days': 100,
  },
};

Future<void> _pump(
  WidgetTester tester,
  _FakeBilling billing, {
  List<String>? opened,
  String? outcome,
  Size surface = const Size(800, 2000),
}) async {
  // Tall by default: the page lists every tier, and the default 800×600 test
  // surface leaves the lower ones unhittable.
  tester.view.physicalSize = surface;
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(MaterialApp(
    theme: lightTheme,
    home: PlanScreen(
      service: billing,
      launcher: (url) async => opened?.add(url),
      checkoutOutcome: outcome,
    ),
  ));
  await tester.pumpAndSettle();
}

void main() {
  group('self-hosted', () {
    testWidgets('says there are no plans rather than showing an empty one',
        (tester) async {
      await _pump(tester, _FakeBilling({'billing_enabled': false}));
      expect(find.textContaining('self-hosted'), findsOneWidget);
      expect(find.text('Choose Tier 1'), findsNothing);
    });
  });

  group('usage', () {
    testWidgets('shows what is used against what is allowed', (tester) async {
      await _pump(tester, _FakeBilling(_free));
      expect(find.text('Free'), findsWidgets);
      expect(find.text('1 / 1'), findsOneWidget);
      expect(find.text('250 MB / 500 MB'), findsOneWidget);
      expect(find.text('up to 10 days'), findsOneWidget);
    });

    testWidgets('says so when limits are measured but not enforced',
        (tester) async {
      await _pump(tester, _FakeBilling({..._free, 'quotas_enforced': false}));
      expect(find.textContaining('not enforced'), findsOneWidget);
    });

    testWidgets('hides invoices until there is a billing account',
        (tester) async {
      await _pump(tester, _FakeBilling(_free));
      expect(find.text('Manage billing'), findsNothing);
    });
  });

  group('choosing a plan', () {
    testWidgets('lists every tier and marks the current one', (tester) async {
      await _pump(tester, _FakeBilling(_free));
      expect(find.text('Current plan'), findsOneWidget);
      expect(find.text('Choose Tier 1'), findsOneWidget);
      expect(find.text('Choose Tier 2'), findsOneWidget);
    });

    testWidgets('a first-time buyer goes to checkout', (tester) async {
      final billing = _FakeBilling(_free, subscribedOnServer: false);
      final opened = <String>[];
      await _pump(tester, billing, opened: opened);
      await tester.tap(find.text('Choose Tier 2'));
      await tester.pumpAndSettle();
      expect(billing.checkouts, ['tier_2']);
      expect(billing.changes, isEmpty);
      expect(opened, ['https://pay.test/checkout/tier_2']);
    });

    testWidgets('a subscriber changes plan instead of buying a second one',
        (tester) async {
      final billing = _FakeBilling(_subscribed);
      final opened = <String>[];
      await _pump(tester, billing, opened: opened);
      await tester.tap(find.text('Choose Tier 2'));
      await tester.pumpAndSettle();
      expect(billing.changes, ['tier_2']);
      expect(billing.checkouts, isEmpty,
          reason: 'checkout would create a second subscription (#163)');
      expect(opened, ['https://pay.test/change/tier_2']);
    });

    testWidgets('falls back to checkout when the server says nothing is running',
        (tester) async {
      // The subscription can end between loading this page and tapping a tile.
      final billing = _FakeBilling(_subscribed, subscribedOnServer: false);
      final opened = <String>[];
      await _pump(tester, billing, opened: opened);
      await tester.tap(find.text('Choose Tier 2'));
      await tester.pumpAndSettle();
      expect(billing.changes, ['tier_2']);
      expect(opened, ['https://pay.test/checkout/tier_2']);
    });

    testWidgets('free is not offered to someone who is not paying',
        (tester) async {
      await _pump(tester, _FakeBilling(_free));
      expect(find.text('Cancel subscription'), findsNothing);
    });

    testWidgets('a subscriber can move down to free, which cancels',
        (tester) async {
      final billing = _FakeBilling(_subscribed);
      final opened = <String>[];
      await _pump(tester, billing, opened: opened);
      await tester.tap(find.text('Cancel subscription'));
      await tester.pumpAndSettle();
      expect(billing.changes, ['free']);
      expect(opened, ['https://pay.test/change/free']);
    });

    testWidgets('explains when each direction takes effect', (tester) async {
      await _pump(tester, _FakeBilling(_subscribed));
      expect(find.textContaining('Moving down takes effect'), findsOneWidget);
    });

    testWidgets('a downgrade that has not landed yet is visible', (tester) async {
      // Otherwise the page looks untouched after the request succeeded.
      await _pump(
        tester,
        _FakeBilling({
          ..._subscribed,
          'pending_plan': 'free',
          'pending_plan_name': 'Free',
          'pending_plan_at': DateTime.utc(2026, 9, 3).millisecondsSinceEpoch / 1000,
        }),
      );
      expect(find.textContaining('Switching to Free'), findsOneWidget);
      expect(find.text('Plan changing'), findsOneWidget);
      // …and the tier they are still on is still the one in force.
      expect(find.text('Tier 1'), findsWidgets);
    });

    testWidgets('a provider failure says so and leaves the page usable',
        (tester) async {
      final billing = _FakeBilling(_subscribed, failChange: true);
      await _pump(tester, billing);
      await tester.tap(find.text('Choose Tier 2'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Could not start the plan change'),
          findsOneWidget);
      expect(find.text('Choose Tier 2'), findsOneWidget);
    });
  });

  group('managing an existing subscription', () {
    testWidgets('opens the provider portal for invoices', (tester) async {
      final billing = _FakeBilling(_subscribed);
      final opened = <String>[];
      await _pump(tester, billing, opened: opened);
      await tester.tap(find.text('Manage billing'));
      await tester.pumpAndSettle();
      expect(billing.portalCalls, 1);
      expect(opened, ['https://pay.test/portal']);
    });
  });

  group('coming back from the payment provider', () {
    testWidgets('acknowledges a completed payment', (tester) async {
      await _pump(tester, _FakeBilling(_free), outcome: 'success');
      expect(find.textContaining('Payment received'), findsOneWidget);
    });

    testWidgets('says nothing was charged when checkout was abandoned',
        (tester) async {
      await _pump(tester, _FakeBilling(_free), outcome: 'cancelled');
      expect(find.textContaining('nothing was charged'), findsOneWidget);
      expect(find.textContaining('Payment received'), findsNothing);
    });

    testWidgets('re-reads until the new plan arrives', (tester) async {
      // The provider redirects the moment payment succeeds; the plan is granted
      // by a webhook a beat later. Reading once would show the old plan, which
      // reads as "the payment did nothing".
      final billing = _FakeBilling(_free, then: [_subscribed]);
      await _pump(tester, billing, outcome: 'success');
      expect(find.text('Free'), findsWidgets);

      await tester.pump(const Duration(seconds: 1));
      await tester.pumpAndSettle();
      expect(billing.statusCalls, greaterThan(1));
      expect(find.text('Tier 1'), findsWidgets);
    });

    testWidgets('stops re-reading once the plan has changed', (tester) async {
      final billing = _FakeBilling(_free, then: [_subscribed]);
      await _pump(tester, billing, outcome: 'success');
      await tester.pump(const Duration(seconds: 1));
      await tester.pumpAndSettle();
      final settled = billing.statusCalls;
      await tester.pump(const Duration(seconds: 3));
      await tester.pumpAndSettle();
      expect(billing.statusCalls, settled);
    });

    testWidgets('gives up rather than polling forever', (tester) async {
      // A webhook that never lands must not leave the page spinning.
      final billing = _FakeBilling(_free);
      await _pump(tester, billing, outcome: 'success');
      await tester.pump(const Duration(seconds: 10));
      await tester.pumpAndSettle();
      expect(billing.statusCalls, lessThanOrEqualTo(7));
    });
  });

  group('layout', () {
    testWidgets('fits a phone without overflowing', (tester) async {
      await _pump(tester, _FakeBilling(_subscribed),
          surface: const Size(360, 1400));
      expect(tester.takeException(), isNull);
      expect(find.text('Choose Tier 2'), findsOneWidget);
    });
  });
}
