/// Single-project service — wraps /api/projects/{name} and /api/geo/* endpoints.
library;

import 'package:flutter/foundation.dart'
    show compute, protected, visibleForTesting;

import '../api/client.dart';
import '../core/perf_timing.dart';
import '../core/project_ref.dart';
import 'elevation_codec.dart';
import 'geo_viewport.dart';
import 'heavy_decode.dart' as heavy;
import 'map_geometry_memo.dart' show geoGeometrySeeded;
import 'project_data_cache.dart';

/// Deduplicates concurrent identical heavy fetches across *all* [ProjectService]
/// instances (module-level, not a field, because view mode constructs a fresh
/// [ProjectService] subclass on every mode toggle while manage mode keeps one
/// long-lived instance — a per-instance map would miss exactly the case this
/// exists for).
///
/// Switching from view mode to manage mode (or back) *while the trip is still
/// loading* used to fire the same multi-MB request twice: the mode being left
/// had it in flight, and the mode being entered started load() from scratch
/// and issued its own copy before the first one had a chance to populate
/// [projectDataCache]. Both responses then landed and got `jsonDecode`d back
/// to back on the UI isolate — reported as a ~6 s Android ANR on a large trip.
/// A caller that finds a fetch for the same key already in flight now awaits
/// that one instead of starting a second.
final Map<String, Future<Map<String, dynamic>>> _inFlightFetches = {};

Future<Map<String, dynamic>> _dedupFetch(
    String key, Future<Map<String, dynamic>> Function() fetch) {
  final existing = _inFlightFetches[key];
  if (existing != null) return existing;
  final future = fetch();
  _inFlightFetches[key] = future;
  // Whatever the outcome, the next caller after this one settles should be
  // free to start a fresh fetch rather than replay a failure or a now-stale
  // response. whenComplete() returns its own derived Future carrying the same
  // outcome as `future` — since `future` itself is already returned below for
  // every real caller to observe, this second one would otherwise go
  // unlistened-to and surface a failed fetch as an unhandled async error the
  // moment `future` rejects. .ignore() marks that deliberate (see the same
  // pattern in ProjectNotifier.load()).
  //
  // Removed only while it is still this fetch's entry: after a
  // [resetInFlightFetches] the key may already hold the next account's.
  future.whenComplete(() {
    if (identical(_inFlightFetches[key], future)) _inFlightFetches.remove(key);
  }).ignore();
  return future;
}

/// Forgets every fetch in flight, so the next caller starts its own. Called
/// when the session ends (issue #418): the keys name a trip, not an account,
/// so the next account to open a trip of the same name would otherwise be
/// handed the previous one's response.
void resetInFlightFetches() => _inFlightFetches.clear();

/// Human-readable payload size for a [PerfSpans.note]. Shared with the
/// shared-project service, which records the same notes for its own endpoints.
String perfSizeLabel(int bytes) => bytes >= 1024 * 1024
    ? '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB'
    : '${(bytes / 1024).toStringAsFixed(0)} KB';

class ProjectService {
  /// Fetches the full project dict for [ref] including elevation_profile data.
  /// GET /api/projects/{name}
  ///
  /// The heaviest endpoint in the app (~12 MB on a large trip), so it gets the
  /// most generous budget rather than the 30 s default it used to inherit.
  ///
  /// Served from [projectDataCache] when a prior fetch (in this session or,
  /// on native platforms, a previous app run) is still current — see that
  /// class for how staleness is detected. Pass [bypassCache] true for any
  /// reload that follows a mutation *this client just made*: the cache can
  /// only be validated against a `/meta` response, and a reload right after
  /// a local write needs the server's actual post-write state, not whatever
  /// was last confirmed valid.
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async {
    // Read before anything awaits: the response is cached for the account
    // that asked, not whoever is signed in when it lands (U5-R1-1).
    final scope = projectDataCache.scope;
    if (!bypassCache) {
      final cached = await projectDataCache.readFullDetails(ref);
      if (cached != null) return cached;
    }
    return _dedupFetch('details:${ref.ownerId ?? 0}:${ref.name}', () async {
      // Bytes, then a worker-isolate parse: this is the ~12 MB payload whose
      // inline jsonDecode is the single largest UI-isolate stall of a cold
      // open (issue #292). _dedupFetch's own doc comment above already named
      // "jsonDecode'd back to back on the UI isolate" as an ANR cause; that
      // round removed the duplicate fetch but left the decode where it was.
      final bytes = await perfSpans.stage('fetch_details',
          () => api.getBytes(ref.path(), timeout: const Duration(minutes: 2)));
      perfSpans.note('details', perfSizeLabel(bytes.length));
      final data = await perfSpans.stage(
          'decode_details', () => heavy.decodeJsonMapOffIsolate(bytes));
      projectDataCache.writeFullDetails(ref, data, scope: scope);
      return data;
    });
  }

