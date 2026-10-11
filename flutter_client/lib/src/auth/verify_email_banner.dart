/// "Verify your email" prompt (issue #110).
///
/// Shown on the projects screen while the signed-in account's address is
/// unconfirmed. The gate is soft — the app works fine unverified — so this is
/// informational, dismissible, and never blocks anything.
///
/// Hidden entirely for accounts with no address at all (the seeded admin):
/// prompting someone to verify an address they don't have is nonsense.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../api/client.dart' show apiErrorMessage;
import 'auth_notifier.dart';

class VerifyEmailBanner extends StatefulWidget {
  const VerifyEmailBanner({super.key});

  /// Dismissal is session-scoped: this resets when the app reloads. Persisting
  /// it would mean storage plumbing for a prompt the user clears for good the
  /// moment they actually verify.
  static bool dismissed = false;

  @override
  State<VerifyEmailBanner> createState() => _VerifyEmailBannerState();
}

class _VerifyEmailBannerState extends State<VerifyEmailBanner> {
  bool _sending = false;
  String? _message;

  Future<void> _resend() async {
    setState(() {
      _sending = true;
      _message = null;
    });
    try {
      await context.read<AuthNotifier>().resendVerification();
      if (mounted) {
        setState(() => _message = 'Sent — check your inbox.');
      }
    } catch (e) {
      if (mounted) setState(() => _message = apiErrorMessage(e));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final user = context.watch<AuthNotifier>().user;
    if (user == null ||
        user.emailVerified ||
        user.email.isEmpty ||
        VerifyEmailBanner.dismissed) {
      return const SizedBox.shrink();
    }

    final theme = Theme.of(context);
    const amber = Color(0xFFF59E0B);

    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: amber.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: amber),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.mark_email_unread_outlined,
              color: amber, size: 18),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Confirm ${user.email} to send and receive trip invites.',
                  style: theme.textTheme.bodySmall,
                ),
                if (_message != null) ...[
                  const SizedBox(height: 4),
                  Text(_message!,
                      style: theme.textTheme.bodySmall
                          ?.copyWith(color: amber)),
                ],
              ],
            ),
          ),
          const SizedBox(width: 8),
          if (_sending)
            const SizedBox(
                width: 16, height: 16,
                child: CircularProgressIndicator(strokeWidth: 2))
          else
            TextButton(
              onPressed: _resend,
              child: const Text('Resend'),
            ),
          IconButton(
            tooltip: 'Dismiss',
            icon: const Icon(Icons.close, size: 16),
            onPressed: () => setState(() {
              VerifyEmailBanner.dismissed = true;
            }),
          ),
        ],
      ),
    );
  }
}
