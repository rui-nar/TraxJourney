import 'package:flutter/material.dart';

import '../core/design_tokens.dart';
import 'projects_notifier.dart';

/// Asks what to do with an imported trip whose name the user already has
/// (issue #452): keep both, replace the existing trip, or cancel (null).
///
/// Replace is destructive, so the dialog says what it overwrites, what it
/// deletes and what it keeps before the user can pick it.
Future<ImportConflictChoice?> showImportConflictDialog(
    BuildContext context, String name) {
  return showDialog<ImportConflictChoice>(
    context: context,
    builder: (ctx) {
      final theme = Theme.of(ctx);
      final dark = theme.brightness == Brightness.dark;
      final warning = dark ? kWarningDark : kWarning;
      final destructive = dark ? kAccentDark : kAccent;
      return AlertDialog(
        title: Text('You already have a trip called “$name”'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Keep both imports the file as a new trip, '
              '“$name (2)” or the next free number.',
              style: theme.textTheme.bodyMedium,
            ),
            const SizedBox(height: 16),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(Icons.warning_amber_rounded, color: warning, size: 20),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'Replace overwrites that trip’s timeline, memories and your '
                    'journal with the file’s. Memories the file doesn’t have '
                    'are deleted with their photos. Its share links and '
                    'companions are kept.',
                    style: theme.textTheme.bodyMedium,
                  ),
                ),
              ],
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancel'),
          ),
          TextButton(
            style: TextButton.styleFrom(foregroundColor: destructive),
            onPressed: () => Navigator.of(ctx).pop(ImportConflictChoice.replace),
            child: const Text('Replace'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.of(ctx).pop(ImportConflictChoice.keepBoth),
            child: const Text('Keep both'),
          ),
        ],
      );
    },
  );
}
