/// Choosing a plan, and the recommendation after a refusal (issue #121).
///
/// Pumped under the real app theme, whose button sizing is what broke the
/// Settings plan actions while a themeless test suite stayed green (#153).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/billing/billing_service.dart';
import 'package:traxjourney_client/src/billing/plan_picker.dart';
import 'package:traxjourney_client/src/billing/upgrade_sheet.dart';
import 'package:traxjourney_client/src/core/theme.dart';

const _mb = 1024 * 1024;
const _gb = 1024 * _mb;

PlanInfo _plan(String id, String name,
        {int? projects, int? storage, int? days}) =>
    PlanInfo(
      id: id,
      name: name,
      priceLabel: '€x / month',
      features: ['$name feature'],
      limits: PlanLimits(
          maxProjects: projects, maxStorageBytes: storage, maxTripDays: days),
    );

final _catalogue = [
  _plan('free', 'Free', projects: 1, storage: 500 * _mb, days: 10),
  _plan('tier_1', 'Tier 1', projects: 2, storage: 5 * _gb, days: 100),
  _plan('tier_2', 'Tier 2', projects: 10, storage: 20 * _gb, days: 365),
  _plan('tier_3', 'Tier 3', storage: 50 * _gb),
];

class _FakeBilling implements BillingService {
  final List<PlanInfo> catalogue;
  final bool failCheckout;
  final List<String> bought = [];
  final List<String> changed = [];

  _FakeBilling({List<PlanInfo>? catalogue, this.failCheckout = false})
      : catalogue = catalogue ?? _catalogue;

  @override
  Future<List<PlanInfo>> plans() async => catalogue;

  @override
  Future<String> checkoutUrl({String? plan, String returnPath = kPlanRoute}) async {
    bought.add(plan ?? '');
    if (failCheckout) throw Exception('nope');
    return 'https://pay.test/${plan ?? 'none'}';
  }

  @override
  Future<String> planChangeUrl({
    required String plan,
    String returnPath = kPlanRoute,
  }) async {
    changed.add(plan);
    if (failCheckout) throw Exception('nope');
    return 'https://pay.test/change/$plan';
  }

  @override
  Future<String> urlToReach({
    required String plan,
    required bool subscribed,
    String returnPath = kPlanRoute,
  }) async =>
      subscribed
          ? planChangeUrl(plan: plan, returnPath: returnPath)
          : checkoutUrl(plan: plan, returnPath: returnPath);

  @override
  Future<BillingStatus> status() async =>
      BillingStatus.fromJson({'billing_enabled': true});

  @override
  Future<String> portalUrl({String returnPath = kPlanRoute}) async =>
      'https://pay.test/portal';
}

QuotaError _refusal({
  String resource = 'trip_days',
  int needed = 30,
  String plan = 'free',
}) =>
    QuotaError(
      resource: resource,
      plan: plan,
      detail: 'That would make this trip $needed days long.',
      limit: 10,
      used: 10,
      needed: needed,
    );

Future<void> _pump(
  WidgetTester tester, {
  required _FakeBilling billing,
  QuotaError? because,
  String currentPlan = 'free',
  List<String>? opened,
  Size? surface,
}) async {
  if (surface != null) {
    tester.view.physicalSize = surface;
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
  }
  await tester.pumpWidget(MaterialApp(
    theme: lightTheme,
    home: Scaffold(
      body: PlanPicker(
        currentPlan: currentPlan,
        because: because,
        service: billing,
        launcher: (url) async => opened?.add(url),
      ),
    ),
  ));
  await tester.pumpAndSettle();
}

