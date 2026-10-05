/// Mixin providing Segment CRUD operations to ProjectNotifier.
///
/// Also owns the geo-patch helpers (upsertSegmentInGeo, removeSegmentFromGeo)
/// and the great-circle / segment-feature geometry, which were only ever used
/// by segment operations.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';

import '../api/client.dart';
import '../core/project_ref.dart';
import 'project_data_cache.dart';
import 'project_service.dart';

mixin ProjectSegmentCrudMixin on ChangeNotifier {
  // ── Abstract: project state (satisfied by ProjectNotifier fields) ──────────
  ProjectRef? get projectRef;
  List<Map<String, dynamic>> get items;
  set items(List<Map<String, dynamic>> v);
  Map<String, dynamic>? get geo;
  set geo(Map<String, dynamic>? v);
  String? get error;
  set error(String? v);

  /// Access to the underlying ProjectService — satisfied by the notifier's
  /// `get service => _service` delegate.
  ProjectService get service;

  /// Details-only reload — thin delegate to _silentReloadDetailsOnly.
  Future<void> reloadDetailsOnly(ProjectRef ref);

  /// Format an Exception into a user-readable string — delegates to _msg.
  String errorMessage(Exception e);

  /// False once the notifier is disposed — satisfied by ProjectNotifier.
  bool get isAlive;

  /// If [e] is a 409 optimistic-lock conflict, resync items + geo from the
  /// server (discarding the optimistic change) and surface a soft retry
  /// message. Returns true when the conflict was handled.
  Future<bool> _resyncOnConflict(Object e, ProjectRef ref) async {
    if (e is! ApiException || e.statusCode != 409) return false;
    // The account this conflict belongs to (I1-R3-1, issue #418). The trip
    // check below compares name and owner, and an own trip has no owner, so
    // after a sign-out the next account's trip of the same name passes it.
    final scope = projectDataCache.scope;
    try {
      await reloadDetailsOnly(ref);
      final fetched =
          await fetchServerGeo(() => service.getGeo(ref, bypassCache: true));
      // The overlay belongs to whatever trip is open now, under this account.
      if (projectDataCache.scope == scope && _sameTrip(projectRef, ref)) {
        reconcileSegmentOverlay(fetched.geo, requestedAt: fetched.requestedAt);
        geo = {
          'type': 'FeatureCollection',
          'features': mergePendingSegmentPatches(
              List<dynamic>.from(fetched.geo['features'] as List? ?? [])),
        };
      }
    } catch (_) {
      // Best-effort resync; the next load will reconcile regardless.
    }
    // Signed out meanwhile: the message is not the open trip's either.
    if (projectDataCache.scope != scope) return true;
    error = 'This trip changed elsewhere — refreshed from server, please retry';
    notifyListeners();
    return true;
  }

  // ── Segment CRUD ───────────────────────────────────────────────────────────

  Future<String> addSegment({
    required String segmentType,
    required String label,
    required double startLat,
    required double startLon,
    required double endLat,
    required double endLon,
    int? insertAfterIndex,
    String? date,
    String? trainNumber,
    String? hafasProvider,
  }) async {
    final ref = projectRef;
    if (ref == null) return '';
    final placeholder = {
      'item_type': 'segment',
      'segment': {
        'id': '__optimistic__',
        'segment_type': segmentType,
        'label': label,
        'date': date,
        'start': {'lat': startLat, 'lon': startLon},
        'end':   {'lat': endLat,   'lon': endLon},
      },
    };
    final insertAt = insertAfterIndex != null
        ? (insertAfterIndex + 1).clamp(0, items.length)
        : items.length;
    // Assign a new list so identical() in the panel detects the change.
    final newItems = List<Map<String, dynamic>>.from(items);
    newItems.insert(insertAt, placeholder);
    items = newItems;
    notifyListeners();
    try {
      final result = await api.post(
        ref.path('/segments'),
        {
          'segment_type': segmentType,
          'label': label,
          'start_lat': startLat,
          'start_lon': startLon,
          'end_lat': endLat,
          'end_lon': endLon,
          if (insertAfterIndex != null) 'insert_after_index': insertAfterIndex,
          if (date != null) 'date': date,
          if (trainNumber != null) 'train_number': trainNumber,
          if (hafasProvider != null) 'hafas_provider': hafasProvider,
        },
      ) as Map<String, dynamic>;
      final newId = result['id'] as String;
      // Replace the optimistic placeholder with the confirmed segment,
      // creating a new list so identical() in the panel triggers a rebuild.
      items = [
        for (final item in items)
          if (item['item_type'] == 'segment' &&
              item['segment']?['id'] == '__optimistic__')
            {
              'item_type': 'segment',
              'segment': {
                'id': newId,
                'segment_type': segmentType,
                'label': label,
                'date': date,
                'start': {'lat': startLat, 'lon': startLon},
                'end': {'lat': endLat, 'lon': endLon},
              },
            }
          else
            item,
      ];
      upsertSegmentInGeo(newId, _segmentFeature(
          newId, segmentType, label, startLat, startLon, endLat, endLon));
      notifyListeners();
      return newId;
    } on Exception catch (e) {
      // Roll back the optimistic placeholder so a failed create leaves no ghost.
      items = items
          .where((item) => !(item['item_type'] == 'segment' &&
              item['segment']?['id'] == '__optimistic__'))
          .toList();
      removeSegmentFromGeo('__optimistic__');
      _segmentTombstones.remove('__optimistic__'); // not a real server segment
      if (await _resyncOnConflict(e, ref)) return '';
      error = errorMessage(e);
      notifyListeners();
      return '';
    }
  }

  Future<void> updateSegment(
    String segId, {
    required String segmentType,
    required String label,
    required double startLat,
    required double startLon,
    required double endLat,
    required double endLon,
    String? date,
    String? trainNumber,
    String? hafasProvider,
    String? routeMode,
  }) async {
    final ref = projectRef;
    if (ref == null) return;
    String? prevRouteMode;
    double? prevStartLat, prevStartLon, prevEndLat, prevEndLon;
    Map<String, dynamic>? prevSegment;  // full snapshot for rollback on error
    // Build a new list (new reference) so identical() in the panel fires.
    items = [
      for (final item in items)
        if (item['item_type'] == 'segment' &&
            item['segment']?['id']?.toString() == segId)
          () {
            final old = item['segment'] as Map;
            prevSegment   = Map<String, dynamic>.from(old);
            prevRouteMode = old['route_mode'] as String?;
            prevStartLat  = (old['start']?['lat'] as num?)?.toDouble();
            prevStartLon  = (old['start']?['lon'] as num?)?.toDouble();
            prevEndLat    = (old['end']?['lat'] as num?)?.toDouble();
            prevEndLon    = (old['end']?['lon'] as num?)?.toDouble();
            return {
              'item_type': 'segment',
              'segment': {
                ...Map<String, dynamic>.from(old),
                'segment_type': segmentType,
                'label': label,
                'date': date,
                'start': {'lat': startLat, 'lon': startLon},
                'end': {'lat': endLat, 'lon': endLon},
                if (routeMode != null) 'route_mode': routeMode,
              },
            };
          }()
        else
          item,
    ];
    notifyListeners();
    try {
      await api.put(
        ref.path('/segments/${Uri.encodeComponent(segId)}'),
        {
          'segment_type': segmentType,
          'label': label,
          'start_lat': startLat,
          'start_lon': startLon,
          'end_lat': endLat,
          'end_lon': endLon,
          if (date != null) 'date': date,
          if (trainNumber != null) 'train_number': trainNumber,
          if (hafasProvider != null) 'hafas_provider': hafasProvider,
          if (routeMode != null) 'route_mode': routeMode,
        },
      );
      final coordsChanged = prevStartLat != startLat || prevStartLon != startLon ||
          prevEndLat != endLat || prevEndLon != endLon;
      final resetToGreatCircle = coordsChanged || routeMode == 'great_circle';
      if (resetToGreatCircle || (prevRouteMode != 'rail' &&
                                  prevRouteMode != 'ferry' &&
                                  prevRouteMode != 'bus')) {
        upsertSegmentInGeo(segId, _segmentFeature(
            segId, segmentType, label, startLat, startLon, endLat, endLon));
      }
      notifyListeners();
    } on Exception catch (e) {
      // Roll back the optimistic edit so the UI doesn't drift from the server.
      if (prevSegment != null) {
        items = [
          for (final item in items)
            if (item['item_type'] == 'segment' &&
                item['segment']?['id']?.toString() == segId)
              {'item_type': 'segment', 'segment': prevSegment}
            else
              item,
        ];
      }
      if (await _resyncOnConflict(e, ref)) return;
      error = errorMessage(e);
      notifyListeners();
    }
  }

  /// Save a manually edited route track (add/remove/move/trim, issue #150) for
  /// [segId]. [payload] is [TrackEditModel.toSavePayload]. The server echoes
  /// back the canonical stored route fields, which are patched straight into
  /// [items]/[geo] — no full reload needed since segments are already fully
  /// loaded client-side (unlike activities' meta-only list).
  Future<void> saveSegmentTrack(
    String segId, Map<String, dynamic> payload,
  ) async {
    final ref = projectRef;
    if (ref == null) return;
    final result = await service.saveSegmentTrack(ref, segId, payload);
    applyResolvedSegment(segId, result);
    notifyListeners();
  }

  /// Trigger async OSM route resolution for a train, boat, or bus segment and
  /// wait until it completes.
  ///
  /// The server marks the segment `route_status="pending"` and returns 202
  /// immediately (the HAFAS + Overpass work runs in a background task), so this
  /// method optimistically flips the segment to `pending` — driving a spinner on
  /// the tile — then hands it to the trip's resolve poller
  /// ([pollSegmentResolution]).
  ///
  /// Returns a result map `{route_status, …}`: `resolved`, or `cancelled` when
  /// the trip was left or the segment deleted first. Throws on a `failed`
  /// resolution so callers can surface the server's error message.
  Future<Map<String, dynamic>> resolveTrainRoute(
    String segId, {
    String routeMode = 'rail',
    String? hafasProvider,
    String? trainNumber,
    String? date,
    bool force = false,
  }) async {
    final ref = projectRef;
    if (ref == null) throw Exception('No project open');
    await service.resolveTrainRoute(
      ref, segId,
      hafasProvider: hafasProvider,
      trainNumber: trainNumber,
      date: date,
      force: force,
    );
    _patchSegmentFields(segId, {
      'route_status': 'pending',
      'route_error': null,
      if (trainNumber != null) 'train_number': trainNumber,
      if (hafasProvider != null) 'hafas_provider': hafasProvider,
    });
    notifyListeners();
    return pollSegmentResolution(segId);
  }

  // ── Resolve polling (issue #278) ──────────────────────────────────────────
  //
  // One poller per trip, over every segment whose resolve is pending, rather
  // than a loop per segment with a deadline. The server runs two resolve jobs
  // at a time, so a third one queued behind them routinely outlived the old
  // 2-minute deadline, and nothing ever polled for it again: the route only
  // reached the map once the trip was reopened. There is no deadline now — a
  // job stuck server-side is the server's to report (it logs route jobs
  // pending for over 30 minutes) — and the poll backs off instead, so a
  // forgotten tab costs one /meta a minute.

  /// The running poller, or null when no resolve is pending.
  _ResolvePoll? _resolvePoll;

  /// Wait for [segId]'s pending resolve to finish, polling `/meta` for it
  /// along with every other pending segment of this trip.
  ///
  /// On `resolved`, patches the segment's route into [items] and [geo] and
  /// returns `{route_status: resolved, …}`. On `failed`, throws with the
  /// server's error message. Returns `{route_status: cancelled}` when the trip
  /// is left (another trip opened, [resetSegmentState]) or the segment is
  /// deleted first.
  Future<Map<String, dynamic>> pollSegmentResolution(String segId) {
    final ref = projectRef;
    if (ref == null || !isAlive) {
      return Future.value(const {'route_status': 'cancelled'});
    }
    final poll = _joinResolvePoll(ref, segId);
    return poll.waiters.putIfAbsent(segId, Completer.new).future;
  }

  /// Resume polling for every segment in [items] the server reports as still
  /// resolving — after a load, so a resolve started before the trip was
  /// opened, or on another device, still reaches the map. Stops a poller left
  /// running for another trip.
  void resumeSegmentResolves() {
    final ref = projectRef;
    if (ref == null) return;
    final poll = _resolvePoll;
    if (poll != null && !_sameTrip(ref, poll.trip)) stopSegmentResolvePolling();
    for (final item in items) {
      final seg = item['segment'];
      if (item['item_type'] == 'segment' &&
          seg is Map &&
          seg['route_status'] == 'pending' &&
          seg['id'] != null) {
        _joinResolvePoll(ref, seg['id'].toString());
      }
    }
  }

  /// Stop the poller. Anyone still waiting on a segment gets `cancelled`.
  void stopSegmentResolvePolling() {
    final poll = _resolvePoll;
    _resolvePoll = null;
    if (poll == null) return;
    poll.timer?.cancel();
    for (final waiter in poll.waiters.values) {
      waiter.complete(const {'route_status': 'cancelled'});
    }
  }

  /// A trip is the same trip whatever the caller's role on it: the role is
  /// corrected from the server mid-session, and [ProjectRef.==] includes it.
  static bool _sameTrip(ProjectRef? a, ProjectRef b) =>
      a != null && a.name == b.name && a.ownerId == b.ownerId;

  /// The wait before the next poll, by time spent polling since the last
  /// segment joined: quick while a fresh resolve may land any second, then
  /// backing off for one queued behind others or stuck server-side.
  static Duration _resolvePollDelay(Duration elapsed) {
    if (elapsed < const Duration(minutes: 2)) return const Duration(seconds: 3);
    if (elapsed < const Duration(minutes: 10)) return const Duration(seconds: 15);
    return const Duration(seconds: 60);
  }

  /// Add [segId] to the poller for [ref], starting one if needed. A joining
  /// segment restarts the quick schedule.
  _ResolvePoll _joinResolvePoll(ProjectRef ref, String segId) {
    var poll = _resolvePoll;
    if (poll != null && !_sameTrip(ref, poll.trip)) {
      stopSegmentResolvePolling();
      poll = null;
    }
    poll ??= _resolvePoll = _ResolvePoll(ref);
    // Only a poll sent after this point may judge the segment: one already
    // in flight can carry the state from before the resolve was requested.
    poll.segments[segId] = poll.polls;
    poll.elapsed = Duration.zero;
    if (!poll.busy) _scheduleResolvePoll(poll);
    return poll;
  }

  void _scheduleResolvePoll(_ResolvePoll poll) {
    // A disposed notifier — discarded at an account change (issue #418) —
    // keeps no poller: its waiters get `cancelled`, as on leaving the trip.
    if (!isAlive) {
      stopSegmentResolvePolling();
      return;
    }
    poll.timer?.cancel();
    final delay = _resolvePollDelay(poll.elapsed);
    poll.timer = Timer(delay, () {
      poll.timer = null;
      poll.elapsed += delay;
      _pollResolves(poll);
    });
  }

  Future<void> _pollResolves(_ResolvePoll poll) async {
    final ref = projectRef;
    if (!_sameTrip(ref, poll.trip)) {
      stopSegmentResolvePolling();
      return;
    }
    poll.busy = true;
    final pollNo = ++poll.polls;
    Map<String, dynamic>? meta;
    try {
      meta = await service.getDetailsMeta(ref!);
    } on Exception {
      meta = null; // transient network error — retry on the next tick
    }
    if (!identical(_resolvePoll, poll)) return; // stopped meanwhile
    poll.busy = false;
    if (!_sameTrip(projectRef, poll.trip)) {
      stopSegmentResolvePolling();
      return;
    }
    if (meta != null) {
      var changed = false;
      for (final entry in poll.segments.entries.toList()) {
        final segId = entry.key;
        if (entry.value >= pollNo) continue; // joined after this poll was sent
        final seg = _segmentFromMeta(meta, segId);
        final stat = seg == null ? null : seg['route_status'] as String?;
        if (stat == 'pending') continue;
        poll.segments.remove(segId);
        final waiter = poll.waiters.remove(segId);
        changed = true;
        if (seg == null) {
          waiter?.complete(const {'route_status': 'cancelled'}); // deleted
        } else if (stat == 'resolved') {
          applyResolvedSegment(segId, seg);
          waiter?.complete(_resolvedOutcome(seg));
        } else if (stat == 'failed') {
          _patchSegmentFields(segId, {
            'route_status': 'failed',
            'route_error': seg['route_error'],
          });
          waiter?.completeError(
              Exception(seg['route_error'] ?? 'Route resolution failed'));
        } else {
          // No longer resolving, nor resolved: the segment was edited back to
          // a plain line elsewhere. Show what the server has; nothing to wait on.
          _patchSegmentFields(segId, {'route_status': stat});
          waiter?.complete(const {'route_status': 'cancelled'});
        }
      }
      if (changed) notifyListeners();
    }
    if (poll.segments.isEmpty) {
      stopSegmentResolvePolling();
    } else {
      _scheduleResolvePoll(poll);
    }
  }

  /// What [pollSegmentResolution] returns for a resolved segment.
  Map<String, dynamic> _resolvedOutcome(Map<String, dynamic> seg) => {
        'route_status': 'resolved',
        'stop_count': _polylineLength(seg),
        // True when the server fell back to a straight endpoint chord (no real
        // track found) — surfaced so the UI doesn't claim a detailed route.
        'degraded': seg['route_degraded'] == true,
        // True when the HAFAS lookup for the selected train failed and this
        // resolved via the generic two-point OSM fallback instead — distinct
        // from `degraded`.
        'hafas_failed': seg['route_hafas_failed'] == true,
        // On a HAFAS fallback the server keeps the provider's own reason here
        // so the UI can say *why* the train lookup failed (issue #277).
        'route_error': seg['route_error'],
      };

  /// Merge [fields] into the matching segment in [items], assigning a new list
  /// reference so identity-based rebuilds fire.
  void _patchSegmentFields(String segId, Map<String, dynamic> fields) {
    items = [
      for (final item in items)
        if (item['item_type'] == 'segment' &&
            item['segment']?['id']?.toString() == segId)
          {
            'item_type': 'segment',
            'segment': {
              ...Map<String, dynamic>.from(item['segment'] as Map),
              ...fields,
            },
          }
        else
          item,
    ];
  }

  /// Find the `segment` sub-map for [segId] in a `/meta` response, or null.
  Map<String, dynamic>? _segmentFromMeta(Map<String, dynamic> meta, String segId) {
    for (final item in (meta['items'] as List? ?? const [])) {
      if (item is Map &&
          item['item_type'] == 'segment' &&
          item['segment']?['id']?.toString() == segId) {
        return Map<String, dynamic>.from(item['segment'] as Map);
      }
    }
    return null;
  }

  /// Apply a resolved (or manually edited) segment route into [items] and [geo].
  /// Used both after polling an auto-resolve to completion and after a manual
  /// track edit saves (issue #150) — both hand back the same route fields.
  @visibleForTesting
  void applyResolvedSegment(String segId, Map<String, dynamic> segMeta) {
    final routeMode = segMeta['route_mode'] as String? ?? 'rail';
    final degraded = segMeta['route_degraded'] == true;
    _patchSegmentFields(segId, {
      'route_mode': routeMode,
      'route_status': 'resolved',
      'route_error': null,
      'route_degraded': degraded,
      'route_hafas_failed': segMeta['route_hafas_failed'] == true,
      'route_edited': segMeta['route_edited'] == true,
      'route_polyline': segMeta['route_polyline'],
      if (segMeta['train_number'] != null) 'train_number': segMeta['train_number'],
      if (segMeta['hafas_provider'] != null) 'hafas_provider': segMeta['hafas_provider'],
    });
    final coords = _decodePolyline(segMeta['route_polyline']);
    if (coords.isEmpty) return;
    final polyline = segMeta['route_polyline'];
    upsertSegmentInGeo(segId, {
      'type': 'Feature',
      'geometry': {'type': 'LineString', 'coordinates': coords},
      'properties': {
        'type': 'segment',
        'segment_id': segId,
        'route_mode': routeMode,
        'route_degraded': degraded,
        // What [_sameRouteState] checks a server feature against before it
        // lets this patch go: the server's `route_hash` is the same CRC-32 of
        // the same stored string.
        'route_status': 'resolved',
        if (polyline is String) 'route_hash': _crc32(polyline),
        if (segMeta['segment_type'] != null) 'segment_type': segMeta['segment_type'],
      },
    });
  }

  /// CRC-32 (IEEE, as Python's `zlib.crc32`) of [s]'s UTF-8 bytes.
  static int _crc32(String s) {
    var crc = 0xFFFFFFFF;
    for (final b in utf8.encode(s)) {
      crc = _crcTable[(crc ^ b) & 0xFF] ^ (crc >>> 8);
    }
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF;
  }

  static final List<int> _crcTable = List<int>.generate(256, (n) {
    var c = n;
    for (var k = 0; k < 8; k++) {
      c = (c & 1) != 0 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
    }
    return c;
  });

  /// Decode a stored `route_polyline` (JSON string `[[lon,lat],…]`) to coords.
  List<List<double>> _decodePolyline(Object? raw) {
    if (raw == null) return const [];
    final decoded = raw is String ? jsonDecode(raw) : raw;
    if (decoded is! List) return const [];
    return decoded
        .map((pt) => (pt is List && pt.length >= 2)
            ? [(pt[0] as num).toDouble(), (pt[1] as num).toDouble()]
            : null)
        .whereType<List<double>>()
        .toList();
  }

  int _polylineLength(Map<String, dynamic> segMeta) =>
      _decodePolyline(segMeta['route_polyline']).length;

  /// Segments dropped from [items] whose DELETE has not come back yet, keyed by
  /// segment id, with the list index and map feature they were removed from.
  /// [deleteSegment] puts one back when the request fails: the server still has
  /// the segment, so a short local list is a silent divergence that only shows
  /// up as an unexplained reappearance at the next full reload.
  final Map<String, _RemovedSegment> _removedSegments = {};

  /// Immediately remove a segment from the local list and map — no server call.
  /// Replaces `items` with a new list so the identity check in
  /// _rebuildDisplayList detects the change and removes the dismissed widget
  /// from the tree before the SnackBar fires.
  void removeSegmentLocally(String segId) {
    final index = items.indexWhere((item) =>
        item['item_type'] == 'segment' &&
        item['segment']?['id']?.toString() == segId);
    if (index >= 0) {
      _removedSegments[segId] = _RemovedSegment(
        index, items[index], _pendingSegmentPatches[segId] ?? _geoFeature(segId));
    }
    items = items
        .where((item) =>
            !(item['item_type'] == 'segment' &&
              item['segment']?['id']?.toString() == segId))
        .toList();
    removeSegmentFromGeo(segId);
    notifyListeners();
  }

  Future<void> deleteSegment(String segId) async {
    final ref = projectRef;
    if (ref == null) return;
    // removeSegmentLocally already called via onOptimistic before the undo window.
    // No reload needed — reloading would bring back other pending-delete segments
    // (still in the DB) and cause ghost reappearances while their toasts are active.
    try {
      await api.delete(ref.path('/segments/${Uri.encodeComponent(segId)}'));
      _removedSegments.remove(segId);
    } on Exception catch (e) {
      // 404 means it is already gone server-side, so the local removal stands.
      if (e is ApiException && e.statusCode == 404) {
        _removedSegments.remove(segId);
        return;
      }
      // Anything else leaves the segment on the server. Roll the optimistic
      // removal back like addSegment/updateSegment do rather than reloading:
      // a reload here would resurrect the *other* segments whose undo windows
      // are still open. Callers surface [error] once this returns.
      _restoreRemovedSegment(segId);
      error = errorMessage(e);
      notifyListeners();
    }
  }

  /// Put a segment back where [removeSegmentLocally] took it from.
  void _restoreRemovedSegment(String segId) {
    final removed = _removedSegments.remove(segId);
    if (removed == null) return;
    final alreadyBack = items.any((item) =>
        item['item_type'] == 'segment' &&
        item['segment']?['id']?.toString() == segId);
    if (!alreadyBack) {
      final restored = List<Map<String, dynamic>>.from(items);
      restored.insert(removed.index.clamp(0, restored.length), removed.item);
      items = restored;
    }
    final feature = removed.feature;
    if (feature != null) {
      upsertSegmentInGeo(segId, feature);   // also lifts the tombstone
    } else {
      // Nothing to re-draw (geo had not loaded yet) — just stop suppressing it
      // so the next geo rebuild brings the server's own feature back.
      _segmentTombstones.remove(segId);
    }
  }

  /// The [geo] feature for [segId], if geo is loaded and carries one.
  Map<String, dynamic>? _geoFeature(String segId) {
    for (final f in (geo?['features'] as List? ?? const [])) {
      if (f is Map && f['properties']?['segment_id']?.toString() == segId) {
        return Map<String, dynamic>.from(f);
      }
    }
    return null;
  }

  // ── Geo patch helpers ─────────────────────────────────────────────────────
  //
  // Segment edits patch [geo] directly, but [geo] can be null during the load
  // window and is rebuilt from a (possibly stale) server snapshot by
  // _loadFullGeoProgressively. To stop patches being lost or ghost-restored,
  // every upsert/remove is also recorded in a durable overlay that is re-applied
  // whenever geo is rebuilt (see mergePendingSegmentPatches).

  /// Pending segment features keyed by segment_id, awaiting (re)application.
  final Map<String, Map<String, dynamic>> _pendingSegmentPatches = {};

  /// Segment ids the user has removed — suppressed even if a stale server geo
  /// snapshot still contains them.
  final Set<String> _segmentTombstones = {};

  // ── Request ordering (I1-R2-2) ────────────────────────────────────────────
  //
  // A patch is only ever applied once the server has the change it draws: a
  // segment's POST or PUT has returned, a poll has seen its resolve land, a
  // track edit has been saved, a failed DELETE left the segment in place. So a
  // geo request that starts after a patch is applied answers with the server's
  // state at least as new as the patch, and that answer stands — whatever it
  // says, including another writer's re-route (the hourly degraded-route sweep,
  // another device's track edit) that the patch would otherwise hide until the
  // trip is reopened. Only a request that started before the patch can carry
  // the state the patch replaced, and only its content can say whether it
  // already reflects the patch ([_sameRouteState]).

  /// When each pending patch was applied, on [_overlayClock].
  final Map<String, int> _patchAppliedAt = {};

  /// Fetches server geo with [fetch] and returns it with the [_overlayClock]
  /// reading to pass to [reconcileSegmentOverlay].
  ///
  /// The reading is the start of the oldest geo request still in flight, any
  /// notifier's, not just this one's: the service hands an identical request
  /// already in flight to a later caller — whichever notifier started it, the
  /// view-mode and the app-wide one coexisting — so this one's answer may be
  /// that older request's (I1-R3-3). Erring early only keeps a patch until a
  /// later request settles it; erring late would let a stale answer drop it.
  Future<({Map<String, dynamic> geo, int requestedAt})> fetchServerGeo(
      Future<Map<String, dynamic>> Function() fetch) async {
    final request = _GeoRequest(++_overlayClock);
    final requestedAt = _geoRequestsInFlight.fold(
        request.startedAt, (int at, r) => math.min(at, r.startedAt));
    _geoRequestsInFlight.add(request);
    try {
      return (geo: await fetch(), requestedAt: requestedAt);
    } finally {
      _geoRequestsInFlight.remove(request);
    }
  }

  /// Upsert a segment feature into [geo] by segment_id (adds if absent).
  void upsertSegmentInGeo(String segId, Map<String, dynamic> feature) {
    _pendingSegmentPatches[segId] = feature;
    _patchAppliedAt[segId] = ++_overlayClock;
    _segmentTombstones.remove(segId);
    final current = geo;
    if (current == null) return; // overlay re-applies it when geo is rebuilt
    final features = List<dynamic>.from(current['features'] as List? ?? []);
    final idx = features.indexWhere(
        (f) => (f as Map)['properties']?['segment_id']?.toString() == segId);
    if (idx >= 0) {
      features[idx] = feature;
    } else {
      features.add(feature);
    }
    geo = {'type': 'FeatureCollection', 'features': features};
  }

  /// Remove a segment feature from [geo] by segment_id.
  void removeSegmentFromGeo(String segId) {
    _pendingSegmentPatches.remove(segId);
    _patchAppliedAt.remove(segId);
    _segmentTombstones.add(segId);
    final current = geo;
    if (current == null) return;
    final features = List<dynamic>.from(current['features'] as List? ?? []);
    features.removeWhere(
        (f) => (f as Map)['properties']?['segment_id']?.toString() == segId);
    geo = {'type': 'FeatureCollection', 'features': features};
  }

  /// Merge the durable overlay onto a freshly-rebuilt feature [list]: drop
  /// tombstoned segments and (re)apply pending segment patches. Returns a new
  /// list. Call this whenever [geo] is rebuilt from a server snapshot.
  List<dynamic> mergePendingSegmentPatches(List<dynamic> list) {
    final out = <dynamic>[
      for (final f in list)
        if (!(f is Map &&
            _segmentTombstones.contains(
                (f['properties'] as Map? ?? {})['segment_id']?.toString())))
          f,
    ];
    _pendingSegmentPatches.forEach((segId, feature) {
      final idx = out.indexWhere((f) =>
          f is Map &&
          (f['properties'] as Map? ?? {})['segment_id']?.toString() == segId);
      if (idx >= 0) {
        out[idx] = feature;
      } else {
        out.add(feature);
      }
    });
    return out;
  }

  /// Drop overlay entries the authoritative server [serverGeo] already reflects,
  /// so the overlay self-cleans once the backend has caught up. [requestedAt]
  /// is when the request for [serverGeo] started, on [_overlayClock]: what
  /// [fetchServerGeo] returns, or 0 for a snapshot older than every patch (the
  /// offline cache).
  ///
  /// A pending patch is cleared when the request started after the patch was
  /// applied: the server's answer is then at least as new as the patch (see
  /// "Request ordering" above). One that started before clears it only when
  /// the server geo carries its segment *in the same route state*
  /// ([_sameRouteState]). Matching on the id alone dropped a resolved route's
  /// patch on any geo that still had the segment — including one fetched
  /// before the resolve landed, which then put the great-circle line back
  /// (issue #278).
  ///
  /// A tombstone is cleared when the server geo no longer contains its
  /// segment_id, whenever the request started: it is made before the DELETE
  /// is sent (the undo window), so a later request can still have the segment.
  void reconcileSegmentOverlay(Map<String, dynamic> serverGeo,
      {required int requestedAt}) {
    final serverFeatures = <String, Map>{
      for (final f in (serverGeo['features'] as List? ?? const []))
        if (f is Map &&
            (f['properties'] as Map? ?? {})['segment_id'] != null)
          (f['properties'] as Map)['segment_id'].toString(): f,
    };
    _pendingSegmentPatches.entries
        .where((e) {
          if (requestedAt > (_patchAppliedAt[e.key] ?? 0)) return true;
          final server = serverFeatures[e.key];
          return server != null && _sameRouteState(e.value, server);
        })
        .map((e) => e.key)
        .toList()
        .forEach((segId) {
          _pendingSegmentPatches.remove(segId);
          _patchAppliedAt.remove(segId);
        });
    _segmentTombstones.removeWhere((id) => !serverFeatures.containsKey(id));
  }

  /// Whether two segment features draw the same kind of route: the same
  /// `route_mode` (a great-circle arc carries none, or `great_circle`), and the
  /// same `route_degraded` when both say. Every geo endpoint sends
  /// `route_mode`; only the low-res one sends `route_degraded`.
  ///
  /// A resolved-route patch ([applyResolvedSegment]) also needs the server to
  /// say `route_status: resolved`, and the same `route_hash` when both carry
  /// one. `route_mode` alone cannot tell the route from the arc a stale fetch
  /// still holds — the segment PUT stores `rail` before the resolve runs — nor
  /// a re-resolved route from the one it replaced. A server too old to send
  /// `route_status` cannot confirm the route, so the patch stays.
  static bool _sameRouteState(Map patch, Map server) {
    final p = patch['properties'] as Map? ?? const {};
    final s = server['properties'] as Map? ?? const {};
    String mode(Map props) => props['route_mode'] as String? ?? 'great_circle';
    if (mode(p) != mode(s)) return false;
    if (p['route_status'] == 'resolved') {
      if (s['route_status'] != 'resolved') return false;
      final ph = p['route_hash'], sh = s['route_hash'];
      if (ph != null && sh != null && ph != sh) return false;
    }
    final pd = p['route_degraded'], sd = s['route_degraded'];
    return pd == null || sd == null || pd == sd;
  }

  /// Clear the overlay — call when switching projects.
  void clearSegmentOverlay() {
    _pendingSegmentPatches.clear();
    _patchAppliedAt.clear();
    _segmentTombstones.clear();
  }

  /// Drops all of this mixin's state: the overlay, the segments held to put
  /// back if their DELETE fails, and the resolve poller. For
  /// `ProjectNotifier.clear()` (issue #418): a DELETE that fails after it must
  /// not restore a segment into whatever is loaded next, nor a poll write a
  /// route into it.
  void resetSegmentState() {
    clearSegmentOverlay();
    _removedSegments.clear();
    stopSegmentResolvePolling();
  }

  // ── Geometry (SLERP great-circle, mirrors src/models/great_circle.py) ─────

  static List<List<double>> _greatCircleCoords(
      double lat1, double lon1, double lat2, double lon2, {int n = 50}) {
    double r(double d) => d * math.pi / 180;
    double d(double r) => r * 180 / math.pi;

    final p1 = r(lat1), l1 = r(lon1), p2 = r(lat2), l2 = r(lon2);
    final x1 = math.cos(p1) * math.cos(l1);
    final y1 = math.cos(p1) * math.sin(l1);
    final z1 = math.sin(p1);
    final x2 = math.cos(p2) * math.cos(l2);
    final y2 = math.cos(p2) * math.sin(l2);
    final z2 = math.sin(p2);

    final dot = (x1 * x2 + y1 * y2 + z1 * z2).clamp(-1.0, 1.0);
    final omega = math.acos(dot);

    if (omega < 1e-10 || (omega - math.pi).abs() < 1e-10) {
      return [[lon1, lat1], [lon2, lat2]];
    }
    final sinOmega = math.sin(omega);
    return List.generate(n, (i) {
      final t = i / (n - 1);
      final k1 = math.sin((1 - t) * omega) / sinOmega;
      final k2 = math.sin(t * omega) / sinOmega;
      final lat = d(math.asin((k1 * z1 + k2 * z2).clamp(-1.0, 1.0)));
      final lon = d(math.atan2(k1 * y1 + k2 * y2, k1 * x1 + k2 * x2));
      return [lon, lat];
    });
  }

  static Map<String, dynamic> _segmentFeature(
      String id, String type, String label,
      double startLat, double startLon, double endLat, double endLon) {
    return {
      'type': 'Feature',
      'geometry': {
        'type': 'LineString',
        'coordinates': _greatCircleCoords(startLat, startLon, endLat, endLon),
      },
      'properties': {
        'type': 'segment',
        'segment_id': id,
        'segment_type': type,
        'label': label,
      },
    };
  }
}

