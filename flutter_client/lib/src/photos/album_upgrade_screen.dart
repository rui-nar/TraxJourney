/// "Upgrade photos from Immich" (issue #33): pick an album, let the server
/// match it to the trip's photos, review the proposals, replace the ticked
/// ones with their originals.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../billing/billing_service.dart' show kPlanRoute;
import '../core/design_tokens.dart';
import '../core/project_ref.dart';
import '../projects/project_notifier.dart';
import 'album_upgrade_notifier.dart';
import 'album_upgrade_service.dart';
import 'album_upgrade_widgets.dart';

String _unreadableSentence(int n) => n == 1
    ? '1 photo in the album could not be read from Immich, so it could not be '
        'matched. Scan again to retry.'
    : '$n photos in the album could not be read from Immich, so they could not '
        'be matched. Scan again to retry.';

const _kNoWorkerSentence = 'Photo upgrades are not available on this server right now.';

class AlbumUpgradeScreen extends StatefulWidget {
  final String projectName;

  /// Owning user's id for a project shared with the caller (issue #106);
  /// null for one of the caller's own projects.
  final int? ownerId;

  const AlbumUpgradeScreen({super.key, required this.projectName, this.ownerId});

  ProjectRef get projectRef => ProjectRef(name: projectName, ownerId: ownerId);

  @override
  State<AlbumUpgradeScreen> createState() => _AlbumUpgradeScreenState();
}

