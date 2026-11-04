/// Pieces of the "Upgrade photos from Immich" screen (issue #33): the row,
/// the chips, the bar, the sheets. All stateless about the review itself; the
/// notifier owns that.
library;

import 'package:flutter/material.dart';

import '../billing/billing_service.dart' show formatBytes;
import '../core/design_tokens.dart';
import 'album_upgrade_service.dart';

/// A network image that falls back to a photo icon, with the API's auth
/// headers (the photo routes are not public).
class UpgradeImage extends StatelessWidget {
  final String url;
  final Map<String, String> headers;
  final double size;
  final BoxFit fit;

  const UpgradeImage({
    super.key,
    required this.url,
    required this.headers,
    this.size = 64,
    this.fit = BoxFit.cover,
  });

  @override
  Widget build(BuildContext context) {
    final placeholder = Container(
      width: size,
      height: size,
      color: Theme.of(context).colorScheme.surfaceContainerHighest,
      child: const Icon(Icons.photo_outlined, size: 20),
    );
    return Image.network(
      url,
      headers: headers,
      width: size,
      height: size,
      fit: fit,
      errorBuilder: (_, __, ___) => placeholder,
    );
  }
}

String _dims(int? w, int? h) => (w != null && h != null) ? '$w×$h' : '';

String _sizeLine(int? bytes, int? w, int? h) {
  final parts = [
    if (bytes != null) formatBytes(bytes),
    if (_dims(w, h).isNotEmpty) _dims(w, h),
  ];
  return parts.isEmpty ? 'size unknown' : parts.join(' · ');
}

/// "+1.2 MB" / "−300 KB".
String signedBytes(int delta) =>
    delta < 0 ? '−${formatBytes(-delta)}' : '+${formatBytes(delta)}';

// ── Notice card ───────────────────────────────────────────────────────────────

/// An icon, a title, a sentence and an optional button, centred.
class UpgradeNotice extends StatelessWidget {
  final IconData icon;
  final String title;
  final String message;
  final String? actionLabel;
  final VoidCallback? onAction;

  const UpgradeNotice({
    super.key,
    required this.icon,
    required this.title,
    required this.message,
    this.actionLabel,
    this.onAction,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(32),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 48, color: theme.colorScheme.primary),
              const SizedBox(height: 16),
              Text(title, textAlign: TextAlign.center, style: theme.textTheme.titleMedium),
              const SizedBox(height: 8),
              Text(message, textAlign: TextAlign.center, style: theme.textTheme.bodyMedium),
              if (actionLabel != null) ...[
                const SizedBox(height: 16),
                OutlinedButton(onPressed: onAction, child: Text(actionLabel!)),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

/// A strip above the review: a coloured message with optional buttons.
class UpgradeBanner extends StatelessWidget {
  final IconData icon;
  final Color color;
  final String message;
  final List<Widget> actions;
  final Widget? progress;

  const UpgradeBanner({
    super.key,
    required this.icon,
    required this.color,
    required this.message,
    this.actions = const [],
    this.progress,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.10),
        border: Border.all(color: color),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(icon, size: 20, color: color),
              const SizedBox(width: 8),
              Expanded(child: Text(message)),
            ],
          ),
          if (progress != null) ...[const SizedBox(height: 8), progress!],
          if (actions.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(spacing: 8, runSpacing: 4, children: actions),
          ],
        ],
      ),
    );
  }
}

// ── Setup ─────────────────────────────────────────────────────────────────────

class AlbumTile extends StatelessWidget {
  final ImmichAlbum album;
  final bool selected;
  final bool overlapsTrip;
  final String? thumbUrl;
  final Map<String, String> headers;
  final VoidCallback onTap;

