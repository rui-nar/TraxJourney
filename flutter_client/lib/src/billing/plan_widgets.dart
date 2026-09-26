/// The two pieces of billing UI that appear in more than one place (#153).
///
/// [PlanTile] renders one tier; [PlanUsage] renders one "used / allowed" bar.
/// Both are shared between the Settings summary card, the plan page and the
/// upgrade sheet — the same numbers described three different ways is exactly
/// how a limit shown to a user starts disagreeing with the limit enforced on
/// them.
library;

import 'package:flutter/material.dart';

import 'billing_service.dart';

/// One tier: name, price, what it includes, and the action that reaches it.
class PlanTile extends StatelessWidget {
  final PlanInfo plan;
  final bool isCurrent;
  final bool isRecommended;
  final bool busy;

  /// Button label, or null when this tile offers nothing to do.
  final String? action;
  final VoidCallback onChoose;

  const PlanTile({
    super.key,
    required this.plan,
    required this.isCurrent,
    required this.isRecommended,
    required this.busy,
    required this.action,
    required this.onChoose,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        border: Border.all(
          color: isRecommended ? theme.colorScheme.primary : theme.dividerColor,
          width: isRecommended ? 1.5 : 1,
        ),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Name and price wrap onto a second line rather than pushing the
              // badge off the edge — the three together overflow a 360 px phone
              // once the tier names get any longer (issue #153).
              Expanded(
                child: Wrap(
                  spacing: 8,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    Text(plan.name, style: theme.textTheme.titleMedium),
                    Text(plan.priceLabel, style: theme.textTheme.bodySmall),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              if (isCurrent)
                Text('Current plan', style: theme.textTheme.labelSmall)
              else if (isRecommended)
                Text('Recommended',
                    style: theme.textTheme.labelSmall
                        ?.copyWith(color: theme.colorScheme.primary)),
            ],
          ),
          const SizedBox(height: 10),
          for (final feature in plan.features)
            Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Row(
                children: [
                  Icon(Icons.check_rounded,
                      size: 16, color: theme.colorScheme.primary),
                  const SizedBox(width: 6),
                  Expanded(
                      child: Text(feature, style: theme.textTheme.bodySmall)),
                ],
              ),
            ),
          if (action != null) ...[
            const SizedBox(height: 12),
            SizedBox(
              width: double.infinity,
              child: ElevatedButton(
                onPressed: busy ? null : onChoose,
                child: busy
                    ? const SizedBox(
                        width: 16, height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : Text(action!),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

/// One "used / allowed" row with its bar. [fraction] null = unlimited.
class PlanUsage extends StatelessWidget {
  final String label;
  final String used;
  final String limit;
  final double? fraction;

  const PlanUsage({
    super.key,
    required this.label,
    required this.used,
    required this.limit,
    required this.fraction,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _LabelledValue(label: label, value: '$used / $limit'),
        const SizedBox(height: 6),
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(
            value: fraction ?? 0,
            minHeight: 6,
            backgroundColor: theme.colorScheme.surfaceContainerHighest,
          ),
        ),
      ],
    );
  }
}

/// The three usage bars for [status], in the order the limits are enforced.
///
/// Built from [BillingStatus] rather than from loose numbers so the summary
/// card and the plan page cannot show different things.
class PlanUsageBars extends StatelessWidget {
  final BillingStatus status;

  const PlanUsageBars({super.key, required this.status});

  @override
  Widget build(BuildContext context) {
    final maxProjects = status.limits.maxProjects;
    final maxStorage = status.limits.maxStorageBytes;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        PlanUsage(
          label: 'Trips',
          used: '${status.projects}',
          limit: maxProjects == null ? 'unlimited' : '$maxProjects',
          fraction: maxProjects == null
              ? null
              : (status.projects / maxProjects).clamp(0.0, 1.0),
        ),
        const SizedBox(height: 12),
        PlanUsage(
          label: 'Photos',
          used: formatBytes(status.storageBytes),
          limit: maxStorage == null ? 'unlimited' : formatBytes(maxStorage),
          fraction: status.storageFraction,
        ),
        const SizedBox(height: 12),
        // No bar: nothing is "used up" here — it is a ceiling on one trip, not
        // a running total across the account.
        _LabelledValue(
          label: 'Trip length',
          value: status.limits.maxTripDays == null
              ? 'any length'
              : 'up to ${status.limits.maxTripDays} days',
        ),
      ],
    );
  }
}

/// A label on the left, its value on the right, on one line where they fit.
///
/// The label yields rather than the row overflowing: these sit inside a card
/// inside a page, so the width they get is already three levels of padding
/// short of the screen, and "500 MB / 500 MB" against a long label does not fit
/// a narrow phone.
class _LabelledValue extends StatelessWidget {
  final String label;
  final String value;

  const _LabelledValue({required this.label, required this.value});

  @override
  Widget build(BuildContext context) {
    final style = Theme.of(context).textTheme.bodySmall;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(child: Text(label, style: style)),
        const SizedBox(width: 8),
        Text(value, style: style, textAlign: TextAlign.end),
      ],
    );
  }
}

/// "Switching to Tier 1 on 3 September", or null when nothing is scheduled.
///
/// Pure and exported so the wording is testable without a widget. A downgrade
/// is agreed now and applied when the paid period ends, and between the two the
/// account still reports the old tier — without this the page looks exactly as
/// it did before the request (issue #153).
String? pendingPlanNotice(BillingStatus status) {
  if (status.pendingPlan.isEmpty) return null;
  final name =
      status.pendingPlanName.isEmpty ? status.pendingPlan : status.pendingPlanName;
  if (status.pendingPlanAt <= 0) return 'Switching to $name.';
  final when = DateTime.fromMillisecondsSinceEpoch(
      (status.pendingPlanAt * 1000).round(),
      isUtc: true).toLocal();
  return 'Switching to $name on ${_dayAndMonth(when)}.';
}

/// What the plan page says above "Withdraw and get a refund" (issue #441).
String withdrawalNotice(BillingStatus status) {
  const rest = 'you can withdraw: your plan ends now and the unused part of '
      'the period you paid for is refunded.';
  if (status.withdrawalClosesAt <= 0) {
    return 'Within 14 days of your first purchase, $rest';
  }
  final until = DateTime.fromMillisecondsSinceEpoch(
      (status.withdrawalClosesAt * 1000).round(),
      isUtc: true).toLocal();
  return 'Until ${_dayAndMonth(until)}, $rest';
}

const _months = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

String _dayAndMonth(DateTime d) => '${d.day} ${_months[d.month - 1]}';

/// "Ends soon" / "Payment failed" / "Granted" — the one thing about a
/// subscription that is not obvious from its plan name.
class PlanStatusChip extends StatelessWidget {
  final BillingStatus status;

  const PlanStatusChip({super.key, required this.status});

  /// The label for [status], or null when there is nothing worth saying.
  ///
  /// A scheduled change outranks "Ends soon": both are cancellations at the
  /// provider, but "switching to Tier 1" is the true and less alarming one.
  static String? labelFor(BillingStatus status) {
    if (status.adminOverride) return 'Granted';
    if (status.pendingPlan.isNotEmpty) return 'Plan changing';
    if (status.cancelAtPeriodEnd) return 'Ends soon';
    if (status.status == 'past_due') return 'Payment failed';
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final label = labelFor(status);
    if (label == null) return const SizedBox.shrink();
    final theme = Theme.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: theme.colorScheme.secondaryContainer,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(label,
          style: theme.textTheme.labelSmall
              ?.copyWith(color: theme.colorScheme.onSecondaryContainer)),
    );
  }
}