/// A segment removed from the timeline while its DELETE is in flight — enough
/// to put it back verbatim if the request fails. See
/// [ProjectSegmentCrudMixin.removeSegmentLocally].
class _RemovedSegment {
  final int index;
  final Map<String, dynamic> item;
  final Map<String, dynamic>? feature;
  const _RemovedSegment(this.index, this.item, this.feature);
}

/// Orders geo requests against patches: every request start and every patch,
/// in every notifier, takes the next value. Library-wide, like the service's
/// in-flight fetches a request may join: a notifier's patch and the request
/// another notifier started must read the same clock (I1-R3-3). Never reset,
/// not even by `clear()`: a request still in flight from before would then
/// look newer than patches made after. Holds no trip data.
int _overlayClock = 0;

/// The server geo requests [ProjectSegmentCrudMixin.fetchServerGeo] has in
/// flight, across notifiers. Each removes itself when it settles; emptied
/// early, a request joining one of them would look newer than the answer it
/// gets. Holds start readings only.
final Set<_GeoRequest> _geoRequestsInFlight = {};

/// A server geo request in flight. See [ProjectSegmentCrudMixin.fetchServerGeo].
/// An object rather than its start value, so each request removes only itself.
class _GeoRequest {
  _GeoRequest(this.startedAt);
  final int startedAt;
}

/// One trip's resolve poller. See [ProjectSegmentCrudMixin.pollSegmentResolution].
class _ResolvePoll {
  _ResolvePoll(this.trip);

  /// The trip it polls for, compared by name and owner only.
  final ProjectRef trip;

  /// Pending segment ids, each with the number of polls sent before it joined.
  final Map<String, int> segments = {};

  /// Callers waiting on a segment's outcome, by segment id.
  final Map<String, Completer<Map<String, dynamic>>> waiters = {};

  Timer? timer;

  /// True while a poll's `/meta` request is in flight.
  bool busy = false;

  /// Polls sent so far.
  int polls = 0;

  /// Time spent waiting between polls since the last segment joined.
  Duration elapsed = Duration.zero;
}
