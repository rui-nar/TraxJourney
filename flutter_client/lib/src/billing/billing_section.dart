/// "Plan" summary in the settings screen (issues #121, #153).
///
/// A summary, not the whole story: plan, the state worth flagging, and how full
/// the account is. Anything you can *do* about it lives on the plan page, which
/// the whole card opens — keeping the actions here is what let a broken button
/// row leave Settings with no way to change plan at all.
///
/// Renders nothing when the server reports `billing_enabled: false` — a
/// self-hosted instance has no tiers, and showing a greyed-out plan card there
/// would advertise a limit that does not exist.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import 'billing_service.dart';
import 'plan_widgets.dart';

class BillingSection extends StatefulWidget {
  /// Injected in tests; defaults to the live service.
  final BillingService? service;

  /// Opens the plan page. Injected in tests.
  final void Function()? onOpen;

  /// The page URL, checked for `?checkout=success` (issue #192). Injected in
  /// tests; defaults to the real browser URL — checkout only ever happens on
  /// web (see docs/BILLING.md), so this is always empty on other platforms.
  ///
  /// Still checked here even though the plan page is now where the provider
  /// returns to (issue #153): `_return_url` falls back to `/settings` for a
  /// missing or unsafe return path, and sessions created before that change
  /// come back here too.
  final Uri? currentUri;

  /// Spacing between plan-status retries after a checkout redirect.
  /// Injected in tests to keep them fast; defaults to a real wait.
  final Duration retryDelay;

  /// Told each plan status this section loads, so the rest of the screen can
  /// use it without asking the server again (the delete-account warning,
  /// issue #429).
  final ValueChanged<BillingStatus>? onStatus;

  const BillingSection({
    super.key,
    this.service,
    this.onOpen,
    this.currentUri,
    this.retryDelay = const Duration(seconds: 2),
    this.onStatus,
  });

  @override
  State<BillingSection> createState() => _BillingSectionState();
}

class _BillingSectionState extends State<BillingSection> {
  late final BillingService _billing = widget.service ?? BillingService();
  late Future<BillingStatus> _future = _report(_loadStatus());

  Future<BillingStatus> _report(Future<BillingStatus> status) =>
      status.then((s) {
        widget.onStatus?.call(s);
        return s;
      });

  /// Stripe's webhook can lag a beat behind the checkout redirect (issue
  /// #192): landing back on `?checkout=success` right after paying can still
  /// read the pre-upgrade plan if we ask before the webhook has landed. Poll
  /// briefly rather than believe the very first read.
  Future<BillingStatus> _loadStatus() async {
    final uri = widget.currentUri ?? Uri.base;
    final justPaid = uri.queryParameters['checkout'] == 'success';
    var status = await _billing.status();
    if (!justPaid) return status;
    for (var i = 0; i < 5 && !status.isPaid; i++) {
      await Future.delayed(widget.retryDelay);
      status = await _billing.status();
    }
    return status;
  }

  Future<void> _open() async {
    final open = widget.onOpen;
    if (open != null) {
      open();
      return;
    }
    await context.push(kPlanRoute);
    // Re-read on the way back: the plan may have changed while they were away.
    // Braces, not an arrow: an arrow would *return* the assigned Future, and
    // setState rejects a callback that returns one.
    if (mounted) {
      setState(() {
        _future = _report(_billing.status());
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<BillingStatus>(
      future: _future,
      builder: (context, snapshot) {
        final status = snapshot.data;
        // No response yet (and no prior one to fall back on, e.g. from a
        // refetch after returning from the plan page): reserve the card's
        // position and show a spinner in it, like Strava/Polarsteps do,
        // rather than collapsing to zero height (issue #196).
        if (status == null) {
          if (snapshot.hasError) {
            return const SizedBox.shrink();
          }
          return Padding(
            padding: const EdgeInsets.only(bottom: 16),
            child: _loadingCard(context),
          );
        }
        // Hidden on self-hosted instances, now that the response has
        // actually come back: the section must never flash a plan the
        // deployment doesn't sell.
        if (!status.billingEnabled) {
          return const SizedBox.shrink();
        }
        return Padding(
          padding: const EdgeInsets.only(bottom: 16),
          child: _card(context, status),
        );
      },
    );
  }

  Widget _loadingCard(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      clipBehavior: Clip.antiAlias,
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.workspace_premium_outlined,
                    color: theme.colorScheme.primary, size: 20),
                const SizedBox(width: 8),
                Text('Plan', style: theme.textTheme.titleLarge),
              ],
            ),
            const Divider(height: 24),
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 8),
              child: Center(child: CircularProgressIndicator()),
            ),
          ],
        ),
      ),
    );
  }

  Widget _card(BuildContext context, BillingStatus status) {
    final theme = Theme.of(context);
    return Card(
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: _open,
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(Icons.workspace_premium_outlined,
                      color: theme.colorScheme.primary, size: 20),
                  const SizedBox(width: 8),
                  Text('Plan', style: theme.textTheme.titleLarge),
                  const Spacer(),
                  Icon(Icons.chevron_right,
                      color: theme.colorScheme.onSurfaceVariant),
                ],
              ),
              const Divider(height: 24),
              Wrap(
                spacing: 8,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  Text(status.planName, style: theme.textTheme.titleMedium),
                  PlanStatusChip(status: status),
                ],
              ),
              if (pendingPlanNotice(status) case final notice?) ...[
                const SizedBox(height: 6),
                Text(notice,
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.primary)),
              ],
              const SizedBox(height: 16),
              PlanUsageBars(status: status),
              const SizedBox(height: 16),
              Text(
                status.isPaid ? 'Manage or change plan' : 'See plans & upgrade',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.primary),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