  /// Lightweight project dict — no elevation_profile or summary_polyline.
  /// Typically 10-15× smaller than getDetails(); use for initial load and
  /// reloads that don't need the elevation chart to update.
  /// GET /api/projects/{name}/meta
  ///
  /// 60 s, not the 30 s default: a cold load of a large project measured 11-13 s
  /// server-side before a byte of the body moved, so the default left almost no
  /// headroom for the transfer — see issue #178.
  ///
  /// Always live — this is the response [projectDataCache] uses to decide
  /// whether the heavier payloads it may be holding are still valid, so it
  /// would be circular for this call itself to skip the network.
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async {
    final scope = projectDataCache.scope; // see getDetails
    // Routed through the same seam as the heavier payloads: /meta is 10-15x
    // smaller than getDetails but still hundreds of KB on a large trip, and
    // unlike them it lands *before* the spinner clears, where the user is
    // already waiting. heavy.decodeJsonMapOffIsolate stays inline below
    // kInlineDecodeThresholdBytes, so a small trip pays no isolate hop.
    final metaBytes = await perfSpans.stage('fetch_meta',
        () => api.getBytes(ref.path('/meta'), timeout: _kLoadTimeout));
    perfSpans.note('meta', perfSizeLabel(metaBytes.length));
    final data = await perfSpans.stage(
        'decode_meta', () => heavy.decodeJsonMapOffIsolate(metaBytes));
    projectDataCache.onMetaFetched(ref, data, scope: scope);
    return data;
  }

  /// Budget for the two requests the activity panel blocks on (see
  /// getDetailsMeta and getLowResGeo). ProjectNotifier.load() retries them, so
  /// this is the wait before a *retry*, not before a visible failure.
  static const _kLoadTimeout = Duration(seconds: 60);

  /// Photo uuids per memory — `{memory_id: [uuid, ...]}` — and nothing else.
  /// GET /api/projects/{name}/memory-photos
  ///
  /// The one question the post-import poll asks, at the size of its answer
  /// (issue #308). Measured on the 219-activity / 600-memory shape: 75 KB
  /// against 492 KB for `/meta` and 36 MB for [getDetails], which is what that
  /// poll used to fetch sixty times over.
  ///
  /// Uuids, not counts: a marker needs `photos.first` to build its thumbnail
  /// URL, so a count can never take a memory from "no photo" to "thumbnail".
  ///
  /// Always live, and deliberately outside [projectDataCache]: this exists to
  /// observe a change the client cannot predict, so a cached answer is the one
  /// answer it must never give.
  Future<Map<String, List<String>>> getMemoryPhotos(ProjectRef ref) async {
    final data = await api.get(ref.path('/memory-photos'));
    final photos = (data as Map<String, dynamic>)['photos'];
    if (photos is! Map) return const {};
    return photos.map((id, uuids) => MapEntry(
        id.toString(), (uuids as List?)?.cast<String>().toList() ?? const []));
  }

