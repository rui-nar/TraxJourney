/// Mixin providing Memory CRUD operations to ProjectNotifier.
///
/// Depends on abstract members satisfied by ProjectNotifier's fields and
/// two thin public delegates (reloadDetailsOnly, errorMessage) that
/// forward to the private helpers in the notifier's library.
library;

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../api/client.dart';
import '../core/project_ref.dart';
import '../crypto/e2ee_crypto.dart' show EncryptedField;
import '../crypto/encryption.dart';
import '../crypto/encryption_service.dart' show encryptionRefusalMessage;
import '../crypto/undecrypted_fields.dart';
import 'project_quota_mixin.dart';

/// Thrown by [ProjectMemoryCrudMixin.fetchTranslation] when a memory is
/// encrypted — a permanent state, not a transient failure, so callers should
/// show a distinct message rather than the generic "please try again" (#27).
class TranslationUnavailableException implements Exception {}

/// Monotonic counter backing createMemory's optimistic placeholder ids — a
/// counter (rather than a timestamp alone) guarantees two concurrent creates
/// never collide even if they land in the same clock tick.
int _optimisticMemoryIdCounter = 0;

mixin ProjectMemoryCrudMixin on ChangeNotifier, ProjectQuotaMixin {
  // ── Abstract: project state (satisfied by ProjectNotifier fields) ─────────
  ProjectRef? get projectRef;
  List<Map<String, dynamic>> get items;
  set items(List<Map<String, dynamic>> v);
  UndecryptedFields get undecryptedFields;
  String? get error;
  set error(String? v);

  /// Details-only reload — thin delegate to the private helper in the notifier.
  Future<void> reloadDetailsOnly(ProjectRef ref);

  /// Formats an Exception into a user-readable string — delegates to _msg.
  String errorMessage(Exception e);

  // ── Memory CRUD ───────────────────────────────────────────────────────────

  /// A trip's memories are shared with everyone on it, so they are encrypted
  /// under the trip owner's key: this device encrypts them only on a trip
  /// the user owns (#505). A companion's memory text stays plaintext, which
  /// the owner and the other travellers can read.
  bool get _memoriesUseOwnKey => projectRef?.role == 'owner';

  /// Why a memory can't be saved from this device now, or null when it can:
  /// the user owns the trip, the account is encrypted and the key is not
  /// unlocked (#506). The memory editor disables Save with this message.
  String? get memoryWriteBlockedMessage =>
      _memoriesUseOwnKey ? encryption.writeBlockedMessage : null;

  Future<String?> _protectMemoryText(String? value) async =>
      _memoriesUseOwnKey ? await encryption.protect(value) : value;

  /// [errorMessage], with the server's encryption refusals in plain words.
  String _memoryErrorMessage(Exception e) =>
      (e is ApiException ? encryptionRefusalMessage(e.statusCode, e.body) : null) ??
      errorMessage(e);

  /// Creates a memory. Returns `true` on success, `false` on failure (in
  /// which case the optimistic placeholder is rolled back and [error] is
  /// set) — callers must check this before treating the save as done.
  Future<bool> createMemory({
    required String date,
    required String geoMode,
    String? name,
    String? time,
    String? description,
    double? lat,
    double? lon,
    int? insertAfterIndex,
  }) async {
    final ref = projectRef;
    if (ref == null) return false;
    final blocked = memoryWriteBlockedMessage;
    if (blocked != null) {
      error = blocked;
      notifyListeners();
      return false;
    }
    // Unique per call so two concurrent creates never share a placeholder id
    // (a literal '__optimistic__' would collide and produce duplicate
    // ValueKeys in the map marker layer).
    final tempId = 'optimistic-${_optimisticMemoryIdCounter++}';
    final placeholder = {
      'item_type': 'memory',
      'memory': {
        'id': tempId,
        'name': name,
        'date': date,
        'time': time,
        'description': description,
        'photos': <String>[],
        'geo_mode': geoMode,
        'lat': lat,
        'lon': lon,
      },
    };
    final insertAt = insertAfterIndex != null
        ? (insertAfterIndex + 1).clamp(0, items.length)
        : items.length;
    // New list object, not an in-place insert: map_panel's marker cache and
    // ProjectNotifier's dayStats/orderedDayKeys caches invalidate via
    // identical(items, _last...), which a same-object mutation never trips.
    final newItems = List.of(items);
    newItems.insert(insertAt, placeholder);
    items = newItems;
    notifyListeners();
    try {
      final encName = await _protectMemoryText(name);
      final encDescription = await _protectMemoryText(description);
      await api.post(ref.withOwner('/api/memories/'), {
        'project_name': ref.name,
        'date': date,
        'geo_mode': geoMode,
        if (encName != null) 'name': encName,
        if (time != null) 'time': time,
        if (encDescription != null) 'description': encDescription,
        if (lat != null) 'lat': lat,
        if (lon != null) 'lon': lon,
        if (insertAfterIndex != null) 'insert_after_index': insertAfterIndex,
      });
      await reloadDetailsOnly(ref);
      return true;
    } on Exception catch (e) {
      // Roll back the placeholder so a failed create leaves no phantom item.
      items = items
          .where((item) =>
              !(item['item_type'] == 'memory' &&
                item['memory']?['id']?.toString() == tempId))
          .toList();
      error = _memoryErrorMessage(e);
      notifyListeners();
      return false;
    }
  }

  /// Updates a memory. Returns `true` on success, `false` on failure (in
  /// which case [error] is set), like [createMemory].
  Future<bool> updateMemory(
    String memoryId, {
    required String date,
    required String geoMode,
    String? name,
    String? time,
    String? description,
    double? lat,
    double? lon,
    bool keepStoredName = false,
    bool keepStoredDescription = false,
  }) async {
    if (projectRef == null) return false;
    final blocked = memoryWriteBlockedMessage;
    if (blocked != null) {
      error = blocked;
      notifyListeners();
      return false;
    }
    // keepStored*: the editor hands back the stored envelope it could not
    // decrypt, untouched; it is resent as it is, never encrypted again. A
    // value that is not a well-formed envelope is text the user typed, and is
    // encrypted like every other value (#466).
    final nameAsStored =
        keepStoredName && name != null && EncryptedField.isWellFormed(name);
    final descriptionAsStored = keepStoredDescription &&
        description != null &&
        EncryptedField.isWellFormed(description);
    if (!nameAsStored) undecryptedFields.remove('memory', memoryId, 'name');
    if (!descriptionAsStored) {
      undecryptedFields.remove('memory', memoryId, 'description');
    }
    // New list + new item map, not an in-place mutation of the existing
    // item — see createMemory's comment above for why identity matters here.
    final newItems = List.of(items);
    for (var i = 0; i < newItems.length; i++) {
      final item = newItems[i];
      if (item['item_type'] == 'memory' &&
          item['memory']?['id']?.toString() == memoryId) {
        final mem = Map<String, dynamic>.from(item['memory'] as Map);
        mem['name'] = name;
        mem['date'] = date;
        mem['time'] = time;
        mem['description'] = description;
        mem['geo_mode'] = geoMode;
        mem['lat'] = lat;
        mem['lon'] = lon;
        newItems[i] = {...item, 'memory': mem};
        break;
      }
    }
    items = newItems;
    notifyListeners();
    try {
      final encName = nameAsStored ? name : await _protectMemoryText(name);
      final encDescription = descriptionAsStored
          ? description
          : await _protectMemoryText(description);
      await api.put('/api/memories/$memoryId', {
        'date': date,
        'geo_mode': geoMode,
        if (encName != null) 'name': encName,
        if (time != null) 'time': time,
        if (encDescription != null) 'description': encDescription,
        if (lat != null) 'lat': lat,
        if (lon != null) 'lon': lon,
      });
      // No reload needed — optimistic update already applied above.
      return true;
    } on Exception catch (e) {
      error = _memoryErrorMessage(e);
      notifyListeners();
      return false;
    }
  }

  void removeMemoryLocally(String memoryId) {
    items = items
        .where((item) =>
            !(item['item_type'] == 'memory' &&
              item['memory']?['id']?.toString() == memoryId))
        .toList();
    notifyListeners();
  }

  Future<void> deleteMemory(String memoryId) async {
    if (projectRef == null) return;
    removeMemoryLocally(memoryId);
    try {
      await api.delete('/api/memories/$memoryId');
      // No reload needed — memory already removed locally above.
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
    }
  }

  /// Upload a photo for a memory. Returns the UUID string on success.
  Future<String?> uploadMemoryPhoto(
    String memoryId,
    Uint8List bytes,
    String filename,
  ) async {
    final token = api.tokenForUpload;
    if (token == null) return null;
    final uri = Uri.parse('${api.baseUrl}/api/memories/$memoryId/photos');
    final request = http.MultipartRequest('POST', uri)
      ..headers['Authorization'] = 'Bearer $token'
      ..files.add(http.MultipartFile.fromBytes('file', bytes, filename: filename));
    try {
      final streamed = await request.send();
      final res = await http.Response.fromStream(streamed);
      if (res.statusCode >= 200 && res.statusCode < 300) {
        final match = RegExp(r'"uuid"\s*:\s*"([^"]+)"').firstMatch(res.body);
        return match?.group(1);
      }
      // A plan limit (issue #121) must not vanish into this null — record it so
      // the dialog can offer an upgrade instead of dropping the photo silently.
      recordQuotaRefusal(res.statusCode, res.body);
      return null;
    } catch (_) {
      return null;
    }
  }

  Future<void> deleteMemoryPhoto(
    String memoryId,
    String photoUuid, {
    bool reload = true,
  }) async {
    final ref = projectRef;
    try {
      await api.delete('/api/memories/$memoryId/photos/$photoUuid');
      if (reload && ref != null) await reloadDetailsOnly(ref);
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
    }
  }

  /// Replace a photo's bytes in place for a memory (issue #33 photo
  /// upgrade). Returns the new UUID on success and swaps it into local
  /// state at the same position — the server preserves photo order, so no
  /// reload is needed, matching updateMemory's optimistic-update style.
  Future<String?> replaceMemoryPhoto(
    String memoryId,
    String oldPhotoUuid,
    Uint8List bytes,
    String filename,
  ) async {
    final token = api.tokenForUpload;
    if (token == null) return null;
    final uri = Uri.parse(
        '${api.baseUrl}/api/memories/$memoryId/photos/$oldPhotoUuid/replace');
    final request = http.MultipartRequest('PUT', uri)
      ..headers['Authorization'] = 'Bearer $token'
      ..files.add(http.MultipartFile.fromBytes('file', bytes, filename: filename));
    try {
      final streamed = await request.send();
      final res = await http.Response.fromStream(streamed);
      if (res.statusCode < 200 || res.statusCode >= 300) {
        recordQuotaRefusal(res.statusCode, res.body);
        return null;
      }
      final match = RegExp(r'"uuid"\s*:\s*"([^"]+)"').firstMatch(res.body);
      final newUuid = match?.group(1);
      if (newUuid == null) return null;

      final newItems = List.of(items);
      for (var i = 0; i < newItems.length; i++) {
        final item = newItems[i];
        if (item['item_type'] == 'memory' &&
            item['memory']?['id']?.toString() == memoryId) {
          final mem = Map<String, dynamic>.from(item['memory'] as Map);
          final photos =
              List<String>.from((mem['photos'] as List?)?.cast<String>() ?? []);
          final idx = photos.indexOf(oldPhotoUuid);
          if (idx != -1) photos[idx] = newUuid;
          mem['photos'] = photos;
          newItems[i] = {...item, 'memory': mem};
          break;
        }
      }
      items = newItems;
      notifyListeners();
      return newUuid;
    } catch (_) {
      return null;
    }
  }

  // ── Comments ────────────────────────────────────────────────────────────────

  Future<List<Map<String, dynamic>>> fetchComments(String memoryId) async {
    final data = await api.get('/api/memories/$memoryId/comments');
    return (data as List).cast<Map<String, dynamic>>();
  }

  Future<void> addComment(
    String memoryId,
    String text, {
    int? parentCommentId,
  }) async {
    await api.post('/api/memories/$memoryId/comments', {
      'text': text,
      if (parentCommentId != null) 'parent_comment_id': parentCommentId,
    });
  }

  Future<void> deleteComment(String memoryId, int commentId) async {
    await api.delete('/api/memories/$memoryId/comments/$commentId');
  }

  // ── Likes ───────────────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> fetchLikes(String memoryId) async {
    final data = await api.get('/api/memories/$memoryId/likes');
    return data as Map<String, dynamic>;
  }

  Future<void> likeMemory(String memoryId) async {
    await api.post('/api/memories/$memoryId/like', {});
  }

  Future<void> unlikeMemory(String memoryId) async {
    await api.delete('/api/memories/$memoryId/like');
  }

  // ── Translations ─────────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> fetchTranslation(
    String memoryId,
    String langCode,
  ) async {
    // When encryption is on, the server only holds ciphertext and cannot
    // translate it (#26/#27). Don't send ciphertext to the translator — surface
    // a distinct "unavailable" error so the UI can show a message that doesn't
    // invite a retry (the server also rejects this independently, see #27).
    if (encryption.isUnlocked) {
      throw TranslationUnavailableException();
    }
    final data = await api.get('/api/memories/$memoryId/translations/$langCode');
    return data as Map<String, dynamic>;
  }
}