void main() {
  group('cheapestPlanCovering', () {
    test('picks the cheapest tier that covers the need', () {
      final plan = cheapestPlanCovering(_catalogue, 'trip_days', 30);
      expect(plan!.id, 'tier_1');
    });

    test('a longer trip needs a stronger tier', () {
      expect(cheapestPlanCovering(_catalogue, 'trip_days', 200)!.id, 'tier_2');
    });

    test('an unlimited tier covers any need', () {
      expect(cheapestPlanCovering(_catalogue, 'trip_days', 5000)!.id, 'tier_3');
    });

    test('never suggests the free plan', () {
      expect(cheapestPlanCovering(_catalogue, 'trip_days', 5)!.isFree, isFalse);
    });

    test('never suggests what the user already has', () {
      final plan = cheapestPlanCovering(_catalogue, 'trip_days', 30,
          strongerThan: 'tier_1');
      expect(plan!.id, 'tier_2');
    });

    test('returns null when nothing covers it', () {
      expect(cheapestPlanCovering(_catalogue, 'storage', 900 * _gb), isNull);
    });

    test('storage and trips are considered separately', () {
      expect(cheapestPlanCovering(_catalogue, 'projects', 5)!.id, 'tier_2');
      expect(cheapestPlanCovering(_catalogue, 'storage', 6 * _gb)!.id, 'tier_2');
    });
  });

  group('picker', () {
    testWidgets('lists every plan', (tester) async {
      await _pump(tester, billing: _FakeBilling());
      for (final name in ['Free', 'Tier 1', 'Tier 2', 'Tier 3']) {
        expect(find.text(name), findsOneWidget);
      }
    });

    testWidgets('marks the current plan and does not offer it', (tester) async {
      await _pump(tester, billing: _FakeBilling(), currentPlan: 'tier_1');
      expect(find.text('Current plan'), findsOneWidget);
      expect(find.text('Choose Tier 1'), findsNothing);
    });

    testWidgets('the free plan is never buyable', (tester) async {
      await _pump(tester, billing: _FakeBilling());
      expect(find.text('Choose Free'), findsNothing);
    });

    testWidgets('choosing a plan buys that plan', (tester) async {
      final billing = _FakeBilling();
      final opened = <String>[];
      await _pump(tester, billing: billing, opened: opened);
      await tester.tap(find.text('Choose Tier 2'));
      await tester.pumpAndSettle();
      expect(billing.bought, ['tier_2']);
      expect(opened, ['https://pay.test/tier_2']);
    });

    testWidgets('a failed checkout says so and keeps the sheet open',
        (tester) async {
      await _pump(tester, billing: _FakeBilling(failCheckout: true));
      await tester.tap(find.text('Choose Tier 1'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Could not start checkout'), findsOneWidget);
      expect(find.text('Choose Tier 1'), findsOneWidget);
    });

    testWidgets('an empty catalogue says so rather than showing nothing',
        (tester) async {
      await _pump(tester, billing: _FakeBilling(catalogue: []));
      expect(find.textContaining('unavailable'), findsOneWidget);
    });

    testWidgets('lays out on a phone under the app theme', (tester) async {
      await _pump(tester,
          billing: _FakeBilling(), surface: const Size(360, 800));
      expect(tester.takeException(), isNull);
      expect(find.text('Choose Tier 1'), findsOneWidget);
    });
  });

  group('opened by a refusal', () {
    testWidgets('explains what was refused', (tester) async {
      await _pump(tester, billing: _FakeBilling(), because: _refusal());
      expect(find.text('Upgrade to keep going'), findsOneWidget);
      expect(find.textContaining('30 days long'), findsOneWidget);
    });

    testWidgets('recommends the cheapest plan that would have allowed it',
        (tester) async {
      await _pump(tester, billing: _FakeBilling(), because: _refusal(needed: 200));
      expect(find.text('Recommended'), findsOneWidget);
      // Tier 2 is the first that covers 200 days.
      final tile = find.ancestor(
        of: find.text('Tier 2'),
        matching: find.byType(Container),
      );
      expect(find.descendant(of: tile.first, matching: find.text('Recommended')),
          findsOneWidget);
    });

    testWidgets('recommends nothing when no plan covers the need',
        (tester) async {
      await _pump(tester, billing: _FakeBilling(),
          because: _refusal(resource: 'storage', needed: 900 * _gb));
      expect(find.text('Recommended'), findsNothing);
    });

    testWidgets('without a refusal it is a plain plan chooser', (tester) async {
      await _pump(tester, billing: _FakeBilling());
      expect(find.text('Choose a plan'), findsOneWidget);
      expect(find.text('Recommended'), findsNothing);
    });
  });

  group('showUpgradeSheet', () {
    testWidgets('opens the picker framed by the refusal', (tester) async {
      final billing = _FakeBilling();
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => ElevatedButton(
              onPressed: () => showUpgradeSheet(context, _refusal(),
                  service: billing, launcher: (_) async {}),
              child: const Text('go'),
            ),
          ),
        ),
      ));
      await tester.tap(find.text('go'));
      await tester.pumpAndSettle();
      expect(find.text('Upgrade to keep going'), findsOneWidget);
      expect(find.text('Choose Tier 1'), findsOneWidget);
    });

    testWidgets('maybeShowUpgradeSheet does nothing without a refusal',
        (tester) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => ElevatedButton(
              onPressed: () => maybeShowUpgradeSheet(context, null),
              child: const Text('go'),
            ),
          ),
        ),
      ));
      await tester.tap(find.text('go'));
      await tester.pumpAndSettle();
      expect(find.text('Upgrade to keep going'), findsNothing);
    });
  });
}
