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
import 'facets/project_facet.dart';
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
  /// The content facet's writer — satisfied by ProjectNotifier's.
  ItemsFacetWriter get itemsFacetWriter;
  List<Map<String, dynamic>> get _items => itemsFacetWriter.facet.items;
  String? get error;
  set error(String? v);

  /// Details-only reload — thin delegate to the private helper in the notifier.
  Future<void> reloadDetailsOnly(ProjectRef ref);

  /// Formats an Exception into a user-readable string — delegates to _msg.
  String errorMessage(Exception e);

  // ── Memory CRUD ───────────────────────────────────────────────────────────

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
        ? (insertAfterIndex + 1).clamp(0, _items.length)
        : _items.length;
    // New list object, not an in-place insert: map_panel's marker cache and
    // ProjectNotifier's dayStats/orderedDayKeys caches invalidate via
    // identical(items, _last...), which a same-object mutation never trips.
    final newItems = List.of(_items);
    newItems.insert(insertAt, placeholder);
    itemsFacetWriter.setItems(newItems);
    notifyListeners();
    try {
      final encName = await encryption.protect(name);
      final encDescription = await encryption.protect(description);
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
      itemsFacetWriter.setItems(_items
          .where((item) =>
              !(item['item_type'] == 'memory' &&
                item['memory']?['id']?.toString() == tempId))
          .toList());
      error = errorMessage(e);
      notifyListeners();
      return false;
    }
  }

  Future<void> updateMemory(
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
    if (projectRef == null) return;
    // keepStored*: the editor hands back the stored envelope it could not
    // decrypt, untouched; it is resent as it is, never encrypted again. A
    // value that is not a well-formed envelope is text the user typed, and is
    // encrypted like every other value (#466).
    final nameAsStored =
        keepStoredName && name != null && EncryptedField.isWellFormed(name);
    final descriptionAsStored = keepStoredDescription &&
        description != null &&
        EncryptedField.isWellFormed(description);
    if (!nameAsStored) itemsFacetWriter.forgetUndecrypted('memory', memoryId, 'name');
    if (!descriptionAsStored) {
      itemsFacetWriter.forgetUndecrypted('memory', memoryId, 'description');
    }
    // New list + new item map, not an in-place mutation of the existing
    // item — see createMemory's comment above for why identity matters here.
    final newItems = List.of(_items);
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
    itemsFacetWriter.setItems(newItems);
    notifyListeners();
    try {
      final encName = nameAsStored ? name : await encryption.protect(name);
      final encDescription = descriptionAsStored
          ? description
          : await encryption.protect(description);
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
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
    }
  }

  void removeMemoryLocally(String memoryId) {
    itemsFacetWriter.setItems(_items
        .where((item) =>
            !(item['item_type'] == 'memory' &&
              item['memory']?['id']?.toString() == memoryId))
        .toList());
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

      final newItems = List.of(_items);
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
      itemsFacetWriter.setItems(newItems);
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
