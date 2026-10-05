/// "Your recovery key was never confirmed" prompt (Decision 16).
///
/// Shown on the projects screen while this session holds the key and the
/// server lists `recovery_key` as unconfirmed: the user never said they saved
/// it, so it is offered for replacement. Not dismissible — without a saved
/// recovery key, losing every trusted device loses the encrypted data.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import 'encryption_service.dart';
import 'replace_recovery_key_screen.dart' show kReplaceRecoveryKeyRoute;

class RecoveryKeyBanner extends StatelessWidget {
  final EncryptionService service;
  const RecoveryKeyBanner({super.key, required this.service});

  @override
  Widget build(BuildContext context) {
    // The service says when the answer may have changed (an unlock, a lock,
    // a confirm); the answer itself is always read from it.
    return StreamBuilder<void>(
      stream: service.changes,
      builder: (context, _) {
        if (!service.needsRecoveryKeyReplacement) {
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
              const Icon(Icons.key_outlined, color: amber, size: 18),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  'Your recovery key was never confirmed. Create a new one now.',
                  style: theme.textTheme.bodySmall,
                ),
              ),
              const SizedBox(width: 8),
              TextButton(
                onPressed: () => context.push(kReplaceRecoveryKeyRoute),
                child: const Text('Create'),
              ),
            ],
          ),
        );
      },
    );
  }
}
