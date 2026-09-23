/// Login screen — email/password + Google Sign-In, link to register.
library;

import 'dart:async' show StreamSubscription;

import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:google_sign_in/google_sign_in.dart';
import 'package:provider/provider.dart';

// Web-only import: renderButton() from google_sign_in_web.
// Stubbed on non-web platforms via conditional import.
import 'google_button_stub.dart'
    if (dart.library.html) 'google_button_web.dart';

import '../core/app_version.dart';
import '../core/brand.dart';
import '../core/platform.dart';
import '../core/return_to.dart';
import '../core/server_config.dart';
import 'auth_notifier.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _emailCtrl = TextEditingController();
  final _passwordCtrl = TextEditingController();
  final _formKey = GlobalKey<FormState>();
  bool _obscurePassword = true;
  StreamSubscription<GoogleSignInAuthenticationEvent>? _googleSignInSub;

  @override
  void initState() {
    super.initState();
    // GoogleSignIn.instance.initialize() is called once in main() before runApp.
    _googleSignInSub = GoogleSignIn.instance.authenticationEvents.listen(
      _handleAuthEvent,
      onError: _handleAuthError,
    );
    // Best-effort: if the platform isn't ready (initialize() never ran —
    // e.g. a widget test that doesn't run main(), or a misconfigured client
    // ID on a real device), this can fail either as a synchronous throw or
    // as a rejected Future depending on how the platform call is wrapped —
    // cover both, since the user still has the manual email/password form
    // either way.
    try {
      GoogleSignIn.instance.attemptLightweightAuthentication()?.catchError((_) => null);
    } catch (_) {}
  }

  @override
  void dispose() {
    _emailCtrl.dispose();
    _passwordCtrl.dispose();
    _googleSignInSub?.cancel();
    super.dispose();
  }

  // ── Handlers ──────────────────────────────────────────────────────────────────

  void _handleAuthEvent(GoogleSignInAuthenticationEvent event) {
    if (event is GoogleSignInAuthenticationEventSignIn) {
      _handleGoogleAccount(event.user);
    }
  }

  void _handleAuthError(Object error) {
    if (error is GoogleSignInException &&
        error.code == GoogleSignInExceptionCode.canceled) {
      return;
    }
    _showError('Google sign-in failed: $error');
  }

  String? get _returnTo =>
      GoRouterState.of(context).uri.queryParameters['return_to'];

  void _navigateAfterLogin() {
    // Same relative-path-only guard as the router redirect (issue #111).
    context.go(safeReturnTo(_returnTo) ?? '/projects');
  }

  Future<void> _submit() async {
    if (!(_formKey.currentState?.validate() ?? false)) return;
    final auth = context.read<AuthNotifier>();
    await auth.loginWithPassword(
      _emailCtrl.text.trim(),
      _passwordCtrl.text,
    );
    if (!mounted) return;
    if (auth.user != null) _navigateAfterLogin();
  }

  /// Called when Google Sign-In produces an account — works for both
  /// renderButton (web) and authenticate() (Android/iOS).
  Future<void> _handleGoogleAccount(GoogleSignInAccount account) async {
    try {
      // authentication is a synchronous getter in v7 (no await needed)
      final idToken = account.authentication.idToken;
      if (idToken == null || idToken.isEmpty) {
        _showError('Google sign-in: no ID token received.');
        return;
      }
      if (!mounted) return;
      final auth = context.read<AuthNotifier>();
      await auth.loginWithGoogle(idToken);
      if (!mounted) return;
      if (auth.user != null) _navigateAfterLogin();
    } catch (e) {
      _showError('Google sign-in failed: $e');
    }
  }

  /// Mobile-only: trigger the native Google account picker.
  Future<void> _mobileGoogleSignIn() async {
    if (!GoogleSignIn.instance.supportsAuthenticate()) return;
    try {
      await GoogleSignIn.instance.authenticate(
        scopeHint: ['email', 'profile', 'openid'],
      );
      // Result delivered via authenticationEvents stream → _handleAuthEvent
    } on GoogleSignInException catch (e) {
      if (e.code == GoogleSignInExceptionCode.canceled) return;
      _showError('Google sign-in failed: $e');
    }
  }

  void _showError(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(msg)));
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
                      Text('Sign in to continue',
                          style: theme.textTheme.bodySmall),
                      const SizedBox(height: 28),

                      // ── Error banner ──────────────────────────────────────
                      Consumer<AuthNotifier>(
                        builder: (_, auth, __) => auth.error == null
                            ? const SizedBox.shrink()
                            : _ErrorBanner(
                                message: auth.error!,
                                onDismiss: auth.clearError,
                              ),
                      ),

                      // ── Form ──────────────────────────────────────────────
                      Form(
                        key: _formKey,
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            TextFormField(
                              controller: _emailCtrl,
                              decoration: const InputDecoration(
                                labelText: 'Email or username',
                                prefixIcon: Icon(Icons.person_outline),
                              ),
                              autocorrect: false,
                              textInputAction: TextInputAction.next,
                              validator: (v) =>
                                  (v == null || v.trim().isEmpty)
                                      ? 'Required'
                                      : null,
                            ),
                            const SizedBox(height: 16),
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
                              textInputAction: TextInputAction.done,
                              onFieldSubmitted: (_) => _submit(),
                              validator: (v) =>
                                  (v == null || v.isEmpty)
                                      ? 'Required'
                                      : null,
                            ),
                            const SizedBox(height: 24),
                            Consumer<AuthNotifier>(
                              builder: (_, auth, __) => auth.isLoading
                                  ? const SizedBox(
                                      height: 44,
                                      child: Center(
                                          child: CircularProgressIndicator()),
                                    )
                                  : ElevatedButton(
                                      onPressed: _submit,
                                      child: const Text('Sign In'),
                                    ),
                            ),
                          ],
                        ),
                      ),

                      const SizedBox(height: 16),
                      Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Flexible(
                            child: Text("Don't have an account?",
                                style: theme.textTheme.bodySmall),
                          ),
                          TextButton(
                            onPressed: () {
                              final ret = _returnTo;
                              if (ret != null && ret.isNotEmpty) {
                                context.go('/register?return_to=${Uri.encodeComponent(ret)}');
                              } else {
                                context.go('/register');
                              }
                            },
                            child: const Text('Register'),
                          ),
                        ],
                      ),

                      const Divider(height: 32),

                      // ── Google Sign-In ────────────────────────────────────
                      // Web:    Google's own renderButton() (GIS — correct flow)
                      // Mobile: our OutlinedButton calling signIn()
                      kIsWeb
                          ? buildGoogleSignInButton()
                          : Consumer<AuthNotifier>(
                              builder: (_, auth, __) => OutlinedButton.icon(
                                onPressed: auth.isLoading
                                    ? null
                                    : _mobileGoogleSignIn,
                                icon: Image.network(
                                  'https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg',
                                  width: 18,
                                  height: 18,
                                  errorBuilder: (_, __, ___) => const Icon(
                                      Icons.account_circle_outlined,
                                      size: 20),
                                ),
                                label:
                                    const Text('Continue with Google'),
                              ),
                            ),

                      if (isNativeMobile) ...[
                        const SizedBox(height: 8),
                        const SelfHostingLink(),
                      ],
                      const SizedBox(height: 24),
                      VersionText(
                        prefix: '© ${DateTime.now().year} $kAppName · ',
                        style: theme.textTheme.bodySmall?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant,
                        ),
                        textAlign: TextAlign.center,
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