  const AlbumTile({
    super.key,
    required this.album,
    required this.selected,
    required this.overlapsTrip,
    required this.thumbUrl,
    required this.headers,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    String day(String s) => s.length > 10 ? s.substring(0, 10) : s;
    final dates = [
      if (album.startDate != null) day(album.startDate!),
      if (album.endDate != null && day(album.endDate!) != day(album.startDate ?? ''))
        day(album.endDate!),
    ].join(' – ');
    final subtitle = [
      if (dates.isNotEmpty) dates,
      if (album.assetCount != null) '${album.assetCount} photos',
      if (album.shared) 'shared',
    ].join(' · ');
    return Card(
      margin: const EdgeInsets.only(bottom: 6),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(10),
        side: BorderSide(
            color: selected ? theme.colorScheme.primary : theme.dividerColor,
            width: selected ? 2 : 1),
      ),
      child: ListTile(
        selected: selected,
        onTap: onTap,
        leading: ClipRRect(
          borderRadius: BorderRadius.circular(6),
          child: thumbUrl == null
              ? Container(
                  width: 48,
                  height: 48,
                  color: theme.colorScheme.surfaceContainerHighest,
                  child: const Icon(Icons.photo_library_outlined, size: 20),
                )
              : UpgradeImage(url: thumbUrl!, headers: headers, size: 48),
        ),
        title: Text(album.name, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(
          overlapsTrip ? '$subtitle · matches your trip dates' : subtitle,
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
        ),
        trailing: selected ? Icon(Icons.check_circle, color: theme.colorScheme.primary) : null,
      ),
    );
  }
}

// ── Review ────────────────────────────────────────────────────────────────────

/// The summary chips; each also filters the list. Tapping the active one
/// clears the filter.
class SummaryChips extends StatelessWidget {
  final UpgradeSummary summary;
  final String filter;
  final ValueChanged<String> onFilter;

  const SummaryChips({
    super.key,
    required this.summary,
    required this.filter,
    required this.onFilter,
  });

  @override
  Widget build(BuildContext context) {
    final entries = <(String, String, int)>[
      ('to_check', 'To check', summary.toCheck),
      ('confident', 'Confident', summary.confident),
      ('no_match', 'No match', summary.count('no_match')),
      ('already_original', 'Already original', summary.count('already_original')),
      ('rejected', 'Rejected', summary.count('rejected')),
      ('failed', 'Failed', summary.count('failed')),
    ];
    return Wrap(
      spacing: 6,
      runSpacing: 4,
      children: [
        for (final (value, label, count) in entries)
          FilterChip(
            key: ValueKey('chip-$value'),
            label: Text('$label $count'),
            selected: filter == value,
            onSelected: (_) => onFilter(value),
          ),
      ],
    );
  }
}

/// What a row can do, given its state.
class SuggestionRow extends StatelessWidget {
  final UpgradeSuggestion row;
  final AlbumUpgradeService service;

  /// False while an apply runs: the server refuses edits then.
  final bool enabled;
  final bool busy;
  final VoidCallback onToggle;
  final VoidCallback onChoose;
  final VoidCallback onReject;
  final VoidCallback onUnreject;
  final VoidCallback onCompare;

  const SuggestionRow({
    super.key,
    required this.row,
    required this.service,
    required this.enabled,
    required this.busy,
    required this.onToggle,
    required this.onChoose,
    required this.onReject,
    required this.onUnreject,
    required this.onCompare,
  });

  bool get _tickable => row.hasAsset && (row.state == 'proposed' || row.state == 'failed');

  (String, Color?) _status(ThemeData theme) => switch (row.state) {
        'proposed' => row.isConfident
            ? ('Confident match', kSuccess)
            : ('Please check this one', kWarning),
        'no_match' => ('No match found in this album', null),
        'already_original' => ('Already the original', kSuccess),
        'rejected' => ('You turned this one down', null),
        'applying' => ('Replacing…', null),
        'applied' => ('Replaced with the original', kSuccess),
        'failed' => (upgradeErrorSentence(row.errorCode), theme.colorScheme.error),
        _ => ('Changed since the check', kWarning),
      };

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final headers = service.authHeaders;
    final (status, statusColor) = _status(theme);
    final canAct = enabled && !busy;
    final state = row.state;
    final canChoose = const {'proposed', 'failed', 'rejected', 'applied'}.contains(state);

