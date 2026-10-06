/// Says on the trip screen when the account is encrypted and this device
/// can't use the key (#506): locked, or waiting to be approved. Memory and
/// journal editors are disabled then, with the same message
/// ([EncryptionBlockedNote]).
///
/// On an unlocked device it instead says how many of the trip's activities
/// could not be encrypted (E2EE remnants decisions 6, 14 and 15): rows
/// imported by another traveller, which the server will not let this account
/// encrypt, and rows another traveller's trip also holds, which stay readable
/// to that trip.
///
/// The trip screen mounts [EncryptionLockedBanner] unconditionally; the
/// banner decides on its own whether it shows, from the service's state.
library;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/design_tokens.dart' show kWarning, kWarningDark;
import '../projects/project_notifier.dart';
import 'encryption.dart';
import 'encryption_service.dart';
import 'manage_devices_screen.dart';
import 'recover_screen.dart';

class EncryptionLockedBanner extends StatelessWidget {
  /// The app's [encryption] singleton unless a test supplies one.
  final EncryptionService? service;

  /// The open trip's count of activities that stay unencrypted: the
  /// [ProjectNotifier] provided above unless a test supplies one.
  final ValueListenable<int>? unencryptableActivityCount;

  const EncryptionLockedBanner(
      {super.key, this.service, this.unencryptableActivityCount});

  @override
  Widget build(BuildContext context) {
    final svc = service ?? encryption;
    final count = unencryptableActivityCount ??
        Provider.of<ProjectNotifier?>(context, listen: false)
            ?.unencryptableActivityCount;
    return ValueListenableBuilder<EncryptionState>(
      valueListenable: svc.state,
      builder: (context, state, _) {
        final message = switch (state) {
          EncryptionState.locked => kEncryptionLockedMessage,
          EncryptionState.awaitingApproval => kEncryptionAwaitingApprovalMessage,
          EncryptionState.disabled || EncryptionState.unlocked => null,
        };
        if (state == EncryptionState.unlocked && count != null) {
          return _UnencryptableNotice(count: count);
        }
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

/// "N activities are also used by another traveller and stay unencrypted",
/// while N > 0: one wording for both reasons, a ride they imported or one in
/// their trip.
class _UnencryptableNotice extends StatelessWidget {
  final ValueListenable<int> count;

  const _UnencryptableNotice({required this.count});

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<int>(
      valueListenable: count,
      builder: (context, n, _) {
        if (n <= 0) return const SizedBox.shrink();
        return MaterialBanner(
          padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
          content: Text(n == 1
              ? '1 activity is also used by another traveller and stays '
                  'unencrypted.'
              : '$n activities are also used by another traveller and stay '
                  'unencrypted.'),
          leading: Icon(Icons.lock_open, size: 20, color: _warning(context)),
          // A MaterialBanner needs an action; there is nothing to do here.
          actions: const [SizedBox.shrink()],
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
