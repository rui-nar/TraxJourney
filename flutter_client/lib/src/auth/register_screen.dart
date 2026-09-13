/// Register screen — email, first/last name, password (issue #110).
///
/// The email is the login identifier as well as the address the account is
/// reachable at, so it is validated here and again server-side; first and
/// last name are combined into the display name by the server.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../core/brand.dart';
import '../core/return_to.dart';
import 'auth_notifier.dart';

class RegisterScreen extends StatefulWidget {
  const RegisterScreen({super.key});

  @override
  State<RegisterScreen> createState() => _RegisterScreenState();
}

class _RegisterScreenState extends State<RegisterScreen> {
  final _emailCtrl = TextEditingController();
  final _firstNameCtrl = TextEditingController();
  final _lastNameCtrl = TextEditingController();
  final _passwordCtrl = TextEditingController();
  final _confirmCtrl = TextEditingController();
  final _formKey = GlobalKey<FormState>();
  bool _obscurePassword = true;
  bool _obscureConfirm = true;

  @override
  void dispose() {
    _emailCtrl.dispose();
    _firstNameCtrl.dispose();
    _lastNameCtrl.dispose();
    _passwordCtrl.dispose();
    _confirmCtrl.dispose();
    super.dispose();
  }

  // ── Handler ───────────────────────────────────────────────────────────────────

  String? get _returnTo =>
      GoRouterState.of(context).uri.queryParameters['return_to'];

  void _navigateAfterRegister() {
    // Same relative-path-only guard as the router redirect (issue #111).
    context.go(safeReturnTo(_returnTo) ?? '/projects');
  }

  Future<void> _submit() async {
    if (!(_formKey.currentState?.validate() ?? false)) return;
    final auth = context.read<AuthNotifier>();

    await auth.register(
      _emailCtrl.text.trim(),
      _passwordCtrl.text,
      firstName: _firstNameCtrl.text.trim(),
      lastName: _lastNameCtrl.text.trim(),
    );

    if (!mounted) return;
    if (auth.user != null) _navigateAfterRegister();
  }

  // ── Build ─────────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isDark = theme.brightness == Brightness.dark;

