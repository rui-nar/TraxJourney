import 'package:flutter/foundation.dart';

import '../api/client.dart';
import '../core/project_ref.dart';
import '../crypto/encryption.dart';
import '../crypto/encryption_service.dart' show encryptionRefusalMessage;

/// One step that failed to import, with the reason shown in the "Details"
/// list on the completion summary (see [PolarstepsImportNotifier.failedSteps]).
class ImportFailure {
  final String stepName;
  final String reason;
  const ImportFailure(this.stepName, this.reason);
}

/// Per-step outcome of [PolarstepsImportNotifier._importStep], used to build
/// the batch summary in [PolarstepsImportNotifier.importSelected].
typedef _StepOutcome = ({
  bool success,
  String stepName,
  String? failReason,
  int photosAttempted,
  int photosSucceeded,
});

class PolarstepsImportNotifier extends ChangeNotifier {
  // Injectable so tests can supply an ApiClient backed by a MockClient.
  final ApiClient _api;

  PolarstepsImportNotifier({ApiClient? client}) : _api = client ?? api;

  // ── Project context ────────────────────────────────────────────────────────
  ProjectRef? _projectRef;
  set projectRef(ProjectRef value) => _projectRef = value;

  // ── Trips ──────────────────────────────────────────────────────────────────
  List<Map<String, dynamic>> trips = [];
  Map<String, dynamic>? selectedTrip;
  bool isLoadingTrips = false;

  // ── Steps ──────────────────────────────────────────────────────────────────
  List<Map<String, dynamic>> steps = [];
  final Set<int> selectedStepIds = {};
  final Set<int> alreadyImportedIds = {};
  bool isLoadingSteps = false;

  // ── Import ─────────────────────────────────────────────────────────────────
  bool isImporting = false;
  int importedCount = 0;
  int importTotal = 0;

  /// Steps that failed to import in the last [importSelected] run, with why —
  /// so a partial-batch failure is attributable instead of only showing up as
  /// a lower success count. Cleared at the start of each run.
  final List<ImportFailure> failedSteps = [];

  /// Count of successfully-imported steps in the last run whose memory was
  /// created but at least one photo failed to upload. Cleared at the start
  /// of each run.
  int stepsWithMissingPhotos = 0;

  String? error;
  bool polarstepsNotConnected = false;

  // ── Token expiry / reconnect ─────────────────────────────────────────────────
  /// True when a Polarsteps call failed because the remember_token expired.
  /// The screen shows an inline reconnect panel instead of a raw error.
  bool tokenExpired = false;

  /// True while a reconnect (+ resume) is in flight.
  bool reconnecting = false;

  /// The operation to re-run after a successful reconnect (the call that hit
  /// the 401), so the user resumes exactly where they were.
  Future<void> Function()? _resumeAction;

  /// A 401 from a Polarsteps endpoint (detail contains "polarsteps"), as
  /// opposed to an app-JWT 401 (detail "Token expired"/"Invalid token") which
  /// must NOT be treated as a Polarsteps reconnect.
  bool _isPolarstepsAuth(ApiException e) =>
      e.statusCode == 401 && e.body.toLowerCase().contains('polarsteps');

  // ── Load trips ─────────────────────────────────────────────────────────────

  Future<void> loadTrips() async {
    isLoadingTrips = true;
    error = null;
    polarstepsNotConnected = false;
    notifyListeners();
    try {
      final raw =
          await _api.get('/api/polarsteps/trips') as List<dynamic>;
      trips = raw.cast<Map<String, dynamic>>();
    } on ApiException catch (e) {
      if (_isPolarstepsAuth(e)) {
        tokenExpired = true;
        _resumeAction = loadTrips;
      } else if (e.statusCode == 400 &&
          e.body.toLowerCase().contains('not connected')) {
        polarstepsNotConnected = true;
      } else {
        error = e.toString().replaceFirst('Exception: ', '');
      }
    } on Exception catch (e) {
      error = e.toString().replaceFirst('Exception: ', '');
    } finally {
      isLoadingTrips = false;
      notifyListeners();
    }
  }

  // ── Select trip → load steps ───────────────────────────────────────────────

