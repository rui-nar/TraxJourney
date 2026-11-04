/// API client and models for "Upgrade photos from Immich" (issue #33, U12 of
/// docs/plans/IMMICH_ALBUM_UPGRADE_PLAN.md). Mirrors the shapes of the
/// server's `api/photo_upgrade.py`; the matching happens there, this side
/// only reads the review and sends decisions.
library;

import 'dart:convert';

import '../api/client.dart';
import '../core/project_ref.dart';

/// Error codes a failed row carries (plan Conventions), each with a sentence.
const kRetryableErrorCodes = {'immich_permission', 'immich_unreachable'};

String upgradeErrorSentence(String? code) => switch (code) {
      'too_large' => 'The original is larger than 25 MB, so it was left as it is.',
      'unsupported_format' => 'The original is in a format that cannot be used here.',
      'not_found_in_immich' => 'The photo is no longer in Immich.',
      'immich_permission' => 'Immich refused access. Reconnect in Settings with a '
          'valid API key that has the asset.download permission.',
      'immich_unreachable' => 'Immich could not be reached. Try again in a moment.',
      'photo_gone' => 'The photo is no longer in this trip.',
      'storage_full' => 'Your storage is full.',
      'invalid_image' => 'The original is not a readable image.',
      _ => 'This photo could not be replaced.',
    };

/// A failed call, reduced to what the screen shows.
class UpgradeFailure implements Exception {
  final int? statusCode;

  /// The `code` of a 409's `{code, message}` detail, else null.
  final String? code;
  final String message;

  /// 503: a broker is there but no worker takes photo jobs.
  bool get noWorker => statusCode == 503;

  /// 400 from the Immich routes: no Immich connection stored.
  bool get notConnected => statusCode == 400 && message.toLowerCase().contains('not connected');

  const UpgradeFailure(this.message, {this.statusCode, this.code});

  @override
  String toString() => message;
}

UpgradeFailure _failure(Object e) {
  if (e is ApiException) {
    String? code;
    var message = apiErrorReason(e) ?? '';
    try {
      final decoded = jsonDecode(e.body);
      final detail = decoded is Map ? decoded['detail'] : null;
      if (detail is Map) {
        code = detail['code'] as String?;
        message = detail['message'] as String? ?? message;
      }
    } on FormatException {
      // Not JSON: keep what apiErrorReason found.
    }
    if (message.isEmpty) message = 'Something went wrong (${e.statusCode}).';
    return UpgradeFailure(message, statusCode: e.statusCode, code: code);
  }
  return UpgradeFailure(apiErrorMessage(e));
}

// ── Models ────────────────────────────────────────────────────────────────────

int _int(Object? v) => (v as num?)?.toInt() ?? 0;

class UpgradeSummary {
  final Map<String, int> counts;
  final int confident;
  final int toCheck;
  final int selected;
  final int selectedBytesDelta;
  final int storageUsed;

  /// Null = unlimited.
  final int? storageLimit;

  const UpgradeSummary({
    required this.counts,
    required this.confident,
    required this.toCheck,
    required this.selected,
    required this.selectedBytesDelta,
    required this.storageUsed,
    this.storageLimit,
  });

  factory UpgradeSummary.fromJson(Map<String, dynamic> j) {
    final storage = (j['storage'] as Map?) ?? const {};
    final confidence = (j['confidence'] as Map?) ?? const {};
    return UpgradeSummary(
      counts: {
        for (final e in ((j['counts'] as Map?) ?? const {}).entries)
          e.key as String: _int(e.value),
      },
      confident: _int(confidence['high']),
      toCheck: _int(confidence['low']),
      selected: _int(j['selected']),
      selectedBytesDelta: _int(j['selected_bytes_delta']),
      storageUsed: _int(storage['used']),
      storageLimit: (storage['limit'] as num?)?.toInt(),
    );
  }

  int count(String state) => counts[state] ?? 0;

  /// Bytes left under the limit, or null when unlimited.
  int? get free => storageLimit == null ? null : storageLimit! - storageUsed;

  /// The ticked replacements would not fit.
  bool get overStorage => free != null && selectedBytesDelta > free!;
}