  /// The cached full-res geo for [ref], or null when nothing is on file.
  /// Never touches the network.
  ///
  /// Disk is read as raw bytes so a cold start's cached geo goes through the
  /// same parse-derive-seed worker hop a network response does (issue #299).
  /// Decoding it to a Map elsewhere produced fresh coordinate lists none of
  /// map_geometry_memo.dart's identity-keyed caches had seen, so the map paid
  /// the whole cold derivation on the UI isolate — the stall that tripped the
  /// ANR watchdog mid-pan. L1 is preferred when present: that Map is the very
  /// object the decode hop already seeded this session.
  Future<Map<String, dynamic>?> readCachedGeo(ProjectRef ref) async {
    final scope = projectDataCache.scope; // see getDetails
    final cached = await projectDataCache.readFullGeo(ref);
    // Ask the geometry, not the cache: L1 residency does not imply the decode
    // hop ever ran over this Map (see geoGeometrySeeded).
    if (cached != null && geoGeometrySeeded(cached)) return cached;
    // Read before the bytes: an edit's /meta landing during the decode makes
    // them stale, and they must not become L1's answer (issue #379).
    final lockVersion = projectDataCache.lockVersionOf(ref);
    final bytes = await projectDataCache.readFullGeoBytes(ref);
    if (bytes != null) {
      final geo = await perfSpans.stage(
          'decode_geo_cached', () => heavy.decodeGeoOffIsolate(bytes));
      projectDataCache.promoteFullGeo(ref, geo,
          scope: scope, lockVersion: lockVersion);
      return geo;
    }
    return cached;
  }

  /// Fetches the GeoJSON FeatureCollection for [ref].
  /// GET /api/geo/project?name={name}
  ///
  /// The full-res endpoint sends activity tracks as Google-encoded polylines
  /// (in `properties.polyline`, with empty `coordinates`) to keep the payload
  /// small; [_expandEncodedActivities] decodes them back to `coordinates` so
  /// the rest of the app sees standard GeoJSON. The timeout is generous because
  /// a cold-cache build of a large trip can take a while on NAS storage.
  /// See [getDetails] for [bypassCache] — a post-mutation reload must always
  /// hit the network.
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async {
    final scope = projectDataCache.scope; // see getDetails
    if (!bypassCache) {
      final cached = await readCachedGeo(ref);
      if (cached != null) return cached;
    }
    return _dedupFetch('geo:${ref.ownerId ?? 0}:${ref.name}', () async {
      // The version the answer is for, read before the request (issue #379).
      final lockVersion = projectDataCache.lockVersionOf(ref);
      final expanded = await _fetchFullGeo(ref);
      projectDataCache.writeFullGeo(ref, expanded,
          scope: scope, lockVersion: lockVersion);
      return expanded;
    });
  }

  /// The same full-resolution payload [getGeo] serves, fetched fresh and
  /// returned to the caller alone — [projectDataCache] is consulted neither
  /// before (no cached answer) nor after (no L1 or disk write).
  ///
  /// For the two callers that need full-resolution geometry without the map
  /// keeping it (issue #317): the offline cache seed, which writes it to disk
  /// itself and drops it, and the image export, which draws it once. Both
  /// would otherwise leave the whole payload resident in L1 for the rest of
  /// the session — the ~180 MB the zoom level-of-detail path exists to avoid
  /// (see [getSimplifiedGeo]).
  ///
  /// Deduplicated under its own key rather than [getGeo]'s, so that a caller
  /// joining an in-flight [getGeo] still gets the cache write [getGeo]
  /// promises, and one joining this does not get one it did not ask for.
  ///
  /// Overridden by the shared-project service, which is on the share
  /// endpoints and would 401 against this one.
  Future<Map<String, dynamic>> fetchFullGeoUncached(ProjectRef ref) =>
      _dedupFetch('geoFullUncached:${ref.ownerId ?? 0}:${ref.name}',
          () => _fetchFullGeo(ref));

  Future<Map<String, dynamic>> _fetchFullGeo(ProjectRef ref) async {
    final encoded = Uri.encodeComponent(ref.name);
    // Compact payload: activity tracks as Google-encoded polylines, decoded by
    // expandEncodedActivities (~4.5× smaller than expanded coordinates). The
    // earlier web crash — decodePolyline yielding a ~42e6 latitude — was a
    // Dart-on-web bitwise/`~` semantics bug, now fixed in the decoder itself
    // (see polyline_decoder.dart). The generous timeout covers a cold-cache
    // build of a large trip.
    final bytes = await perfSpans.stage(
        'fetch_geo',
        () => api.getBytes(
            ref.withOwner('/api/geo/project?name=$encoded&encoded=1'),
            timeout: const Duration(seconds: 90)));
    perfSpans.note('geo', perfSizeLabel(bytes.length));
    // Parse and polyline-expansion fused into one worker-isolate hop — they
    // used to run as two consecutive inline passes over the same 300k+
    // points. See heavy_decode.dart.
    return perfSpans.stage(
        'decode_geo', () => heavy.decodeGeoOffIsolate(bytes));
  }