  Future<void> selectTrip(Map<String, dynamic> trip) async {
    selectedTrip = trip;
    steps = [];
    selectedStepIds.clear();
    alreadyImportedIds.clear();
    isLoadingSteps = true;
    error = null;
    notifyListeners();
    try {
      final tripId = trip['id'] as int;
      final ref = _projectRef;
      final projectParam = ref != null
          ? ref.withOwner('?project_name=${Uri.encodeComponent(ref.name)}')
          : '';
      final raw = await _api.get(
              '/api/polarsteps/trips/$tripId/steps$projectParam')
          as List<dynamic>;
      steps = raw.cast<Map<String, dynamic>>();

      // Identify already-imported steps
      for (final s in steps) {
        if (s['already_imported'] == true) {
          final id = s['id'];
          if (id is int) alreadyImportedIds.add(id);
        }
      }

      // Pre-select only new (not yet imported) steps
      for (final s in steps) {
        final id = s['id'];
        if (id is int && !alreadyImportedIds.contains(id)) {
          selectedStepIds.add(id);
        }
      }
    } on ApiException catch (e) {
      if (_isPolarstepsAuth(e)) {
        // Keep selectedTrip so the reconnect resumes by reloading these steps.
        tokenExpired = true;
        _resumeAction = () => selectTrip(trip);
      } else {
        error = e.toString().replaceFirst('Exception: ', '');
      }
    } on Exception catch (e) {
      error = e.toString().replaceFirst('Exception: ', '');
    } finally {
      isLoadingSteps = false;
      notifyListeners();
    }
  }

  void clearTrip() {
    selectedTrip = null;
    steps = [];
    selectedStepIds.clear();
    alreadyImportedIds.clear();
    notifyListeners();
  }

  // ── Reconnect after token expiry ─────────────────────────────────────────────

  /// Re-validate a freshly pasted remember_token, then resume the call that
  /// hit the 401. Returns true on success. On failure (invalid token), keeps
  /// the reconnect panel up and surfaces [error].
  Future<bool> reconnect(String token) async {
    final trimmed = token.trim();
    if (trimmed.isEmpty) return false;
    reconnecting = true;
    error = null;
    notifyListeners();
    try {
      await _api.post('/api/polarsteps/connect', {'remember_token': trimmed});
    } on ApiException catch (e) {
      error = _detail(e.body);
      reconnecting = false;
      notifyListeners();
      return false;
    } on Exception catch (e) {
      error = e.toString().replaceFirst('Exception: ', '');
      reconnecting = false;
      notifyListeners();
      return false;
    }

    // Token accepted — drop the expired state and resume where we left off.
    tokenExpired = false;
    final resume = _resumeAction;
    _resumeAction = null;
    notifyListeners();
    if (resume != null) await resume();
    reconnecting = false;
    notifyListeners();
    return true;
  }

  static String _detail(String body) {
    final m = RegExp(r'"detail"\s*:\s*"([^"]+)"').firstMatch(body);
    return m?.group(1) ?? body;
  }

  // ── Selection ──────────────────────────────────────────────────────────────

  void toggleStep(int id) {
    if (selectedStepIds.contains(id)) {
      selectedStepIds.remove(id);
    } else {
      selectedStepIds.add(id);
    }
    notifyListeners();
  }

  void selectAll() {
    for (final s in steps) {
      final id = s['id'];
      if (id is int && !alreadyImportedIds.contains(id)) selectedStepIds.add(id);
    }
    notifyListeners();
  }

  void clearSelection() {
    selectedStepIds.clear();
    notifyListeners();
  }

  // ── Import ─────────────────────────────────────────────────────────────────

  /// Import selected steps as memories into [ref]'s project.
  ///
  /// For each step: POST /api/memories/ then upload each photo.
  /// Returns the count of successfully created memories.
  /// Memory text is encrypted when the user owns the trip and the account is
  /// encrypted (#505); while the key is locked, it refuses to start (#506).
  Future<int> importSelected(ProjectRef ref) async {
    final toImport = steps
        .where((s) => selectedStepIds.contains(s['id'] as int?))
        .toList();
    if (toImport.isEmpty) return 0;

    // Who owns the trip, from the session's own id: the screen's ref is built
    // from the URL and has no server role (its default says "owner").
    final owned =
        ref.resolveRoleFor(_api.tokenUserId?.toString()).role == 'owner';
    final blocked = owned ? encryption.writeBlockedMessage : null;
    if (blocked != null) {
      error = blocked;
      notifyListeners();
      return 0;
    }

    isImporting = true;
    importedCount = 0;
    importTotal = toImport.length;
    error = null;
    failedSteps.clear();
    stepsWithMissingPhotos = 0;
    notifyListeners();

    int created = 0;
    const batchSize = 8;

    try {
      for (int start = 0; start < toImport.length; start += batchSize) {
        final batch = toImport.sublist(
          start,
          (start + batchSize).clamp(0, toImport.length),
        );

        final results = await Future.wait(
          batch.map((step) => _importStep(step, ref, owned: owned)),
        );

        for (final outcome in results) {
          if (outcome.success) {
            created++;
            if (outcome.photosSucceeded < outcome.photosAttempted) {
              stepsWithMissingPhotos++;
            }
          } else {
            failedSteps.add(ImportFailure(
              outcome.stepName,
              outcome.failReason ?? 'Unknown error',
            ));
          }
        }
        importedCount += batch.length;
        notifyListeners();
      }
    } finally {
      isImporting = false;
      notifyListeners();
    }
    return created;
  }