class UpgradeScan {
  final int id;
  final String albumId;
  final String? albumName;
  final bool includeMemories;
  final bool includeJournal;
  final String status;
  final String? stage;
  final int progressDone;
  final int progressTotal;
  final String? errorMessage;
  final String applyStatus;
  final String? applyStoppedReason;
  final UpgradeSummary? summary;

  /// Album photos that could not be read from Immich even after retries, so
  /// they could not be matched. Older servers omit it (read as 0).
  final int unreadablePhotos;

  const UpgradeScan({
    required this.id,
    required this.albumId,
    this.albumName,
    required this.includeMemories,
    required this.includeJournal,
    required this.status,
    this.stage,
    required this.progressDone,
    required this.progressTotal,
    this.errorMessage,
    required this.applyStatus,
    this.applyStoppedReason,
    this.summary,
    this.unreadablePhotos = 0,
  });

  factory UpgradeScan.fromJson(Map<String, dynamic> j) => UpgradeScan(
        id: _int(j['id']),
        albumId: j['album_id'] as String? ?? '',
        albumName: j['album_name'] as String?,
        includeMemories: j['include_memories'] == true,
        includeJournal: j['include_journal'] == true,
        status: j['status'] as String? ?? 'failed',
        stage: j['stage'] as String?,
        progressDone: _int(j['progress_done']),
        progressTotal: _int(j['progress_total']),
        errorMessage: j['error_message'] as String?,
        applyStatus: j['apply_status'] as String? ?? 'idle',
        applyStoppedReason: j['apply_stopped_reason'] as String?,
        summary: j['summary'] is Map
            ? UpgradeSummary.fromJson((j['summary'] as Map).cast<String, dynamic>())
            : null,
        unreadablePhotos: _int(j['unreadable_photos']),
      );

  UpgradeScan withSummary(UpgradeSummary s) => UpgradeScan(
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
        applyStatus: applyStatus,
        applyStoppedReason: applyStoppedReason,
        summary: s,
        unreadablePhotos: unreadablePhotos,
      );

  bool get isScanning => status == 'pending' || status == 'scanning';
  bool get applyActive => applyStatus == 'pending' || applyStatus == 'running';
}

class UpgradeLatest {
  final UpgradeScan? scan;
  final UpgradeScan? newer;

  /// Photos in the trip's memories; null unless the caller owns the trip.
  final int? memoryPhotos;
  final int journalPhotos;

  const UpgradeLatest({this.scan, this.newer, this.memoryPhotos, this.journalPhotos = 0});

  factory UpgradeLatest.fromJson(Map<String, dynamic> j) {
    final counts = (j['photo_counts'] as Map?) ?? const {};
    return UpgradeLatest(
      scan: j['scan'] is Map
          ? UpgradeScan.fromJson((j['scan'] as Map).cast<String, dynamic>())
          : null,
      newer: j['newer'] is Map
          ? UpgradeScan.fromJson((j['newer'] as Map).cast<String, dynamic>())
          : null,
      memoryPhotos: (counts['memories'] as num?)?.toInt(),
      journalPhotos: _int(counts['journal']),
    );
  }
}

class UpgradeSuggestion {
  final int id;
  final String kind; // memory | journal
  final int artifactId;
  final String photoUuid;
  final String currentUuid;
  final int position;
  final String date;
  final String state;
  final String? confidence;
  final bool selected;
  final String? assetId;
  final int? score;
  final int? assetBytes;
  final int? assetWidth;
  final int? assetHeight;
  final int? currentBytes;
  final int? currentWidth;
  final int? currentHeight;
  final String? errorCode;

  const UpgradeSuggestion({
    required this.id,
    required this.kind,
    required this.artifactId,
    required this.photoUuid,
    required this.currentUuid,
    required this.position,
    required this.date,
    required this.state,
    this.confidence,
    required this.selected,
    this.assetId,
    this.score,
    this.assetBytes,
    this.assetWidth,
    this.assetHeight,
    this.currentBytes,
    this.currentWidth,
    this.currentHeight,
    this.errorCode,
  });

