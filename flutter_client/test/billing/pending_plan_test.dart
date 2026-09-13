/// Telling someone about a plan change that has not happened yet (#153).
///
/// A downgrade is agreed now and applied when the paid period ends, so between
/// the two the account still reports the old tier. Saying nothing makes a
/// successful request look like a no-op — which is exactly the complaint that
/// opened this issue about the plan UI in the first place.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/billing/billing_service.dart';
import 'package:traxjourney_client/src/billing/plan_widgets.dart';
import 'package:traxjourney_client/src/core/theme.dart';

BillingStatus _status({
  String plan = 'tier_3',
  String pending = '',
  String pendingName = '',
  double at = 0,
  bool cancelAtPeriodEnd = false,
  bool adminOverride = false,
  String status = 'active',
}) =>
    BillingStatus.fromJson({
      'billing_enabled': true,
      'plan': plan,
      'plan_name': plan,
      'status': status,
      'cancel_at_period_end': cancelAtPeriodEnd,
      'admin_override': adminOverride,
      'pending_plan': pending,
      'pending_plan_name': pendingName,
      'pending_plan_at': at,
      'limits': {'max_projects': 2},
      'usage': {'projects': 1},
    });

/// 3 September 2026, 10:00 UTC.
final _sept3 =
    DateTime.utc(2026, 9, 3, 10).millisecondsSinceEpoch / 1000;

void main() {
  group('pendingPlanNotice', () {
    test('says nothing when nothing is scheduled', () {
      expect(pendingPlanNotice(_status()), isNull);
    });

    test('names the tier and the day it lands', () {
      final notice = pendingPlanNotice(
          _status(pending: 'tier_1', pendingName: 'Tier 1', at: _sept3));
      expect(notice, contains('Tier 1'));
      expect(notice, contains('September'));
    });

    test('uses the display name rather than the plan id', () {
      final notice = pendingPlanNotice(
          _status(pending: 'tier_1', pendingName: 'Hobby', at: _sept3));
      expect(notice, contains('Hobby'));
      expect(notice, isNot(contains('tier_1')));
    });

    test('falls back to the id when the server sent no name', () {
      final notice = pendingPlanNotice(_status(pending: 'tier_1', at: _sept3));
      expect(notice, contains('tier_1'));
    });

    test('omits the date rather than inventing one', () {
      final notice =
          pendingPlanNotice(_status(pending: 'free', pendingName: 'Free'));
      expect(notice, 'Switching to Free.');
    });

    test('renders the date in the reader local time zone', () {
      // The server sends unix seconds; showing them as UTC would put the switch
      // on the wrong day for anyone far enough east or west.
      final notice = pendingPlanNotice(
          _status(pending: 'tier_1', pendingName: 'Tier 1', at: _sept3));
      final local =
          DateTime.fromMillisecondsSinceEpoch((_sept3 * 1000).round(),
                  isUtc: true)
              .toLocal();
      expect(notice, contains('${local.day} '));
    });
  });

  group('PlanStatusChip.labelFor', () {
    test('a scheduled change outranks "ends soon"', () {
      // Both are cancellations at the provider; "switching to Tier 1" is the
      // true one and the one that does not alarm.
      final label = PlanStatusChip.labelFor(_status(
          pending: 'tier_1', pendingName: 'Tier 1', cancelAtPeriodEnd: true));
      expect(label, 'Plan changing');
    });

    test('a plain cancellation still says "ends soon"', () {
      expect(PlanStatusChip.labelFor(_status(cancelAtPeriodEnd: true)),
          'Ends soon');
    });

    test('a comp outranks everything', () {
      expect(
        PlanStatusChip.labelFor(
            _status(pending: 'tier_1', adminOverride: true)),
        'Granted',
      );
    });

    test('nothing to say about a plain active subscription', () {
      expect(PlanStatusChip.labelFor(_status()), isNull);
    });
  });

  group('rendering', () {
    testWidgets('the notice appears next to the plan', (tester) async {
      await tester.pumpWidget(MaterialApp(
        theme: lightTheme,
        home: Scaffold(
          body: Builder(
            builder: (context) => Text(pendingPlanNotice(_status(
                    pending: 'tier_1', pendingName: 'Tier 1', at: _sept3)) ??
                'nothing'),
          ),
        ),
      ));
      expect(find.textContaining('Switching to Tier 1'), findsOneWidget);
    });
  });
}