  Future<_StepOutcome> _importStep(
    Map<String, dynamic> step,
    ProjectRef ref, {
    required bool owned,
  }) async {
    final stepName = (step['name'] as String?)?.isNotEmpty == true
        ? step['name'] as String
        : 'Step ${step['id'] ?? '?'}';

    final date = step['date'] as String?;
    if (date == null) {
      return (
        success: false,
        stepName: stepName,
        failReason: 'Missing date',
        photosAttempted: 0,
        photosSucceeded: 0,
      );
    }

    final name = (step['name'] as String?)?.isNotEmpty == true
        ? step['name'] as String
        : null;
    final description = step['description'] as String?;
    final lat = (step['lat'] as num?)?.toDouble();
    final lon = (step['lon'] as num?)?.toDouble();
    final stepId = step['id'] as int?;

    try {
      // The trip owner's key encrypts memories; on another's trip they stay
      // plaintext (#505).
      final encName = owned ? await encryption.protect(name) : name;
      final encDescription =
          owned ? await encryption.protect(description) : description;
      final body = <String, dynamic>{
        'project_name': ref.name,
        'date': date,
        'geo_mode': (lat != null && lon != null) ? 'custom' : 'start_of_day',
        if (encName != null) 'name': encName,
        if (encDescription != null) 'description': encDescription,
        if (lat != null) 'lat': lat,
        if (lon != null) 'lon': lon,
        if (stepId != null) 'polarsteps_step_id': stepId,
      };
      final result = await _postWithRetry(ref.withOwner('/api/memories/'), body);
      final memId = result['id']?.toString();
      var photosAttempted = 0;
      var photosSucceeded = 0;
      if (memId != null) {
        // Photo uploads return 202 immediately (background download on server).
        // Fire all in parallel — no need to await sequentially. Each photo's
        // index in the step is sent as its `order`, so the server can place
        // it correctly even when downloads complete out of order. A failed
        // upload no longer vanishes silently — it's counted so the summary
        // can flag "some photos missing" for this step.
        final photos =
            (step['photos'] as List?)?.cast<Map<String, dynamic>>() ?? [];
        final toUpload = [
          for (final entry in photos.asMap().entries)
            if ((entry.value['url'] as String?)?.isNotEmpty == true) entry
        ];
        photosAttempted = toUpload.length;
        final photoOutcomes = await Future.wait([
          for (final entry in toUpload)
            _uploadPhotoFromUrl(memId, entry.value['url'] as String, entry.key)
                .then((_) => true)
                .catchError((_) => false),
        ]);
        photosSucceeded = photoOutcomes.where((ok) => ok).length;
      }
      return (
        success: true,
        stepName: stepName,
        failReason: null,
        photosAttempted: photosAttempted,
        photosSucceeded: photosSucceeded,
      );
    } on Exception catch (e) {
      return (
        success: false,
        stepName: stepName,
        failReason: (e is ApiException
                ? encryptionRefusalMessage(e.statusCode, e.body)
                : null) ??
            e.toString().replaceFirst('Exception: ', ''),
        photosAttempted: 0,
        photosSucceeded: 0,
      );
    }
  }

  Future<void> _uploadPhotoFromUrl(String memId, String photoUrl, int order) async {
    await _api.post(
      '/api/memories/$memId/photos/from-url',
      {'url': photoUrl, 'order': order},
    );
  }

  /// POST with automatic retry on 5xx — handles transient server errors
  /// (DB startup race, SQLite lock contention, temporary overload).
  Future<Map<String, dynamic>> _postWithRetry(
    String url,
    Map<String, dynamic> body, {
    int maxAttempts = 3,
  }) async {
    for (int attempt = 0; attempt < maxAttempts; attempt++) {
      try {
        return await _api.post(url, body) as Map<String, dynamic>;
      } on ApiException catch (e) {
        if (e.statusCode >= 500 && attempt < maxAttempts - 1) {
          await Future.delayed(Duration(milliseconds: 600 * (attempt + 1)));
          continue;
        }
        rethrow;
      }
    }
    throw StateError('unreachable');
  }
}