  /// Geometry simplified to roughly one screen pixel at [zoom] (issue #295).
  ///
  /// The client holds what this returns, so the size of its map geometry
  /// becomes a function of what is on screen rather than of trip length. The
  /// measurement behind it: a 219-activity trip carries 1,465,345 coordinates
  /// and the map renders 6,051 of them, the rest costing roughly 180 MB of a
  /// Dart heap that device profiling put at ~625 MB steady and ~804 MB during
  /// load, on a process killed above ~1.3 GB.
  ///
  /// Deliberately not cached on disk. Entries would be per zoom bucket, they
  /// are cheap for the server to rebuild and short-lived there, and the
  /// full-resolution payload remains the offline fallback — see
  /// `ProjectNotifier._loadFullGeoProgressively`, which falls back to it when
  /// this fails for any reason, including an older server that has no such
  /// endpoint.
  ///
  /// [bbox], when given, additionally scopes the answer to what is on screen
  /// (issue #324). Zoom bounds the detail, not the extent, so without it a
  /// deep zoom still ships — and makes the server simplify — the whole trip.
  /// A line outside the box comes back at the floor a whole-trip zoom would
  /// have given it; no feature is ever dropped. An older server ignores the
  /// parameter and returns the whole trip, which is a superset.
  ///
  /// GET /api/geo/project/simplified?name={name}&zoom={zoom}&bbox={bbox}
  Future<Map<String, dynamic>> getSimplifiedGeo(ProjectRef ref, double zoom,
      {GeoBox? bbox}) {
    // Keyed by the server's own quantisation, so a mode toggle mid-load and a
    // concurrent refetch for the same level share one 90 s request rather than
    // racing two. The box is part of that key for the same reason: two
    // requests for different regions are two different answers.
    final bucket = zoom.ceil();
    return _dedupFetch(
        'geoLod:${ref.ownerId ?? 0}:${ref.name}:$bucket:${bbox?.param ?? 'all'}',
        () => fetchSimplifiedGeo(ref, zoom, bbox));
  }

  /// The same answer as [getSimplifiedGeo], from a request of its own that
  /// never joins one already in flight, and that no later caller joins.
  ///
  /// For the refresh after a write (issue #379): an identical request already
  /// in flight may have started before the write, and its answer would show
  /// the trip as it was. The server's simplified answers are invalidated by
  /// every write, so a request sent after the write sees it.
  ///
  /// A method of its own rather than a parameter of [getSimplifiedGeo], so the
  /// test fakes overriding that one keep compiling.
  Future<Map<String, dynamic>> getSimplifiedGeoFresh(
          ProjectRef ref, double zoom, {GeoBox? bbox}) =>
      fetchSimplifiedGeo(ref, zoom, bbox);

  /// The request [getSimplifiedGeo] deduplicates. Override *this*, not
  /// [getSimplifiedGeo], to serve the same answer from a different endpoint —
  /// the share-token one does (issue #321) — and the dedup, and its key, stay
  /// with the base class rather than being re-derived per subclass.
  @protected
  Future<Map<String, dynamic>> fetchSimplifiedGeo(
      ProjectRef ref, double zoom, GeoBox? bbox) async {
    final encoded = Uri.encodeComponent(ref.name);
    final box = bbox == null ? '' : '&bbox=${bbox.param}';
    final bytes = await perfSpans.geoLodFetch(() => api.getBytesWithHeaders(
        ref.withOwner(
            '/api/geo/project/simplified?name=$encoded&zoom=$zoom$box'),
        timeout: const Duration(seconds: 90)));
    perfSpans.note('geo_lod', perfSizeLabel(bytes.length));
    // Same hop as the full-res path, so the geometry caches are seeded and
    // the map's first build after the swap does no O(points) work.
    return perfSpans.stage(
        'decode_geo_lod', () => heavy.decodeGeoOffIsolate(bytes));
  }