    return Container(
      key: ValueKey('row-${row.id}'),
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        border: Border.all(
            color: state == 'failed'
                ? theme.colorScheme.error
                : (state == 'proposed' && !row.isConfident ? kWarning : theme.dividerColor)),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (_tickable)
                Checkbox(
                  key: ValueKey('row-check-${row.id}'),
                  value: row.selected,
                  onChanged: canAct ? (_) => onToggle() : null,
                )
              else
                const SizedBox(width: 8),
              InkWell(
                key: ValueKey('row-compare-${row.id}'),
                borderRadius: BorderRadius.circular(6),
                onTap: row.hasAsset ? onCompare : null,
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    ClipRRect(
                      borderRadius: BorderRadius.circular(6),
                      child: UpgradeImage(
                          url: service.currentThumbUrl(row), headers: headers),
                    ),
                    if (row.hasAsset) ...[
                      const Padding(
                        padding: EdgeInsets.symmetric(horizontal: 4),
                        child: Icon(Icons.arrow_forward, size: 16),
                      ),
                      ClipRRect(
                        borderRadius: BorderRadius.circular(6),
                        child: UpgradeImage(
                            url: service.assetThumbUrl(row.assetId!), headers: headers),
                      ),
                    ],
                  ],
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Now: ${_sizeLine(row.currentBytes, row.currentWidth, row.currentHeight)}',
                        style: theme.textTheme.bodySmall),
                    if (row.hasAsset)
                      Text(
                          'Original: ${_sizeLine(row.assetBytes, row.assetWidth, row.assetHeight)}',
                          style: theme.textTheme.bodySmall),
                    const SizedBox(height: 4),
                    Text(status,
                        style: theme.textTheme.bodySmall
                            ?.copyWith(color: statusColor, fontWeight: FontWeight.w600)),
                  ],
                ),
              ),
              if (busy || state == 'applying')
                const Padding(
                  padding: EdgeInsets.only(left: 6),
                  child: SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2)),
                )
              else if (state == 'applied')
                const Icon(Icons.check_circle, color: kSuccess),
            ],
          ),
          if (canChoose || state == 'no_match')
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Wrap(
                spacing: 6,
                runSpacing: 4,
                children: [
                  OutlinedButton(
                    onPressed: canAct ? onChoose : null,
                    child: Text(state == 'no_match' ? 'Choose from album' : 'Choose another'),
                  ),
                  if ((state == 'proposed' || state == 'failed') && row.hasAsset)
                    TextButton(
                        onPressed: canAct ? onReject : null,
                        child: const Text('Not this one')),
                  if (state == 'rejected')
                    TextButton(
                        onPressed: canAct ? onUnreject : null, child: const Text('Undo')),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

/// Current and proposed photo side by side, each pinch/scroll zoomable.
Future<void> showCompareDialog(
    BuildContext context, AlbumUpgradeService service, UpgradeSuggestion row) {
  final headers = service.authHeaders;
  Widget pane(String caption, String url, {String? fallbackUrl}) => Expanded(
        child: Column(
          children: [
            Text(caption, style: Theme.of(context).textTheme.labelMedium),
            const SizedBox(height: 4),
            Expanded(
              child: ClipRect(
                child: InteractiveViewer(
                  maxScale: 8,
                  child: Image.network(
                    url,
                    headers: headers,
                    fit: BoxFit.contain,
                    errorBuilder: (_, __, ___) => fallbackUrl == null
                        ? const Center(child: Icon(Icons.broken_image_outlined))
                        : Image.network(fallbackUrl,
                            headers: headers,
                            fit: BoxFit.contain,
                            errorBuilder: (_, __, ___) =>
                                const Center(child: Icon(Icons.broken_image_outlined))),
                  ),
                ),
              ),
            ),
          ],
        ),
      );
  return showDialog<void>(
    context: context,
    builder: (ctx) => Dialog(
      insetPadding: const EdgeInsets.all(12),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: SizedBox(
          height: 420,
          child: Column(
            children: [
              Expanded(
                child: Row(
                  children: [
                    pane('In your trip', service.currentFullUrl(row)),
                    const SizedBox(width: 8),
                    pane('Immich original', service.assetOriginalUrl(row.assetId!),
                        fallbackUrl: service.assetThumbUrl(row.assetId!)),
                  ],
                ),
              ),
              Align(
                alignment: Alignment.centerRight,
                child: TextButton(
                    onPressed: () => Navigator.of(ctx).pop(), child: const Text('Close')),
              ),
            ],
          ),
        ),
      ),
    ),
  );
}

/// The sheet of that day's album photos; pops the chosen asset id.
Future<String?> showCandidateSheet(
  BuildContext context, {
  required AlbumUpgradeService service,
  required Future<List<AlbumCandidate>> candidates,
  required String? currentAssetId,
}) {
  final headers = service.authHeaders;
  return showModalBottomSheet<String>(
    context: context,
    showDragHandle: true,
    isScrollControlled: true,
    builder: (ctx) => ConstrainedBox(
      constraints: BoxConstraints(maxHeight: MediaQuery.of(ctx).size.height * 0.6),
      child: FutureBuilder<List<AlbumCandidate>>(
        future: candidates,
        builder: (ctx, snap) {
          if (snap.hasError) {
            return Padding(
              padding: const EdgeInsets.all(24),
              child: Text(snap.error.toString()),
            );
          }
          if (!snap.hasData) {
            return const Padding(
              padding: EdgeInsets.all(24),
              child: Center(child: CircularProgressIndicator()),
            );
          }
          final list = snap.data!;
          if (list.isEmpty) {
            return const Padding(
              padding: EdgeInsets.all(24),
              child: Text('No photos from that day in this album.'),
            );
          }
          return SingleChildScrollView(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final c in list)
                  InkWell(
                    key: ValueKey('candidate-${c.assetId}'),
                    borderRadius: BorderRadius.circular(6),
                    onTap: () => Navigator.of(ctx).pop(c.assetId),
                    child: SizedBox(
                      width: 96,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Container(
                            decoration: BoxDecoration(
                              borderRadius: BorderRadius.circular(6),
                              border: c.assetId == currentAssetId
                                  ? Border.all(
                                      color: Theme.of(ctx).colorScheme.primary, width: 2)
                                  : null,
                            ),
                            child: ClipRRect(
                              borderRadius: BorderRadius.circular(6),
                              child: UpgradeImage(
                                  url: service.assetThumbUrl(c.assetId),
                                  headers: headers,
                                  size: 96),
                            ),
                          ),
                          Text(c.localDate.length >= 10 ? c.localDate.substring(0, 10) : c.localDate,
                              style: Theme.of(ctx).textTheme.labelSmall),
                          if (c.bytes != null)
                            Text(formatBytes(c.bytes!),
                                style: Theme.of(ctx).textTheme.labelSmall),
                        ],
                      ),
                    ),
                  ),
              ],
            ),
          );
        },
      ),
    ),
  );
}

