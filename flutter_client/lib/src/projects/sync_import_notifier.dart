import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../api/client.dart';
import '../billing/billing_service.dart';
import '../core/project_ref.dart';
import '../crypto/encryption.dart';
import '../crypto/encryption_service.dart' show encryptionRefusalMessage;

class SyncImportNotifier extends ChangeNotifier {
  final ApiClient _api;
  final List<Map<String, dynamic>> stravaActivities;
  final List<Map<String, dynamic>> psSteps;

  final Set<dynamic> selectedStravaIds = {};
  final Set<int> selectedPsIds = {};

  bool isImporting = false;
  int importedCount = 0;
  int importTotal = 0;
  String? error;

  /// A plan-limit refusal from the last [importSelected] call, until consumed.
  /// Separate from [error] (issue #192): hitting a quota is not a failure the
  /// user caused, and the dialog should offer an upgrade, not a raw exception.
  QuotaError? quotaError;

  /// Take the pending refusal, clearing it — one 402, one prompt.
  QuotaError? takeQuotaError() {
    final quota = quotaError;
    quotaError = null;
    return quota;
  }

  // apiClient is injectable so tests can supply one backed by a MockClient.
  SyncImportNotifier({
    required this.stravaActivities,
    required this.psSteps,
    ApiClient? apiClient,
  }) : _api = apiClient ?? api {
    // Pre-select everything
    for (final a in stravaActivities) {
      final id = a['id'];
      if (id != null) selectedStravaIds.add(id);
    }
    for (final s in psSteps) {
      final id = s['id'];
      if (id is int) selectedPsIds.add(id);
    }
  }

  int get selectedCount => selectedStravaIds.length + selectedPsIds.length;

  // ── Strava selection ───────────────────────────────────────────────────────

  void toggleStrava(dynamic id) {
    if (selectedStravaIds.contains(id)) {
      selectedStravaIds.remove(id);
    } else {
      selectedStravaIds.add(id);
    }
    notifyListeners();
  }

  void selectAllStrava() {
    for (final a in stravaActivities) {
      final id = a['id'];
      if (id != null) selectedStravaIds.add(id);
    }
    notifyListeners();
  }

  void clearStrava() {
    selectedStravaIds.clear();
    notifyListeners();
  }

  // ── Polarsteps selection ───────────────────────────────────────────────────

  void togglePs(int id) {
    if (selectedPsIds.contains(id)) {
      selectedPsIds.remove(id);
    } else {
      selectedPsIds.add(id);
    }
    notifyListeners();
  }

  void selectAllPs() {
    for (final s in psSteps) {
      final id = s['id'];
      if (id is int) selectedPsIds.add(id);
    }
    notifyListeners();
  }

  void clearPs() {
    selectedPsIds.clear();
    notifyListeners();
  }

  // ── Import ─────────────────────────────────────────────────────────────────

  Future<int> importSelected(ProjectRef ref) async {
    final stravaToImport = stravaActivities
        .where((a) => selectedStravaIds.contains(a['id']))
        .toList();
    final psToImport = psSteps
        .where((s) => selectedPsIds.contains(s['id'] as int?))
        .toList();

    if (stravaToImport.isEmpty && psToImport.isEmpty) return 0;

    // Polarsteps memory text is encrypted when the user owns the trip and the
    // account is encrypted (#505); while the key is locked, an import with
    // steps doesn't start (#506). Strava activities alone need no key.
    // Ownership comes from the session's own id: the dialog's ref is built
    // from the URL and has no server role (its default says "owner").
    final owned =
        ref.resolveRoleFor(_api.tokenUserId?.toString()).role == 'owner';
    final blocked = owned && psToImport.isNotEmpty
        ? encryption.writeBlockedMessage
        : null;
    if (blocked != null) {
      error = blocked;
      notifyListeners();
      return 0;
    }

    isImporting = true;
    importedCount = 0;
    importTotal = stravaToImport.length + psToImport.length;
    error = null;
    quotaError = null;
    notifyListeners();

    int created = 0;
    try {
      // ── Strava: one batch POST ─────────────────────────────────────────
      if (stravaToImport.isNotEmpty) {
        try {
          final result = await _api.post(
            ref.path('/activities'),
            {'activities': stravaToImport},
          ) as Map<String, dynamic>;
          created += (result['added'] as int?) ?? 0;
        } on ApiException catch (e) {
          final quota = QuotaError.fromApiException(e);
          if (quota != null) {
            quotaError = quota;
          } else {
            error = e.toString().replaceFirst('Exception: ', '');
          }
        } on Exception catch (e) {
          error = e.toString().replaceFirst('Exception: ', '');
        }
        importedCount += stravaToImport.length;
        notifyListeners();
      }

      // ── Polarsteps: sequential (memory + photos per step) ─────────────
      for (final step in psToImport) {
        final date = step['date'] as String?;
        if (date == null) {
          importedCount++;
          notifyListeners();
          continue;
        }

        final name = (step['name'] as String?)?.isNotEmpty == true
            ? step['name'] as String
            : null;
        final description = step['description'] as String?;
        final lat = (step['lat'] as num?)?.toDouble();
        final lon = (step['lon'] as num?)?.toDouble();

        try {
          final encName = owned ? await encryption.protect(name) : name;
          final encDescription =
              owned ? await encryption.protect(description) : description;
          final result = await _api.post(ref.withOwner('/api/memories/'), {
            'project_name': ref.name,
            'date': date,
            'geo_mode': (lat != null && lon != null) ? 'custom' : 'start_of_day',
            if (encName != null) 'name': encName,
            if (encDescription != null) 'description': encDescription,
            if (lat != null) 'lat': lat,
            if (lon != null) 'lon': lon,
          }) as Map<String, dynamic>;

          final memId = result['id']?.toString();
          if (memId != null) {
            final photos =
                (step['photos'] as List?)?.cast<Map<String, dynamic>>() ?? [];
            for (final photo in photos) {
              final url = photo['url'] as String?;
              if (url == null || url.isEmpty) continue;
              try {
                await _uploadPhotoFromUrl(memId, url);
              } catch (_) {
                // Non-fatal
              }
            }
          }
          created++;
        } on Exception catch (e) {
          error = (e is ApiException
                  ? encryptionRefusalMessage(e.statusCode, e.body)
                  : null) ??
              e.toString().replaceFirst('Exception: ', '');
        }

        importedCount++;
        notifyListeners();
      }
    } finally {
      isImporting = false;
      notifyListeners();
    }
    return created;
  }

  Future<void> _uploadPhotoFromUrl(String memId, String photoUrl) async {
    final token = _api.tokenForUpload;
    if (token == null) return;
    final photoResp = await http.get(Uri.parse(photoUrl));
    if (photoResp.statusCode < 200 || photoResp.statusCode >= 300) return;
    final bytes = photoResp.bodyBytes;
    final filename =
        Uri.parse(photoUrl).pathSegments.lastOrNull ?? 'photo.jpg';
    final uploadUri =
        Uri.parse('${_api.baseUrl}/api/memories/$memId/photos');
    final request = http.MultipartRequest('POST', uploadUri)
      ..headers['Authorization'] = 'Bearer $token'
      ..files
          .add(http.MultipartFile.fromBytes('file', bytes, filename: filename));
    await request.send();
  }
}
