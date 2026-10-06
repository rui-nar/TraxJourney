/// Enable-encryption flow (issue #26): generate the CMK and let the user pick a
/// security LEVEL, with honest, side-by-side tradeoffs.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/client.dart';
import '../core/design_tokens.dart';
import 'encryption_migration.dart';
import 'encryption_service.dart';

/// Pool of security questions offered for the Medium level. The user picks a few
/// (at least [kMinSecurityQuestions]) and answers them.
const List<String> kSecurityQuestions = [
  'What was the name of your first pet?',
  'What city were you born in?',
  'What was the name of your primary school?',
  "What is your mother's maiden name?",
  'What was the make and model of your first car?',
  'What is the name of the street you grew up on?',
  'What was your childhood nickname?',
  'What is the name of a teacher you remember?',
  'In what city did your parents meet?',
  'What is the title of your favourite film?',
];

/// Minimum number of questions the user must pick+answer for the Medium level.
const int kMinSecurityQuestions = 3;

/// The three offered levels. Low is admin-recoverable (NOT zero-knowledge) and
/// depends on server escrow + email infra not yet built — surfaced but disabled.
enum SecurityLevel { low, medium, high }

enum _HighMethod { passphrase, recoveryKey }

enum _Step { choose, showRecoveryKey, done }

class EnableEncryptionScreen extends StatefulWidget {
  final EncryptionService service;

  /// Runs after encryption is enabled to encrypt existing entries. Defaults to
  /// the real migration; injectable so tests don't hit the network.
  final Future<void> Function(EncryptionService service)? onEnabled;

  const EnableEncryptionScreen({
    super.key,
    required this.service,
    this.onEnabled,
  });

  @override
  State<EnableEncryptionScreen> createState() => _EnableEncryptionScreenState();
}

class _EnableEncryptionScreenState extends State<EnableEncryptionScreen> {
  _Step _step = _Step.choose;
  SecurityLevel _level = SecurityLevel.high; // strongest by default
  _HighMethod _highMethod = _HighMethod.passphrase;
  bool _busy = false;
  EnableResult? _result;

  final _passphrase = TextEditingController();
  // Selected question indices (into kSecurityQuestions), in the order added, each
  // with its answer controller.
  final List<int> _selectedQuestions = [];
  final Map<int, TextEditingController> _qnaAnswers = {};

  @override
  void dispose() {
    _passphrase.dispose();
    for (final c in _qnaAnswers.values) {
      c.dispose();
    }
    super.dispose();
  }

  void _addQuestion(int index) {
    setState(() {
      _selectedQuestions.add(index);
      _qnaAnswers[index] = TextEditingController();
    });
  }

  void _removeQuestion(int index) {
    setState(() {
      _selectedQuestions.remove(index);
      _qnaAnswers.remove(index)?.dispose();
    });
  }

  bool get _canEnable {
    switch (_level) {
      case SecurityLevel.low:
        return false; // backend not built yet
      case SecurityLevel.medium:
        return _selectedQuestions.length >= kMinSecurityQuestions &&
            _selectedQuestions.every((i) => _qnaAnswers[i]!.text.trim().isNotEmpty);
      case SecurityLevel.high:
        return _highMethod == _HighMethod.recoveryKey ||
            _passphrase.text.trim().isNotEmpty;
    }
  }

  RecoveryChoice _choice() {
    switch (_level) {
      case SecurityLevel.medium:
        return QnaChoice(
          _selectedQuestions.map((i) => kSecurityQuestions[i]).toList(),
          _selectedQuestions.map((i) => _qnaAnswers[i]!.text).toList(),
        );
      case SecurityLevel.high:
        return _highMethod == _HighMethod.passphrase
            ? PassphraseChoice(_passphrase.text)
            : const RecoveryKeyChoice();
      case SecurityLevel.low:
        throw StateError('Low tier is not available yet');
    }
  }

