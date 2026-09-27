/// The plan page: where you stand, and how to stand somewhere else (#153).
///
/// Settings shows a summary card; this is the whole picture. The two halves of
/// the question live on one screen on purpose — "am I running out of room" and
/// "what would fix that" are the same thought, and answering the first while
/// hiding the second is what the section used to do.
///
/// Renders nothing billing-related when the server reports
/// `billing_enabled: false`: a self-hosted instance has no tiers, and this page
/// says so rather than showing a plan nobody can buy.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api/client.dart';
import '../core/design_tokens.dart';
import 'billing_service.dart';
import 'plan_widgets.dart';

/// What the withdrawal confirmation says (issue #441). [quote] is the
/// server's estimate: the refund itself is measured the moment the
/// cancellation lands, so it can differ by a cent. A part that cannot go back
/// to the card (paid from the Stripe balance, say) is said separately.
String withdrawalConfirmation(Withdrawal quote) {
  const ends = 'Your plan ends immediately and is not renewed.';
  final card = quote.refunded;
  final owed = quote.owed;
  if (card.cents <= 0 && owed.cents <= 0) {
    return '$ends Nothing is left to refund for the period you paid for.';
  }
  final parts = <String>[ends];
  if (card.cents > 0) {
    parts.add('About ${card.label} — the unused part of the period you paid '
        'for — is refunded to your payment method.');
  }
  if (owed.cents > 0) {
    parts.add('About ${owed.label} cannot go back to your payment method '
        'automatically; it will be refunded to you separately.');
  }
  return parts.join(' ');
}

/// What the page says once the withdrawal went through.
///
/// When part of the refund could not be made automatically, the server has
/// recorded it as owed (issue #441): say so, rather than a smaller number
/// that reads as the whole refund.
String withdrawalDone(Withdrawal done) {
  final refunded = done.refunded;
  final owed = done.owed;
  final head = refunded.cents > 0
      ? 'Withdrawn. ${refunded.label} is on its way back to your payment method.'
      : 'Withdrawn. Your plan has ended.';
  if (owed.cents <= 0) return head;
  return '$head ${owed.label} could not be refunded automatically. It is '
      'recorded and will be refunded to you.';
}

class PlanScreen extends StatefulWidget {
  /// Injected in tests; defaults to the live service.
  final BillingService? service;

  /// Opens provider URLs. Injected in tests.
  final Future<void> Function(String url)? launcher;

  /// `?checkout=` on the URL the provider redirected back to.
  final String? checkoutOutcome;

  const PlanScreen({
    super.key,
    this.service,
    this.launcher,
    this.checkoutOutcome,
  });

  @override
  State<PlanScreen> createState() => _PlanScreenState();
}

class _PlanScreenState extends State<PlanScreen> {
  late final BillingService _billing = widget.service ?? BillingService();

  late Future<BillingStatus> _status = _billing.status();
  late final Future<List<PlanInfo>> _plans = _billing.plans();

  String? _busyPlan;
  bool _busyPortal = false;
  bool _busyWithdraw = false;
  String? _failure;

  /// How long, and how often, to wait for the webhook after a payment.
  static const _pollInterval = Duration(milliseconds: 900);
  static const _maxPolls = 5;

  Timer? _poll;

