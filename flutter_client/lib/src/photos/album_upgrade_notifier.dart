import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/project_ref.dart';
import 'album_upgrade_service.dart';

/// What the screen shows, derived from the notifier's data.
enum UpgradeView { loading, notConnected, error, setup, scanning, review }

/// State of the "Upgrade photos from Immich" screen (issue #33).
///
/// The server holds the truth (scan, review rows, apply progress); this keeps
/// a copy, polls while something is running, and sends decisions.
class AlbumUpgradeNotifier extends ChangeNotifier {
  final AlbumUpgradeService _service;

  /// How often a running scan or apply is re-read. The timer exists only while
  /// something is in progress and dies with the notifier (the page being left).
  final Duration pollInterval;

  AlbumUpgradeNotifier({AlbumUpgradeService? service, this.pollInterval = const Duration(seconds: 2)})
      : _service = service ?? AlbumUpgradeService();

  AlbumUpgradeService get service => _service;

  late ProjectRef _ref;
  bool _disposed = false;

  /// Called once when an apply that was running ends, so the screen can have
  /// the project reloaded and the new photos show (Do 9).
  VoidCallback? onApplyFinished;

  // ── Loaded state ───────────────────────────────────────────────────────────
  bool loaded = false;
  bool? connected;
  String? loadError;
  UpgradeScan? scan; // the newest ready scan, with its summary
  UpgradeScan? newer; // a newer scan that is not ready
  int? memoryPhotos;
  int journalPhotos = 0;

  /// 503: the server runs no worker for photo jobs.
  bool noWorker = false;

  /// Shown above the screen when an action was refused or failed.
  String? actionError;

  // ── Setup ──────────────────────────────────────────────────────────────────
  List<ImmichAlbum> albums = [];
  bool albumsLoading = false;
  String? albumsError;
  String? selectedAlbumId;
  bool includeMemories = false;
  bool includeJournal = false;
  bool choosingNew = false;
  bool starting = false;
  String? _tripStart;
  String? _tripEnd;

  // ── Review ─────────────────────────────────────────────────────────────────
  String filter = '';
  List<UpgradeSuggestion> rows = [];
  String? nextCursor;
  bool rowsLoading = false;
  final Set<int> busyRows = {};

  /// Whether some failed row failed for a reason "Retry failed" can fix.
  bool retryableFailed = false;
  int _failedChecked = -1;

  bool applyStarting = false;

  Timer? _timer;
  bool _refreshing = false;
  bool _applyWasActive = false;

  // ── Derived ────────────────────────────────────────────────────────────────

  UpgradeView get view {
    if (!loaded) return loadError != null ? UpgradeView.error : UpgradeView.loading;
    return _contentView;
  }

  /// The view once the first load is over (also used by [load] itself, to
  /// know what to fetch before it marks the load done).
  UpgradeView get _contentView {
    if (connected == false) return UpgradeView.notConnected;
    if (newer?.isScanning ?? false) return UpgradeView.scanning;
    if (scan != null && !choosingNew) return UpgradeView.review;
    return UpgradeView.setup;
  }

  UpgradeSummary? get summary => scan?.summary;
  bool get applyActive => scan?.applyActive ?? false;

  /// A scan newer than the review that failed (shown as a banner over it).
  UpgradeScan? get failedNewer => newer != null && newer!.status == 'failed' ? newer : null;

  // ── Loading ────────────────────────────────────────────────────────────────

  Future<void> load(ProjectRef ref, {String? tripStart, String? tripEnd}) async {
    _ref = ref;
    _tripStart = tripStart;
    _tripEnd = tripEnd;
    loaded = false;
    loadError = null;
    _notify();
    try {
      connected = await _service.immichConnected();
      if (connected!) {
        _apply(await _service.latest(ref));
        _applyWasActive = applyActive;
        _defaultCheckboxes();
        if (_contentView == UpgradeView.review) await _loadFirstPage();
        if (_contentView == UpgradeView.setup) await loadAlbums();
      }
      loaded = true;
    } on UpgradeFailure catch (e) {
      loadError = e.message;
    }
    _syncPolling();
    _notify();
  }

  void _apply(UpgradeLatest l) {
    final changedScan = l.scan?.id != scan?.id;
    scan = l.scan;
    newer = l.newer;
    memoryPhotos = l.memoryPhotos;
    journalPhotos = l.journalPhotos;
    if (changedScan) {
      rows = [];
      nextCursor = null;
      filter = '';
      _failedChecked = -1;
      retryableFailed = false;
    }
  }

  void _defaultCheckboxes() {
    includeMemories = (memoryPhotos ?? 0) > 0;
    includeJournal = journalPhotos > 0;
  }

  Future<void> loadAlbums() async {
    albumsLoading = true;
    albumsError = null;
    _notify();
    try {
      albums = _orderedAlbums(await _service.albums());
      final last = (newer ?? scan)?.albumId;
      if (selectedAlbumId == null && last != null && albums.any((a) => a.id == last)) {
        selectedAlbumId = last;
      }
    } on UpgradeFailure catch (e) {
      albumsError = e.message;
    }
    albumsLoading = false;
    _notify();
  }