  /// Expand any activity feature carrying a Google-encoded `polyline` property
  /// into a standard GeoJSON `coordinates` array (`[[lon, lat], …]`). No-op for
  /// features that already have coordinates (segments, straight-line fallbacks,
  /// and the share endpoint's expanded responses).
  @visibleForTesting
  static Map<String, dynamic> expandEncodedActivities(Map<String, dynamic> geo) =>
      heavy.expandEncodedActivities(geo);

  /// Fetches pre-computed low-res GeoJSON (straight lines per activity) for [ref].
  /// GET /api/geo/project/low-res?name={name}
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async {
    final scope = projectDataCache.scope; // see getDetails
    final cached = await projectDataCache.readLowResGeo(ref);
    if (cached != null) return cached;
    return _dedupFetch('lowResGeo:${ref.ownerId ?? 0}:${ref.name}', () async {
      // The version the answer is for, read before the request (issue #379).
      final lockVersion = projectDataCache.lockVersionOf(ref);
      final encoded = Uri.encodeComponent(ref.name);
      final bytes = await perfSpans.stage(
          'fetch_low_res_geo',
          () => api.getBytes(
              ref.withOwner('/api/geo/project/low-res?name=$encoded'),
              timeout: _kLoadTimeout));
      perfSpans.note('low_res_geo', perfSizeLabel(bytes.length));
      // decodeGeoOffIsolate, not the plain JSON decode: the low-res payload
      // already carries expanded coordinates (the expansion pass is a no-op on
      // those), and on a long trip it is still well over the render budget, so
      // it needs the same derive-and-seed treatment as the full-res geo.
      final data = await perfSpans.stage(
          'decode_low_res_geo', () => heavy.decodeGeoOffIsolate(bytes));
      projectDataCache.writeLowResGeo(ref, data,
          scope: scope, lockVersion: lockVersion);
      return data;
    });
  }

  /// Per-activity elevation profiles from the compact endpoint (issue #295).
  ///
  /// The reason this exists rather than reusing [getDetails]: that payload is
  /// 33 MB on a long trip, ~9 s to fetch and ~5 s to decode, and elevation is
  /// the only part of it the load actually needs. Decoded on a worker, since
  /// keeping the work off the UI isolate is the whole point.
  ///
  /// Returns `(profiles, encryptedIds)`. An E2EE trip's profiles arrive as
  /// opaque envelopes this endpoint cannot open, so their ids are reported
  /// back and the caller falls through to the details payload for those.
  /// GET /api/projects/{name}/elevation
  Future<({Map<String, List<List<double>>> profiles, Set<String> encryptedIds})>
      getElevation(ProjectRef ref) async {
    final bytes = await perfSpans.stage(
        'fetch_elevation',
        () => api.getBytes(ref.path('/elevation'),
            timeout: const Duration(seconds: 90)));
    perfSpans.note('elevation', perfSizeLabel(bytes.length));
    final body = await perfSpans.stage(
        'decode_elevation', () => heavy.decodeJsonMapOffIsolate(bytes));
    final raw = body['profiles'];
    final encrypted = body['encrypted'];
    final profiles = raw is Map<String, dynamic>
        ? await compute(decodeElevationProfiles, raw)
        : <String, List<List<double>>>{};
    return (
      profiles: profiles,
      encryptedIds:
          encrypted is Map ? encrypted.keys.map((k) => k.toString()).toSet() : <String>{},
    );
  }

  /// Fetches pre-computed project statistics for [ref].
  /// Pass [tags] to filter stats to only days with matching tags.
  /// GET /api/projects/{name}/stats[?tags=x&tags=y]
  Future<Map<String, dynamic>> getStats(ProjectRef ref,
      {List<String> tags = const []}) async {
    final query = tags.isEmpty
        ? ''
        : '?${tags.map((t) => 'tags=${Uri.encodeComponent(t)}').join('&')}';
    final data = await api.get(ref.path('/stats$query'));
    return data as Map<String, dynamic>;
  }

