/// Email-verification landing screen (issue #110).
///
/// Reached from the link in the verification email, signed in or not — so it
/// must render standalone and offer a route onward in both cases. Consumes the
/// token once on first build; on success it refreshes the profile so the
/// "verify your email" banner clears without a re-login.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../api/client.dart' show apiErrorMessage;
import 'auth_notifier.dart';

class VerifyEmailScreen extends StatefulWidget {
  final String token;

  const VerifyEmailScreen({super.key, required this.token});

  @override
  State<VerifyEmailScreen> createState() => _VerifyEmailScreenState();
}

class _VerifyEmailScreenState extends State<VerifyEmailScreen> {
  bool _busy = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    // Deferred to after the first frame: verifyEmail refreshes the profile,
    // which notifies listeners, and notifying during build throws.
    WidgetsBinding.instance.addPostFrameCallback((_) => _verify());
  }

  Future<void> _verify() async {
    try {
      await context.read<AuthNotifier>().verifyEmail(widget.token);
      if (mounted) setState(() => _busy = false);
    } catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = apiErrorMessage(e);
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isLoggedIn = context.watch<AuthNotifier>().user != null;

    return Scaffold(
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: Card(
            margin: const EdgeInsets.all(24),
            child: Padding(
              padding: const EdgeInsets.all(32),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (_busy) ...[
                    const CircularProgressIndicator(),
                    const SizedBox(height: 20),
                    Text('Confirming your email…',
                        style: theme.textTheme.bodyMedium),
                  ] else if (_error != null) ...[
                    Icon(Icons.error_outline,
                        size: 44, color: theme.colorScheme.error),
                    const SizedBox(height: 16),
                    Text('Could not confirm',
                        style: theme.textTheme.titleMedium),
                    const SizedBox(height: 8),
                    Text(_error!,
                        textAlign: TextAlign.center,
                        style: theme.textTheme.bodySmall),
                    const SizedBox(height: 20),
                    ElevatedButton(
                      onPressed: () =>
                          context.go(isLoggedIn ? '/projects' : '/login'),
                      child: Text(isLoggedIn ? 'Back to my trips' : 'Sign in'),
                    ),
                  ] else ...[
                    const Icon(Icons.mark_email_read_outlined,
                        size: 44, color: Color(0xFF22C55E)),
                    const SizedBox(height: 16),
                    Text('Email confirmed',
                        style: theme.textTheme.titleMedium),
                    const SizedBox(height: 8),
                    Text(
                      isLoggedIn
                          ? 'You can now send and receive trip invites.'
                          : 'Sign in to start planning your trips.',
                      textAlign: TextAlign.center,
                      style: theme.textTheme.bodySmall,
                    ),
                    const SizedBox(height: 20),
                    ElevatedButton(
                      onPressed: () =>
                          context.go(isLoggedIn ? '/projects' : '/login'),
                      child: Text(isLoggedIn ? 'Go to my trips' : 'Sign in'),
                    ),
                  ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