  Future<void> _enable() async {
    setState(() => _busy = true);
    try {
      final result = await widget.service.enable(_choice());
      // Encrypt any existing plaintext entries in the background (idempotent).
      final migrate = widget.onEnabled ??
          (s) async {
            await EncryptionMigration(api, s).run();
          };
      unawaited(migrate(widget.service).catchError((_) {}));
      if (!mounted) return;
      setState(() {
        _result = result;
        _step = result.recoverySecret != null && result.confirmation != null
            ? _Step.showRecoveryKey
            : _Step.done;
      });
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Couldn\'t enable encryption: $e')),
      );
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    // No leaving while the request is out (U5-R3-1): a screen popped mid-way
    // is not there to show the recovery key the server was just given.
    return PopScope(
      canPop: !_busy,
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Encrypt your data'),
          automaticallyImplyLeading: !_busy,
        ),
        body: SafeArea(
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 520),
              child: switch (_step) {
                _Step.choose => _buildChoose(context),
                _Step.showRecoveryKey => SaveRecoveryKeyView(
                    service: widget.service,
                    secret: _result!.recoverySecret!,
                    confirmation: _result!.confirmation!,
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

  Widget _buildChoose(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text(
            'When encryption is on, your memories, journal and trip names are '
            'locked with a key only you hold. Choose how much convenience you '
            'want to trade for privacy.',
            style: t.bodyMedium),
        const SizedBox(height: 20),
        Text('Choose a level', style: t.titleMedium),
        const SizedBox(height: 12),

        _levelCard(
          level: SecurityLevel.high,
          accent: kSuccess,
          title: 'High  ·  Strongest',
          subtitle: 'A passphrase or a generated recovery key only you hold. '
              'Not even an administrator can read your data.',
        ),
        const SizedBox(height: 10),
        _levelCard(
          level: SecurityLevel.medium,
          accent: kWarning,
          title: 'Medium  ·  Security questions',
          subtitle: 'Answer a few questions instead of holding a secret. '
              'Easier — but weaker.',
        ),
        const SizedBox(height: 10),
        _levelCard(
          level: SecurityLevel.low,
          accent: kAccent,
          title: 'Low  ·  Recoverable by email  (coming soon)',
          subtitle: 'Reset by email if you forget. Most convenient — but because '
              'we can recover it, an administrator could read it. Not yet available.',
          enabled: false,
        ),

        const SizedBox(height: 16),
        if (_level == SecurityLevel.high) _buildHighOptions(context),
        if (_level == SecurityLevel.medium) _buildMediumOptions(context),

        const SizedBox(height: 24),
        FilledButton(
          onPressed: _busy || !_canEnable ? null : _enable,
          child: _busy
              ? const SizedBox(
                  height: 18, width: 18,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('Turn on encryption'),
        ),
      ],
    );
  }

  Widget _buildHighOptions(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SegmentedButton<_HighMethod>(
          segments: const [
            ButtonSegment(value: _HighMethod.passphrase, label: Text('Passphrase')),
            ButtonSegment(value: _HighMethod.recoveryKey, label: Text('Recovery key')),
          ],
          selected: {_highMethod},
          onSelectionChanged: (s) => setState(() => _highMethod = s.first),
        ),
        const SizedBox(height: 12),
        if (_highMethod == _HighMethod.passphrase)
          TextField(
            controller: _passphrase,
            obscureText: true,
            onChanged: (_) => setState(() {}),
            decoration: const InputDecoration(
              labelText: 'Passphrase',
              helperText: 'Use a long, memorable phrase. Case matters.',
              border: OutlineInputBorder(),
            ),
          )
        else
          Text(
            'We\'ll generate a one-time recovery key for you to save on the next '
            'screen.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
      ],
    );
  }

  Widget _buildMediumOptions(BuildContext context) {
    final t = Theme.of(context).textTheme;
    final available = [
      for (var i = 0; i < kSecurityQuestions.length; i++)
        if (!_selectedQuestions.contains(i)) i
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _warningBanner(
          context,
          'Easier, but weaker. Security answers can be guessed, so someone with '
          'access to the server has a chance of recovering your data. High is '
          'much safer.',
        ),
        const SizedBox(height: 12),
        Text('Pick at least $kMinSecurityQuestions questions and answer them.',
            style: t.bodySmall),

        // Each chosen question: its text, answer field, and a remove button.
        for (final i in _selectedQuestions) ...[
          const SizedBox(height: 12),
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(kSecurityQuestions[i], style: t.bodySmall),
                    const SizedBox(height: 4),
                    TextField(
                      controller: _qnaAnswers[i],
                      onChanged: (_) => setState(() {}),
                      decoration: const InputDecoration(
                        isDense: true, border: OutlineInputBorder()),
                    ),
                  ],
                ),
              ),
              IconButton(
                tooltip: 'Remove',
                icon: const Icon(Icons.close),
                onPressed: () => _removeQuestion(i),
              ),
            ],
          ),
        ],

        // Add-a-question dropdown (hidden once all are chosen).
        if (available.isNotEmpty) ...[
          const SizedBox(height: 12),
          DropdownButtonFormField<int>(
            // Reset after each pick so the field never retains a value that has
            // just been removed from `items` (avoids the dropdown value assertion).
            key: ValueKey('add-question-${_selectedQuestions.length}'),
            isExpanded: true,
            decoration: const InputDecoration(
              labelText: 'Add a question',
              isDense: true,
              border: OutlineInputBorder(),
            ),
            items: [
              for (final i in available)
                DropdownMenuItem(value: i, child: Text(kSecurityQuestions[i])),
            ],
            onChanged: (i) {
              if (i != null) _addQuestion(i);
            },
          ),
        ],
      ],
    );
  }

  Widget _warningBanner(BuildContext context, String text) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: kWarning.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.warning_amber_rounded, color: kWarning, size: 20),
          const SizedBox(width: 8),
          Expanded(
              child: Text(text, style: Theme.of(context).textTheme.bodySmall)),
        ],
      ),
    );
  }

  Widget _levelCard({
    required SecurityLevel level,
    required Color accent,
    required String title,
    required String subtitle,
    bool enabled = true,
  }) {
    final selected = _level == level && enabled;
    return Opacity(
      opacity: enabled ? 1 : 0.55,
      child: InkWell(
        onTap: enabled ? () => setState(() => _level = level) : null,
        borderRadius: BorderRadius.circular(10),
        child: Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(10),
            border: Border.all(
              color: selected ? accent : Theme.of(context).dividerColor,
              width: selected ? 2 : 1,
            ),
            color: selected ? accent.withValues(alpha: 0.06) : null,
          ),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(selected ? Icons.check_circle : Icons.circle_outlined,
                  color: accent, size: 20),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(title, style: Theme.of(context).textTheme.titleSmall),
                    const SizedBox(height: 4),
                    Text(subtitle,
                        style: Theme.of(context).textTheme.bodySmall),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildDone(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.lock, color: kSuccess, size: 48),
          const SizedBox(height: 12),
          Text('Encryption is on',
              style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          const Text('Your data is now end-to-end encrypted.'),
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

/// Grouped uppercase hex. (A BIP39 word phrase is a possible future format.)
String formatRecoveryKey(List<int> bytes) {
  final hex = bytes
      .map((b) => b.toRadixString(16).padLeft(2, '0').toUpperCase())
      .join();
  final groups = <String>[];
  for (var i = 0; i < hex.length; i += 4) {
    groups.add(hex.substring(i, i + 4));
  }
  return groups.join('-');
}

/// Shows a new recovery key once and records on the server, at Done, that the
/// user saved it (Decision 16). Used by the setup screen and by the
/// replacement screen.
class SaveRecoveryKeyView extends StatefulWidget {
  final EncryptionService service;
  final Uint8List secret;
  final RecoveryKeyConfirmation confirmation;

  /// Runs once the server has recorded the confirmation.
  final VoidCallback onConfirmed;

  const SaveRecoveryKeyView({
    super.key,
    required this.service,
    required this.secret,
    required this.confirmation,
    required this.onConfirmed,
  });

  @override
  State<SaveRecoveryKeyView> createState() => _SaveRecoveryKeyViewState();
}

class _SaveRecoveryKeyViewState extends State<SaveRecoveryKeyView> {
  bool _saved = false;
  bool _confirming = false;
  String? _failure;
  bool _discard = false;

  late final String _keyText = formatRecoveryKey(widget.secret);

  Future<void> _done() async {
    setState(() {
      _confirming = true;
      _failure = null;
    });
    final outcome = await widget.service.confirmRecoveryKey(widget.confirmation);
    if (!mounted) return;
    setState(() {
      _confirming = false;
      switch (outcome) {
        case RecoveryConfirmOutcome.confirmed:
          break;
        case RecoveryConfirmOutcome.conflict:
          _discard = true;
        case RecoveryConfirmOutcome.failed:
          // Never suggest the key is unusable: it is the one on the server,
          // only the record that it was saved is missing.
          _failure = "Couldn't record that you saved it; you'll be asked again.";
      }
    });
    if (outcome == RecoveryConfirmOutcome.confirmed) widget.onConfirmed();
  }

  @override
  Widget build(BuildContext context) {
    if (_discard) return _buildDiscard(context);
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text('Save your recovery key',
            style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        const Text('This is shown once. Store it in a password manager or print '
            'it. Without it — and without a trusted device — your data cannot be '
            'recovered.'),
        const SizedBox(height: 16),
        SelectableText(
          _keyText,
          style: monoStyle(fontSize: 15, letterSpacing: 0.5),
        ),
        const SizedBox(height: 12),
        OutlinedButton.icon(
          onPressed: () {
            Clipboard.setData(ClipboardData(text: _keyText));
            ScaffoldMessenger.of(context).showSnackBar(
              const SnackBar(content: Text('Recovery key copied')),
            );
          },
          icon: const Icon(Icons.copy),
          label: const Text('Copy'),
        ),
        const SizedBox(height: 16),
        CheckboxListTile(
          contentPadding: EdgeInsets.zero,
          value: _saved,
          onChanged: (v) => setState(() => _saved = v ?? false),
          title: const Text("I've saved my recovery key somewhere safe"),
        ),
        if (_failure != null) ...[
          const SizedBox(height: 8),
          Text(_failure!,
              style: Theme.of(context)
                  .textTheme
                  .bodySmall
                  ?.copyWith(color: kWarning)),
        ],
        const SizedBox(height: 8),
        FilledButton(
          onPressed: _saved && !_confirming ? _done : null,
          child: _confirming
              ? const SizedBox(
                  height: 18, width: 18,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('Done'),
        ),
      ],
    );
  }

  /// The key shown is not the one on the server (409, U5b-R2-4): another
  /// device replaced it. No second chance is promised; the key is hidden.
  Widget _buildDiscard(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.key_off, color: Theme.of(context).colorScheme.error, size: 48),
          const SizedBox(height: 12),
          const Text('This key is no longer your recovery key. Discard it.',
              textAlign: TextAlign.center),
          const SizedBox(height: 20),
          FilledButton(
            onPressed: () => Navigator.of(context).maybePop(),
            child: const Text('Close'),
          ),
        ],
      ),
    );
  }
}