  /// PUT /api/projects/{name}/track-style
  Future<void> saveTrackStyle(
    ProjectRef ref, {
    String? trackColor,
    Object? trackSecondaryColor = _kUnset, // null = clear, _kUnset = don't send
    double? trackWidth,
    bool? alternating,
    Object? elevationChartColor = _kUnset, // null = clear, _kUnset = don't send
    bool? elevationChartShowLine,
    bool? colorByType,
    Map<String, Map<String, dynamic>>? typeStyles,
  }) async {
    await api.put(ref.path('/track-style'), {
      if (trackColor != null) 'track_color': trackColor,
      if (trackSecondaryColor != _kUnset) 'track_secondary_color': trackSecondaryColor,
      if (trackWidth != null) 'track_width': trackWidth,
      if (alternating != null) 'alternating_track_colors': alternating,
      if (elevationChartColor != _kUnset) 'elevation_chart_color': elevationChartColor,
      if (elevationChartShowLine != null) 'elevation_chart_show_line': elevationChartShowLine,
      if (colorByType != null) 'color_by_type': colorByType,
      if (typeStyles != null) 'type_styles': typeStyles,
    });
  }

  static const Object _kUnset = Object();

  /// PUT /api/projects/{name}/items/sort
  Future<void> sortItems(ProjectRef ref) async {
    await api.put(ref.path('/items/sort'), {});
  }

  /// PUT /api/projects/{name}/languages
  Future<void> saveLanguages(ProjectRef ref, List<String> languages) async {
    await api.put(ref.path('/languages'), {'languages': languages});
  }

  /// Fetch a single activity's editable geometry (polyline + elevation pairs).
  /// GET /api/projects/{name}/activities/{id}/track
  /// Far smaller than getDetails() — used to open the track editor without
  /// downloading the whole project.
  Future<Map<String, dynamic>> getActivityTrack(
    ProjectRef ref,
    int activityId,
  ) async {
    final data = await api.get(ref.path('/activities/$activityId/track'));
    return data as Map<String, dynamic>;
  }

  /// Replace an activity's track geometry with an edited point list.
  /// PUT /api/projects/{name}/activities/{id}/track
  /// [payload] is [TrackEditModel.toSavePayload]. Returns the updated project.
  ///
  /// [lockVersion], when given, is the project's lock_version the editor last
  /// saw (from getActivityTrack) — the server rejects the save with a 409 if
  /// the project changed since, e.g. this same activity edited from another
  /// tab (issue #31). Omit it to save unconditionally.
  ///
  /// Generous timeout: see splitActivity below — the same recompute +
  /// reload can run well over the client's default 30s on a large trip, and
  /// this response is discarded by the caller anyway (ProjectNotifier just
  /// re-fetches via _silentReload).
  Future<Map<String, dynamic>> saveActivityTrack(
    ProjectRef ref,
    int activityId,
    Map<String, dynamic> payload, {
    int? lockVersion,
  }) async {
    final data = await api.put(
      ref.path('/activities/$activityId/track'),
      {
        ...payload,
        if (lockVersion != null) 'lock_version': lockVersion,
      },
      timeout: const Duration(minutes: 2),
    );
    return data as Map<String, dynamic>;
  }

  /// Reset an edited activity's track to the original Strava geometry.
  /// POST /api/projects/{name}/activities/{id}/reset
  ///
  /// Generous timeout: see saveActivityTrack above.
  Future<Map<String, dynamic>> resetActivityTrack(
    ProjectRef ref,
    int activityId,
  ) async {
    final data = await api.post(
      ref.path('/activities/$activityId/reset'),
      const {},
      timeout: const Duration(minutes: 2),
    );
    return data as Map<String, dynamic>;
  }