    return Scaffold(
      body: Container(
        decoration: BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: isDark
                ? const [Color(0xFF0D1B2A), Color(0xFF1B2838)]
                : const [Color(0xFFF1F5F9), Color(0xFFE2E8F0)],
          ),
        ),
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Card(
                child: Padding(
                  padding: const EdgeInsets.all(32),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      // ── Brand header ──────────────────────────────────────
                      Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Icon(Icons.map_rounded,
                              color: theme.colorScheme.primary, size: 32),
                          const SizedBox(width: 10),
                          // Scales down rather than overflowing when the
                          // card is narrower than the wordmark.
                          Flexible(
                            child: FittedBox(
                              fit: BoxFit.scaleDown,
                              child: Text(kAppName,
                                  style: theme.textTheme.headlineMedium),
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 6),
                      Text('Create your account',
                          style: theme.textTheme.bodySmall),
                      const SizedBox(height: 28),

                      // ── Error banner ──────────────────────────────────────
                      Consumer<AuthNotifier>(
                        builder: (_, auth, __) {
                          if (auth.error == null) return const SizedBox.shrink();
                          return _ErrorBanner(
                            message: auth.error!,
                            onDismiss: auth.clearError,
                          );
                        },
                      ),

                      // ── Form ──────────────────────────────────────────────
                      Form(
                        key: _formKey,
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            // Email — the login identifier and the address
                            // invites are matched against (issue #110).
                            TextFormField(
                              controller: _emailCtrl,
                              decoration: const InputDecoration(
                                labelText: 'Email',
                                prefixIcon: Icon(Icons.email_outlined),
                              ),
                              keyboardType: TextInputType.emailAddress,
                              autocorrect: false,
                              textInputAction: TextInputAction.next,
                              validator: (v) {
                                if (v == null || v.trim().isEmpty) {
                                  return 'Email is required';
                                }
                                final ok = RegExp(
                                        r'^[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}$')
                                    .hasMatch(v.trim());
                                return ok ? null : 'Enter a valid email';
                              },
                            ),
                            const SizedBox(height: 16),

                            // First / last name — combined into the display
                            // name server-side.
                            TextFormField(
                              controller: _firstNameCtrl,
                              decoration: const InputDecoration(
                                labelText: 'First name',
                                prefixIcon: Icon(Icons.badge_outlined),
                              ),
                              textInputAction: TextInputAction.next,
                              validator: (v) =>
                                  (v == null || v.trim().isEmpty)
                                      ? 'First name is required'
                                      : null,
                            ),
                            const SizedBox(height: 16),

                            TextFormField(
                              controller: _lastNameCtrl,
                              decoration: const InputDecoration(
                                labelText: 'Last name',
                                prefixIcon: Icon(Icons.badge_outlined),
                              ),
                              textInputAction: TextInputAction.next,
                              validator: (v) =>
                                  (v == null || v.trim().isEmpty)
                                      ? 'Last name is required'
                                      : null,
                            ),
                            const SizedBox(height: 16),

                            // Password
                            TextFormField(
                              controller: _passwordCtrl,
                              decoration: InputDecoration(
                                labelText: 'Password',
                                prefixIcon: const Icon(Icons.lock_outline),
                                suffixIcon: IconButton(
                                  icon: Icon(_obscurePassword
                                      ? Icons.visibility_outlined
                                      : Icons.visibility_off_outlined),
                                  onPressed: () => setState(() =>
                                      _obscurePassword = !_obscurePassword),
                                ),
                              ),
                              obscureText: _obscurePassword,
                              textInputAction: TextInputAction.next,
                              validator: (v) {
                                if (v == null || v.isEmpty) {
                                  return 'Password is required';
                                }
                                if (v.length < 8) {
                                  return 'Minimum 8 characters';
                                }
                                return null;
                              },
                            ),
                            const SizedBox(height: 16),

                            // Confirm password
                            TextFormField(
                              controller: _confirmCtrl,
                              decoration: InputDecoration(
                                labelText: 'Confirm password',
                                prefixIcon: const Icon(Icons.lock_outline),
                                suffixIcon: IconButton(
                                  icon: Icon(_obscureConfirm
                                      ? Icons.visibility_outlined
                                      : Icons.visibility_off_outlined),
                                  onPressed: () => setState(() =>
                                      _obscureConfirm = !_obscureConfirm),
                                ),
                              ),
                              obscureText: _obscureConfirm,
                              textInputAction: TextInputAction.done,
                              onFieldSubmitted: (_) => _submit(),
                              validator: (v) {
                                if (v != _passwordCtrl.text) {
                                  return 'Passwords do not match';
                                }
                                return null;
                              },
                            ),
                            const SizedBox(height: 24),

                            // Submit button
                            Consumer<AuthNotifier>(
                              builder: (_, auth, __) {
                                if (auth.isLoading) {
                                  return const SizedBox(
                                    height: 44,
                                    child: Center(
                                        child: CircularProgressIndicator()),
                                  );
                                }
                                return ElevatedButton(
                                  onPressed: _submit,
                                  child: const Text('Create Account'),
                                );
                              },
                            ),
                          ],
                        ),
                      ),

                      const SizedBox(height: 16),

                      // ── Link to login ─────────────────────────────────────
                      Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Text('Already have an account?',
                              style: theme.textTheme.bodySmall),
                          TextButton(
                            onPressed: () {
                              final ret = _returnTo;
                              if (ret != null && ret.isNotEmpty) {
                                context.go('/login?return_to=${Uri.encodeComponent(ret)}');
                              } else {
                                context.go('/login');
                              }
                            },
                            child: const Text('Sign In'),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

// ── Error banner ───────────────────────────────────────────────────────────────

class _ErrorBanner extends StatelessWidget {
  final String message;
  final VoidCallback onDismiss;

  const _ErrorBanner({required this.message, required this.onDismiss});

  @override
  Widget build(BuildContext context) {
    final color = Theme.of(context).colorScheme.error;
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: color),
      ),
      child: Row(
        children: [
          Icon(Icons.error_outline, color: color, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: Text(message,
                style: TextStyle(color: color, fontSize: 13)),
          ),
          GestureDetector(
            onTap: onDismiss,
            child: Icon(Icons.close, color: color, size: 16),
          ),
        ],
      ),
    );
  }
}
