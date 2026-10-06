/// Replace a recovery key that was never confirmed (Decision 16): the key from
/// setup may never have been shown, or the user left before saying they saved
/// it. The server cannot show it again, so a new one is made, shown once and
/// confirmed like at setup.
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';
import 'enable_encryption_screen.dart' show SaveRecoveryKeyView;
import 'encryption_service.dart';

/// Where the projects-screen banner opens [ReplaceRecoveryKeyScreen].
const kReplaceRecoveryKeyRoute = '/recovery-key/new';

enum _Step { intro, showKey, done }

class ReplaceRecoveryKeyScreen extends StatefulWidget {
  final EncryptionService service;
  const ReplaceRecoveryKeyScreen({super.key, required this.service});

  @override
  State<ReplaceRecoveryKeyScreen> createState() =>
      _ReplaceRecoveryKeyScreenState();
}

class _ReplaceRecoveryKeyScreenState extends State<ReplaceRecoveryKeyScreen> {
  _Step _step = _Step.intro;
  bool _busy = false;
  String? _error;
  NewRecoveryKey? _key;

  Future<void> _create() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final key = await widget.service.replaceRecoveryKey();
      if (!mounted) return;
      setState(() {
        _key = key;
        _step = _Step.showKey;
      });
    } on RecoveryKeyConflict {
      if (mounted) {
        setState(() => _error =
            'Your recovery key has already been confirmed. Nothing was changed.');
      }
    } on EncryptionSessionEnded {
      if (mounted) {
        setState(() => _error = 'Your session ended. Nothing was changed.');
      }
    } catch (e) {
      if (mounted) {
        setState(() => _error = "Couldn't create a new recovery key: $e");
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    // No leaving while the request is out: a screen popped mid-way is not
    // there to show the key the server was just given.
    return PopScope(
      canPop: !_busy,
      child: Scaffold(
        appBar: AppBar(
          title: const Text('New recovery key'),
          automaticallyImplyLeading: !_busy,
        ),
        body: SafeArea(
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 520),
              child: switch (_step) {
                _Step.intro => _buildIntro(context),
                _Step.showKey => SaveRecoveryKeyView(
                    service: widget.service,
                    secret: _key!.secret,
                    confirmation: _key!.confirmation,
                    onConfirmed: () => setState(() => _step = _Step.done),
                  ),
                _Step.done => _buildDone(context),
              },
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildIntro(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text('Your recovery key was never confirmed',
            style: t.titleMedium),
        const SizedBox(height: 8),
        const Text(
            'Without a recovery key, losing all your trusted devices means '
            'losing your encrypted data. Create a new key now and save it; '
            'the unconfirmed one stops working.'),
        if (_error != null) ...[
          const SizedBox(height: 12),
          Text(_error!,
              style: t.bodySmall?.copyWith(
                  color: Theme.of(context).colorScheme.error)),
        ],
        const SizedBox(height: 24),
        FilledButton(
          onPressed: _busy ? null : _create,
          child: _busy
              ? const SizedBox(
                  height: 18, width: 18,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('Create a new recovery key'),
        ),
      ],
    );
  }

  Widget _buildDone(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.verified_user, color: kSuccess, size: 48),
          const SizedBox(height: 12),
          Text('Recovery key saved',
              style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 20),
          FilledButton(
            onPressed: () => Navigator.of(context).maybePop(true),
            child: const Text('Close'),
          ),
        ],
      ),
    );
  }
}