  /// Split an activity at [splitIndex]; the tail becomes a new local activity.
  /// When [dropBoundary] is true, the tail excludes the shared boundary point
  /// (#104 — used when a transportation segment will bridge the cut).
  /// POST /api/projects/{name}/activities/{id}/split
  ///
  /// [payload] is the editor's current TrackEditModel.toSavePayload — the point
  /// list [splitIndex] indexes into. Sending it lets the server cut the track
  /// the user is actually looking at, so unsaved trims/deletes compound with
  /// the split instead of being discarded (#127). Omit it to split the stored
  /// geometry.
  ///
  /// [lockVersion], when given, is the project's lock_version the editor last
  /// saw (from getActivityTrack) — the server rejects the split with a 409 if
  /// the project changed since, e.g. this same activity edited from another
  /// tab (issue #31). Omit it to split unconditionally.
  ///
  /// Generous timeout: on a large trip, committing the split + reloading and
  /// serialising the updated project can take well over the client's default
  /// 30s (this response is discarded by the caller anyway — see
  /// ProjectNotifier.splitActivity — but the request still has to finish).
  Future<Map<String, dynamic>> splitActivity(
    ProjectRef ref,
    int activityId,
    int splitIndex, {
    bool dropBoundary = false,
    Map<String, dynamic>? payload,
    int? lockVersion,
  }) async {
    final data = await api.post(
      ref.path('/activities/$activityId/split'),
      {
        'split_index': splitIndex,
        'drop_boundary': dropBoundary,
        if (payload != null) ...payload,
        if (lockVersion != null) 'lock_version': lockVersion,
      },
      timeout: const Duration(minutes: 2),
    );
    return data as Map<String, dynamic>;
  }

  /// Delete a local (split-tail, negative-id) activity.
  /// DELETE /api/projects/{name}/activities/{id}/local
  Future<void> deleteLocalActivity(ProjectRef ref, int activityId) async {
    await api.delete(ref.path('/activities/$activityId/local'));
  }

  /// POST /api/projects/{name}/activities/{id}/refresh
  ///
  /// Returns immediately with 202 `{refresh_status: pending}` — the Strava
  /// re-fetch runs server-side as a background job (issue #148). Callers poll
  /// `/meta` for the verdict; the default 30 s timeout is plenty for the
  /// trigger, which makes no network calls of its own.
  Future<Map<String, dynamic>> refreshActivity(
    ProjectRef ref,
    int activityId,
  ) async {
    final data = await api.post(
      ref.path('/activities/$activityId/refresh'),
      {},
    );
    return (data as Map?)?.cast<String, dynamic>() ??
        {'refresh_status': 'pending'};
  }

  /// POST /api/projects/{name}/segments/{segId}/resolve-route
  ///
  /// [force] bypasses the server's guard against silently discarding a
  /// manually edited route (issue #150) — pass it only after the caller has
  /// confirmed with the user.
  Future<Map<String, dynamic>> resolveTrainRoute(
    ProjectRef ref,
    String segId, {
    String? hafasProvider,
    String? trainNumber,
    String? date,
    bool force = false,
  }) async {
    final sid = Uri.encodeComponent(segId);
    final body = <String, dynamic>{
      if (hafasProvider != null && hafasProvider.isNotEmpty)
        'hafas_provider': hafasProvider,
      if (trainNumber != null && trainNumber.isNotEmpty)
        'train_number': trainNumber,
      if (date != null && date.isNotEmpty) 'date': date,
      if (force) 'force': true,
    };
    final data = await api.post(
      ref.path('/segments/$sid/resolve-route'),
      body,
      timeout: const Duration(minutes: 3),
    );
    return data as Map<String, dynamic>;
  }

  /// Replace a segment's route geometry with a manually edited point list.
  /// PUT /api/projects/{name}/segments/{segId}/track
  /// [payload] is [TrackEditModel.toSavePayload]. Returns the updated segment
  /// route fields (`route_polyline`/`route_mode`/`route_status`/`route_edited`).
  Future<Map<String, dynamic>> saveSegmentTrack(
    ProjectRef ref,
    String segId,
    Map<String, dynamic> payload,
  ) async {
    final sid = Uri.encodeComponent(segId);
    final data = await api.put(ref.path('/segments/$sid/track'), payload);
    return data as Map<String, dynamic>;
  }
}
