/// Review/confirm UI for the Polarsteps photo-upgrade feature (issue #33).
///
/// Memories only — that's what Polarsteps imports produce, and what issue
/// #33 is about. Phase 1's replace endpoint also exists for journal entries,
/// but wiring a UI entry point for those is out of scope here.
///
/// Photos are picked one at a time against a specific existing thumbnail
/// (rather than a bulk multi-select auto-matched in a batch): the system
/// picker has no way to pre-filter the device's photo library down to "this
/// day", so showing the exact thumbnail being replaced while the user picks
/// is what tells them which photo to look for — the day/geo/pHash checks
/// below are then just a confirmation signal, not a filter.
library;

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../core/design_tokens.dart';
import '../projects/project_notifier.dart';
import 'immich_source.dart';
import 'perceptual_hash.dart';
import 'photo_match.dart';
import 'photo_source.dart';

/// Opens the photo-upgrade review dialog for [memory].
///
/// [pickSinglePhotoOverride] and [fetchThumbnailHashOverride] exist only so
/// widget tests can feed in known candidates/hashes without touching the
/// platform file picker or making real network calls — production callers
/// should never pass them. Same for the Immich-suggestion overrides
/// ([checkImmichConnectedOverride], [fetchImmichCandidatesOverride],
/// [fetchImmichThumbnailHashOverride], [downloadImmichCandidateOverride]),
/// added for the Immich auto-suggestion flow (issue #33).
void showPhotoUpgradeDialog(
  BuildContext context,
  ProjectNotifier notifier,
  Map<String, dynamic> memory, {
  @visibleForTesting Future<PickedPhoto?> Function()? pickSinglePhotoOverride,
  @visibleForTesting Future<PerceptualHash?> Function(String uuid)? fetchThumbnailHashOverride,
  @visibleForTesting Future<bool> Function()? checkImmichConnectedOverride,
  @visibleForTesting Future<List<ImmichCandidate>> Function()? fetchImmichCandidatesOverride,
  @visibleForTesting
  Future<PerceptualHash?> Function(ImmichCandidate candidate)? fetchImmichThumbnailHashOverride,
  @visibleForTesting
  Future<PickedPhoto> Function(ImmichCandidate candidate)? downloadImmichCandidateOverride,
}) {
  showDialog(
    context: context,
    useRootNavigator: true,
    builder: (_) => _PhotoUpgradeDialog(
      notifier: notifier,
      memory: memory,
      pickSinglePhoto: pickSinglePhotoOverride ?? pickSinglePhotoForUpgrade,
      fetchThumbnailHash: fetchThumbnailHashOverride,
      checkImmichConnected: checkImmichConnectedOverride,
      fetchImmichCandidates: fetchImmichCandidatesOverride,
      fetchImmichThumbnailHash: fetchImmichThumbnailHashOverride,
      downloadImmichCandidate: downloadImmichCandidateOverride,
    ),
  );
}

enum _RowStatus { empty, picked, applying, applied, failed }

class _UpgradeRow {
  final String oldUuid;
  PickedPhoto? picked;
  PerceptualHash? thumbHash;
  bool thumbHashChecked = false;
  bool? looksSame;
  DayGeoMismatch dayGeoMismatch = DayGeoMismatch.none;
  bool comparing = false;
  _RowStatus status = _RowStatus.empty;

  /// Immich candidates that looked like a plausible match for this row's
  /// thumbnail but weren't confident enough to auto-fill (issue #33) —
  /// non-empty only while the "Choose match" chooser is on offer.
  List<ImmichCandidate> immichMatches = [];

  /// True while this row's Immich candidates are being fetched/compared or
  /// a chosen candidate is being downloaded — separate from [comparing]
  /// (the manual file-picker's busy flag) so the two flows don't share
  /// state.
  bool immichBusy = false;

  _UpgradeRow(this.oldUuid);
}

class _PhotoUpgradeDialog extends StatefulWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic> memory;
  final Future<PickedPhoto?> Function() pickSinglePhoto;
  final Future<PerceptualHash?> Function(String uuid)? fetchThumbnailHash;
  final Future<bool> Function()? checkImmichConnected;
  final Future<List<ImmichCandidate>> Function()? fetchImmichCandidates;
  final Future<PerceptualHash?> Function(ImmichCandidate candidate)? fetchImmichThumbnailHash;
  final Future<PickedPhoto> Function(ImmichCandidate candidate)? downloadImmichCandidate;

  const _PhotoUpgradeDialog({
    required this.notifier,
    required this.memory,
    required this.pickSinglePhoto,
    this.fetchThumbnailHash,
    this.checkImmichConnected,
    this.fetchImmichCandidates,
    this.fetchImmichThumbnailHash,
    this.downloadImmichCandidate,
  });

  @override
  State<_PhotoUpgradeDialog> createState() => _PhotoUpgradeDialogState();
}