  factory UpgradeSuggestion.fromJson(Map<String, dynamic> j) => UpgradeSuggestion(
        id: _int(j['id']),
        kind: j['artifact_kind'] as String? ?? 'memory',
        artifactId: _int(j['artifact_id']),
        photoUuid: j['photo_uuid'] as String? ?? '',
        currentUuid: j['current_uuid'] as String? ?? j['photo_uuid'] as String? ?? '',
        position: _int(j['position']),
        date: j['date'] as String? ?? '',
        state: j['state'] as String? ?? 'stale',
        confidence: j['confidence'] as String?,
        selected: j['selected'] == true,
        assetId: j['asset_id'] as String?,
        score: (j['score'] as num?)?.toInt(),
        assetBytes: (j['asset_bytes'] as num?)?.toInt(),
        assetWidth: (j['asset_width'] as num?)?.toInt(),
        assetHeight: (j['asset_height'] as num?)?.toInt(),
        currentBytes: (j['current_bytes'] as num?)?.toInt(),
        currentWidth: (j['current_width'] as num?)?.toInt(),
        currentHeight: (j['current_height'] as num?)?.toInt(),
        errorCode: j['error_code'] as String?,
      );

  bool get isMemory => kind == 'memory';
  bool get hasAsset => assetId != null;
  bool get isConfident => confidence == 'high';
}

class UpgradePage {
  final List<UpgradeSuggestion> items;
  final String? nextCursor;
  const UpgradePage(this.items, this.nextCursor);
}

class ImmichAlbum {
  final String id;
  final String name;
  final int? assetCount;
  final String? startDate;
  final String? endDate;
  final String? thumbUrl;
  final bool shared;

  const ImmichAlbum({
    required this.id,
    required this.name,
    this.assetCount,
    this.startDate,
    this.endDate,
    this.thumbUrl,
    this.shared = false,
  });

  factory ImmichAlbum.fromJson(Map<String, dynamic> j) => ImmichAlbum(
        id: j['id'] as String? ?? '',
        name: j['name'] as String? ?? '',
        assetCount: (j['asset_count'] as num?)?.toInt(),
        startDate: j['start_date'] as String?,
        endDate: j['end_date'] as String?,
        thumbUrl: j['thumb_url'] as String?,
        shared: j['shared'] == true,
      );
}

class AlbumCandidate {
  final String assetId;
  final String localDate;
  final int? score;
  final int? bytes;
  final int? width;
  final int? height;

  const AlbumCandidate({
    required this.assetId,
    required this.localDate,
    this.score,
    this.bytes,
    this.width,
    this.height,
  });

  factory AlbumCandidate.fromJson(Map<String, dynamic> j) => AlbumCandidate(
        assetId: j['asset_id'] as String? ?? '',
        localDate: j['local_date'] as String? ?? '',
        score: (j['score'] as num?)?.toInt(),
        bytes: (j['bytes'] as num?)?.toInt(),
        width: (j['width'] as num?)?.toInt(),
        height: (j['height'] as num?)?.toInt(),
      );
}

// ── Service ───────────────────────────────────────────────────────────────────

class AlbumUpgradeService {
  final ApiClient? _client;

  /// [client] is injectable for tests; production reads the global [api] at
  /// call time, so a server override swapped in at startup is honoured.
  AlbumUpgradeService({ApiClient? client}) : _client = client;

  ApiClient get _api => _client ?? api;

  String get baseUrl => _api.baseUrl;

  Map<String, String> get authHeaders {
    final token = _api.tokenForUpload;
    return token != null ? {'Authorization': 'Bearer $token'} : {};
  }

  Future<T> _run<T>(Future<T> Function() call) async {
    try {
      return await call();
    } on UpgradeFailure {
      rethrow;
    } catch (e) {
      throw _failure(e);
    }
  }

  Future<bool> immichConnected() => _run(() async {
        final data = await _api.get('/api/immich/status') as Map<String, dynamic>;
        return data['connected'] == true;
      });

  Future<List<ImmichAlbum>> albums() => _run(() async {
        final data = await _api.get('/api/immich/albums') as Map<String, dynamic>;
        return [
          for (final a in (data['albums'] as List? ?? const []))
            ImmichAlbum.fromJson((a as Map).cast<String, dynamic>()),
        ];
      });

  Future<UpgradeLatest> latest(ProjectRef ref) => _run(() async {
        final data = await _api.get(ref.path('/photo-upgrade/scans/latest'))
            as Map<String, dynamic>;
        return UpgradeLatest.fromJson(data);
      });

