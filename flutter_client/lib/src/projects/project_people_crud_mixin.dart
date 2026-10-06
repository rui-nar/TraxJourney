/// Mixin providing People + Encounter CRUD to ProjectNotifier (issue #40).
library;

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../api/client.dart';
import '../core/project_ref.dart';
import 'facets/project_facet.dart';
import 'project_quota_mixin.dart';

/// The steps of a Polarsteps trip that can actually be drawn — those carrying
/// coordinates. A trip whose steps are all coordinate-less yields an empty
/// list, which callers must treat as "nothing to show" rather than as an
/// (invisible) overlay (#123).
List<Map<String, dynamic>> mappablePolarstepsSteps(List<dynamic> raw) => raw
    .cast<Map<String, dynamic>>()
    .where((s) => s['lat'] != null && s['lon'] != null)
    .toList();

/// Monotonic counter backing createEncounter's optimistic placeholder ids — a
/// counter (rather than a literal id) guarantees two concurrent creates never
/// collide even if they land in the same clock tick.
int _optimisticEncounterIdCounter = 0;

mixin ProjectPeopleCrudMixin on ChangeNotifier, ProjectQuotaMixin {
  // ── Abstract: project state (satisfied by ProjectNotifier fields) ─────────
  ProjectRef? get projectRef;
  /// The content facet's writer — satisfied by ProjectNotifier's.
  ItemsFacetWriter get itemsFacetWriter;
  List<Map<String, dynamic>> get _items => itemsFacetWriter.facet.items;
  List<Map<String, dynamic>> get _people => itemsFacetWriter.facet.people;
  List<Map<String, dynamic>> get _groups => itemsFacetWriter.facet.groups;
  String? get error;
  set error(String? v);

  Future<void> reloadDetailsOnly(ProjectRef ref);
  String errorMessage(Exception e);

  /// Replaces the item list with one without the items [drop] matches, when
  /// there are any: the list is left as it is otherwise, so deleting a person
  /// or group with no encounters changes people only (Decision 23 of
  /// docs/CLIENT_STATE_MAP_PLAN.md).
  void _dropItemsWhere(bool Function(Map<String, dynamic> item) drop) {
    final kept = _items.where((it) => !drop(it)).toList();
    if (kept.length != _items.length) itemsFacetWriter.setItems(kept);
  }

  // ── People CRUD ───────────────────────────────────────────────────────────

  /// Create a person; returns the new id, or null on failure.
  Future<int?> createPerson({
    String? name,
    String? email,
    String? phone,
    String? notes,
    List<Map<String, String>>? socials,
    List<String>? nationalities,
    String? residence,
  }) async {
    final ref = projectRef;
    if (ref == null) return null;
    try {
      final res = await api.post(ref.withOwner('/api/people/'), {
        'project_name': ref.name,
        if (name != null) 'name': name,
        if (email != null) 'email': email,
        if (phone != null) 'phone': phone,
        if (notes != null) 'notes': notes,
        if (socials != null) 'socials': socials,
        if (nationalities != null) 'nationalities': nationalities,
        if (residence != null) 'residence': residence,
      });
      await reloadDetailsOnly(ref);
      return (res as Map)['id'] as int?;
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
      return null;
    }
  }

  /// Update a person. Returns `true` on success, `false` on failure (with
  /// [error] set) — callers must check this before treating the save as done.
  Future<bool> updatePerson(
    int personId, {
    String? name,
    String? email,
    String? phone,
    String? notes,
    List<Map<String, String>>? socials,
    List<String>? nationalities,
    String? residence,
  }) async {
    final ref = projectRef;
    if (ref == null) return false;
    try {
      await api.put('/api/people/$personId', {
        'name': name,
        'email': email,
        'phone': phone,
        'notes': notes,
        'socials': socials,
        'nationalities': nationalities,
        'residence': residence,
      });
      await reloadDetailsOnly(ref);
      return true;
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
      return false;
    }
  }

  /// City autocomplete for the residence field — proxied through the server's
  /// Nominatim endpoint. Returns [] on failure or a too-short query.
  Future<List<String>> searchPlaces(String query) async {
    final q = query.trim();
    if (q.length < 2) return [];
    try {
      final res = await api.get('/api/geo/places?q=${Uri.encodeQueryComponent(q)}');
      return (res as List).cast<String>();
    } on Exception {
      return [];
    }
  }

  Future<void> deletePerson(int personId) async {
    final ref = projectRef;
    if (ref == null) return;
    // Optimistic: drop the person and any of their encounter items.
    itemsFacetWriter.setPeople(_people.where((p) => p['id'] != personId).toList());
    _dropItemsWhere((it) => it['item_type'] == 'encounter' &&
        it['encounter']?['person_id'] == personId);
    notifyListeners();
    try {
      await api.delete('/api/people/$personId');
      await reloadDetailsOnly(ref);
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
    }
  }

  /// Fetch a person with their encounters (date, place, note).
  Future<Map<String, dynamic>?> fetchPerson(int personId) async {
    try {
      final res = await api.get('/api/people/$personId');
      return (res as Map).cast<String, dynamic>();
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
      return null;
    }
  }

  /// Upload/replace a person's avatar; reloads on success.
  Future<bool> uploadPersonAvatar(
    int personId,
    Uint8List bytes,
    String filename,
  ) async {
    final token = api.tokenForUpload;
    final ref = projectRef;
    if (token == null || ref == null) return false;
    final uri = Uri.parse('${api.baseUrl}/api/people/$personId/avatar');
    final request = http.MultipartRequest('POST', uri)
      ..headers['Authorization'] = 'Bearer $token'
      ..files.add(http.MultipartFile.fromBytes('file', bytes, filename: filename));
    try {
      final streamed = await request.send();
      final res = await http.Response.fromStream(streamed);
      if (res.statusCode >= 200 && res.statusCode < 300) {
        await reloadDetailsOnly(ref);
        return true;
      }
      // A plan limit (issue #121) must not vanish into this false — record it
      // so the screen can offer an upgrade instead of doing nothing visible.
      recordQuotaRefusal(res.statusCode, res.body);
      return false;
    } catch (_) {
      return false;
    }
  }

  // ── Encounter CRUD ────────────────────────────────────────────────────────

  /// Create an encounter with a person OR a group met (issue #56 — caller
  /// guarantees exactly one of [personId]/[groupId] is set).
  /// Creates an encounter. Returns `true` on success, `false` on failure (in
  /// which case the optimistic placeholder is rolled back and [error] is
  /// set) — callers must check this before treating the save as done.
  Future<bool> createEncounter({
    int? personId,
    int? groupId,
    required String date,
    required String geoMode,
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
    final tempId = 'optimistic-${_optimisticEncounterIdCounter++}';
    final placeholder = {
      'item_type': 'encounter',
      'encounter': {
        'id': tempId,
        'person_id': personId,
        'group_id': groupId,
        'date': date,
        'time': time,
        'description': description,
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
      await api.post(ref.withOwner('/api/encounters/'), {
        'project_name': ref.name,
        'person_id': personId,
        'group_id': groupId,
        'date': date,
        'geo_mode': geoMode,
        if (time != null) 'time': time,
        if (description != null) 'description': description,
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
              !(item['item_type'] == 'encounter' &&
                item['encounter']?['id']?.toString() == tempId))
          .toList());
      error = errorMessage(e);
      notifyListeners();
      return false;
    }
  }

  /// Update an encounter's person/group, date, place, or note (issue #56 —
  /// caller guarantees exactly one of [personId]/[groupId] is set). Returns
  /// `true` on success, `false` on failure (with [error] set) — callers must
  /// check this before treating the save as done.
  Future<bool> updateEncounter(
    String encounterId, {
    int? personId,
    int? groupId,
    required String date,
    required String geoMode,
    String? time,
    String? description,
    double? lat,
    double? lon,
  }) async {
    final ref = projectRef;
    if (ref == null) return false;
    try {
      await api.put('/api/encounters/$encounterId', {
        'person_id': personId,
        'group_id': groupId,
        'date': date,
        'geo_mode': geoMode,
        if (time != null) 'time': time,
        if (description != null) 'description': description,
        if (lat != null) 'lat': lat,
        if (lon != null) 'lon': lon,
      });
      await reloadDetailsOnly(ref);
      return true;
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
      return false;
    }
  }

  /// Removes an encounter from local state only — no API call. Used both by
  /// [deleteEncounter]'s optimistic step and directly by the swipe-to-dismiss
  /// undo flow (activity_panel.dart), which needs the map pin gone
  /// immediately, before the undo window's delayed confirm ever calls
  /// [deleteEncounter] itself.
  void removeEncounterLocally(String encounterId) {
    itemsFacetWriter.setItems(_items
        .where((item) =>
            !(item['item_type'] == 'encounter' &&
              item['encounter']?['id']?.toString() == encounterId))
        .toList());
    notifyListeners();
  }

  Future<void> deleteEncounter(String encounterId) async {
    final ref = projectRef;
    if (ref == null) return;
    removeEncounterLocally(encounterId);
    try {
      await api.delete('/api/encounters/$encounterId');
      await reloadDetailsOnly(ref);
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
    }
  }

  // ── Polarsteps: view a person's shared trip (#40 follow-up) ─────────────────
  // View-only overlay: steps of a followed person's trip, fetched on demand and
  // rendered on the manage map. Never persisted into the project.

  /// Step points of the currently-displayed Polarsteps trip overlay (each with
  /// `lat`/`lon`/`date`/`name`), or empty when no overlay is shown. Root
  /// state, held by ProjectNotifier.
  List<Map<String, dynamic>> get polarstepsOverlaySteps;
  set polarstepsOverlaySteps(List<Map<String, dynamic>> value);

  /// Label for the current overlay (e.g. "Alice · Asia 2024"), or null.
  String? get polarstepsOverlayLabel;
  set polarstepsOverlayLabel(String? value);

  /// Fetch a person's shared Polarsteps trips. Returns null on failure (error set).
  Future<List<Map<String, dynamic>>?> fetchPersonPolarstepsTrips(
      int personId) async {
    try {
      final res = await api.get('/api/people/$personId/polarsteps/trips');
      return (res as List).cast<Map<String, dynamic>>();
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
      return null;
    }
  }

  /// Load one trip's steps and show it as the map overlay. Returns false on
  /// failure — including a trip whose steps carry no coordinates, which would
  /// otherwise "succeed" into an invisible overlay (#123).
  Future<bool> showPersonPolarstepsTrip(
      int personId, int tripId, String label) async {
    try {
      final res =
          await api.get('/api/people/$personId/polarsteps/trips/$tripId/steps');
      final steps = mappablePolarstepsSteps(res as List);
      if (steps.isEmpty) {
        error = 'That trip has no mapped steps yet';
        notifyListeners();
        return false;
      }
      polarstepsOverlaySteps = steps;
      polarstepsOverlayLabel = label;
      notifyListeners();
      return true;
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
      return false;
    }
  }

  void clearPolarstepsOverlay() {
    if (polarstepsOverlaySteps.isEmpty && polarstepsOverlayLabel == null) return;
    polarstepsOverlaySteps = [];
    polarstepsOverlayLabel = null;
    notifyListeners();
  }

  // ── Group CRUD (issue #50) ──────────────────────────────────────────────────

  /// Create a group; returns the new id, or null on failure.
  Future<int?> createGroup({
    String? name,
    List<String>? nationalities,
    List<Map<String, String>>? socials,
  }) async {
    final ref = projectRef;
    if (ref == null) return null;
    try {
      final res = await api.post(ref.withOwner('/api/groups/'), {
        'project_name': ref.name,
        if (name != null) 'name': name,
        if (nationalities != null) 'nationalities': nationalities,
        if (socials != null) 'socials': socials,
      });
      await reloadDetailsOnly(ref);
      return (res as Map)['id'] as int?;
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
      return null;
    }
  }

  /// Update a group. Returns `true` on success, `false` on failure (with
  /// [error] set) — callers must check this before treating the save as done.
  Future<bool> updateGroup(
    int groupId, {
    String? name,
    List<String>? nationalities,
    List<Map<String, String>>? socials,
  }) async {
    final ref = projectRef;
    if (ref == null) return false;
    try {
      await api.put('/api/groups/$groupId', {
        'name': name,
        'nationalities': nationalities,
        'socials': socials,
      });
      await reloadDetailsOnly(ref);
      return true;
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
      return false;
    }
  }

  Future<void> deleteGroup(int groupId) async {
    final ref = projectRef;
    if (ref == null) return;
    // Optimistic: drop the group, its direct group-encounters (issue #56 —
    // unlike a member, they have no fallback), and ungroup remaining members.
    itemsFacetWriter.setGroups(_groups.where((g) => g['id'] != groupId).toList());
    _dropItemsWhere((it) => it['item_type'] == 'encounter' &&
        it['encounter']?['group_id'] == groupId);
    for (final p in _people) {
      if (p['group_id'] == groupId) p['group_id'] = null;
    }
    notifyListeners();
    try {
      await api.delete('/api/groups/$groupId');
      await reloadDetailsOnly(ref);
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
    }
  }

  /// Set the group's member list to exactly [personIds] (assigns them, clears
  /// others). Returns `true` on success, `false` on failure (with [error]
  /// set) — callers must check this before treating the save as done.
  Future<bool> setGroupMembers(int groupId, List<int> personIds) async {
    final ref = projectRef;
    if (ref == null) return false;
    try {
      await api.put('/api/groups/$groupId/members', {'person_ids': personIds});
      await reloadDetailsOnly(ref);
      return true;
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
      return false;
    }
  }

  /// Fetch a group with its members (id, name, avatar).
  Future<Map<String, dynamic>?> fetchGroup(int groupId) async {
    try {
      final res = await api.get('/api/groups/$groupId');
      return (res as Map).cast<String, dynamic>();
    } on Exception catch (e) {
      error = errorMessage(e);
      notifyListeners();
      return null;
    }
  }
}