// ── Scanning / applying ───────────────────────────────────────────────────────

String scanStageText(UpgradeScan scan) => switch (scan.stage) {
      'reading_album' => 'Reading your album…',
      'fingerprinting' => 'Comparing your photos with the album…',
      'matching' => 'Matching photos…',
      'done' => 'Almost done…',
      _ => 'Waiting to start…',
    };

class ScanProgressCard extends StatelessWidget {
  final UpgradeScan scan;
  const ScanProgressCard({super.key, required this.scan});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text('Checking your album', style: theme.textTheme.titleMedium),
              const SizedBox(height: 12),
              LinearProgressIndicator(
                value: scan.progressTotal > 0 ? scan.progressDone / scan.progressTotal : null,
              ),
              const SizedBox(height: 8),
              Text(scanStageText(scan), key: const ValueKey('scan-stage')),
              if (scan.progressTotal > 0)
                Text('${scan.progressDone} / ${scan.progressTotal}',
                    style: theme.textTheme.bodySmall),
            ],
          ),
        ),
      ),
    );
  }
}

// ── Sticky bar ────────────────────────────────────────────────────────────────

String replaceBarText(UpgradeSummary s) {
  final n = s.selected;
  final base = 'Replace $n ${n == 1 ? 'photo' : 'photos'}';
  final free = s.free;
  if (free == null) return '$base · ${signedBytes(s.selectedBytesDelta)}';
  return '$base · ${signedBytes(s.selectedBytesDelta)} of ${formatBytes(free < 0 ? 0 : free)} free';
}

class ReplaceBar extends StatelessWidget {
  final UpgradeSummary summary;
  final bool starting;
  final VoidCallback onReplace;
  final VoidCallback onSelectConfident;
  final VoidCallback onClear;
  final VoidCallback onUpgradePlan;

  const ReplaceBar({
    super.key,
    required this.summary,
    required this.starting,
    required this.onReplace,
    required this.onSelectConfident,
    required this.onClear,
    required this.onUpgradePlan,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final blocked = summary.overStorage;
    return Material(
      elevation: 8,
      color: theme.colorScheme.surface,
      child: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(replaceBarText(summary),
                  key: const ValueKey('replace-bar-text'),
                  style: theme.textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w600)),
              if (blocked)
                Row(
                  children: [
                    Expanded(
                      child: Text('Not enough storage for these photos.',
                          style: TextStyle(color: theme.colorScheme.error, fontSize: 12)),
                    ),
                    TextButton(
                        onPressed: onUpgradePlan, child: const Text('Upgrade your plan')),
                  ],
                ),
              Wrap(
                spacing: 8,
                runSpacing: 4,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  TextButton(
                      onPressed: starting ? null : onSelectConfident,
                      child: const Text('Select all confident')),
                  TextButton(
                      onPressed: starting ? null : onClear, child: const Text('Clear')),
                  FilledButton(
                    onPressed: (summary.selected == 0 || blocked || starting) ? null : onReplace,
                    child: const Text('Replace'),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}