  Future<UpgradeScan> startScan(
    ProjectRef ref, {
    required String albumId,
    String? albumName,
    required bool includeMemories,
    required bool includeJournal,
  }) =>
      _run(() async {
        final data = await _api.post(ref.path('/photo-upgrade/scans'), {
          'album_id': albumId,
          if (albumName != null) 'album_name': albumName,
          'include_memories': includeMemories,
          'include_journal': includeJournal,
        }) as Map<String, dynamic>;
        return UpgradeScan.fromJson(data);
      });

  String _scan(int scanId) => '/api/photo-upgrade/scans/$scanId';

  Future<UpgradePage> suggestions(int scanId, {String filter = '', String? cursor}) =>
      _run(() async {
        final q = [
          if (filter.isNotEmpty) 'filter=$filter',
          if (cursor != null) 'cursor=${Uri.encodeQueryComponent(cursor)}',
        ].join('&');
        final data = await _api.get('${_scan(scanId)}/suggestions${q.isEmpty ? '' : '?$q'}')
            as Map<String, dynamic>;
        return UpgradePage([
          for (final s in (data['items'] as List? ?? const []))
            UpgradeSuggestion.fromJson((s as Map).cast<String, dynamic>()),
        ], data['next_cursor'] as String?);
      });

  /// PATCH one row; returns the row as it now is and the new summary.
  Future<(UpgradeSuggestion, UpgradeSummary)> patch(
    int scanId,
    int suggestionId, {
    bool? selected,
    String? assetId,
    bool? rejected,
  }) =>
      _run(() async {
        final data = await _api.patch('${_scan(scanId)}/suggestions/$suggestionId', {
          if (selected != null) 'selected': selected,
          if (assetId != null) 'asset_id': assetId,
          if (rejected != null) 'rejected': rejected,
        }) as Map<String, dynamic>;
        return (
          UpgradeSuggestion.fromJson((data['suggestion'] as Map).cast<String, dynamic>()),
          UpgradeSummary.fromJson((data['summary'] as Map).cast<String, dynamic>()),
        );
      });

  /// `which`: confident | all_proposed | none | failed.
  Future<UpgradeSummary> select(int scanId, String which) => _run(() async {
        final data = await _api.post('${_scan(scanId)}/select', {'which': which})
            as Map<String, dynamic>;
        return UpgradeSummary.fromJson(data);
      });

  Future<List<AlbumCandidate>> candidates(int scanId, int suggestionId) => _run(() async {
        final data = await _api.get(
                '${_scan(scanId)}/candidates?suggestion_id=$suggestionId')
            as Map<String, dynamic>;
        return [
          for (final c in (data['candidates'] as List? ?? const []))
            AlbumCandidate.fromJson((c as Map).cast<String, dynamic>()),
        ];
      });

  Future<UpgradeScan> apply(int scanId) => _run(() async {
        final data = await _api.post('${_scan(scanId)}/apply', const {})
            as Map<String, dynamic>;
        return UpgradeScan.fromJson(data);
      });

  Future<void> discard(int scanId) => _run(() => _api.delete(_scan(scanId)));

  // ── Image URLs (same-origin relative paths resolved against the API base) ──

  String currentThumbUrl(UpgradeSuggestion s) => s.isMemory
      ? '$baseUrl/api/memories/${s.artifactId}/photos/${s.currentUuid}/thumb'
      : '$baseUrl/api/journal/${s.artifactId}/photos/${s.currentUuid}/thumb';

  String currentFullUrl(UpgradeSuggestion s) => s.isMemory
      ? '$baseUrl/api/memories/${s.artifactId}/photos/${s.currentUuid}'
      : '$baseUrl/api/journal/${s.artifactId}/photos/${s.currentUuid}';

  /// [preview] asks for Immich's preview JPEG (about 1440 px) instead of the
  /// small thumbnail; the original is never fetched for display.
  String assetThumbUrl(String assetId, {bool preview = false}) =>
      '$baseUrl/api/immich/assets/$assetId/thumbnail${preview ? '?size=preview' : ''}';

  /// An album cover: the server hands back its own proxied relative path.
  String albumThumbUrl(String thumbUrl) => '$baseUrl$thumbUrl';
}