class _PhotoUpgradeDialogState extends State<_PhotoUpgradeDialog> {
  late final List<_UpgradeRow> _rows;
  String? _error;
  bool _immichConnected = false;
  bool _suggestingAll = false;
  List<ImmichCandidate>? _dayCandidatesCache;
  final Map<String, PerceptualHash?> _immichHashCache = {};

  String get _memoryId => widget.memory['id']?.toString() ?? '';

  /// `ImmichCandidate.thumbUrl` is a same-origin-relative path (e.g.
  /// `/api/immich/assets/{id}/thumbnail`) — resolve it against the API base
  /// URL the same way [ProjectNotifier.photoThumbUrl] builds its own
  /// absolute thumbnail URLs, so it works on native platforms too (not just
  /// web, where a relative path happens to resolve against the page origin).
  String _immichThumbUrl(ImmichCandidate candidate) =>
      '${widget.notifier.apiBaseUrl}${candidate.thumbUrl}';

  @override
  void initState() {
    super.initState();
    final existingUuids = (widget.memory['photos'] as List?)?.cast<String>() ?? [];
    _rows = [for (final uuid in existingUuids) _UpgradeRow(uuid)];
    _loadImmichConnected();
  }

  Future<void> _loadImmichConnected() async {
    final connected = await _checkImmichConnected();
    if (!mounted) return;
    setState(() => _immichConnected = connected);
  }

  Future<bool> _checkImmichConnected() {
    if (widget.checkImmichConnected != null) return widget.checkImmichConnected!();
    return widget.notifier.immichConnected();
  }

  Future<List<ImmichCandidate>> _fetchImmichCandidatesForDay() async {
    if (widget.fetchImmichCandidates != null) return widget.fetchImmichCandidates!();
    final date = widget.memory['date'] as String?;
    if (date == null) return const [];

    // EXIF-as-UTC convention (see photo_source.dart's _parseExifDateTime):
    // the trip's local day is treated as a UTC day, matching classifyDayGeoMatch's
    // localOffset: Duration.zero elsewhere in this file.
    final parts = date.split('-');
    final dayStartUtc = DateTime.utc(
      int.parse(parts[0]),
      int.parse(parts[1]),
      int.parse(parts[2]),
    );
    final dayEndUtc = dayStartUtc.add(const Duration(days: 1));

    final raw = await fetchImmichCandidatesForDay(
      baseUrl: widget.notifier.apiBaseUrl,
      authHeaders: widget.notifier.photoAuthHeaders,
      dayStartUtc: dayStartUtc,
      dayEndUtc: dayEndUtc,
    );
    return filterImmichCandidatesForDay(
      date: date,
      localOffset: Duration.zero,
      candidates: raw,
      memoryLat: (widget.memory['lat'] as num?)?.toDouble(),
      memoryLon: (widget.memory['lon'] as num?)?.toDouble(),
    );
  }

  Future<List<ImmichCandidate>> _dayImmichCandidates() async {
    if (_dayCandidatesCache != null) return _dayCandidatesCache!;
    final fetched = await _fetchImmichCandidatesForDay();
    _dayCandidatesCache = fetched;
    return fetched;
  }

  /// Downloads an Immich candidate's thumbnail and computes its pHash, for
  /// comparing against a row's *existing* thumbnail hash (fetched via
  /// [_fetchThumbnailHash]). Cached per candidate id since the same day's
  /// candidates are compared against every row.
  Future<PerceptualHash?> _immichCandidateHash(ImmichCandidate candidate) async {
    if (_immichHashCache.containsKey(candidate.id)) return _immichHashCache[candidate.id];
    final hash = await _fetchImmichThumbnailHash(candidate);
    _immichHashCache[candidate.id] = hash;
    return hash;
  }

  Future<PerceptualHash?> _fetchImmichThumbnailHash(ImmichCandidate candidate) async {
    if (widget.fetchImmichThumbnailHash != null) {
      return widget.fetchImmichThumbnailHash!(candidate);
    }
    try {
      final res = await http.get(
        Uri.parse(_immichThumbUrl(candidate)),
        headers: widget.notifier.photoAuthHeaders,
      );
      if (res.statusCode < 200 || res.statusCode >= 300) return null;
      return computeAverageHash(res.bodyBytes);
    } catch (_) {
      return null;
    }
  }