// ── Self-hosting link / current server indicator ─────────────────────────────

/// Shows the currently configured self-hosted server URL, so the user can
/// see which server they're about to sign in to, or — when none is set — a
/// link to configure one. Kept as its own widget (rather than inline in
/// LoginScreen) so it can be pumped in tests without LoginScreen's
/// GoogleSignIn initState wiring (see login_screen_test.dart).
class SelfHostingLink extends StatefulWidget {
  const SelfHostingLink({super.key});

  @override
  State<SelfHostingLink> createState() => SelfHostingLinkState();
}

class SelfHostingLinkState extends State<SelfHostingLink> {
  String? _url;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final url = await readCustomServerUrl();
    if (mounted) setState(() => _url = url);
  }

  Future<void> _openDialog() async {
    final saved = await showDialog<bool>(
      context: context,
      builder: (_) => const ServerConfigDialog(),
    );
    if (saved != true) return;
    await _load();
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('Server updated — restart the app to apply it.'),
      ));
    }
  }

  @override
  Widget build(BuildContext context) {
    final url = _url;
    if (url == null) {
      return TextButton(
        onPressed: _openDialog,
        child: const Text('Self-hosting? Click here'),
      );
    }
    final theme = Theme.of(context);
    return InkWell(
      onTap: _openDialog,
      borderRadius: BorderRadius.circular(8),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.dns_outlined,
                size: 16, color: theme.colorScheme.primary),
            const SizedBox(width: 6),
            Flexible(
              child: Text(
                'Signing in to $url',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.primary),
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Self-hosting server override dialog ──────────────────────────────────────

/// Lets the user point the app at a self-hosted server instead of the
/// built-in default. Pops `true` if a change was saved, so the caller can
/// tell the user a restart is needed.
class ServerConfigDialog extends StatefulWidget {
  const ServerConfigDialog({super.key});

  @override
  State<ServerConfigDialog> createState() => ServerConfigDialogState();
}

class ServerConfigDialogState extends State<ServerConfigDialog> {
  final _urlCtrl = TextEditingController();
  String? _error;
  bool _loaded = false;

  @override
  void initState() {
    super.initState();
    readCustomServerUrl().then((url) {
      if (!mounted) return;
      setState(() {
        _urlCtrl.text = url ?? '';
        _loaded = true;
      });
    });
  }

  @override
  void dispose() {
    _urlCtrl.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final url = _urlCtrl.text.trim();
    if (url.isNotEmpty && !isValidServerUrl(url)) {
      setState(() => _error = 'Enter a full URL, e.g. https://trax.example.com');
      return;
    }
    await writeCustomServerUrl(url);
    if (mounted) Navigator.of(context).pop(true);
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Self-hosted server'),
      content: !_loaded
          ? const SizedBox(
              height: 48,
              child: Center(child: CircularProgressIndicator()),
            )
          : Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'Point this app at your own $kAppName server instead of '
                  'the default one. Leave blank to use the default.',
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: _urlCtrl,
                  decoration: InputDecoration(
                    labelText: 'Server URL',
                    hintText: 'https://trax.example.com',
                    errorText: _error,
                  ),
                  keyboardType: TextInputType.url,
                  autocorrect: false,
                  onChanged: (_) {
                    if (_error != null) setState(() => _error = null);
                  },
                ),
              ],
            ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text('Cancel'),
        ),
        ElevatedButton(
          onPressed: _loaded ? _save : null,
          child: const Text('Save'),
        ),
      ],
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
              child:
                  Text(message, style: TextStyle(color: color, fontSize: 13))),
          GestureDetector(
              onTap: onDismiss,
              child: Icon(Icons.close, color: color, size: 16)),
        ],
      ),
    );
  }
}
