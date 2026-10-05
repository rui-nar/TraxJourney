/// Says on the trip screen when the account is encrypted and this device
/// can't use the key (#506): locked, or waiting to be approved. Memory and
/// journal editors are disabled then, with the same message
/// ([EncryptionBlockedNote]).
///
/// The trip screen mounts [EncryptionLockedBanner] unconditionally; the
/// banner decides on its own whether it shows, from the service's state.
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart' show kWarning, kWarningDark;
import 'encryption.dart';
import 'encryption_service.dart';
import 'manage_devices_screen.dart';
import 'recover_screen.dart';

class EncryptionLockedBanner extends StatelessWidget {
  /// The app's [encryption] singleton unless a test supplies one.
  final EncryptionService? service;

  const EncryptionLockedBanner({super.key, this.service});

  @override
  Widget build(BuildContext context) {
    final svc = service ?? encryption;
    return ValueListenableBuilder<EncryptionState>(
      valueListenable: svc.state,
      builder: (context, state, _) {
        final message = switch (state) {
          EncryptionState.locked => kEncryptionLockedMessage,
          EncryptionState.awaitingApproval => kEncryptionAwaitingApprovalMessage,
          EncryptionState.disabled || EncryptionState.unlocked => null,
        };
        if (message == null) return const SizedBox.shrink();
        return MaterialBanner(
          padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
          content: Text(message),
          leading: Icon(Icons.lock_outline, size: 20, color: _warning(context)),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => ManageDevicesScreen(service: svc),
              )),
              child: const Text('Manage devices'),
            ),
            TextButton(
              onPressed: () => Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => RecoverScreen(service: svc),
              )),
              child: const Text('Recover access'),
            ),
          ],
        );
      },
    );
  }
}

/// The editors' version of the banner: why Save is disabled.
class EncryptionBlockedNote extends StatelessWidget {
  final String message;

  const EncryptionBlockedNote({super.key, required this.message});

  @override
  Widget build(BuildContext context) {
    final color = _warning(context);
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(Icons.lock_outline, size: 18, color: color),
        const SizedBox(width: 8),
        Expanded(
          child: Text(message, style: Theme.of(context).textTheme.bodySmall),
        ),
      ],
    );
  }
}

Color _warning(BuildContext context) =>
    Theme.of(context).brightness == Brightness.dark ? kWarningDark : kWarning;