  Future<PickedPhoto> _downloadImmichCandidate(ImmichCandidate candidate) {
    if (widget.downloadImmichCandidate != null) {
      return widget.downloadImmichCandidate!(candidate);
    }
    return downloadImmichCandidate(
      baseUrl: widget.notifier.apiBaseUrl,
      authHeaders: widget.notifier.photoAuthHeaders,
      candidate: candidate,
    );
  }

  /// Runs the Immich suggestion flow for every row that doesn't already
  /// have a pick — used by the top-of-dialog "Suggest from Immich" button.
  /// Sequential (not `Future.wait`) so the day's candidates are only
  /// fetched once and so only one row shows its busy indicator at a time.
  Future<void> _suggestAllRows() async {
    setState(() => _suggestingAll = true);
    for (final row in _rows) {
      if (row.picked != null) continue;
      await _suggestForRow(row);
    }
    if (!mounted) return;
    setState(() => _suggestingAll = false);
  }

  /// Fetches the day's Immich candidates (once, cached) and pHash-compares
  /// each against [row]'s existing thumbnail, exactly the day/geo-then-pHash
  /// logic a manual pick uses ([classifyDayGeoMatch] + [looksLikeSamePhoto])
  /// but run per-candidate against this row's one fixed target thumbnail.
  /// A single confident match auto-fills the row; more than one shows the
  /// "Choose match" chooser instead of guessing; zero leaves the row as-is.
  Future<void> _suggestForRow(_UpgradeRow row) async {
    setState(() {
      row.immichBusy = true;
      row.immichMatches = [];
      _error = null;
    });
    try {
      final candidates = await _dayImmichCandidates();
      if (!row.thumbHashChecked) {
        row.thumbHash = await _fetchThumbnailHash(row.oldUuid);
        row.thumbHashChecked = true;
      }

      final matches = <ImmichCandidate>[];
      for (final candidate in candidates) {
        final hash = await _immichCandidateHash(candidate);
        if (looksLikeSamePhoto(hash, row.thumbHash) == true) matches.add(candidate);
      }

      if (matches.isEmpty) {
        setState(() => row.immichBusy = false);
      } else if (matches.length == 1) {
        await _applyImmichCandidate(row, matches.single);
      } else {
        setState(() {
          row.immichMatches = matches;
          row.immichBusy = false;
        });
      }
    } catch (e) {
      setState(() {
        row.immichBusy = false;
        _error = 'Could not fetch Immich suggestions.';
      });
    }
  }