class _AlbumUpgradeScreenState extends State<AlbumUpgradeScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final n = context.read<AlbumUpgradeNotifier>();
      final project = context.read<ProjectNotifier>();
      // Once the apply ends, reload the project so the new photos show.
      n.onApplyFinished = () {
        if (mounted) project.load(widget.projectRef);
      };
      n.load(widget.projectRef,
          tripStart: project.itemsFacet.tripStart, tripEnd: project.itemsFacet.tripEnd);
    });
  }

  void _openPlan() => context.push(kPlanRoute);

  /// The user may connect Immich in Settings: read the connection again on return.
  Future<void> _openSettings(AlbumUpgradeNotifier n) async {
    final project = context.read<ProjectNotifier>();
    await context.push('/settings');
    if (!mounted) return;
    await n.load(widget.projectRef,
        tripStart: project.itemsFacet.tripStart, tripEnd: project.itemsFacet.tripEnd);
  }

  Future<void> _choose(AlbumUpgradeNotifier n, UpgradeSuggestion row) async {
    final assetId = await showCandidateSheet(
      context,
      service: n.service,
      candidates: n.candidates(row),
      currentAssetId: row.assetId,
    );
    if (assetId != null) await n.choose(row, assetId);
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<AlbumUpgradeNotifier>(builder: (context, n, _) {
      final view = n.view;
      return Scaffold(
        appBar: AppBar(
          leading: BackButton(onPressed: () => context.pop()),
          title: const Text('Upgrade photos from Immich'),
          actions: [
            if (view == UpgradeView.review)
              TextButton(
                onPressed: n.applyActive ? null : n.newScan,
                child: const Text('New scan'),
              ),
            if (view == UpgradeView.setup && n.scan != null)
              TextButton(onPressed: n.cancelNewScan, child: const Text('Back to review')),
          ],
        ),
        body: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 760),
            child: _body(context, n, view),
          ),
        ),
        bottomNavigationBar: view == UpgradeView.review &&
                !n.applyActive &&
                n.summary != null
            ? Align(
                heightFactor: 1,
                alignment: Alignment.bottomCenter,
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 760),
                  child: ReplaceBar(
                    summary: n.summary!,
                    starting: n.applyStarting,
                    onReplace: n.apply,
                    onSelectConfident: n.selectAllConfident,
                    onClear: n.clearSelection,
                    onUpgradePlan: _openPlan,
                  ),
                ),
              )
            : null,
      );
    });
  }

  Widget _body(BuildContext context, AlbumUpgradeNotifier n, UpgradeView view) {
    switch (view) {
      case UpgradeView.loading:
        return const Center(child: CircularProgressIndicator());
      case UpgradeView.error:
        return UpgradeNotice(
          icon: Icons.error_outline,
          title: 'Could not load',
          message: n.loadError ?? '',
          actionLabel: 'Try again',
          onAction: () => n.load(widget.projectRef),
        );
      case UpgradeView.notConnected:
        return UpgradeNotice(
          icon: Icons.link_off,
          title: 'Immich is not connected',
          message: 'Immich is a photo library you host yourself. Connect yours in '
              'Settings and TraxJourney can swap this trip\'s smaller photos for '
              'the original files.',
          actionLabel: 'Open Settings',
          onAction: () => _openSettings(n),
        );
      case UpgradeView.scanning:
        return ScanProgressCard(scan: n.newer!);
      case UpgradeView.setup:
        return _setup(context, n);
      case UpgradeView.review:
        return _review(context, n);
    }
  }

  // ── Banners shared by setup and review ──────────────────────────────────────

  List<Widget> _notices(AlbumUpgradeNotifier n, {required bool showFailedScan}) {
    final theme = Theme.of(context);
    final failed = n.failedNewer;
    return [
      if (showFailedScan && failed != null)
        UpgradeBanner(
          icon: Icons.error_outline,
          color: theme.colorScheme.error,
          message: 'The latest check of your album failed. ${failed.errorMessage ?? ''}'.trim(),
          actions: [
            FilledButton(
                onPressed: n.starting ? null : n.retryScan, child: const Text('Try again')),
          ],
        ),
      if (n.noWorker)
        const UpgradeBanner(
          icon: Icons.cloud_off_outlined,
          color: kWarning,
          message: _kNoWorkerSentence,
        ),
      if (n.actionError != null)
        UpgradeBanner(
          icon: Icons.info_outline,
          color: theme.colorScheme.error,
          message: n.actionError!,
        ),
    ];
  }

  // ── Setup ────────────────────────────────────────────────────────────────────

  Widget _setup(BuildContext context, AlbumUpgradeNotifier n) {
    final theme = Theme.of(context);
    final headers = n.service.authHeaders;
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text('Replace the small photos in this trip with the originals from an '
            'Immich album.',
            style: theme.textTheme.bodyMedium),
        const SizedBox(height: 4),
        Text('Nothing changes until you approve it.',
            key: const ValueKey('nothing-changes'),
            style: theme.textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w600)),
        const SizedBox(height: 12),
        ..._notices(n, showFailedScan: true),
        Text('Album', style: theme.textTheme.titleSmall),
        const SizedBox(height: 6),
        if (n.albumsLoading)
          const Padding(
              padding: EdgeInsets.all(16), child: Center(child: CircularProgressIndicator()))
        else if (n.albumsError != null) ...[
          Text(n.albumsError!, style: TextStyle(color: theme.colorScheme.error)),
          TextButton(onPressed: n.loadAlbums, child: const Text('Try again')),
        ] else if (n.albums.isEmpty)
          const Text('No albums found in your Immich library.')
        else
          for (final a in n.albums)
            AlbumTile(
              album: a,
              selected: a.id == n.selectedAlbumId,
              overlapsTrip: n.albumOverlapsTrip(a),
              thumbUrl: a.thumbUrl == null ? null : n.service.albumThumbUrl(a.thumbUrl!),
              headers: headers,
              onTap: () => n.selectAlbum(a.id),
            ),
        const SizedBox(height: 12),
        Text('What to check', style: theme.textTheme.titleSmall),
        // Memories are the owner's: a companion has no count and no box (D3).
        if (n.memoryPhotos != null)
          CheckboxListTile(
            key: const ValueKey('include-memories'),
            contentPadding: EdgeInsets.zero,
            controlAffinity: ListTileControlAffinity.leading,
            value: n.includeMemories,
            onChanged: (v) => n.setIncludeMemories(v ?? false),
            title: Text('Memories (${n.memoryPhotos} photos)'),
          ),
        CheckboxListTile(
          key: const ValueKey('include-journal'),
          contentPadding: EdgeInsets.zero,
          controlAffinity: ListTileControlAffinity.leading,
          value: n.includeJournal,
          onChanged: (v) => n.setIncludeJournal(v ?? false),
          title: Text('My journal entries (${n.journalPhotos} photos)'),
        ),
        const SizedBox(height: 12),
        FilledButton(
          key: const ValueKey('scan-button'),
          onPressed: n.canScan ? n.startScan : null,
          child: n.starting
              ? const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
              : const Text('Scan'),
        ),
      ],
    );
  }

  // ── Review ───────────────────────────────────────────────────────────────────

  Widget _review(BuildContext context, AlbumUpgradeNotifier n) {
    final summary = n.summary!;
    final entries = _entries(n.rows);
    final header = <Widget>[
      ..._notices(n, showFailedScan: true),
      ..._applyBanners(context, n, summary),
      if (n.scan!.unreadablePhotos > 0)
        UpgradeBanner(
          key: const ValueKey('unreadable-notice'),
          icon: Icons.info_outline,
          color: kWarning,
          message: _unreadableSentence(n.scan!.unreadablePhotos),
        ),
      SummaryChips(summary: summary, filter: n.filter, onFilter: n.setFilter),
      const SizedBox(height: 10),
      if (n.rows.isEmpty && !n.rowsLoading)
        const Padding(padding: EdgeInsets.all(16), child: Text('Nothing to show here.')),
    ];
    return ListView.builder(
      padding: const EdgeInsets.all(16),
      itemCount: header.length + entries.length + 1,
      itemBuilder: (context, i) {
        if (i < header.length) return header[i];
        final at = i - header.length;
        if (at == entries.length) {
          if (n.rowsLoading) {
            return const Padding(
                padding: EdgeInsets.all(12), child: Center(child: CircularProgressIndicator()));
          }
          return n.nextCursor == null
              ? const SizedBox.shrink()
              : Center(
                  child: TextButton(onPressed: n.loadMore, child: const Text('Load more')));
        }
        final e = entries[at];
        if (e is _Header) {
          return Padding(
            padding: EdgeInsets.only(top: e.isDay ? 12 : 4, bottom: 6),
            child: Text(e.text,
                style: e.isDay
                    ? Theme.of(context).textTheme.titleSmall
                    : Theme.of(context)
                        .textTheme
                        .labelLarge
                        ?.copyWith(color: Theme.of(context).colorScheme.onSurfaceVariant)),
          );
        }
        final row = (e as _RowEntry).row;
        return SuggestionRow(
          row: row,
          service: n.service,
          enabled: !n.applyActive,
          busy: n.busyRows.contains(row.id),
          onToggle: () => n.toggle(row),
          onChoose: () => _choose(n, row),
          onReject: () => n.reject(row),
          onUnreject: () => n.unreject(row),
          onCompare: () => showCompareDialog(context, n.service, row),
        );
      },
    );
  }

  List<Widget> _applyBanners(BuildContext context, AlbumUpgradeNotifier n, UpgradeSummary s) {
    final theme = Theme.of(context);
    final scan = n.scan!;
    final failed = s.count('failed');
    return [
      if (scan.applyActive)
        UpgradeBanner(
          icon: Icons.sync,
          color: theme.colorScheme.primary,
          message: 'Replacing photos… ${s.count('applied')} done, '
              '${s.selected + s.count('applying')} to go.',
          progress: const LinearProgressIndicator(),
        ),
      if (scan.applyStatus == 'stopped')
        UpgradeBanner(
          icon: Icons.pause_circle_outline,
          color: kWarning,
          message: scan.applyStoppedReason == 'storage_full'
              ? 'Replacing stopped because your storage is full. Free some space or '
                  'upgrade your plan, then resume. The photos still to do stay ticked.'
              : 'Replacing was interrupted. Resume to finish the photos still to do.',
          actions: [
            FilledButton(
                onPressed: n.applyStarting ? null : n.apply, child: const Text('Resume')),
            if (scan.applyStoppedReason == 'storage_full')
              TextButton(onPressed: _openPlan, child: const Text('Upgrade your plan')),
          ],
        ),
      if (scan.applyStatus == 'done')
        UpgradeBanner(
          icon: Icons.check_circle_outline,
          color: kSuccess,
          message: '${s.count('applied')} ${s.count('applied') == 1 ? 'photo' : 'photos'} '
              'replaced with the originals.',
        ),
      if (failed > 0 && !scan.applyActive)
        UpgradeBanner(
          icon: Icons.error_outline,
          color: theme.colorScheme.error,
          message: '$failed ${failed == 1 ? 'photo' : 'photos'} could not be replaced.',
          actions: [
            if (n.retryableFailed)
              OutlinedButton(onPressed: n.retryFailed, child: const Text('Retry failed')),
          ],
        ),
    ];
  }

  /// The rows grouped by day, then by memory or entry, in the server's order.
  List<_Entry> _entries(List<UpgradeSuggestion> rows) {
    final out = <_Entry>[];
    String? day;
    String? artifact;
    for (final r in rows) {
      if (r.date != day) {
        day = r.date;
        artifact = null;
        out.add(_Header(r.date, isDay: true));
      }
      final key = '${r.kind}-${r.artifactId}';
      if (key != artifact) {
        artifact = key;
        out.add(_Header(r.isMemory ? 'Memory' : 'Journal entry', isDay: false));
      }
      out.add(_RowEntry(r));
    }
    return out;
  }
}

sealed class _Entry {}

class _Header extends _Entry {
  final String text;
  final bool isDay;
  _Header(this.text, {required this.isDay});
}

class _RowEntry extends _Entry {
  final UpgradeSuggestion row;
  _RowEntry(this.row);
}