  @override
  void initState() {
    super.initState();
    if (widget.checkoutOutcome == 'success') {
      _status.then(
        (first) {
          if (mounted) _awaitNewPlan(first.plan);
        },
        onError: (_) {},
      );
    }
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  /// Re-read until the purchase shows up, or until it clearly is not going to.
  ///
  /// The provider redirects the browser back the moment payment succeeds, but
  /// the plan is granted by a webhook that lands a beat later. Reading `/me`
  /// once on arrival reliably shows the *old* plan, which reads as "the payment
  /// did nothing" — the most alarming thing this page could say. Bounded, so a
  /// webhook that never arrives leaves a stale page rather than a busy loop.
  void _awaitNewPlan(String before) {
    _poll?.cancel();
    var attempts = 0;
    _poll = Timer.periodic(_pollInterval, (timer) async {
      attempts++;
      final next = _billing.status();
      if (!mounted) return timer.cancel();
      // Braces, not an arrow: an arrow would *return* the assigned Future, and
      // setState rejects a callback that returns one.
      setState(() {
        _status = next;
      });
      final settled =
          await next.then<BillingStatus?>((s) => s, onError: (_) => null);
      if (!mounted) return timer.cancel();
      if (attempts >= _maxPolls || (settled != null && settled.plan != before)) {
        timer.cancel();
      }
    });
  }

  Future<void> _refresh() async {
    final next = _billing.status();
    setState(() {
      _status = next;
    });
    await next.then<void>((_) {}, onError: (_) {});
  }

  Future<void> _launch(String url) async {
    final launch = widget.launcher ??
        (String u) async =>
            launchUrl(Uri.parse(u), mode: LaunchMode.externalApplication);
    await launch(url);
  }

  Future<void> _choose(PlanInfo plan, BillingStatus status) async {
    setState(() {
      _busyPlan = plan.id;
      _failure = null;
    });
    try {
      final url = await _billing.urlToReach(
          plan: plan.id, subscribed: status.isLive);
      if (url.isEmpty) throw Exception('empty provider url');
      await _launch(url);
    } catch (_) {
      if (mounted) {
        setState(() => _failure = status.isLive
            ? 'Could not start the plan change. Please try again.'
            : 'Could not start checkout. Please try again.');
      }
    } finally {
      if (mounted) setState(() => _busyPlan = null);
    }
  }

  Future<void> _openPortal() async {
    setState(() {
      _busyPortal = true;
      _failure = null;
    });
    try {
      final url = await _billing.portalUrl();
      if (url.isEmpty) throw Exception('empty portal url');
      await _launch(url);
    } catch (_) {
      if (mounted) {
        setState(() => _failure = 'Could not reach the billing service.');
      }
    } finally {
      if (mounted) setState(() => _busyPortal = false);
    }
  }

  /// The destructive-action colour of the design system, per brightness.
  static Color _accent(ThemeData theme) =>
      theme.brightness == Brightness.dark ? kAccentDark : kAccent;

  /// The server's words when it refused, else [fallback].
  static String _errorText(Object e, String fallback) =>
      e is ApiException ? apiErrorDetail(e.body) : fallback;

  /// Quote, confirm, withdraw (issue #441).
  Future<void> _withdraw() async {
    setState(() {
      _busyWithdraw = true;
      _failure = null;
    });
    final Withdrawal quote;
    try {
      quote = await _billing.withdrawalQuote();
    } catch (e) {
      if (mounted) {
        setState(() {
          _busyWithdraw = false;
          _failure = _errorText(e, 'Could not reach the billing service.');
        });
      }
      return;
    }
    if (!mounted) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Withdraw and get a refund?'),
        content: Text(withdrawalConfirmation(quote)),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Keep my plan'),
          ),
          TextButton(
            style: TextButton.styleFrom(foregroundColor: _accent(Theme.of(ctx))),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Withdraw'),
          ),
        ],
      ),
    );
    if (!mounted) return;
    if (confirmed != true) {
      setState(() => _busyWithdraw = false);
      return;
    }
    try {
      final done = await _billing.withdraw();
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(withdrawalDone(done))));
    } catch (e) {
      if (mounted) {
        setState(() =>
            _failure = _errorText(e, 'Could not withdraw. Please try again.'));
      }
    } finally {
      if (mounted) setState(() => _busyWithdraw = false);
    }
    // Either way the subscription may have changed: a refund can fail after
    // the cancellation went through.
    if (mounted) await _refresh();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        // The provider redirects back to this route as a *fresh* navigation, so
        // the router stack has nothing to pop and an unconditional pop() is a
        // silent no-op — the same dead arrow issue #192 fixed on Settings, which
        // this page inherits by being where checkout now returns to.
        leading: BackButton(
          onPressed: () => context.canPop() ? context.pop() : context.go('/'),
        ),
        title: const Text('Plan'),
      ),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: SingleChildScrollView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(20),
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 720),
              child: FutureBuilder<BillingStatus>(
                future: _status,
                builder: (context, snapshot) {
                  if (snapshot.connectionState != ConnectionState.done) {
                    return const Padding(
                      padding: EdgeInsets.all(48),
                      child: Center(child: CircularProgressIndicator()),
                    );
                  }
                  final status = snapshot.data;
                  if (status == null) return _unavailable(context);
                  if (!status.billingEnabled) return _selfHosted(context);
                  return _body(context, status);
                },
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _unavailable(BuildContext context) => Padding(
        padding: const EdgeInsets.all(24),
        child: Text('Plan details are unavailable right now.',
            style: Theme.of(context).textTheme.bodyMedium),
      );

  Widget _selfHosted(BuildContext context) {
    final theme = Theme.of(context);
    return _Card(
      title: 'Plan',
      icon: Icons.workspace_premium_outlined,
      child: Text(
        'This is a self-hosted instance. There are no plans and no limits — '
        'trips, photos and trip length are all unlimited.',
        style: theme.textTheme.bodyMedium,
      ),
    );
  }

  Widget _body(BuildContext context, BillingStatus status) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (widget.checkoutOutcome == 'success')
          _Banner(
            icon: Icons.check_circle_outline,
            text: 'Payment received. Your new plan appears here as soon as '
                'the payment provider confirms it.',
          ),
        if (widget.checkoutOutcome == 'cancelled')
          _Banner(
            icon: Icons.info_outline,
            text: 'Checkout was cancelled — nothing was charged.',
          ),
        _Card(
          title: 'Your plan',
          icon: Icons.workspace_premium_outlined,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
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
              if (!status.quotasEnforced) ...[
                const SizedBox(height: 6),
                Text(
                  'Limits are being measured but not enforced yet — nothing '
                  'is blocked.',
                  style: theme.textTheme.bodySmall,
                ),
              ],
              const SizedBox(height: 16),
              PlanUsageBars(status: status),
              if (_failure != null) ...[
                const SizedBox(height: 12),
                Text(_failure!,
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.error)),
              ],
              if (status.canManage) ...[
                const SizedBox(height: 20),
                Text('Invoices, payment method and cancellation.',
                    style: theme.textTheme.bodySmall),
                const SizedBox(height: 8),
                Align(
                  alignment: Alignment.centerLeft,
                  child: OutlinedButton.icon(
                    // The theme's minimumSize is Size.fromHeight(44), i.e.
                    // infinite width — see billing_section.dart.
                    style:
                        OutlinedButton.styleFrom(minimumSize: const Size(0, 40)),
                    onPressed: _busyPortal ? null : _openPortal,
                    icon: const Icon(Icons.receipt_long_outlined, size: 18),
                    label: const Text('Manage billing'),
                  ),
                ),
              ],
              if (status.withdrawalOpen) ...[
                const SizedBox(height: 20),
                Text(withdrawalNotice(status), style: theme.textTheme.bodySmall),
                const SizedBox(height: 8),
                Align(
                  alignment: Alignment.centerLeft,
                  child: OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(
                      minimumSize: const Size(0, 40),
                      foregroundColor: _accent(theme),
                      side: BorderSide(color: _accent(theme)),
                    ),
                    onPressed: _busyWithdraw ? null : _withdraw,
                    icon: const Icon(Icons.undo, size: 18),
                    label: const Text('Withdraw and get a refund'),
                  ),
                ),
              ],
            ],
          ),
        ),
        const SizedBox(height: 16),
        _Card(
          title: status.isLive ? 'Change plan' : 'Plans',
          icon: Icons.rocket_launch_outlined,
          child: FutureBuilder<List<PlanInfo>>(
            future: _plans,
            builder: (context, snapshot) {
              if (snapshot.connectionState != ConnectionState.done) {
                return const Padding(
                  padding: EdgeInsets.all(24),
                  child: Center(child: CircularProgressIndicator()),
                );
              }
              final plans = snapshot.data ?? const <PlanInfo>[];
              if (plans.isEmpty) {
                return Text('Plans are unavailable right now.',
                    style: theme.textTheme.bodyMedium);
              }
              return Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  for (final plan in plans)
                    PlanTile(
                      plan: plan,
                      isCurrent: plan.id == status.plan,
                      isRecommended: false,
                      busy: _busyPlan == plan.id,
                      // Free is not something you buy — but a subscriber can
                      // move down to it, which means ending the subscription.
                      action: plan.id == status.plan
                          ? null
                          : plan.isFree
                              ? (status.isLive ? 'Cancel subscription' : null)
                              : 'Choose ${plan.name}',
                      onChoose: () => _choose(plan, status),
                    ),
                  if (status.isLive)
                    Text(
                      'Moving up takes effect straight away and is charged for '
                      'the rest of the month. Moving down takes effect when '
                      'the month you have paid for ends.',
                      style: theme.textTheme.bodySmall,
                    ),
                ],
              );
            },
          ),
        ),
      ],
    );
  }
}

/// Same shape as the section cards in Settings, which this page sits under.
class _Card extends StatelessWidget {
  final String title;
  final IconData icon;
  final Widget child;

  const _Card({required this.title, required this.icon, required this.child});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, color: theme.colorScheme.primary, size: 20),
                const SizedBox(width: 8),
                Text(title, style: theme.textTheme.titleLarge),
              ],
            ),
            const Divider(height: 24),
            child,
          ],
        ),
      ),
    );
  }
}

class _Banner extends StatelessWidget {
  final IconData icon;
  final String text;

  const _Banner({required this.icon, required this.text});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: theme.colorScheme.secondaryContainer,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 20, color: theme.colorScheme.onSecondaryContainer),
          const SizedBox(width: 10),
          Expanded(
            child: Text(text,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSecondaryContainer)),
          ),
        ],
      ),
    );
  }
}
