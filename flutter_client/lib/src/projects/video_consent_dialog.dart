/// Consent for rendering an encrypted trip (docs/TRIP_VIDEO_PLAN.md, D2).
///
/// The server can't read an encrypted activity, so a video of it needs the
/// device to send the decrypted tracks for this one render. This dialog says
/// so plainly and returns true only on an explicit "Send and continue"; any
/// other way out (Cancel, the barrier, back) is a no and sends nothing.
library;

import 'package:flutter/material.dart';

class VideoConsentDialog extends StatelessWidget {
  /// How many encrypted activities the server asked for.
  final int activityCount;

  const VideoConsentDialog({super.key, required this.activityCount});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final tracks = activityCount == 1
        ? 'its encrypted activity'
        : 'its $activityCount encrypted activities';
    return AlertDialog(
      icon: const Icon(Icons.lock_open_outlined),
      title: const Text('Send decrypted tracks?'),
      content: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 380),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'This trip is end-to-end encrypted, so our server can’t draw '
              '$tracks. To include them, this device decrypts the tracks and '
              'sends them to the server for this one video.',
              style: theme.textTheme.bodyMedium,
            ),
            const SizedBox(height: 12),
            Text(
              'They are used only to render this video and are deleted as soon '
              'as it finishes or fails. Nothing else is decrypted, and the rest '
              'of your trip stays encrypted.',
              style: theme.textTheme.bodyMedium,
            ),
            const SizedBox(height: 12),
            Text(
              'If you decline, nothing is sent and no video is made.',
              style: theme.textTheme.bodySmall,
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text('Decline'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(true),
          child: const Text('Send and continue'),
        ),
      ],
    );
  }
}

/// Shows [VideoConsentDialog]; true only when the user agreed.
Future<bool> showVideoConsentDialog(BuildContext context,
    {required int activityCount}) async {
  final ok = await showDialog<bool>(
    context: context,
    builder: (_) => VideoConsentDialog(activityCount: activityCount),
  );
  return ok == true;
}