  /// Albums overlapping the trip dates first, each group in the server's order.
  List<ImmichAlbum> _orderedAlbums(List<ImmichAlbum> all) {
    final start = _day(_tripStart);
    final end = _day(_tripEnd);
    if (start == null || end == null) return all;
    bool overlaps(ImmichAlbum a) {
      final s = _day(a.startDate);
      final e = _day(a.endDate);
      if (s == null || e == null) return false;
      return s.compareTo(end) <= 0 && e.compareTo(start) >= 0;
    }

    return [...all.where(overlaps), ...all.where((a) => !overlaps(a))];
  }

  /// `YYYY-MM-DD` of a date or datetime string, which sorts as text.
  static String? _day(String? s) => s != null && s.length >= 10 ? s.substring(0, 10) : null;

  bool albumOverlapsTrip(ImmichAlbum a) {
    final start = _day(_tripStart);
    final end = _day(_tripEnd);
    final s = _day(a.startDate);
    final e = _day(a.endDate);
    return start != null && end != null && s != null && e != null &&
        s.compareTo(end) <= 0 && e.compareTo(start) >= 0;
  }

  // ── Polling ────────────────────────────────────────────────────────────────

  bool get _needsPolling => (newer?.isScanning ?? false) || applyActive;

  void _syncPolling() {
    if (_needsPolling && !_disposed) {
      _timer ??= Timer.periodic(pollInterval, (_) => refresh());
    } else {
      _timer?.cancel();
      _timer = null;
    }
  }

  @visibleForTesting
  bool get isPolling => _timer != null;

  /// Re-reads the latest scan (and the review rows while an apply runs).
  Future<void> refresh() async {
    if (_refreshing || _disposed || !loaded) return;
    _refreshing = true;
    try {
      final wasScanning = newer?.isScanning ?? false;
      final wasApplying = _applyWasActive || applyActive;
      _apply(await _service.latest(_ref));
      if (_disposed) return;
      final nowApplying = applyActive;
      if (scan != null && (rows.isEmpty || wasApplying || nowApplying)) {
        await _reloadRows();
      }
      if (_disposed) return;
      if (wasScanning && !(newer?.isScanning ?? false)) choosingNew = false;
      _applyWasActive = nowApplying;
      if (wasApplying && !nowApplying) {
        unawaited(_checkFailed());
        onApplyFinished?.call();
      }
    } on UpgradeFailure catch (e) {
      // A blip while polling is not worth a banner; the next tick retries.
      debugPrint('photo upgrade poll failed: ${e.message}');
    } finally {
      _refreshing = false;
    }
    _syncPolling();
    _notify();
  }

  // ── Scan ───────────────────────────────────────────────────────────────────

  void newScan() {
    choosingNew = true;
    actionError = null;
    _notify();
    if (albums.isEmpty) loadAlbums();
  }

  void cancelNewScan() {
    choosingNew = false;
    _notify();
  }

  void selectAlbum(String id) {
    selectedAlbumId = id;
    _notify();
  }

  void setIncludeMemories(bool v) {
    includeMemories = v;
    _notify();
  }

  void setIncludeJournal(bool v) {
    includeJournal = v;
    _notify();
  }

  bool get canScan =>
      !starting && selectedAlbumId != null && (includeMemories || includeJournal);

  Future<void> startScan() => _startScan(
        albumId: selectedAlbumId!,
        albumName: albums.where((a) => a.id == selectedAlbumId).firstOrNull?.name,
        memories: includeMemories,
        journal: includeJournal,
      );

  /// "Try again" on a failed scan: the same album and scope.
  Future<void> retryScan() {
    final f = failedNewer!;
    return _startScan(
        albumId: f.albumId,
        albumName: f.albumName,
        memories: f.includeMemories,
        journal: f.includeJournal);
  }

  Future<void> _startScan({
    required String albumId,
    String? albumName,
    required bool memories,
    required bool journal,
  }) async {
    starting = true;
    actionError = null;
    noWorker = false;
    _notify();
    try {
      newer = await _service.startScan(_ref,
          albumId: albumId,
          albumName: albumName,
          includeMemories: memories,
          includeJournal: journal);
    } on UpgradeFailure catch (e) {
      _fail(e);
    }
    starting = false;
    _syncPolling();
    _notify();
  }

  void _fail(UpgradeFailure e) {
    if (e.noWorker) {
      noWorker = true;
    } else {
      actionError = e.message;
    }
  }

  // ── Review rows ────────────────────────────────────────────────────────────

  Future<void> _loadFirstPage() async {
    rowsLoading = true;
    _notify();
    try {
      final page = await _service.suggestions(scan!.id, filter: filter);
      rows = page.items;
      nextCursor = page.nextCursor;
      unawaited(_checkFailed());
    } on UpgradeFailure catch (e) {
      actionError = e.message;
    }
    rowsLoading = false;
  }