  /// Opens the "Choose match" bottom sheet for [row]'s ambiguous
  /// [_UpgradeRow.immichMatches] and applies whichever thumbnail is tapped.
  Future<void> _chooseImmichMatch(_UpgradeRow row) async {
    final selected = await showModalBottomSheet<ImmichCandidate>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
        child: Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final candidate in row.immichMatches)
              InkWell(
                key: ValueKey('immich-candidate-${candidate.id}'),
                borderRadius: BorderRadius.circular(6),
                onTap: () => Navigator.of(sheetContext).pop(candidate),
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(6),
                  child: Image.network(
                    _immichThumbUrl(candidate),
                    width: 72,
                    height: 72,
                    fit: BoxFit.cover,
                    headers: widget.notifier.photoAuthHeaders,
                    errorBuilder: (_, __, ___) => Container(
                      width: 72,
                      height: 72,
                      color: Theme.of(sheetContext).colorScheme.surfaceContainerHighest,
                      child: const Icon(Icons.photo_outlined, size: 20),
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
    if (selected == null) return;
    setState(() => row.immichBusy = true);
    await _applyImmichCandidate(row, selected);
  }

  /// Fills [row.picked] from an Immich [candidate] — same fields, same
  /// day/geo-then-pHash computation, as a confirmed manual pick in
  /// [_pickForRow]. Only ever fills the row; [_confirm] still gates the
  /// actual upload.
  Future<void> _applyImmichCandidate(_UpgradeRow row, ImmichCandidate candidate) async {
    final picked = await _downloadImmichCandidate(candidate);
    final date = widget.memory['date'] as String?;
    final photoCandidate = picked.candidate;
    final dayGeoMismatch = (date == null || photoCandidate == null)
        ? DayGeoMismatch.none
        : classifyDayGeoMatch(
            date: date,
            localOffset: Duration.zero,
            candidate: photoCandidate,
            memoryLat: (widget.memory['lat'] as num?)?.toDouble(),
            memoryLon: (widget.memory['lon'] as num?)?.toDouble(),
          );

    setState(() {
      row.picked = picked;
      row.dayGeoMismatch = dayGeoMismatch;
      row.looksSame = looksLikeSamePhoto(photoCandidate?.pHash, row.thumbHash);
      row.status = _RowStatus.picked;
      row.immichBusy = false;
      row.immichMatches = [];
    });
  }

  Future<PerceptualHash?> _fetchThumbnailHash(String uuid) async {
    if (widget.fetchThumbnailHash != null) {
      return widget.fetchThumbnailHash!(uuid);
    }
    try {
      final url = widget.notifier.photoThumbUrl(_memoryId, uuid);
      final res = await http.get(Uri.parse(url), headers: widget.notifier.photoAuthHeaders);
      if (res.statusCode < 200 || res.statusCode >= 300) return null;
      return computeAverageHash(res.bodyBytes);
    } catch (_) {
      return null;
    }
  }

  Future<void> _pickForRow(_UpgradeRow row) async {
    setState(() {
      row.comparing = true;
      _error = null;
    });
    try {
      final picked = await widget.pickSinglePhoto();
      if (picked == null) {
        setState(() => row.comparing = false);
        return;
      }

      if (!row.thumbHashChecked) {
        row.thumbHash = await _fetchThumbnailHash(row.oldUuid);
        row.thumbHashChecked = true;
      }

      final date = widget.memory['date'] as String?;
      final candidate = picked.candidate;
      final dayGeoMismatch = (date == null || candidate == null)
          ? DayGeoMismatch.none
          : classifyDayGeoMatch(
              date: date,
              localOffset: Duration.zero,
              candidate: candidate,
              memoryLat: (widget.memory['lat'] as num?)?.toDouble(),
              memoryLon: (widget.memory['lon'] as num?)?.toDouble(),
            );

      setState(() {
        row.picked = picked;
        row.dayGeoMismatch = dayGeoMismatch;
        row.looksSame = looksLikeSamePhoto(candidate?.pHash, row.thumbHash);
        row.status = _RowStatus.picked;
        row.comparing = false;
      });
    } catch (e) {
      setState(() {
        row.comparing = false;
        _error = 'Could not process the selected photo.';
      });
    }
  }

  Future<void> _confirm(_UpgradeRow row) async {
    setState(() => row.status = _RowStatus.applying);
    final newUuid = await widget.notifier.replaceMemoryPhoto(
      _memoryId,
      row.oldUuid,
      row.picked!.bytes,
      row.picked!.filename,
    );
    if (!mounted) return;
    setState(() => row.status = newUuid != null ? _RowStatus.applied : _RowStatus.failed);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return AlertDialog(
      title: const Text('Upgrade photos'),
      content: SizedBox(
        width: 520,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                "Pick a higher-quality original from your device for each of this "
                "day's photos below.",
                style: theme.textTheme.bodyMedium,
              ),
              const SizedBox(height: 16),
              if (_immichConnected && _rows.isNotEmpty) ...[
                OutlinedButton.icon(
                  icon: _suggestingAll
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.auto_awesome, size: 18),
                  label: const Text('Suggest from Immich'),
                  onPressed: _suggestingAll ? null : _suggestAllRows,
                ),
                const SizedBox(height: 12),
              ],
              if (_error != null) ...[
                Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
                const SizedBox(height: 8),
              ],
              if (_rows.isEmpty)
                Text('This memory has no photos to upgrade.', style: theme.textTheme.bodyMedium)
              else
                for (final row in _rows) _rowTile(theme, row),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Close'),
        ),
      ],
    );
  }

  Widget _rowTile(ThemeData theme, _UpgradeRow row) {
    final hasPick = row.picked != null;
    final applied = row.status == _RowStatus.applied;
    final failed = row.status == _RowStatus.failed;
    final busy = row.status == _RowStatus.applying || row.comparing || row.immichBusy;

    final warnings = [
      if (hasPick && row.picked!.candidate == null) 'No date info found in this photo.',
      if (hasPick && row.dayGeoMismatch == DayGeoMismatch.wrongDay) "Doesn't look like this day.",
      if (hasPick && row.dayGeoMismatch == DayGeoMismatch.tooFarAway)
        "Taken more than ${kDefaultGeoToleranceKm.round()} km from this memory's location.",
      if (hasPick && row.looksSame == false) 'Looks different from the current photo.',
    ];
    final flagged = warnings.isNotEmpty;

    return Container(
      key: ValueKey('upgrade-row-${row.oldUuid}'),
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        border: Border.all(color: flagged ? kWarning : theme.dividerColor),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(6),
                child: Image.network(
                  widget.notifier.photoThumbUrl(_memoryId, row.oldUuid),
                  width: 56,
                  height: 56,
                  fit: BoxFit.cover,
                  headers: widget.notifier.photoAuthHeaders,
                  errorBuilder: (_, __, ___) => Container(
                    width: 56,
                    height: 56,
                    color: theme.colorScheme.surfaceContainerHighest,
                    child: const Icon(Icons.photo_outlined, size: 20),
                  ),
                ),
              ),
              if (hasPick) ...[
                const SizedBox(width: 6),
                Icon(Icons.arrow_forward, size: 16, color: theme.colorScheme.onSurfaceVariant),
                const SizedBox(width: 6),
                ClipRRect(
                  borderRadius: BorderRadius.circular(6),
                  child: Image.memory(
                    row.picked!.bytes,
                    width: 56,
                    height: 56,
                    fit: BoxFit.cover,
                    errorBuilder: (_, __, ___) => Container(
                      width: 56,
                      height: 56,
                      color: theme.colorScheme.surfaceContainerHighest,
                      child: const Icon(Icons.photo_outlined, size: 20),
                    ),
                  ),
                ),
              ],
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (hasPick)
                      Text(
                        row.picked!.filename,
                        style: theme.textTheme.bodySmall,
                        overflow: TextOverflow.ellipsis,
                      ),
                    if (failed)
                      Text('Upload failed', style: TextStyle(fontSize: 11, color: theme.colorScheme.error)),
                  ],
                ),
              ),
              if (applied)
                const Icon(Icons.check_circle, color: kSuccess)
              else if (!hasPick && row.immichBusy)
                const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 8),
                  child: SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                ),
            ],
          ),
          // A second row for actions (rather than cramming them into the
          // thumbnail row above) is also what the hasPick branch below does —
          // with the Immich buttons added, up to three actions need more
          // width than fits next to a 56px thumbnail + filename column. Wrap
          // (not Row) because even on their own line, three icon+label
          // buttons can still exceed the dialog's width at some font/text
          // scales -- it drops to a second line instead of overflowing.
          if (!hasPick && !applied && !row.immichBusy)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Wrap(
                alignment: WrapAlignment.end,
                crossAxisAlignment: WrapCrossAlignment.center,
                spacing: 6,
                runSpacing: 6,
                children: [
                  if (row.immichMatches.isNotEmpty)
                    OutlinedButton.icon(
                      style: OutlinedButton.styleFrom(minimumSize: const Size(0, 36)),
                      icon: const Icon(Icons.auto_awesome, size: 18),
                      label: const Text('Choose match'),
                      onPressed: busy ? null : () => _chooseImmichMatch(row),
                    ),
                  if (_immichConnected)
                    IconButton(
                      icon: const Icon(Icons.auto_awesome, size: 18),
                      tooltip: 'Suggest from Immich',
                      onPressed: busy ? null : () => _suggestForRow(row),
                    ),
                  OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(minimumSize: const Size(0, 36)),
                    icon: const Icon(Icons.add_photo_alternate_outlined, size: 18),
                    label: const Text('Select picture'),
                    onPressed: busy ? null : () => _pickForRow(row),
                  ),
                ],
              ),
            ),
          if (hasPick && !applied)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  if (row.immichMatches.isNotEmpty) ...[
                    OutlinedButton.icon(
                      style: OutlinedButton.styleFrom(minimumSize: const Size(0, 36)),
                      icon: const Icon(Icons.auto_awesome, size: 18),
                      label: const Text('Choose match'),
                      onPressed: busy ? null : () => _chooseImmichMatch(row),
                    ),
                    const SizedBox(width: 6),
                  ],
                  if (_immichConnected)
                    IconButton(
                      icon: const Icon(Icons.auto_awesome, size: 18),
                      tooltip: 'Suggest from Immich',
                      onPressed: busy ? null : () => _suggestForRow(row),
                    ),
                  TextButton(
                    onPressed: busy ? null : () => _pickForRow(row),
                    child: const Text('Change picture'),
                  ),
                  const SizedBox(width: 6),
                  ElevatedButton(
                    style: ElevatedButton.styleFrom(minimumSize: const Size(0, 36)),
                    onPressed: busy ? null : () => _confirm(row),
                    child: busy
                        ? const SizedBox(
                            width: 16,
                            height: 16,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Text('Confirm'),
                  ),
                ],
              ),
            ),
          if (warnings.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                warnings.join(' '),
                style: theme.textTheme.bodySmall?.copyWith(color: kWarning),
              ),
            ),
        ],
      ),
    );
  }
}
