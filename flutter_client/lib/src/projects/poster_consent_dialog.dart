/// Consent for printing an encrypted trip's memory text on a poster
/// (docs/E2EE_REMNANTS_PLAN.md, decision 11).
///
/// The server can't read encrypted memory text, so printing it needs the device
/// to send it decrypted for this one poster. Modelled on
/// `video_consent_dialog.dart`; the user may also generate the poster without
/// the memory text. Any other way out (the barrier, back) is a cancel.
library;

import 'package:flutter/material.dart';

enum PosterConsentChoice { cancel, withoutText, sendText }

class PosterConsentDialog extends StatelessWidget {
  /// How many memories carry text the server asked about.
  final int memoryCount;

  const PosterConsentDialog({super.key, required this.memoryCount});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final memories = memoryCount == 1 ? '1 memory' : '$memoryCount memories';
    return AlertDialog(
      icon: const Icon(Icons.lock_open_outlined),
      title: const Text('Send memory text?'),
      content: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 380),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'This trip is end-to-end encrypted, so our server can’t read its '
              'memories. To print them, this device sends the titles and notes '
              'of $memories to the server to print them on this poster.',
              style: theme.textTheme.bodyMedium,
            ),
            const SizedBox(height: 12),
            Text(
              'They are removed from the server when the poster job ends. '
              'Nothing else is decrypted, and the rest of your trip stays '
              'encrypted.',
              style: theme.textTheme.bodyMedium,
            ),
            const SizedBox(height: 12),
            Text(
              'Without memory text, the poster is made with the memories’ '
              'places and photos only.',
              style: theme.textTheme.bodySmall,
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () =>
              Navigator.of(context).pop(PosterConsentChoice.cancel),
          child: const Text('Cancel'),
        ),
        TextButton(
          onPressed: () =>
              Navigator.of(context).pop(PosterConsentChoice.withoutText),
          child: const Text('Without memory text'),
        ),
        FilledButton(
          onPressed: () =>
              Navigator.of(context).pop(PosterConsentChoice.sendText),
          child: const Text('Send and generate'),
        ),
      ],
    );
  }
}

/// Shows [PosterConsentDialog]; [PosterConsentChoice.cancel] unless the user
/// picked one of the other two.
Future<PosterConsentChoice> showPosterConsentDialog(BuildContext context,
    {required int memoryCount}) async {
  final choice = await showDialog<PosterConsentChoice>(
    context: context,
    builder: (_) => PosterConsentDialog(memoryCount: memoryCount),
  );
  return choice ?? PosterConsentChoice.cancel;
}