  /// Re-reads as many pages as are shown now, so rows that flipped during an
  /// apply update in place without the list jumping back to the top.
  Future<void> _reloadRows() async {
    final shown = rows.length;
    final out = <UpgradeSuggestion>[];
    String? cursor;
    do {
      final page = await _service.suggestions(scan!.id, filter: filter, cursor: cursor);
      out.addAll(page.items);
      cursor = page.nextCursor;
    } while (cursor != null && out.length < shown);
    rows = out;
    nextCursor = cursor;
  }

  Future<void> setFilter(String value) async {
    filter = filter == value ? '' : value;
    await _loadFirstPage();
    _notify();
  }

  Future<void> loadMore() async {
    final cursor = nextCursor;
    if (cursor == null || rowsLoading) return;
    rowsLoading = true;
    _notify();
    try {
      final page = await _service.suggestions(scan!.id, filter: filter, cursor: cursor);
      rows = [...rows, ...page.items];
      nextCursor = page.nextCursor;
    } on UpgradeFailure catch (e) {
      actionError = e.message;
    }
    rowsLoading = false;
    _notify();
  }

  /// Looks at the failed rows once per change of their count, to know whether
  /// "Retry failed" would put any back.
  Future<void> _checkFailed() async {
    final s = scan;
    final failed = s?.summary?.count('failed') ?? 0;
    if (s == null || failed == _failedChecked) return;
    _failedChecked = failed;
    if (failed == 0) {
      retryableFailed = false;
      return;
    }
    try {
      final page = await _service.suggestions(s.id, filter: 'failed');
      retryableFailed = page.items
          .any((r) => r.hasAsset && kRetryableErrorCodes.contains(r.errorCode));
    } on UpgradeFailure {
      _failedChecked = -1;
    }
    _notify();
  }

  // ── Decisions ──────────────────────────────────────────────────────────────

  Future<void> _patch(UpgradeSuggestion row,
      {bool? selected, String? assetId, bool? rejected}) async {
    busyRows.add(row.id);
    actionError = null;
    _notify();
    try {
      final (changed, sum) = await _service.patch(scan!.id, row.id,
          selected: selected, assetId: assetId, rejected: rejected);
      rows = [for (final r in rows) r.id == changed.id ? changed : r];
      scan = scan!.withSummary(sum);
    } on UpgradeFailure catch (e) {
      actionError = e.message;
      // The row changed under us (the apply took it): show what it is now.
      if (e.code == 'suggestion_changed' || e.code == 'suggestion_locked') {
        await _reloadRows().catchError((_) {});
      }
    }
    busyRows.remove(row.id);
    _notify();
  }

  Future<void> toggle(UpgradeSuggestion row) => _patch(row, selected: !row.selected);

  Future<void> choose(UpgradeSuggestion row, String assetId) => _patch(row, assetId: assetId);

  Future<void> reject(UpgradeSuggestion row) => _patch(row, rejected: true);

  Future<void> unreject(UpgradeSuggestion row) => _patch(row, rejected: false);

  Future<List<AlbumCandidate>> candidates(UpgradeSuggestion row) =>
      _service.candidates(scan!.id, row.id);

  Future<void> select(String which) async {
    actionError = null;
    try {
      final sum = await _service.select(scan!.id, which);
      scan = scan!.withSummary(sum);
      await _reloadRows();
      if (which == 'failed') {
        _failedChecked = -1;
        await _checkFailed();
      }
    } on UpgradeFailure catch (e) {
      actionError = e.message;
    }
    _notify();
  }

  Future<void> selectAllConfident() => select('confident');

  Future<void> clearSelection() => select('none');

  /// "Retry failed": puts back, ticked, the rows that failed because of the
  /// Immich key or connection (U10-R2-1). The bar then offers to replace them.
  Future<void> retryFailed() => select('failed');

  // ── Apply ──────────────────────────────────────────────────────────────────

  /// Starts the apply, or resumes a stopped one.
  Future<void> apply() async {
    applyStarting = true;
    actionError = null;
    noWorker = false;
    _notify();
    try {
      final started = await _service.apply(scan!.id);
      scan = scan!.copyApplyFrom(started);
      _applyWasActive = true;
    } on UpgradeFailure catch (e) {
      _fail(e);
    }
    applyStarting = false;
    _syncPolling();
    _notify();
  }

  // ── Plumbing ───────────────────────────────────────────────────────────────

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _timer?.cancel();
    _timer = null;
    super.dispose();
  }
}

extension on UpgradeScan {
  /// This scan with the apply fields of [other] (the POST /apply answer).
  UpgradeScan copyApplyFrom(UpgradeScan other) => UpgradeScan(
        id: id,
        albumId: albumId,
        albumName: albumName,
        includeMemories: includeMemories,
        includeJournal: includeJournal,
        status: status,
        stage: stage,
        progressDone: progressDone,
        progressTotal: progressTotal,
        errorMessage: errorMessage,
        applyStatus: other.applyStatus,
        applyStoppedReason: other.applyStoppedReason,
        summary: summary,
      );
}
