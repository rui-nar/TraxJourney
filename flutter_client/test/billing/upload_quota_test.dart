/// An upload refused on a plan limit must reach the upgrade prompt (#121).
///
/// The photo/avatar uploads are raw multipart requests that report failure by
/// returning null/false, so a 402 used to drop the file with nothing on screen.
/// They now hand the response to [ProjectQuotaMixin.recordQuotaRefusal], and
/// the dialog that ran the upload consumes it — that hand-off is what is
/// covered here, plus the helper the dialogs call.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/billing/billing_service.dart';
import 'package:traxjourney_client/src/billing/upgrade_sheet.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

final _quotaBody = jsonEncode({
  'detail': "This upload would exceed your plan's storage.",
  'code': 'quota_exceeded',
  'resource': 'storage',
  'plan': 'free',
  'limit': 524288000,
  'used': 524288000,
});

ProjectNotifier _notifier() => ProjectNotifier(ProjectService());

/// The picker fetches the catalogue, so the prompt needs a service to talk to.
class _FakeBilling implements BillingService {
  @override
  Future<List<PlanInfo>> plans() async => const [
        PlanInfo(id: 'free', name: 'Free', priceLabel: 'Free', features: []),
        PlanInfo(id: 'tier_1', name: 'Tier 1', priceLabel: '€1 / month',
            features: [], limits: PlanLimits(maxStorageBytes: 5 * 1024 * 1024 * 1024)),
      ];

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
  Future<BillingStatus> status() async =>
      BillingStatus.fromJson({'billing_enabled': true});
}

void main() {
  group('recordQuotaRefusal', () {
    test('a 402 becomes a pending quota error', () {
      final notifier = _notifier();
      expect(notifier.recordQuotaRefusal(402, _quotaBody), isTrue);
      expect(notifier.quotaError, isNotNull);
      expect(notifier.quotaError!.resource, 'storage');
      expect(notifier.quotaError!.detail, contains('exceed'));
    });

    test('any other failure is left alone', () {
      final notifier = _notifier();
      expect(notifier.recordQuotaRefusal(500, 'boom'), isFalse);
      expect(notifier.quotaError, isNull);
    });

    test('a 402 that is not a quota refusal is left alone', () {
      final notifier = _notifier();
      expect(notifier.recordQuotaRefusal(402, '{"detail":"card declined"}'),
          isFalse);
      expect(notifier.quotaError, isNull);
    });

    test('it does not also set the error banner', () {
      // The sheet is the surface; setting error too would report it twice.
      final notifier = _notifier();
      notifier.recordQuotaRefusal(402, _quotaBody);
      expect(notifier.error, isNull);
    });

    test('it notifies listeners', () {
      final notifier = _notifier();
      var notified = 0;
      notifier.addListener(() => notified++);
      notifier.recordQuotaRefusal(402, _quotaBody);
      expect(notified, 1);
    });
  });

  group('takeQuotaError', () {
    test('one refusal opens exactly one prompt', () {
      final notifier = _notifier();
      notifier.recordQuotaRefusal(402, _quotaBody);
      expect(notifier.takeQuotaError(), isNotNull);
      expect(notifier.takeQuotaError(), isNull);
    });

    test('nothing pending yields nothing', () {
      expect(_notifier().takeQuotaError(), isNull);
    });
  });

  group('maybeShowUpgradeSheet', () {
    testWidgets('opens the upgrade prompt for a refusal', (tester) async {
      final notifier = _notifier()..recordQuotaRefusal(402, _quotaBody);
      await _pumpHost(tester, notifier);
      await tester.tap(find.text('save'));
      await tester.pumpAndSettle();
      expect(find.text('Upgrade to keep going'), findsOneWidget);
      expect(find.textContaining('exceed your'), findsOneWidget);
    });

    testWidgets('stays out of the way when the upload succeeded',
        (tester) async {
      await _pumpHost(tester, _notifier());
      await tester.tap(find.text('save'));
      await tester.pumpAndSettle();
      expect(find.text('Upgrade to keep going'), findsNothing);
    });

    testWidgets('the prompt does not reopen on the next save', (tester) async {
      final notifier = _notifier()..recordQuotaRefusal(402, _quotaBody);
      await _pumpHost(tester, notifier);
      await tester.tap(find.text('save'));
      await tester.pumpAndSettle();
      Navigator.of(tester.element(find.text('save'))).pop();
      await tester.pumpAndSettle();

      await tester.tap(find.text('save'));
      await tester.pumpAndSettle();
      expect(find.text('Upgrade to keep going'), findsNothing);
    });
  });
}

/// A button that runs the same two lines the dialogs run after saving.
Future<void> _pumpHost(WidgetTester tester, ProjectNotifier notifier) async {
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => maybeShowUpgradeSheet(
              context, notifier.takeQuotaError(),
              service: _FakeBilling(), launcher: (_) async {}),
          child: const Text('save'),
        ),
      ),
    ),
  ));
}
