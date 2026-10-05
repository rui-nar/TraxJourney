/// Notifier for a single open project — loads details + GeoJSON in parallel.
library;

import 'dart:async';
import 'dart:convert';

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart' show Color;
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import '../api/client.dart';
import '../core/perf_timing.dart';
import '../core/project_ref.dart';
import '../crypto/e2ee_crypto.dart' show EncryptedField;
import '../crypto/encryption.dart';
import '../crypto/undecrypted_fields.dart';
import '../map/geo_point.dart';
import '../map/polyline_decoder.dart';
import '../share/share_content_generator.dart';
import 'client_geo_builder.dart' as client_geo;
import 'geo_viewport.dart';
import 'map_geometry_memo.dart';
import 'members_service.dart';
import 'project_data_cache.dart';
import 'project_filter_mixin.dart';
import 'project_filters.dart';
import 'project_journal_crud_mixin.dart';
import 'project_memory_crud_mixin.dart';
import 'project_people_crud_mixin.dart';
import 'project_quota_mixin.dart';
import 'project_segment_crud_mixin.dart';
import 'project_service.dart';
import 'trip_end_days.dart';

/// Waits between the automatic retries of a failed project fetch.
///
/// A slow server or a stalled connection is transient far more often than not —
/// a cold load of a large trip can outlast even a generous timeout, and the
/// server finishes and caches the result regardless, so the next attempt lands
/// on warm data. Retrying is therefore the right default, and the only one the
/// user should ever have to take: the panel stays in its loading state while
/// this runs (issue #178).
///
/// Bounded on purpose: three attempts, then the caller surfaces a plain-language
/// error. An unbounded retry would hammer an already-struggling server and never
/// tell the user anything was wrong.
const kFetchRetryBackoff = [Duration(seconds: 2), Duration(seconds: 6)];

/// Whether [e] is worth another attempt.
///
/// A 4xx is the server's considered answer — "you can't see this trip" doesn't
/// become truer after 8 s of backoff, it just reaches the user 8 s later. 408
/// and 429 are the exceptions: they mean "ask again". Everything else here is a
/// transport failure (timeout, dropped socket, truncated body) and is exactly
/// what retrying exists for.
@visibleForTesting
bool isRetriableFetchError(Object e) {
  if (e is ApiException) {
    return e.statusCode >= 500 || e.statusCode == 408 || e.statusCode == 429;
  }
  return true;
}

/// Runs [operation], retrying it after each delay in [backoff] until it
/// succeeds, the delays run out (the last failure is rethrown), the failure
/// isn't worth retrying, or [abort] returns true between attempts (the user
/// navigated away).
///
/// Catches Object, not Exception: a decode failure throws an Error
/// (RangeError/TypeError), and that is exactly the kind of transient corruption
/// a truncated response produces — letting it escape uncaught would be worse
/// than retrying it.
Future<T> retryFetch<T>(
  Future<T> Function() operation, {
  List<Duration> backoff = kFetchRetryBackoff,
  bool Function()? abort,
}) async {
  for (var attempt = 0; ; attempt++) {
    try {
      return await operation();
    } on Object catch (e) {
      if (attempt >= backoff.length || !isRetriableFetchError(e)) rethrow;
      await Future<void>.delayed(backoff[attempt]);
      if (abort?.call() ?? false) rethrow;
    }
  }
}

/// Calendar day-of-trip numbering for the hero, matching the activity-panel
/// headers: day N = whole days from the trip start (gaps counted), total = span
/// to the last day present. Sort-order independent (uses min/max, not index).
/// [tripStart] is the optional explicit override (ISO `yyyy-MM-dd`); otherwise
/// the earliest day in [orderedDateKeys] is the start.
///
/// Shared by the day-meta editor (manage mode) and the map selection-stats
/// overlay (view + manage mode, issue #74) — the only correct implementation
/// of trip day-numbering, so it lives here rather than in manage-only UI.
({int dayNumber, int totalDays}) dayTripNumbering(
  String dateKey,
  List<String> orderedDateKeys,
  String? tripStart,
) {
  final thisDate = DateTime.tryParse(dateKey) ?? DateTime.now();
  DateTime dayOnly(DateTime d) => DateTime.utc(d.year, d.month, d.day);
  final keyDates = orderedDateKeys
      .map(DateTime.tryParse)
      .whereType<DateTime>()
      .toList();
  final earliest = keyDates.isEmpty
      ? thisDate
      : keyDates.reduce((a, b) => a.isBefore(b) ? a : b);
  final latest = keyDates.isEmpty
      ? thisDate
      : keyDates.reduce((a, b) => a.isAfter(b) ? a : b);
  final startOverride =
      tripStart != null ? DateTime.tryParse(tripStart) : null;
  final startUtc = dayOnly(startOverride ?? earliest);
  return (
    dayNumber: dayOnly(thisDate).difference(startUtc).inDays + 1,
    totalDays: dayOnly(latest).difference(startUtc).inDays + 1,
  );
}

/// Cursor-track resolution kept per activity.
///
/// These tracks exist for one job: mapping a distance to a position, for the
/// map-to-chart cursor and for `hitTestMapTap`'s nearest-point scan. They do
/// not draw anything — the polylines the map renders come from `geo` and are
/// separately capped at [kMaxTotalPolylinePoints].
///
/// A 219-activity trip measured 1,465,345 points in `fullTrack` and the same
/// again in `perActivityTracks` (issue #276's diagnostics), roughly 200 MB
/// held to answer a question a fraction of that resolves. At 1000 points a
/// 50 km activity resolves to ~50 m, which is sub-pixel at any zoom that
/// shows the whole activity, and the scan `hitTestMapTap` runs on every tap
/// gets an order of magnitude cheaper with it.
///
/// This regressed in #295: removing the index-aligned pairing made the track
/// follow the geometry (~6,700 points per activity here) rather than the
/// elevation profile (~300).
const int kMaxTrackPointsPerActivity = 1000;

/// Uniformly reduces [track] to at most [maxPoints], keeping the first and
/// last exactly.
///
/// Uniform in index rather than in distance: track points come from GPS
/// samples, which are already roughly evenly spaced along the path, and
/// preserving the endpoints is what keeps the distance range — and therefore
/// the chart's X axis — intact.
@visibleForTesting
List<(double, GeoPoint)> downsampleTrack(
    List<(double, GeoPoint)> track, int maxPoints) {
  if (maxPoints < 2 || track.length <= maxPoints) return track;
  final out = <(double, GeoPoint)>[track.first];
  // Stride across the interior, then append the true last point.
  final step = (track.length - 1) / (maxPoints - 1);
  for (var i = 1; i < maxPoints - 1; i++) {
    out.add(track[(i * step).round()]);
  }
  out.add(track.last);
  return out;
}

/// Cheap O(activities) count of raw elevation samples. Mirrors
/// elevation_chart.dart's private `_totalProfilePoints`.
@visibleForTesting
int totalElevationProfilePoints(List<Map<String, dynamic>> activities) {
  var total = 0;
  for (final a in activities) {
    final profile = a['elevation_profile'];
    if (profile is List) total += profile.length;
  }
  return total;
}

/// Cheap O(features) count of the coordinates [buildFullTrackResult] will walk.
///
/// This, not the elevation-sample count, is what that function costs: since
/// issue #295 it derives distances from the geometry (haversine per segment)
/// rather than pairing profile samples by index. Gating the isolate hop on
/// profile size alone meant a trip carrying the 300-point low-res profile
/// scored far below the threshold while still walking hundreds of thousands
/// of coordinates on the UI isolate — exactly the pattern the guard exists
/// to prevent.
@visibleForTesting
int totalTrackCoordinatePoints(Map<String, dynamic>? geo) {
  var total = 0;
  final features = geo?['features'];
  if (features is! List) return 0;
  for (final f in features) {
    if (f is! Map) continue;
    if ((f['properties'] as Map? ?? {})['type'] == 'segment') continue;
    final coords = (f['geometry'] as Map? ?? {})['coordinates'];
    if (coords is List) total += coords.length;
  }
  return total;
}

/// Above this many points, [ProjectNotifier._buildFullTrack] moves the work to
/// a background isolate instead of running it inline — mirrors elevation_chart.dart's
/// `_kInlineComputeThreshold` / map_panel.dart's `_kInlineHitTestThreshold`
/// precedent.
const kInlineFullTrackThreshold = 5000;

/// The only thing [buildFullTrackFromTotals] needs from an activity: its id
/// and its profile's total distance.
///
/// Since issue #295 that function derives distances from the *geometry* and
/// scales them to the profile's total, so one double per activity is the
/// entire contribution of a profile that can be hundreds of thousands of
/// points. Projecting to this before the isolate hop is not a micro
/// optimisation: `compute()` *copies its argument*, so passing the activity
/// maps serialised every one of those points to read one number from each.
typedef ActivityTotals = List<({String? id, double elevTotalKm})>;

/// Projects [activities] to the [ActivityTotals] the track builder needs.
///
/// Skips exactly what the builder used to skip inline (no profile, or an
/// empty one), so the resulting order and membership are unchanged.
@visibleForTesting
ActivityTotals activityElevationTotals(List<Map<String, dynamic>> activities) {
  final out = <({String? id, double elevTotalKm})>[];
  for (final a in activities) {
    final profile = a['elevation_profile'];
    if (profile is! List || profile.isEmpty) continue;
    final last = profile.last;
    out.add((
      id: a['id']?.toString(),
      elevTotalKm: (last is List && last.isNotEmpty)
          ? (last[0] as num).toDouble()
          : 0.0,
    ));
  }
  return out;
}

/// Nested-input form of [buildFullTrackFromTotals], kept as the readable
/// contract these computations are tested through.
@visibleForTesting
({List<(double, GeoPoint)> fullTrack, Map<String, List<(double, GeoPoint)>> perActivityTracks})
    buildFullTrackResult(
        ({Map<String, dynamic>? geo, List<Map<String, dynamic>> activities}) args) =>
        buildFullTrackFromTotals((
          coords: flattenGeoCoords(args.geo),
          totals: activityElevationTotals(args.activities),
        ));

/// A trip's activity geometry, flattened for crossing an isolate boundary.
///
/// GeoJSON coordinates arrive as `[[lon, lat], …]` — one Dart list object per
/// point, ~1.47 M of them on the trip this was measured against. [compute]
/// *copies its argument*, so handing it the GeoJSON serialised every one of
/// those; interleaved in a [Float64List] the same numbers copy as one buffer
/// per activity. This is the third instance of that trap in this codebase
/// (issue #276's `build_specs`, #320's `activities`, and this), and the one
/// that amplified #332's refetch loop into a 2.6 GB heap.
typedef FlatGeoCoords = Map<String, Float64List>;

/// Projects [geo]'s activity features into [FlatGeoCoords].
///
/// Applies exactly the skips the nested walk applied inline — non-map
/// features, segments, features with no activity_id, empty coordinate lists,
/// and individual points that are not a pair — so membership and point counts
/// are unchanged. [trackFeatureCoords] is the first four of those;
/// [flatCoords], behind [memoFlatCoords], is the last.
///
/// **O(features), not O(points), on a geo that came through
/// `decodeGeoOffIsolate`** (issue #337). Flattening a real trip's coordinates
/// measured 22.6 ms here, over the frame budget on its own, so the decode
/// worker — which is already holding these coordinates, and returns its
/// result zero-copy — produces the buffers and seeds them. This walk then
/// only looks them up. A feature the worker never saw still flattens on
/// demand, so client-built geometry keeps working; it is slower, never empty.
@visibleForTesting
FlatGeoCoords flattenGeoCoords(Map<String, dynamic>? geo) {
  final out = <String, Float64List>{};
  final features = geo?['features'];
  if (features is! List) return out;
  for (final f in features) {
    final t = trackFeatureCoords(f);
    if (t == null) continue;
    out[t.activityId] = memoFlatCoords(t.coords);
  }
  return out;
}

/// Builds the distance-indexed full-trip track (and per-activity tracks) from
/// [args.geo] + [args.totals] — the pure computation behind
/// [ProjectNotifier._buildFullTrack]. Pure and top-level (only plain
/// JSON-shaped input/output) so it can run via [compute] on a background
/// isolate for a large trip: walking every coordinate of every activity with
/// no size threshold (issue #276) was the same "big computation on the UI
/// isolate" mistake this codebase's other per-trip-size computations
/// (computeElevationSpots, hitTestMapTap, decimatePolylinePoints) already
/// guard against.
///
/// It takes [ActivityTotals] rather than the activity maps because the
/// argument to [compute] is *copied*: see that typedef. Exposed (not
/// `_`-prefixed) only for testing this computation directly; every real
/// caller is ProjectNotifier._buildFullTrack.
@visibleForTesting
({List<(double, GeoPoint)> fullTrack, Map<String, List<(double, GeoPoint)>> perActivityTracks})
    buildFullTrackFromTotals(
        ({FlatGeoCoords coords, ActivityTotals totals}) args) {
  final geoCoords = args.coords;
  final combined = <(double, GeoPoint)>[];
  final perAct = <String, List<(double, GeoPoint)>>{};
  double offsetKm = 0;
  for (final a in args.totals) {
    final actId = a.id;
    final coords = actId != null ? geoCoords[actId] : null;
    final elevTotalKm = a.elevTotalKm;
    final actTrack = <(double, GeoPoint)>[];
    if (coords != null && coords.isNotEmpty) {
      // Always distance-based, never index-based.
      //
      // This used to pair profile[i] with coords[i] whenever there were at
      // least as many coordinates as profile samples, which is only correct
      // when the two are index-aligned — i.e. when the profile is at full GPS
      // resolution. Given a *downsampled* profile it silently mapped the
      // whole distance range onto the leading fraction of the geometry: a
      // 10 km track with a 10-sample profile ended at 10% of its true length,
      // so the map cursor pointed at the wrong place entirely.
      //
      // That is the reason the client fetched the ~33 MB full-resolution
      // details payload at all (issue #295): the low-res profile /meta
      // already carries could not be used. Distances come from the geometry
      // and are scaled to the profile's total, so the only thing needed from
      // the profile is that one number — at any resolution.
      // Interleaved [lon, lat, lon, lat, …] — malformed points were already
      // dropped by flattenGeoCoords, so every pair here is usable.
      final pts = <GeoPoint>[];
      for (var i = 0; i + 1 < coords.length; i += 2) {
        pts.add((lat: coords[i + 1], lon: coords[i]));
      }
      actTrack.addAll(buildTrackFromPolyline(pts, elevTotalKm: elevTotalKm));
    }
    final reduced = downsampleTrack(actTrack, kMaxTrackPointsPerActivity);
    if (actId != null) perAct[actId] = reduced;
    for (final pt in reduced) {
      combined.add((pt.$1 + offsetKm, pt.$2));
    }
    if (elevTotalKm > 0) offsetKm += elevTotalKm;
  }
  return (fullTrack: combined, perActivityTracks: perAct);
}

/// A monotonic call-identity counter paired with the ref its current holder
/// was started for. Closes two related gaps a bare `_loadKey != ref` /
/// `this.ref != ref` guard has (issue #283):
///
/// - `ProjectRef.==` is structural (name/ownerId/role — see project_ref.dart),
///   so comparing against *any* ref only ever catches navigation to a
///   *different* project, never a second concurrent call for the *same* one
///   superseding an earlier one still mid-flight (e.g. the user mashing a
///   Retry button). The token adds call identity on top of that.
/// - Comparing against a *mutable* ref field that the call's own body
///   legitimately reassigns mid-flight (`ProjectNotifier._applyDetails`
///   corrects `this.ref`'s name/role from the server's response) can
///   misreport a still-current, unraced call as stale the moment that
///   correction lands. Comparing against the ref this track was *started*
///   with — frozen at [begin], the same convention `_loadKey` already used —
///   isn't affected by that, since nothing but [begin] ever touches it.
///
/// Give each independent family of top-level entry points that should be
/// able to supersede *each other* (but must not cancel some *other* family's
/// unrelated background work) its own instance — seeing three of these on
/// [ProjectNotifier] is intentional, not an oversight: sharing one across
/// families that don't have a legitimate reason to cancel each other is
/// exactly the bug this PR's own first cut introduced and then had to walk
/// back for `_silentReloadDetailsOnly`.
class _SupersessionTrack {
  int _token = 0;
  ProjectRef? _ref;

  /// The token/ref most recently passed to [begin] — exposed so a
  /// staged-loading subclass can read a track it doesn't own the entry point
  /// for (e.g. loadView()/loadShared() reading the base ProjectNotifier's
  /// load track via [ProjectNotifier.currentLoadToken]/[ProjectNotifier.currentLoadKey]).
  int get token => _token;
  ProjectRef? get ref => _ref;

  /// Starts a new call for [ref]. Call synchronously, before any await, at
  /// the very top of a top-level entry point. Returns the token to thread
  /// through that call's background continuations.
  int begin(ProjectRef ref) {
    _ref = ref;
    return ++_token;
  }

  /// Bumps the token without starting a new call for any particular ref —
  /// invalidates every previously-[begin]-ed call without this track itself
  /// now claiming anything is current. For teardown (e.g.
  /// [ProjectNotifier.clear]), where there's no new ref to hand a fresh call.
  void invalidate() => _token++;

  /// True while [token]/[ref] are still what this track was most recently
  /// [begin]-ed with. Check immediately before every mutation of shared
  /// notifier state and before every notify in a method that has awaited —
  /// not just once at the top — since anything can supersede this call
  /// during any of those awaits.
  bool isCurrent(int token, ProjectRef ref) => token == _token && _ref == ref;
}

class ProjectNotifier extends ChangeNotifier
    with ProjectFilterMixin, ProjectQuotaMixin, ProjectJournalCrudMixin, ProjectMemoryCrudMixin, ProjectPeopleCrudMixin, ProjectSegmentCrudMixin {
  final ProjectService _service;
  final MembersService _membersService;

  ProjectNotifier(this._service, {MembersService? membersService})
      : _membersService = membersService ?? MembersService();

  /// The addressing for the currently open project (name + owner + role —
  /// issue #106). Null until [load] has been called at least once.
  ProjectRef? ref;

  String? get projectName => ref?.name;

  /// Capability getters (issue #109) — screens gate UI on these rather than
  /// a single editor/owner boolean. Default to the most permissive tier when
  /// no ref is set yet (mirrors [ProjectRef]'s own-project default), so a
  /// pre-load screen doesn't flash a locked-down UI for what will turn out
  /// to be the caller's own project.
  bool get isViewer => ref?.isViewer ?? false;
  bool get canEditContent => ref?.canEditContent ?? true;
  bool get canManageTrip => ref?.canManageTrip ?? true;
  bool get isProjectOwner => ref?.isOwner ?? true;

  @override List<Map<String, dynamic>> activities = [];
  @override List<Map<String, dynamic>> items = [];   // ordered project items (activities + segments + memories)
  @override List<Map<String, dynamic>> people = [];  // trip people directory (#40)
  @override List<Map<String, dynamic>> groups = [];  // people groups (#50)
  /// Memory/journal fields [_revealItems] left as ciphertext (#466).
  @override final UndecryptedFields undecryptedFields = UndecryptedFields();
  @override Map<String, dynamic>? geo;
  bool isLoading = false;
  @override String? error;

  /// HTTP status of the [ApiException] that failed the last [load], or null
  /// when the load succeeded / failed for a non-API reason. Lets screens
  /// distinguish a 404 (stale shared-project ref after an owner rename —
  /// issue #111) from other errors.
  int? loadErrorStatus;

  /// True when the current [activities]/[items]/[geo] came from
  /// [projectDataCache]'s on-device store rather than a live server
  /// response — set only when the initial `/meta` fetch in [load] fails
  /// outright (offline / server unreachable) and a previously cached copy of
  /// this project exists. Screens show a "showing last saved version" banner
  /// while this is true; the next successful [load] clears it.
  bool offlineFromCache = false;

  /// Progressive-loading flags — default true so manage-mode screens that use
  /// the base load() see no behaviour change.  Set to false at the start of
  /// loadShared() / loadView() and flipped to true as each phase completes.
  bool isMetaLoaded = true;
  bool isElevationLoaded = true;
  bool isGeoLoaded = true;

  /// True once the background sync-status/share-link fetch (auto-sync
  /// setting, linked Polarsteps trip, share tokens) has completed for the
  /// current [ref] — false while it's still in flight. Unlike the flags
  /// above, this is set by base [load()] itself (every mode fetches this
  /// data the same way, gated only by [loadOwnerExtras]): false at the start
  /// of a load whenever there's something to wait for, true once that
  /// background fetch lands (or immediately when [loadOwnerExtras] is
  /// false — nothing to wait for there). A screen reading autoSyncEnabled /
  /// linkedPsTripId / shareToken / shareTokenNoMemories directly should gate
  /// on this rather than assume isLoading turning false means they're
  /// already populated.
  bool isSyncMetaLoaded = true;

  /// The activity currently highlighted on the map. Null = no selection.
  @override dynamic selectedActivityId;

  /// The connecting segment currently highlighted on the map. Null = no selection.
  @override dynamic selectedSegmentId;

  /// The memory currently highlighted on the map/panel. Null = no selection.
  @override dynamic selectedMemoryId;

  /// The journal entry currently highlighted on the map/panel. Null = no selection.
  dynamic selectedJournalId;

  /// Whether journal markers and list items are visible.
  bool showJournals = true;

  /// The day currently selected in the activity panel ("YYYY-MM-DD" or null).
  @override String? selectedDay;

  /// Days selected in multi-select mode. Empty = no multi-day filter.
  @override Set<String> selectedDays = {};

  /// User-defined trip start date override ("YYYY-MM-DD"); null = infer from activities.
  String? tripStart;

  /// User-defined trip end date ("YYYY-MM-DD"); null = trip still ongoing.
  String? tripEnd;

  /// True if the trip is still active (no tripEnd set, or tripEnd is today or later).
  bool get _tripIsActive {
    if (tripEnd == null) return true;
    final end = DateTime.tryParse(tripEnd!);
    if (end == null) return true;
    final now = DateTime.now();
    return !end.isBefore(DateTime(now.year, now.month, now.day));
  }

  /// Day metadata keyed by "YYYY-MM-DD".
  @override Map<String, Map<String, dynamic>> dayMeta = {};

  /// Project-specific list of sleeping type options.
  List<String> sleepingOptions = [];

  /// Group assignment for each sleeping option: name → "Outdoors"|"Indoors"|"Other".
  Map<String, String> sleepingOptionGroups = {};

  /// Project-defined counters: [{name: String, start: double}].
  List<Map<String, dynamic>> counters = [];

  // ── Share tokens ─────────────────────────────────────────────────────────
  String? shareToken;
  String? shareTokenNoMemories;

  // ── Auto-sync state ──────────────────────────────────────────────────────
  bool autoSyncEnabled = true;
  int? linkedPsTripId;
  double? lastStravaSyncAt;
  double? lastPsSyncAt;

  /// Non-null when background check found new items; cleared by markSynced().
  ({List<Map<String, dynamic>> strava, List<Map<String, dynamic>> polarsteps})? pendingSync;

  /// True when a periodic background check found a segment that was only an
  /// approximate straight line (route_degraded=true) has since resolved with
  /// real track data — most likely sweep_degraded_segments() on the server
  /// (issue #207). Cleared by dismissDegradedRouteUpgrade() or
  /// reloadForDegradedUpgrade(). Deliberately never applied automatically —
  /// a silent background rewrite of the map/track a user is actively looking
  /// at or editing is exactly what this avoids; the user picks the timing.
  bool degradedRouteUpgradeAvailable = false;

  // ── Track style ───────────────────────────────────────────────────────────
  // Named so [clear] puts back the same defaults a fresh notifier starts with.
  static const _kDefaultTrackColor = Color(0xFF6B7280); // gray-500 — shown while project loads
  static const _kDefaultTrackWidth = 2.5;
  Color trackColor = _kDefaultTrackColor;
  Color? trackSecondaryColor; // null = auto-derive from primary
  double trackWidth = _kDefaultTrackWidth;
  bool alternatingTrackColors = false;
  Color? elevationChartColor; // null = "auto" → match the map track line (#22)
  bool elevationChartShowLine = true;

  /// Opt-in per-type colouring (issue #95). Off by default so existing
  /// projects keep today's flat trackColor line rendering unchanged.
  bool colorByType = false;
  /// Per-bucket overrides, keyed by activity bucket ("ride"/"run"/"hike"/
  /// "other") or segment type ("flight"/"train"/"bus"/"boat"). Each value
  /// e.g. {"color": "#RRGGBB", "style": "solid"|"dashed"|"dotted"}. Missing
  /// bucket = built-in default (see design_tokens.dart resolveTypeStyle).
  Map<String, Map<String, dynamic>> typeStyles = {};

  /// Colour the elevation chart actually renders with: the user's explicit
  /// override, or — when unset ("auto") — the map track line colour, so the
  /// chart matches the line on the map by default (issue #22).
  Color get effectiveElevationChartColor => elevationChartColor ?? trackColor;

  // ── Translation languages ─────────────────────────────────────────────────
  List<String> languages = [];

  Future<void> setTrackStyle({
    Color? color,
    Object? secondaryColor = _kUnset, // pass null explicitly to clear
    double? width,
    bool? alternating,
    Object? elevationColor = _kUnset, // pass null explicitly to clear
    bool? elevationShowLine,
    bool? colorByTypeEnabled,
    Map<String, Map<String, dynamic>>? typeStyleOverrides,
  }) async {
    if (color != null) trackColor = color;
    if (secondaryColor != _kUnset) trackSecondaryColor = secondaryColor as Color?;
    if (width != null) trackWidth = width;
    if (alternating != null) alternatingTrackColors = alternating;
    if (elevationColor != _kUnset) elevationChartColor = elevationColor as Color?;
    if (elevationShowLine != null) elevationChartShowLine = elevationShowLine;
    if (colorByTypeEnabled != null) colorByType = colorByTypeEnabled;
    if (typeStyleOverrides != null) typeStyles = typeStyleOverrides;
    notifyListeners();
    final ref = this.ref;
    if (ref == null) return;
    try {
      await _service.saveTrackStyle(
        ref,
        trackColor: color != null ? _colorToHex(color) : null,
        trackSecondaryColor: secondaryColor != _kUnset
            ? (secondaryColor != null ? _colorToHex(secondaryColor as Color) : null)
            : _kUnset,
        trackWidth: width,
        alternating: alternating,
        elevationChartColor: elevationColor != _kUnset
            ? (elevationColor != null ? _colorToHex(elevationColor as Color) : null)
            : _kUnset,
        elevationChartShowLine: elevationShowLine,
        colorByType: colorByTypeEnabled,
        typeStyles: typeStyleOverrides,
      );
    } on Exception catch (e) {
      error = _msg(e);
      notifyListeners();
    }
  }

  static const Object _kUnset = Object();

  Future<void> saveLanguages(List<String> langs) async {
    languages = List<String>.from(langs);
    notifyListeners();
    final ref = this.ref;
    if (ref == null) return;
    try {
      await _service.saveLanguages(ref, langs);
    } on Exception catch (e) {
      error = _msg(e);
      notifyListeners();
    }
  }

  static String _colorToHex(Color c) =>
      '#${(c.toARGB32() & 0xFFFFFF).toRadixString(16).padLeft(6, '0').toUpperCase()}';

  static const _defaultSleepingOptions = [
    'Camping', 'Bivouac', 'Shelter', 'Pension/Guesthouse', 'Hotel', 'Apartment',
  ];

  static const _defaultSleepingGroups = {
    'Camping': 'Outdoors', 'Bivouac': 'Outdoors', 'Shelter': 'Outdoors',
    'Hotel': 'Indoors', 'Pension/Guesthouse': 'Indoors',
    'Apartment': 'Indoors', 'Warmshower': 'Indoors',
    'Friend': 'Other', 'Transportation': 'Other',
  };

  void selectActivity(dynamic id) {
    selectedActivityId =
        selectedActivityId?.toString() == id?.toString() ? null : id;
    selectedSegmentId = null;
    selectedMemoryId = null;
    selectedJournalId = null;
    selectedDay = null;
    selectedDays = {};
    saveUiState();
    notifyListeners();
  }

  void selectSegment(dynamic id) {
    selectedSegmentId =
        selectedSegmentId?.toString() == id?.toString() ? null : id;
    selectedActivityId = null;
    selectedMemoryId = null;
    selectedJournalId = null;
    selectedDay = null;
    selectedDays = {};
    saveUiState();
    notifyListeners();
  }

  void selectMemory(dynamic id) {
    selectedMemoryId =
        selectedMemoryId?.toString() == id?.toString() ? null : id;
    selectedActivityId = null;
    selectedSegmentId = null;
    selectedJournalId = null;
    selectedDay = null;
    selectedDays = {};
    saveUiState();
    notifyListeners();
  }

  void selectJournal(dynamic id) {
    selectedJournalId =
        selectedJournalId?.toString() == id?.toString() ? null : id;
    selectedActivityId = null;
    selectedSegmentId = null;
    selectedMemoryId = null;
    selectedDay = null;
    selectedDays = {};
    notifyListeners();
  }

  void toggleJournals() {
    showJournals = !showJournals;
    notifyListeners();
  }

  void selectDay(String? dateKey) {
    selectedDay = dateKey;
    selectedActivityId = null;
    selectedSegmentId = null;
    selectedMemoryId = null;
    selectedDays = {};
    saveUiState();
    notifyListeners();
  }

  void selectDays(Set<String> days) {
    selectedDays = Set.from(days);
    selectedActivityId = null;
    selectedSegmentId = null;
    selectedMemoryId = null;
    selectedDay = null;
    saveUiState();
    notifyListeners();
  }

  // ── UI-state persistence (issue #76 follow-up) ─────────────────────────────
  // A forced page reload (the black-screen JS backstop) wipes all in-memory
  // Dart state. Selection + filters are cheap to round-trip through
  // shared_preferences, keyed per project so switching projects on this
  // (singleton, manage-mode) notifier doesn't cross-write between them.

  /// Whether this notifier saves and restores selection + filters at all.
  ///
  /// A share link's notifier must not: its trip is addressed by a token, and
  /// once /meta lands its ref is just the trip's name, with no owner — so it
  /// would read and write the state of the viewer's own trip of that name.
  @protected
  bool get persistsUiState => true;

  /// Where [ref]'s UI state lives, or null when it is not persisted. Every
  /// read and write goes through here (#409 review).
  ///
  /// `project_ui_state_<you>:<owner>:<name>`, which is unique where the name
  /// alone never was:
  ///   * a trip name is unique per owner, not globally, so a companion's trip
  ///     is likely to share a name with one of yours (#106);
  ///   * one browser can hold more than one account's sessions (#93).
  /// Keyed by name alone, opening either of those restored *your* state,
  /// pruned it against *their* data and wrote the loss back.
  ///
  /// "Own" is decided by id, not by role: the owner is the signed-in account
  /// whether [ProjectRef.ownerId] is null (a deep link) or your own id (the
  /// projects list opens your trips as `?owner=<you>`), and neither depends on
  /// a role that is only a guess until /meta answers. Null — nothing saved or
  /// restored — when no account is signed in to say whose state it is.
  String? _uiStateKey(ProjectRef ref) {
    if (!persistsUiState) return null;
    final self = api.tokenUserId;
    if (self == null) return null;
    return 'project_ui_state_$self:${ref.ownerId ?? self}:${ref.name}';
  }

  /// The name-only key UI state lived under before [_uiStateKey]. Only ever
  /// read for the signed-in account's own trip, and only online, when that trip
  /// has no state under [_uiStateKey] yet; that restore moves it across and
  /// deletes it once the store confirms the write, so state saved before the
  /// upgrade restores once, for whichever account claims it first.
  ///
  /// Nothing else deletes it. A trip that already has state of its own (a tap
  /// during an offline first open, say) never reads it, and it stays behind
  /// for another account's same-named trip to claim: deleting a key nobody has
  /// claimed lost state outright, once when the cache held a write the store
  /// had refused, and once when it was another account's (#409 review).
  String? _legacyUiStateKey(ProjectRef ref) {
    final self = api.tokenUserId;
    final own = ref.ownerId == null || ref.ownerId == self;
    return own ? 'project_ui_state_${ref.name}' : null;
  }

  /// The saved-state key the in-memory filters belong to; see [load].
  String? _heldStateKey;

  @override
  void saveUiState() => unawaited(_saveUiState());

  /// Whether the state reached storage. `containsKey` cannot say: the plugin
  /// updates its in-memory cache before it calls the store, so a write that
  /// the store refuses (a full localStorage, a failed Android commit) still
  /// reads back until the next page load.
  Future<bool> _saveUiState() async {
    // A discarded notifier's state is not the signed-in account's to save:
    // the key is built from the token, which may be the next account's
    // (issue #418).
    if (_isDisposed) return false;
    final ref = this.ref;
    if (ref == null) return false;
    final key = _uiStateKey(ref);
    if (key == null) return false;
    try {
      final data = <String, dynamic>{
        'selectedDay': selectedDay,
        'selectedActivityId': selectedActivityId?.toString(),
        'selectedSegmentId': selectedSegmentId?.toString(),
        'selectedMemoryId': selectedMemoryId?.toString(),
        'tags': filters.tags.toList(),
        'sleeping': filters.sleeping.toList(),
        'activityTypes': filters.activityTypes.toList(),
        'transport': filters.transport.toList(),
        'sources': filters.sources.toList(),
      };
      final prefs = await SharedPreferences.getInstance();
      return await prefs.setString(key, jsonEncode(data));
    } catch (_) {
      // Best-effort only — this is fire-and-forget from every selection/filter
      // mutator, so a plugin/storage failure here must never surface as an
      // unhandled async error (e.g. a browser blocking storage access).
      return false;
    }
  }

  /// Restores selection + filters persisted by [_saveUiState] for the
  /// just-loaded project, dropping any reference that no longer resolves to
  /// real data (e.g. a deleted activity/segment/memory or a day that's no
  /// longer in [dayMeta]) so a stale selection can't resurrect as a dangling
  /// reference. Silently no-ops on missing/malformed prefs.
  ///
  /// [loadRef] is the ref [load] was called with, which is what the load track
  /// knows it by. [ref] has been replaced since with the server's name and
  /// `caller_role`, so checking currency against it failed whenever the role
  /// guessed before /meta was wrong — every viewer and co-owner, on every
  /// open — and restore silently did nothing (#409 review).
  Future<void> _restoreUiState(int token, ProjectRef loadRef) async {
    final ref = this.ref;
    if (ref == null) return;
    final key = _uiStateKey(ref);
    if (key == null) return;
    try {
      final prefs = await SharedPreferences.getInstance();
      // issue #283: also reject a same-ref load that superseded this one
      // while awaiting prefs, which a bare ref comparison can't detect.
      if (!_isCurrent(token, loadRef)) return;
      var raw = prefs.getString(key);
      // Not offline: an offline restore does not prune (below), so the old
      // key's values would be applied raw — and that key is not per account,
      // so they may be another account's tags and sleeping modes, offered as
      // chips and saved under this account on the next tap.
      final oldKey = offlineFromCache ? null : _legacyUiStateKey(ref);
      final hasOld = oldKey != null && prefs.containsKey(oldKey);
      final migrating = raw == null && hasOld;
      if (migrating) raw = prefs.getString(oldKey);
      if (raw == null) return;
      final data = jsonDecode(raw) as Map<String, dynamic>;

      final saved = ProjectFilters(
        tags: (data['tags'] as List?)?.cast<String>().toSet() ?? const {},
        sleeping:
            (data['sleeping'] as List?)?.cast<String>().toSet() ?? const {},
        activityTypes:
            (data['activityTypes'] as List?)?.cast<String>().toSet() ?? const {},
        transport:
            (data['transport'] as List?)?.cast<String>().toSet() ?? const {},
        // Absent from state saved before the source filter existed, which the
        // ?? handles: an older payload restores with no source constraint.
        sources: (data['sources'] as List?)?.cast<String>().toSet() ?? const {},
      );
      // Offline, the trip is a cached /meta snapshot that local edits do not
      // refresh (saveDayMeta never touches it), so "the trip no longer holds
      // it" cannot be told from "the snapshot predates it". Pruning against it
      // would drop a filter that is still good, and not only for this session:
      // skipping just the write-back is not enough, because the next selection
      // change saves the pruned in-memory set anyway. A stale value restored
      // here still has its chip in the sheet, and the next online load prunes.
      final pruned = restoreFilters(saved, prune: !offlineFromCache);

      final savedDay = data['selectedDay'] as String?;
      if (savedDay != null && dayMeta.containsKey(savedDay)) {
        selectedDay = savedDay;
      }

      final savedActivityId = data['selectedActivityId'] as String?;
      if (savedActivityId != null) {
        final activityIds = activities.map((a) => a['id']?.toString()).toSet();
        if (activityIds.contains(savedActivityId)) {
          selectedActivityId = savedActivityId;
        }
      }

      final savedSegmentId = data['selectedSegmentId'] as String?;
      if (savedSegmentId != null) {
        final segmentIds = items
            .where((i) => i['item_type'] == 'segment')
            .map((i) => (i['segment'] as Map?)?['id']?.toString())
            .whereType<String>()
            .toSet();
        if (segmentIds.contains(savedSegmentId)) {
          selectedSegmentId = savedSegmentId;
        }
      }

      final savedMemoryId = data['selectedMemoryId'] as String?;
      if (savedMemoryId != null) {
        final memoryIds = items
            .where((i) => i['item_type'] == 'memory')
            .map((i) => (i['memory'] as Map?)?['id']?.toString())
            .whereType<String>()
            .toSet();
        if (memoryIds.contains(savedMemoryId)) {
          selectedMemoryId = savedMemoryId;
        }
      }

      // A filter value dropped above is dropped in memory only. Left in storage
      // it comes back to life the next time the trip gains matching data (a
      // new import, another hike, a re-added tag): the list narrows and the
      // badge lights up for a filter the user never re-ticked.
      //
      // State read from the old key is written under the new one the same
      // way, and the old key deleted only once the store has confirmed that
      // write — otherwise it would be read again on every open, and deleting
      // it after a refused write would lose it outright.
      //
      // This has to run LAST. _saveUiState builds its payload synchronously
      // before its first await, and load() nulls the four selection fields
      // before fetching — so saving here from anywhere above would persist
      // those nulls and destroy the saved day/activity/segment/memory that the
      // restores just above are in the middle of reading back.
      if (pruned || migrating) {
        final written = await _saveUiState();
        if (migrating && written) await prefs.remove(oldKey);
      }
    } catch (_) {
      // Malformed/missing prefs — restore is best-effort only.
    }
  }

  /// Re-reads the saved selection and filters into a notifier that is reused
  /// without a reload (issue #418, second comment). Another notifier — view
  /// mode's — may have changed them since this one last read them, and the
  /// next tap here would save this one's stale copy over that change.
  Future<void> restoreSavedUiState() async {
    final loadRef = _loadTrack.ref;
    if (loadRef == null) return;
    final token = _loadTrack.token;
    // Cleared first, as load() does: the restore applies only what was saved,
    // so a selection this notifier made would survive beside the one view
    // mode saved since (U5-R1-4).
    selectedDay = null;
    selectedActivityId = null;
    selectedSegmentId = null;
    selectedMemoryId = null;
    await _restoreUiState(token, loadRef);
    if (_isCurrent(token, loadRef)) notifyListeners();
  }

  // Cached aggregate stats — computed once in load(), not on every build.
  double totalDistanceM = 0;
  int totalMovingSeconds = 0;
  double totalElevationGainM = 0;

  /// Loads project details and GeoJSON in two phases.
  ///
  /// Phase 1 (fast): fetches details + low-res GeoJSON in parallel.
  /// The map renders immediately with straight-line approximations.
  ///
  /// Phase 2 (background): fetches full-res GeoJSON and progressively
  /// replaces each activity's straight line with its real GPS trace,
  /// starting from the last activity.
  /// Subclasses can return false to skip owner-only authenticated calls
  /// (sync-meta, share-info, background sync check).
  bool get loadOwnerExtras => true;

  /// Per-share content key (issue #28), or null when not applicable. Only
  /// [SharedProjectNotifier] overrides this — the owner's authenticated
  /// notifier never has one (it doesn't need it; the owner already sees
  /// decrypted content via the CMK).
  SecretKey? get shareContentKey => null;

  // ── Load supersession (issue #283) ────────────────────────────────────────
  //
  // Three independent _SupersessionTrack instances — see that class's doc for
  // why one shared counter across all of them would be wrong. Each protects a
  // family of top-level entry points that legitimately supersede *each
  // other*, without being able to cancel a *different* family's unrelated
  // background work:
  //
  // - _loadTrack: load() and its own progressive Phase 2
  //   (_loadFullGeoProgressively/_loadElevationData/applyFullActivities) —
  //   the original full project load.
  // - _reloadTrack: _silentReload()/_applyRefreshedProject() — CRUD-triggered
  //   full reloads (remove/split/reset/refresh an activity, etc.). These
  //   fetch their own fresh geo/details, so superseding each other is fine,
  //   but sharing _loadTrack with them would let one of these unrelated
  //   mutations cancel load()'s still-useful, still in-flight background
  //   fetch, discarding real network I/O for no reason.
  // - _detailsOnlyReloadTrack: _silentReloadDetailsOnly() (reorder, sort,
  //   trip-date edit, memory/people/journal/segment CRUD) — kept separate
  //   from _reloadTrack too, for the same reason again one level down: unlike
  //   _silentReload/_applyRefreshedProject, this one never refreshes
  //   geo/fullTrack itself, so it has even less business superseding
  //   anything that does.
  final _loadTrack = _SupersessionTrack();
  final _reloadTrack = _SupersessionTrack();
  final _detailsOnlyReloadTrack = _SupersessionTrack();

  /// The ref [load] was most recently started with. Lets a staged-loading
  /// subclass's own background phase (e.g. loadView's/loadShared's Phase 2)
  /// detect navigation away without comparing against the mutable `ref`
  /// field (whose name/role get corrected from the server's response during
  /// Phase 1).
  @protected
  ProjectRef? get currentLoadKey => _loadTrack.ref;

  /// The token [load] most recently started with — lets a staged-loading
  /// subclass's own background phase detect supersession the same way
  /// [applyFullActivities] does, including a same-ref race that
  /// [currentLoadKey] alone can't catch (see [_SupersessionTrack]).
  @protected
  int get currentLoadToken => _loadTrack.token;

  /// True while [token]/[ref] are still current for [_loadTrack] — i.e.
  /// [load]'s own flow. Shorthand for `_loadTrack.isCurrent(token, ref)`,
  /// used throughout `load()`/`_loadFullGeoProgressively`/`_loadElevationData`.
  bool _isCurrent(int token, ProjectRef ref) => _loadTrack.isCurrent(token, ref);

  /// Retry delays for the initial fetch pair — overridable so tests don't wait
  /// real seconds, like refreshActivity's pollInterval/pollTimeout.
  @visibleForTesting
  List<Duration> loadRetryBackoff = kFetchRetryBackoff;

  Timer? _photoPollingTimer;
  Timer? _degradedRouteCheckTimer;
  int? _lastDegradedRouteCount;

  Future<void> load(ProjectRef ref) async {
    if (ref.name.isEmpty) return;
    final name = ref.name;
    _stopPhotoPolling();
    final token = _loadTrack.begin(ref);
    // A reload still in flight — full or details-only, a segment conflict's
    // resync among them — sets the open trip when it lands, so one for the
    // trip the user is leaving would bring it back (I1-R4-3, I1-R5-1). Dropped
    // for this trip too: this load fetches its details afresh. One begun after
    // this line checks the open trip itself.
    _reloadTrack.invalidate();
    _detailsOnlyReloadTrack.invalidate();
    this.ref = ref;
    // Filters are kept only while they belong to the same saved state: the
    // same account's view of the same trip. This notifier is app-wide and
    // outlives a logout, so keeping them for "the same trip" by name and owner
    // handed one account's filter to the next, and a trip switch kept the last
    // trip's filter with selectedDays emptied below (#409 review). Resetting on
    // every load instead lost a filter changed during a same-trip reload —
    // after an import, an Undo or a Retry the sheet's chips stay up while
    // /meta is out, and a tap saved over the reset set.
    //
    // A null key (nothing is saved: no account, or a share link) always
    // resets, because there is no saved state to say the filters are still
    // this trip's. A failed load leaves the key held, so its Retry keeps them.
    final stateKey = _uiStateKey(ref);
    if (stateKey == null || stateKey != _heldStateKey) resetFilters();
    _heldStateKey = stateKey;
    // Zoom refetching is armed only once this load's own geometry lands, and
    // never carries across projects: this notifier is a single app-wide
    // provider, so a bucket left from the previous trip would let a refetch
    // replace geometry it has nothing to do with — and on an E2EE trip, whose
    // geo is built client-side and which the server cannot serve at all,
    // replace the route with an empty FeatureCollection.
    _zoomRefetchTimer?.cancel();
    _loadedZoomBucket = null;
    // Dropped with the bucket for the same reason: a box from the previous
    // trip describes a region this one may be nowhere near.
    _loadedGeoBox = null;
    _mapViewport = null;
    if (perfSpans.enabled) perfSpans.reset();  // scope spans to this load
    // Counted session-wide: an unexpected reload is the likeliest way heavy
    // work lands mid-gesture, and it is invisible unless someone counts.
    perfSpans.recordLoad();
    isLoading = true;
    error = null;
    loadErrorStatus = null;
    offlineFromCache = false;
    // Nothing to wait for when loadOwnerExtras is false (view mode still
    // has this true; shared mode doesn't) — see isSyncMetaLoaded's doc.
    isSyncMetaLoaded = !loadOwnerExtras;
    activities = [];
    items = [];
    geo = null;
    clearSegmentOverlay();  // discard any prior project's pending segment patches
    selectedActivityId = null;
    selectedSegmentId = null;
    selectedMemoryId = null;
    selectedDay = null;
    selectedDays = {};
    pendingSync = null;
    members = [];
    pendingInvites = [];
    memberInviteToken = null;
    memberInviteRole = null;
    notifyListeners();

    try {
      // Fire both simultaneously.  /meta omits elevation_profile (~12 MB) so
      // the panel becomes interactive in ~1-2 s instead of ~17 s.  Elevation
      // data is fetched separately in the background (see below).
      //
      // When E2EE is unlocked the server can't build geo for an encrypted
      // activity's geometry (issue #29) — skip the parallel low-res fetch
      // (it would be discarded) and build low-res geo client-side below,
      // once decrypted activities/items are available.
      //
      // Each is retried on its own (issue #178): both start immediately, so the
      // parallelism is unchanged, but a slow /meta no longer re-fetches the
      // low-res geo that already arrived. isLoading stays true throughout, so
      // the panel shows its spinner while the retries run rather than an error.
      final detailsFuture = retryFetch(() => _service.getDetailsMeta(ref),
          backoff: loadRetryBackoff, abort: () => !_isCurrent(token, ref));
      final lowResFuture = encryption.isUnlocked
          ? null
          : retryFetch(() => _service.getLowResGeo(ref),
              backoff: loadRetryBackoff, abort: () => !_isCurrent(token, ref));
      // Both futures need a listener from the moment they're created: if
      // lowResFuture rejects first, the catch below returns before
      // detailsFuture is ever awaited, leaving it truly unobserved — a later
      // rejection on it then surfaces as an orphaned, uncatchable async error
      // instead of being handled here. ignore() is a no-op when we do go on
      // to await each future normally below.
      detailsFuture.ignore();
      lowResFuture?.ignore();

      if (lowResFuture != null) {
        Map<String, dynamic>? lowRes;
        try {
          lowRes = await lowResFuture;
        } on Object catch (_) {
          // Non-fatal: fall back to whatever low-res geo is on file (may be
          // null, e.g. a project never opened on this device before) and let
          // the /meta fallback below decide whether this load can proceed at
          // all — a bare map with no track is still better than an error
          // screen when offline and a fuller cache entry exists.
          lowRes = await projectDataCache.readLowResGeo(ref);
        }
        // Checked before assigning: a load superseded or cleared meanwhile —
        // an account change among them — must not put its geometry back
        // (U5-R1-3).
        if (!_isCurrent(token, ref)) return;
        geo = lowRes;
        notifyListeners(); // map visible at ~2.2s
      }

      Map<String, dynamic> details;
      try {
        details = await detailsFuture;
      } on Object catch (_) {
        final cachedMeta = await projectDataCache.readMetaForOfflineFallback(ref);
        if (cachedMeta == null) rethrow; // no cache to fall back to — surface the real error
        details = cachedMeta;
        offlineFromCache = true;
      }
      if (!_isCurrent(token, ref)) return;

      // caller_role (issue #109) is the server's authoritative answer to
      // "what's my tier here" — corrects the resolveRoleFor() placeholder
      // guess (own projects don't send it; role stays "owner").
      this.ref = ref.copyWith(
        name: details['name'] as String? ?? name,
        role: details['caller_role'] as String? ?? ref.role,
      );
      tripStart = details['trip_start'] as String?;
      tripEnd = details['trip_end'] as String?;
      final rawActivities = details['activities'];
      activities = rawActivities is List
          ? rawActivities.cast<Map<String, dynamic>>()
          : [];
      await _revealActivities(activities);
      final rawItems = details['items'];
      items = rawItems is List
          ? rawItems.cast<Map<String, dynamic>>()
          : [];
      await _revealItems(items);
      final rawPeople = details['people'];
      people = rawPeople is List
          ? rawPeople.cast<Map<String, dynamic>>()
          : [];
      final rawPeopleGroups = details['groups'];
      groups = rawPeopleGroups is List
          ? rawPeopleGroups.cast<Map<String, dynamic>>()
          : [];
      final rawDm = details['day_meta'];
      dayMeta = rawDm is Map
          ? rawDm.map((k, v) => MapEntry(k as String, Map<String, dynamic>.from(v as Map)))
          : {};
      final rawOpts = details['sleeping_options'];
      final optList = rawOpts is List ? List<String>.from(rawOpts) : <String>[];
      sleepingOptions = optList.isNotEmpty ? optList : List<String>.from(_defaultSleepingOptions);
      final rawGroups = details['sleeping_option_groups'];
      sleepingOptionGroups = rawGroups is Map
          ? Map<String, String>.from(rawGroups.cast<String, String>())
          : { for (final n in sleepingOptions) n: _defaultSleepingGroups[n] ?? 'Other' };
      final rawCounters = details['counters'];
      counters = rawCounters is List
          ? rawCounters.map((c) => Map<String, dynamic>.from(c as Map)).toList()
          : [];
      final rawColor = details['track_color'] as String?;
      if (rawColor != null && rawColor.length == 7 && rawColor.startsWith('#')) {
        trackColor = Color(int.parse(rawColor.substring(1), radix: 16) | 0xFF000000);
      }
      final rawSecColor = details['track_secondary_color'] as String?;
      trackSecondaryColor = (rawSecColor != null && rawSecColor.length == 7 && rawSecColor.startsWith('#'))
          ? Color(int.parse(rawSecColor.substring(1), radix: 16) | 0xFF000000)
          : null;
      final rawWidth = details['track_width'] as num?;
      if (rawWidth != null) trackWidth = rawWidth.toDouble();
      final rawAlt = details['alternating_track_colors'] as bool?;
      if (rawAlt != null) alternatingTrackColors = rawAlt;
      final rawElColor = details['elevation_chart_color'] as String?;
      elevationChartColor = (rawElColor != null && rawElColor.length == 7 && rawElColor.startsWith('#'))
          ? Color(int.parse(rawElColor.substring(1), radix: 16) | 0xFF000000)
          : null;
      final rawElLine = details['elevation_chart_show_line'] as bool?;
      if (rawElLine != null) elevationChartShowLine = rawElLine;
      final rawLangs = details['languages'];
      if (rawLangs is List) languages = rawLangs.cast<String>();
      final rawColorByType = details['color_by_type'] as bool?;
      if (rawColorByType != null) colorByType = rawColorByType;
      final rawTypeStyles = details['type_styles'];
      typeStyles = rawTypeStyles is Map
          ? rawTypeStyles.map((k, v) =>
              MapEntry(k as String, Map<String, dynamic>.from(v as Map)))
          : {};
      _updateStats();
      if (encryption.isUnlocked) {
        // Decrypted activities/items are ready now — build the low-res map
        // client-side (mirrors src/project/repo_core.py's _compute_low_res_geo).
        geo = client_geo.buildLowResGeo(items, client_geo.activitiesById(activities));
      }
      await _buildFullTrack();
      // Bug #1 of issue #283: without this check, a load() superseded while
      // _buildFullTrack() was hopping through compute() could still mutate
      // dayMeta/activities below for a project the user has since left.
      if (!_isCurrent(token, ref)) return;
      // A resolve started before this load — earlier in the session, before
      // the app was closed, or on another device — is still polled for, so its
      // route reaches the map without reopening the trip (issue #278).
      resumeSegmentResolves();
      _autoFillDaysToToday();  // fill missing dates in-memory before first render
      await _restoreUiState(token, ref);  // issue #76 follow-up: reapply persisted selection/filters
      // Catch Object, not just Exception: retryFetch rethrows whatever the last
      // attempt threw, and a truncated response decodes into an Error
      // (RangeError/TypeError) that would otherwise escape this handler.
    } on Object catch (e) {
      if (_isCurrent(token, ref)) {
        error = _loadErrorMessage(e);
        if (e is ApiException) loadErrorStatus = e.statusCode;
      }
    } finally {
      // Guard like the catch block above: a stale finally (from a load()
      // superseded while awaiting _buildFullTrack()'s compute() hop) must not
      // flip isLoading back to false or notify for a project that's no longer
      // current — a new load() for the current project may already be in
      // flight with isLoading = true.
      if (_isCurrent(token, ref)) {
        isLoading = false;
        notifyListeners();   // map appears here with low-res straight lines
      }
    }

    // Phase 2: full-res GeoJSON, then elevation data — chained rather than
    // fired together. Both are whole-project loads server-side (the elevation
    // one fetches the ~12 MB full dict), so racing them made a cold open ask
    // for the same project three times at once, each slowing the others down
    // into the timeout that produced issue #178.
    unawaited(_loadFullGeoProgressively(ref, token)
        .whenComplete(() => _loadElevationData(ref, token))
        // Dev diagnostic (issue #291): the whole load is finished here, so this
        // is where a run's span report is worth printing. No-op unless built
        // with --dart-define=PERF_TIMING=true.
        .whenComplete(perfSpans.report));

    // Sync status / share-link info — neither the map nor the activity panel
    // already rendered above needs them, so fetching them must not hold the
    // loading spinner open. Backgrounded the same way as Phase 2 above;
    // isSyncMetaLoaded lets a screen that reads autoSyncEnabled/
    // linkedPsTripId/shareToken/shareTokenNoMemories directly (e.g.
    // project-settings) know when it's safe to trust them instead of
    // assuming isLoading turning false means they're already populated.
    if (loadOwnerExtras) {
      unawaited(Future.wait([_loadSyncMeta(ref), _loadShareInfo(ref)]).then((_) {
        if (!_isCurrent(token, ref)) return;
        isSyncMetaLoaded = true;
        notifyListeners();
        // Background sync check — fires only for active trips with auto-sync
        // on, which is only known for certain once _loadSyncMeta above has
        // actually returned. Delayed 5s so it doesn't compete with the
        // full-res geo fetch on load.
        if (_tripIsActive && autoSyncEnabled) {
          Future.delayed(const Duration(seconds: 5), () {
            if (_isCurrent(token, ref)) _backgroundSyncCheck(ref);
          });
        }
      }));
    }
  }

  /// Set by the map screen's camera-event listener while the map camera is
  /// actively moving (drag, fling, programmatic fit animation, ...), so
  /// [_loadFullGeoProgressively]'s background full-map rebuilds can defer
  /// themselves until the camera settles instead of landing mid-gesture and
  /// competing with it for the same frame budget — jerkiness/ANR while
  /// panning shortly after a trip opens, since each geo upgrade forces
  /// MapPanel to rebuild every polyline and marker.
  bool _mapCameraActive = false;

  // ── Zoom level of detail (issue #295) ──────────────────────────────────
  //
  // Geometry is fetched simplified to about one screen pixel at the current
  // zoom, so what the client holds is a function of what is on screen rather
  // than of trip length. Zoom in and it asks for more; zoom out and it drops
  // back. The bucket is the whole level the server quantises to, so a pinch
  // does not mint a request per frame.
  double _mapZoom = 11;
  int? _loadedZoomBucket;
  Timer? _zoomRefetchTimer;

  // ── Viewport scoping (issue #324) ──────────────────────────────────────
  //
  // Zoom bounds the detail, not the extent, so a deep zoom still fetched the
  // whole trip at that detail — and made the server simplify all of it, at
  // 4.0 s per request for a 219-activity trip against 0.26 s at whole-trip
  // zoom (issue #324). The camera's box is sent with the request and
  // remembered; leaving it is a reason to refetch, exactly like changing
  // level is.
  //
  // Both are null until geometry has been loaded, and _loadedGeoBox stays
  // null whenever the geometry on hand covers the whole trip — which is a
  // superset of any viewport, so it is never a reason to refetch. That is the
  // state after every load, since the notifier has no camera box before the
  // map's first event.
  GeoBox? _mapViewport;
  GeoBox? _loadedGeoBox;

  /// How long the camera must settle before a zoom change is acted on. Long
  /// enough that a pinch through several levels causes one refetch, not five.
  @visibleForTesting
  Duration zoomRefetchDebounce = const Duration(milliseconds: 700);

  int _bucketOf(double zoom) => zoom.ceil();

  /// Whether the geometry on hand is the wrong geometry for where the camera
  /// is now. False while nothing has been loaded, so a camera event during a
  /// load — or one carrying a bucket left over from the previous project —
  /// arms nothing.
  bool _geoIsStaleForCamera() {
    if (_loadedZoomBucket == null) return false;
    if (_bucketOf(_mapZoom) != _loadedZoomBucket) return true;
    final loaded = _loadedGeoBox;
    final viewport = _mapViewport;
    // No box means whole-trip geometry: nothing the camera does makes that
    // insufficient at the same level.
    if (loaded == null || viewport == null) return false;
    return !loaded.contains(viewport);
  }

  /// Told by the map screens on every camera event, with the camera's visible
  /// bounds when they are usable as a box. Cheap: only geometry that is
  /// actually wrong for the camera schedules anything.
  void setMapZoom(double zoom, {GeoBox? viewport}) {
    _mapZoom = zoom;
    // Only overwritten by a usable box. A null one means the camera reported
    // something that cannot be a bbox — crossing the antimeridian, or not laid
    // out yet — and keeping the last good box is the conservative choice: it
    // can leave the detail slightly stale, where clearing it would fire a
    // whole-trip fetch off a transient reading.
    if (viewport != null) _mapViewport = viewport;
    // Client-built geometry (E2EE) has no server counterpart to refetch.
    if (encryption.isUnlocked) return;
    if (_isDisposed || !_geoIsStaleForCamera()) return;
    _zoomRefetchTimer?.cancel();
    _zoomRefetchTimer = Timer(zoomRefetchDebounce, () {
      final r = ref;
      if (r != null) unawaited(_refetchGeoForZoom(r));
    });
  }

  /// Swaps in geometry for the current zoom bucket.
  ///
  /// Gated on the camera being idle for the same reason the load-time upgrade
  /// is: replacing geo rebuilds every polyline and marker spec, and landing
  /// that mid-gesture is what issue #276 was originally about.
  Future<void> _refetchGeoForZoom(ProjectRef r) async {
    if (!_geoIsStaleForCamera()) return;
    // At most one at a time. This is fired unawaited from a debounce timer and
    // then awaits a fetch, a camera-idle wait and _buildFullTrack — so without
    // this a new one starts every time the camera settles while the last is
    // still running, and each of those copies `geo` into a compute() isolate.
    // Measured on device: a 2.6 GB Dart heap and an ANR.
    if (_refetchInFlight) return;
    _refetchInFlight = true;
    // Always counted. The perf report is written at load time and read from
    // Settings, so an app stuck in a refetch loop never reaches it — but the
    // count is what makes the loop obvious the moment a report *is* produced,
    // and "how many times did we go back to the server" was the one line that
    // would have diagnosed #332 immediately.
    perfSpans.note('geo_refetches', '${++_geoRefetchCount}');
    try {
      await _refetchGeoForZoomInner(r);
    } finally {
      _refetchInFlight = false;
    }
  }

  bool _refetchInFlight = false;
  int _geoRefetchCount = 0;

  /// True while the refetch that captured [token] and [r] is still the one to
  /// apply.
  ///
  /// Deliberately not [_isCurrent]. That compares against the ref [load] was
  /// *begun* with, and `ref` drifts away from it the moment the server
  /// answers: load() corrects the name and the role from `caller_role`. On a
  /// share link the name goes from the token to the trip's own name, and for
  /// a companion whose real tier is not the "editor" placeholder the role
  /// changes — so on both paths a refetch, which reads `ref`, held a ref the
  /// track could never match, and every refetched level was fetched and then
  /// thrown away. A viewer kept the detail of whatever zoom the trip first
  /// loaded at however far they zoomed in (issue #321).
  ///
  /// The token is what catches supersession — a second load, a project
  /// switch, a teardown all bump it. The ref only has to still be the project
  /// this refetch was started for.
  bool _refetchIsCurrent(int token, ProjectRef r) =>
      _loadTrack.token == token && ref == r;

  Future<void> _refetchGeoForZoomInner(ProjectRef r) async {
    final bucket = _bucketOf(_mapZoom);
    // Captured before the awaits, for the same reason the load path captures
    // its bucket: the camera keeps moving during the fetch and the idle wait,
    // and stamping a box that was never requested would leave the geometry
    // permanently mismatched to what is on screen. Null when the camera has
    // no usable box — the whole trip is then fetched, as before.
    final viewport = _mapViewport;
    final box = viewport == null ? null : fetchBoxFor(viewport, bucket);
    final token = _loadTrack.token;
    try {
      final fetched = await fetchServerGeo(
          () => _service.getSimplifiedGeo(r, _mapZoom, bbox: box));
      final next = fetched.geo;
      if (!_refetchIsCurrent(token, r)) return;
      // See the load path: a response with no feature list is not an empty
      // trip. Keep what is on screen rather than blanking it.
      if (next['features'] is! List) return;
      await _waitForCameraIdle();
      if (!_refetchIsCurrent(token, r)) return;
      // Re-read the bucket: the user may have kept zooming while this was in
      // flight, in which case a newer refetch is already scheduled and this
      // result is for a level nobody is looking at. The BOX is deliberately
      // not re-checked: a result for the right level but a stale region is
      // still better than the older geometry it replaces, and if the camera
      // has left the box, the pan events that took it there have already
      // scheduled the next refetch.
      if (_bucketOf(_mapZoom) != bucket) return;
      reconcileSegmentOverlay(next, requestedAt: fetched.requestedAt);
      geo = {
        'type': 'FeatureCollection',
        'features': mergePendingSegmentPatches(
            List<dynamic>.from(next['features'] as List? ?? [])),
      };
      _loadedZoomBucket = bucket;
      _loadedGeoBox = box;
      // Self-healing, and the reason this cannot loop again.
      //
      // A refetch exists to make `_geoIsStaleForCamera` false. If it is still
      // true right after a *successful* one, the predicate is not satisfiable
      // by the action taken to satisfy it — which is precisely the bug that
      // took the app to a 2.6 GB heap and an ANR (issue #332): every camera
      // event scheduled another refetch, forever. fetchBoxFor now guarantees
      // its box contains its viewport, so this should be unreachable; if it
      // ever is reachable again, dropping the box disarms the box branch for
      // the rest of the session and the map falls back to whole-trip
      // geometry — slightly coarser, never wedged.
      if (_geoIsStaleForCamera()) {
        _loadedGeoBox = null;
        perfSpans.note('geo_box_unsatisfiable', 'yes');
      }
      await _buildFullTrack();
      if (!_refetchIsCurrent(token, r)) return;
      notifyListeners();
    } on Object {
      // Non-fatal: the geometry already on screen stays. A failed refetch
      // means slightly wrong detail, never a blank map.
    }
  }

  /// Completes the moment the camera settles. Non-null only while a wait is
  /// actually outstanding, so a camera event costs nothing when nobody is
  /// waiting on it.
  Completer<void>? _cameraIdleWaiter;

  void setMapCameraActive(bool active) {
    _mapCameraActive = active;
    // Frame capture for the pan itself (issue #276): every other span in this
    // app measures loading, and the ANR happens mid-gesture.
    if (active) {
      perfSpans.beginGesture();
    } else {
      perfSpans.endGesture();
    }
    if (!active) {
      final waiter = _cameraIdleWaiter;
      _cameraIdleWaiter = null;
      if (waiter != null && !waiter.isCompleted) waiter.complete();
    }
  }

  /// How long [_waitForCameraIdle] will hold a background upgrade before
  /// applying it anyway. Overridable for the same reason as
  /// [loadRetryBackoff]: so a test never waits real seconds.
  @visibleForTesting
  Duration cameraIdleTimeout = const Duration(seconds: 2);

  /// Waits until the camera is idle before returning, capped so a user who
  /// never stops panning still eventually gets the full-res geo.
  ///
  /// Event-driven rather than polled: the old version woke the isolate every
  /// 100 ms for up to 2 s on every background apply, which is itself work
  /// competing with the gesture it was trying to stay out of the way of.
  ///
  /// Deliberately NOT `SchedulerBinding.scheduleTask(..., Priority.idle)`,
  /// which would be the natural "land between frames" primitive: idle tasks
  /// resolve promptly in a plain `test()` but do not run at all under a
  /// pumped `testWidgets` pipeline, so gating the geo upgrade on one would
  /// hang the background load in widget tests — and, more to the point, would
  /// make the upgrade's arrival depend on the frame pipeline having spare
  /// time, which is exactly what a busy map does not have.
  Future<void> _waitForCameraIdle() async {
    if (!_mapCameraActive || _isDisposed) return;
    final waiter = _cameraIdleWaiter ??= Completer<void>();
    // A Timer rather than Future.delayed, so dispose() can cancel it: a
    // discarded notifier keeps no timer running (issue #418).
    final timedOut = Completer<void>();
    final timeout = Timer(cameraIdleTimeout, timedOut.complete);
    _cameraIdleTimeouts.add(timeout);
    try {
      await Future.any([waiter.future, timedOut.future]);
    } finally {
      timeout.cancel();
      _cameraIdleTimeouts.remove(timeout);
    }
  }

  /// The timeouts of the [_waitForCameraIdle] calls outstanding.
  final Set<Timer> _cameraIdleTimeouts = {};

  /// Fetches full-res GeoJSON and progressively replaces each activity's
  /// straight-line approximation with its real GPS trace (last activity first).
  Future<void> _loadFullGeoProgressively(ProjectRef ref, int token) async {
    // Guard: abort if the user navigated away before we finish
    if (!_isCurrent(token, ref)) return;

    if (encryption.isUnlocked) {
      // Full-res geo is already fully knowable from the decrypted activities
      // held in memory — the server can't build it for encrypted activities
      // anyway (issue #29), so there's no progressive server round trip to
      // race here; build it once, directly, from client_geo_builder.dart.
      try {
        geo = client_geo.buildFullGeo(items, client_geo.activitiesById(activities));
        await _buildFullTrack();
        // Bug #1 of issue #283: this used to be set unconditionally right
        // after the await above, before the ref check below — a load()
        // superseded during _buildFullTrack()'s compute() hop could still
        // flip isGeoLoaded for the wrong project even with its notify
        // suppressed.
        if (!_isCurrent(token, ref)) return;
        isGeoLoaded = true;
      } catch (e) {
        if (!_isCurrent(token, ref)) return;
        error = _loadErrorMessage(e);
      }
      await _waitForCameraIdle();
      if (!_isCurrent(token, ref)) return;
      notifyListeners();
      return;
    }

    // Zoom level of detail first (issue #295): geometry simplified to the
    // zoom actually on screen is a fraction of the full-resolution payload,
    // and it is the client's largest single heap consumer. Falls through to
    // the offline cache and then the full-res path below on any failure — an
    // older server without the endpoint, or offline, where the cached full
    // payload is the answer.
    try {
      // Captured before the await: the fit-bounds animation runs during the
      // fetch and the camera-idle wait, so reading _mapZoom afterwards would
      // stamp a level that was never fetched — and then never refetch it.
      final requestedZoom = _mapZoom;
      final requestedBucket = _bucketOf(requestedZoom);
      final fetched = await fetchServerGeo(
          () => _service.getSimplifiedGeo(ref, requestedZoom));
      final lod = fetched.geo;
      if (!_isCurrent(token, ref)) return;
      // A 200 carrying no feature list is not an empty trip, it is a response
      // this code did not ask for — an older server answering some catch-all,
      // a proxy error page. Accepting it would blank the map; falling through
      // to the full-resolution path recovers. An empty *list* is different and
      // is honoured: a trip really can have no geometry.
      if (lod['features'] is! List) {
        throw StateError('simplified geo response carried no features');
      }
      reconcileSegmentOverlay(lod, requestedAt: fetched.requestedAt);
      final lodFeatures = mergePendingSegmentPatches(
          List<dynamic>.from(lod['features'] as List? ?? []));
      await _waitForCameraIdle();
      if (!_isCurrent(token, ref)) return;
      geo = {'type': 'FeatureCollection', 'features': lodFeatures};
      _loadedZoomBucket = requestedBucket;
      await _buildFullTrack();
      if (!_isCurrent(token, ref)) return;
      isGeoLoaded = true;
      notifyListeners();
      // Nothing above this line ever writes the full-resolution row the
      // offline fallback below reads, so a trip first opened on this device
      // had none to fall back to (issue #317). Seeding runs in the
      // background, after the map is already on screen.
      unawaited(_seedOfflineFullGeo(ref, token));
      return;
    } on Object {
      // Fall through to the offline cache, then the full-resolution path.
    }

    // Offline fallback (issue #317). This branch used to sit *ahead* of the
    // level-of-detail fetch above, as a mode-toggle shortcut. Leaving it
    // there once seeding was restored would have meant the first open used
    // LOD and every open after it rendered full resolution instead — L1 is
    // authoritative for the session (see ProjectDataCache), so the cache
    // would quietly re-hold the ~180 MB the LOD exists to avoid. It is
    // consulted only when the fetch above could not answer: offline, or a
    // server too old to have the endpoint.
    //
    // Applied in one shot, which is what this branch has always been for: a
    // payload that is already complete has nothing to reveal progressively,
    // and replaying a staged reveal over it repainted the whole map (every
    // marker + polyline) several times over. Toggling between view and manage
    // mode re-ran that on every switch — several seconds of back-to-back
    // main-thread rebuilds, enough to trip Android's ANR watchdog. Falling
    // through to the network path below instead would be worse still: the
    // multi-MB full-res fetch, for data already on the device.
    final cachedFullGeo = await _service.readCachedGeo(ref);
    if (cachedFullGeo != null) {
      if (!_isCurrent(token, ref)) return;
      try {
        // Older than every patch, however recently it was read.
        reconcileSegmentOverlay(cachedFullGeo, requestedAt: 0);
        final features = mergePendingSegmentPatches(
            List<dynamic>.from(cachedFullGeo['features'] as List? ?? []));
        geo = {'type': 'FeatureCollection', 'features': features};
        await _buildFullTrack();
        // Same bug #1 fix as the encrypted branch above.
        if (!_isCurrent(token, ref)) return;
        isGeoLoaded = true;
      } catch (e) {
        if (!_isCurrent(token, ref)) return;
        error = _loadErrorMessage(e);
      }
      await _waitForCameraIdle();
      if (!_isCurrent(token, ref)) return;
      notifyListeners();
      return;
    }

    // Fetch the full-res geo with one retry. A cold-cache miss can be slow
    // enough to time out, but the server finishes computing and caches the
    // result regardless — so a brief pause then retry usually lands on the now
    // warm cache. A persistent failure is surfaced (not swallowed) so the user
    // isn't left silently looking at low-res straight lines.
    Map<String, dynamic>? fullGeo;
    var fullGeoRequestedAt = 0;
    for (int attempt = 0; attempt < 2; attempt++) {
      try {
        final fetched = await fetchServerGeo(() => _service.getGeo(ref));
        fullGeo = fetched.geo;
        fullGeoRequestedAt = fetched.requestedAt;
        break;
      } on Object catch (e) {
        // Catch Object (not just Exception): a decode failure can throw an
        // Error (RangeError/TypeError), which would otherwise escape as an
        // unhandled async exception and never surface here.
        if (!_isCurrent(token, ref)) return;
        if (attempt == 1) {
          error = _loadErrorMessage(e);
          isGeoLoaded = false;
          notifyListeners();
          return;
        }
        await Future.delayed(const Duration(seconds: 2));
        if (!_isCurrent(token, ref)) return;
      }
    }
    if (fullGeo == null || !_isCurrent(token, ref)) return;

    try {
      // Drop overlay entries the server geo already reflects, so the durable
      // overlay self-cleans once the backend has caught up.
      reconcileSegmentOverlay(fullGeo, requestedAt: fullGeoRequestedAt);

      if (!_isCurrent(token, ref)) return;
      // One atomic swap: rebuild authoritatively from the server geo
      // (activities at full resolution + the server's segment features), then
      // re-apply the durable segment overlay so any local add/update/delete
      // that happened during this background load wins over the stale server
      // snapshot.
      //
      // This used to be the *final* pass after a staged reveal that replaced
      // the low-res track a few activities at a time, ~8 repaints 80 ms apart
      // (issue #293). That staging made sense when the upgrade genuinely
      // trickled in. It does not now: getGeo returns — and, since #292, fully
      // parses — the whole payload before the first batch could fire, so the
      // reveal bought no perceived progress and cost 8 whole-tree rebuilds
      // (every polyline spec, every marker spec, a compute() decimation hop,
      // ActivityPanel, ElevationChart). That was the reported "small local
      // blocks and unblocks" on #276: a metronome of hitches, because the
      // cause was literally a metronome.
      final features = mergePendingSegmentPatches(
          List<dynamic>.from(fullGeo['features'] as List? ?? []));
      await _waitForCameraIdle();
      if (!_isCurrent(token, ref)) return;
      geo = {'type': 'FeatureCollection', 'features': features};
      await _buildFullTrack();
      // _buildFullTrack() may hop through compute() — re-check like every
      // other await in this function so a superseded load doesn't flip
      // isGeoLoaded or notify for a project the user has since left.
      if (!_isCurrent(token, ref)) return;
      isGeoLoaded = true;
      notifyListeners();
    } on Object catch (e) {
      // Non-fatal — low-res map is still shown. Catch Object so an Error in the
      // apply path can't escape as a recurring unhandled exception. Checked
      // like every mutation above: a stale error must not surface for a
      // project the user has since left (issue #283).
      if (!_isCurrent(token, ref)) return;
      error = _loadErrorMessage(e);
      notifyListeners();
    }
  }

  /// Coordinates above which a trip is not seeded into the offline cache at
  /// all (issue #317).
  ///
  /// The seed is the one place this app deliberately materialises the
  /// full-resolution payload it otherwise no longer holds: expanding it costs
  /// roughly 180 MB for the 1,465,345 coordinates of the 219-activity trip
  /// behind issue #295, and gzipping it for disk hands the same structure to
  /// a second isolate, which copies it. Transient, and off the render path —
  /// but on a device profiled at ~625 MB steady and killed above ~1.3 GB,
  /// "transient" is not the same as "free".
  ///
  /// 500,000 caps that at roughly a third of the worst case (~60 MB expanded,
  /// ~1.5 MB on the wire) while still covering trips far longer than most.
  /// Past it, the offline map keeps the low-res straight lines it has today:
  /// the trips whose offline geometry costs the most are exactly the ones
  /// whose owners can least afford the app being killed for holding it.
  ///
  /// Overridable so a test can reach the ceiling without a 500,000-point
  /// fixture, like [zoomRefetchDebounce].
  @visibleForTesting
  int offlineSeedCoordinateCeiling = 500000;

  /// Writes the full-resolution geometry to the on-device cache so a later
  /// *offline* open has a detailed track to fall back on (issue #317).
  ///
  /// Costs the full-res fetch that the level-of-detail path exists to avoid,
  /// so it is deliberately narrow:
  ///  - native only. L2 is the whole point and web has no L2 (see
  ///    ProjectDataCache), so on web this would be heap and bandwidth spent
  ///    for nothing.
  ///  - owners only. A shared viewer's screen is served by the share
  ///    endpoints, has no offline story, and issue #321 exists to stop it
  ///    fetching this payload at all — SharedProjectNotifier inherits this
  ///    method, and [loadOwnerExtras] is what keeps it out.
  ///  - once per trip per lock_version. A mutation invalidates the row, which
  ///    is correct — the cached geometry really is stale — and the next open
  ///    seeds the new one.
  ///  - never above [offlineSeedCoordinateCeiling].
  ///
  /// The payload is written to disk only, never into L1: holding it in memory
  /// for the rest of the session is precisely the cost the LOD path removed.
  Future<void> _seedOfflineFullGeo(ProjectRef ref, int token) async {
    if (kIsWeb || !loadOwnerExtras) return;
    try {
      if (await projectDataCache.hasFullGeoOnDisk(ref)) return;
      if (!_isCurrent(token, ref)) return;
      // The fetch is background, but its decode lands back on this isolate,
      // so it waits for the camera like every other heavy apply here does.
      await _waitForCameraIdle();
      if (!_isCurrent(token, ref)) return;
      final full = await _service.fetchFullGeoUncached(ref);
      if (!_isCurrent(token, ref)) return;
      // Noted either way: "does this trip have offline geometry, and if not
      // why not" is otherwise invisible until someone is offline.
      final coords = _coordinateCount(full);
      if (coords > offlineSeedCoordinateCeiling) {
        perfSpans.note('geo_offline_seed', 'skipped, $coords coords');
        return;
      }
      projectDataCache.seedFullGeoToDisk(ref, full);
      perfSpans.note('geo_offline_seed', '$coords coords');
    } on Object {
      // Best effort by construction: a failed seed costs the user nothing
      // beyond the offline detail they already do not have, and must never
      // surface as an error over a map that loaded fine.
    }
  }

  /// Total coordinates across every feature of [geo] — the size measure the
  /// seed ceiling is expressed in, since it is what the heap actually holds.
  static int _coordinateCount(Map<String, dynamic> geo) {
    var n = 0;
    final features = geo['features'];
    if (features is! List) return 0;
    for (final f in features) {
      if (f is! Map) continue;
      final coords = (f['geometry'] as Map? ?? const {})['coordinates'];
      if (coords is List) n += coords.length;
    }
    return n;
  }

  /// Full-resolution geometry for a rendering that is not the map — an image
  /// export or a share card (issue #317).
  ///
  /// [geo] is simplified to the zoom the map is showing, which is the right
  /// trade for the map and the wrong one for an export: a day-scoped export
  /// fits a far tighter camera than the geometry was built for, and renders
  /// visibly angular. The extra request is affordable here because the
  /// operation is user-initiated and already slow.
  ///
  /// Returns [geo] unchanged when it is already full resolution (an E2EE trip
  /// builds it client-side, and the offline/older-server fallbacks apply the
  /// full payload), and falls back to it if the fetch fails — an angular
  /// export beats no export.
  Future<Map<String, dynamic>?> fullResGeoForExport() async {
    final r = ref;
    if (_loadedZoomBucket == null || r == null) return geo;
    try {
      final full = await _service.fetchFullGeoUncached(r);
      // The durable segment overlay wins over the server snapshot here for
      // the same reason it does on the load path: a segment the user just
      // added or removed must appear in the export as it does on the map.
      return {
        'type': 'FeatureCollection',
        'features': mergePendingSegmentPatches(
            List<dynamic>.from(full['features'] as List? ?? [])),
      };
    } on Object {
      return geo;
    }
  }

  /// Fetches elevation profiles in the background and merges them into the
  /// already-rendered activity list — but only when the meta load did not
  /// already supply usable ones. See the guard below.
  Future<void> _loadElevationData(ProjectRef ref, int token) async {
    // Nothing left to upgrade when /meta already carried profiles (issue
    // #323). It does: ProjectIO.to_dict's _ep_pairs falls back to the
    // precomputed ~300-point `elevation_profile_low_res_json`, which the
    // lightweight load never defers — and ~300 points per activity is
    // *everything* the two remaining consumers need. The chart
    // LTTB-downsamples whatever it is given to _kMaxChartPoints = 300, and
    // buildFullTrackFromTotals reads one number per activity, the profile's
    // last distance, which the server's downsample keeps exactly (see
    // src/project/elevation_downsample.py — first, last and both global
    // extremes all survive it).
    //
    // Fetching full GPS resolution on top of that spent 2.8 MB and 4.3 s
    // materialising 1,465,345 two-element lists on a 219-activity trip, to
    // redraw the same 300 spots and re-read the same 219 doubles.
    //
    // A server old enough to have no low-res column sends no profiles at all,
    // so it still falls through to the fetches below — as do a trip whose
    // profiles failed to decrypt and one that genuinely has no elevation.
    //
    // E2EE trips are excluded deliberately, even though /meta serves their
    // low-res profile too (as the ciphertext _revealActivities decrypts). The
    // details payload they fall through to carries more than elevation: it is
    // the only load-path source of a *decrypted* map.summary_polyline, which
    // _openTrackEditor reads off the panel's own copy. Skipping it would send
    // the editor to /activities/{id}/track instead, which answers with the
    // envelope — so an encrypted trip would quietly lose track editing. That
    // is not elevation's decision to make here.
    if (!encryption.isUnlocked &&
        activities.any((a) {
          final profile = a['elevation_profile'];
          return profile is List && profile.isNotEmpty;
        })) {
      return;
    }
    perfSpans.recordBackgroundRefresh('elevation_load');
    // The compact endpoint (issue #295) carries the only part of the details
    // payload this load needs. That payload is 33 MB on a long trip — ~9 s to
    // fetch, ~5 s to decode, and retained afterwards; this is a few hundred
    // KB. E2EE trips still take the old path: their profiles are opaque
    // envelopes the endpoint cannot open, and decrypting them is the details
    // payload's job.
    if (!encryption.isUnlocked) {
      final merged = await _loadElevationCompact(ref, token);
      if (merged) return;
      // Fell through (older server without the endpoint, or encrypted
      // profiles) — the details payload is still the source of truth.
    }
    try {
      final details = await _service.getDetails(ref);
      if (!_isCurrent(token, ref)) return;
      final rawActivities = details['activities'];
      if (rawActivities is List) {
        final freshActivities = rawActivities.cast<Map<String, dynamic>>();
        await _revealActivities(freshActivities);
        // issue #283: _revealActivities awaits (per-field decrypt calls), so
        // re-check before merging into the shared `activities` field below —
        // the original code only checked once, before this await.
        if (!_isCurrent(token, ref)) return;
        final byId = <String, Map<String, dynamic>>{};
        for (final a in freshActivities) {
          byId[a['id']?.toString() ?? ''] = a;
        }
        activities = [
          for (final a in activities) byId[a['id']?.toString()] ?? a,
        ];
        await _buildFullTrack();
        if (!_isCurrent(token, ref)) return;
        await _waitForCameraIdle();
        if (!_isCurrent(token, ref)) return;
        notifyListeners();
      }
    } on Object catch (_) {
      // Non-fatal — elevation chart simply stays empty. Catch Object (not
      // just Exception), matching _loadFullGeoProgressively above: a decode
      // failure can throw an Error (RangeError/TypeError) that would
      // otherwise escape as an unhandled async exception.
    }
  }

  /// Fetches elevation from the compact endpoint and merges it into
  /// [activities]. Returns false when the caller should fall back to the full
  /// details payload — an older server, or profiles this endpoint could not
  /// open.
  Future<bool> _loadElevationCompact(ProjectRef ref, int token) async {
    try {
      final result = await _service.getElevation(ref);
      if (!_isCurrent(token, ref)) return true; // superseded: nothing to do
      if (result.encryptedIds.isNotEmpty) return false;
      if (result.profiles.isEmpty) return true; // genuinely no elevation data
      // Copy-on-write per activity, so MapPanel's identical() caches see a
      // new list and nothing mutates an object another listener is reading.
      activities = [
        for (final a in activities)
          result.profiles[a['id']?.toString()] == null
              ? a
              : {...a, 'elevation_profile': result.profiles[a['id'].toString()]},
      ];
      await _buildFullTrack();
      if (!_isCurrent(token, ref)) return true;
      await _waitForCameraIdle();
      if (!_isCurrent(token, ref)) return true;
      notifyListeners();
      return true;
    } on Object {
      // Non-fatal, and deliberately not an error banner: the details payload
      // below can still supply this.
      return false;
    }
  }

  void _updateStats() {
    double dist = 0;
    int moving = 0;
    double elev = 0;
    for (final a in activities) {
      dist   += (a['distance']              as num? ?? 0).toDouble();
      moving += (a['moving_time']           as num? ?? 0).toInt();
      elev   += (a['total_elevation_gain']  as num? ?? 0).toDouble();
    }
    totalDistanceM      = dist;
    totalMovingSeconds  = moving;
    totalElevationGainM = elev;
  }

  // Raw (pre-/1000) meters per day — divided down to km only when read, so
  // caching this can't shift the float rounding of the original single
  // divide-at-the-end computation below.
  Map<String, ({double distanceM, double elevationM})>? _dayStatsCache;
  List<Map<String, dynamic>>? _dayStatsCacheItems;
  List<Map<String, dynamic>>? _dayStatsCacheActivities;

  /// Distance (km) and climb (m) summed over the activities on [dateKey]
  /// ("YYYY-MM-DD"). An activity belongs to the day of its
  /// `start_date_local` — the same rule the activity panel groups by — so the
  /// totals match what the day header shows. Returns zeros for a day with no
  /// activities (the Edit Day hero then hides its stat strip).
  ///
  /// The day carousel calls this once per visible day on every rebuild —
  /// including every rebuild a day *selection* triggers — so recomputing it
  /// with a fresh O(activities) scan of `items` each time compounds with the
  /// map's own per-selection rebuild cost (see map_panel.dart's
  /// buildDayIndex). Cached here instead: one O(items) pass builds stats for
  /// every day at once, reused until `items`/`activities` actually change.
  ({double distanceKm, double elevationM}) dayStats(String dateKey) {
    if (!identical(items, _dayStatsCacheItems) ||
        !identical(activities, _dayStatsCacheActivities)) {
      final byId = {for (final a in activities) a['id']?.toString(): a};
      final cache = <String, ({double distanceM, double elevationM})>{};
      for (final item in items) {
        if (item['item_type'] != 'activity') continue;
        final a = byId[item['activity_id']?.toString()];
        if (a == null) continue;
        final ds = (a['start_date_local'] as String?)?.split('T').first;
        if (ds == null) continue;
        final prev = cache[ds] ?? (distanceM: 0.0, elevationM: 0.0);
        cache[ds] = (
          distanceM: prev.distanceM + (a['distance'] as num? ?? 0).toDouble(),
          elevationM: prev.elevationM +
              (a['total_elevation_gain'] as num? ?? 0).toDouble(),
        );
      }
      _dayStatsCache = cache;
      _dayStatsCacheItems = items;
      _dayStatsCacheActivities = activities;
    }
    final entry = _dayStatsCache![dateKey];
    return entry == null
        ? (distanceKm: 0.0, elevationM: 0.0)
        : (distanceKm: entry.distanceM / 1000.0, elevationM: entry.elevationM);
  }

  static String _ymd(DateTime d) =>
      '${d.year.toString().padLeft(4, '0')}-'
      '${d.month.toString().padLeft(2, '0')}-'
      '${d.day.toString().padLeft(2, '0')}';

  List<String>? _orderedDayKeysCache;
  Map<String, Map<String, dynamic>>? _orderedDayKeysCacheDayMeta;
  List<Map<String, dynamic>>? _orderedDayKeysCacheActivities;
  List<Map<String, dynamic>>? _orderedDayKeysCacheItems;

  /// Every day key ("YYYY-MM-DD") the project touches, ascending: the union of
  /// day-meta days and the days any dated content falls on ([contentDayKeys] —
  /// activities plus memories, journals, encounters and segments). That is the
  /// same bucketing the activity panel gives a day header to, so a day the
  /// panel shows is a day this lists (issue #370). It is the full-trip day
  /// list regardless of any active filter (unlike the activity panel's
  /// display-derived list), so it's safe to use from the add-FAB.
  ///
  /// Called from several places on every selection-triggered rebuild — the
  /// day carousel, computeSelectionStats, activeDayKey — each a fresh
  /// O(activities + items) scan before this cache existed. Same
  /// identical()-based convention as dayStats above.
  List<String> orderedDayKeys() {
    if (!identical(dayMeta, _orderedDayKeysCacheDayMeta) ||
        !identical(activities, _orderedDayKeysCacheActivities) ||
        !identical(items, _orderedDayKeysCacheItems)) {
      final keys = <String>{
        ...dayMeta.keys,
        ...contentDayKeys(activities, items),
      };
      _orderedDayKeysCache = keys.toList()..sort();
      _orderedDayKeysCacheDayMeta = dayMeta;
      _orderedDayKeysCacheActivities = activities;
      _orderedDayKeysCacheItems = items;
    }
    return _orderedDayKeysCache!;
  }

  /// The day the add-FAB should default to: today while the trip is still
  /// active (the day you're most likely adding to), otherwise the last day of
  /// the trip. Null only when the project has no days at all and the trip has
  /// ended.
  String? activeDayKey() {
    if (_tripIsActive) return _ymd(DateTime.now());
    final keys = orderedDayKeys();
    return keys.isEmpty ? null : keys.last;
  }

  /// Merges full activity data (with elevation_profile) returned by the
  /// background full-details request into the already-rendered activity list,
  /// then rebuilds tracks and notifies listeners.
  /// Called by SharedProjectNotifier / ViewProjectNotifier after phase 2.
  ///
  /// [ref]/[token] should be [currentLoadKey]/[currentLoadToken] captured
  /// *before* the caller's own long-running fetch that produced
  /// [fullActivities] — passing them lets this reject a call superseded
  /// during that fetch by a second concurrent load of the *same* project,
  /// which a plain [currentLoadKey] comparison alone can't detect (issue
  /// #283 bug #3: ProjectRef.== is structural, so it only catches navigation
  /// to a *different* project). Required, not optional/defaulted: a caller
  /// that forgot to pass them would otherwise silently compare the current
  /// state against itself and always pass — defeating the guard entirely.
  @protected
  Future<void> applyFullActivities(
    List<Map<String, dynamic>> fullActivities, {
    required ProjectRef ref,
    required int token,
  }) async {
    // Checked before mutating, not just before the trailing notify — a stale
    // call used to merge into `activities` unconditionally and only skip the
    // notify, leaving the mutation live but unbroadcast (issue #283 bug #3).
    if (!_isCurrent(token, ref)) return;
    final byId = {for (final a in fullActivities) a['id']?.toString(): a};
    activities = [
      for (final a in activities) byId[a['id']?.toString()] ?? a,
    ];
    _updateStats();
    await _buildFullTrack();
    if (!_isCurrent(token, ref)) return;
    await _waitForCameraIdle();
    if (!_isCurrent(token, ref)) return;
    notifyListeners();
  }

  /// Live arc preview while a SegmentDialog or LocationPickerDialog is open.
  /// Callers write directly to `.value` — updates don't trigger notifyListeners().
  final ValueNotifier<List<GeoPoint>?> previewArcNotifier = ValueNotifier(null);

  /// Map cursor driven by the elevation chart touch position.
  /// Uses a ValueNotifier so only the marker layer rebuilds on cursor moves.
  final ValueNotifier<GeoPoint?> elevationCursorNotifier = ValueNotifier(null);

  /// Elevation chart cursor driven by a map tap.
  /// Holds the cumulative distance (km) of the nearest track point.
  final ValueNotifier<double?> mapCursorDistNotifier = ValueNotifier(null);

  /// Full distance-indexed track for all activities — used by the map panel
  /// to map a tapped GeoPoint back to a distance on the elevation chart.
  List<(double, GeoPoint)> _fullTrack = const [];
  List<(double, GeoPoint)> get fullTrack => _fullTrack;

  /// Per-activity distance-indexed tracks (0-based distances) — used by
  /// ElevationChart to map chart x-position to a map position.
  /// Keys are activity_id as String.
  Map<String, List<(double, GeoPoint)>> get perActivityTracks => _perActivityTracks;
  Map<String, List<(double, GeoPoint)>> _perActivityTracks = const {};

  // Bumped on every _buildFullTrack call; guards a stale async result (from a
  // superseded call — e.g. the geo-load's call and the elevation-load's call
  // landing out of order) from overwriting a newer one.
  int _buildFullTrackGen = 0;

  /// Rebuilds [_fullTrack]/[_perActivityTracks] from the current [geo] +
  /// [activities]. Delegates to [buildFullTrackResult] — see its doc comment
  /// for why that's a separate top-level function: above
  /// [kInlineFullTrackThreshold] raw elevation_profile points, the work moves
  /// to a background isolate via [compute] instead of running inline on the
  /// UI isolate (issue #276).
  Future<void> _buildFullTrack() async {
    // Bumped unconditionally (before the threshold check) so every call — not
    // just the background-isolate branch — invalidates any still-in-flight
    // compute() from a prior call, even one that itself took the inline
    // branch (issue #276 follow-up: a superseded inline call used to leave
    // the counter untouched, so a stale compute() result could still pass
    // the staleness check below and clobber newer data).
    final gen = ++_buildFullTrackGen;
    final coordPoints = totalTrackCoordinatePoints(geo);
    final samplePoints = totalElevationProfilePoints(activities);
    final work = coordPoints > samplePoints ? coordPoints : samplePoints;
    // Web takes the inline path whatever the size: `compute` has no isolate to
    // hop to there and runs its callback inline, so the hop buys nothing and
    // flattening first would only add a pass over every coordinate on the main
    // thread. Same reasoning as _ElevationChartState._compute.
    if (kIsWeb || work <= kInlineFullTrackThreshold) {
      // No staleness check needed here: buildFullTrackResult() is synchronous,
      // so nothing can bump _buildFullTrackGen between the increment above and
      // this line — unlike the compute() branch below, which awaits across an
      // isolate hop and needs the check after it returns.
      final r = buildFullTrackResult((geo: geo, activities: activities));
      _fullTrack = r.fullTrack;
      _perActivityTracks = r.perActivityTracks;
      _noteTrackSizes();
      return;
    }
    // Project *before* the hop, not after: compute() copies its argument, so
    // handing it `activities` serialised every elevation sample in the trip
    // to read one double from each — measured as a 2.4 s frame (issue #276).
    final totals =
        perfSpans.blocking('elevation_totals', () => activityElevationTotals(activities));
    // Flatten before the hop, for the same reason as `totals`: compute()
    // copies its argument, and GeoJSON coordinates are one list object per
    // point. See [FlatGeoCoords] — this is the third time that copy has cost
    // this project a production problem.
    //
    // Since #337 this is a per-feature cache lookup rather than a pass over
    // every coordinate: the decode worker already produced the buffers. The
    // span stays so a geo that arrived by some other path — and so pays the
    // real flatten here — is visible rather than assumed.
    final coords =
        perfSpans.blocking('geo_flatten', () => flattenGeoCoords(geo));
    final r = await compute(buildFullTrackFromTotals, (coords: coords, totals: totals));
    if (gen != _buildFullTrackGen) return; // superseded by a newer call
    _fullTrack = r.fullTrack;
    _perActivityTracks = r.perActivityTracks;
    _noteTrackSizes();
  }

  /// Records how large the structures this app holds actually are.
  ///
  /// Removing the 33 MB details payload took peak RSS from ~1.9 GB to
  /// ~1.5 GB, so it was a large contributor but not the bulk — and nothing
  /// currently says what the remaining 1.5 GB is. These counts separate "our
  /// data" from engine, GPU and native-image memory, which RSS lumps
  /// together. Every one is an O(1) length read or an O(features) walk.
  void _noteTrackSizes() {
    if (!perfSpans.enabled) return;
    var perAct = 0;
    for (final t in _perActivityTracks.values) {
      perAct += t.length;
    }
    final coords = totalTrackCoordinatePoints(geo);
    perfSpans
      ..note('full_track_points', '${_fullTrack.length}')
      ..note('per_activity_track_points', '$perAct')
      ..note('geo_coords', '$coords')
      // Every sample is a 2-element List: the single largest object count
      // this app holds, and the one the report used to omit entirely — which
      // is how ~700k of them crossed an isolate boundary unnoticed (#276).
      ..note('elevation_points', '${totalElevationProfilePoints(activities)}')
      ..note('activities', '${activities.length}')
      ..note('items', '${items.length}')
      ..note('dart_structs_est', perfEstimateStructBytes(
          fullTrackPoints: _fullTrack.length,
          perActivityTrackPoints: perAct,
          geoCoords: coords));
  }

  /// Test-only seam for driving [_buildFullTrack] directly, without needing a
  /// full [load] — see build_full_track_background_isolate_test.dart.
  @visibleForTesting
  Future<void> buildFullTrack() => _buildFullTrack();

  /// Test-only peek at the staleness counter — see
  /// build_full_track_background_isolate_test.dart.
  @visibleForTesting
  int get buildFullTrackGen => _buildFullTrackGen;

  // ── Account (issue #418) ───────────────────────────────────────────────────

  /// The account [onAuthChanged] last saw: a user id, null for none, or
  /// [_kUnset] before its first call.
  Object? _authUserId = _kUnset;

  /// Called by the [ChangeNotifierProxyProvider] over `AuthNotifier` on every
  /// auth change. The account owns this notifier's lifetime: whenever the
  /// signed-in user id changes — including to or from no account — this
  /// returns true, and the provider (`accountScopedProjectNotifier` in
  /// main.dart) replaces this notifier with a fresh one and disposes it. One
  /// check here covers logout, the 401s that force one, account deletion and
  /// any later way to switch account; a call at each exit is what missed the
  /// 401 paths (issue #418).
  ///
  /// Everything held is also dropped with [clear] first, so work still in
  /// flight here finds its load invalidated and its trip gone.
  ///
  /// [restoring] is true while `AuthNotifier` restores the session at app
  /// start. Nothing is recorded then: the restored token is already the
  /// session's, so a trip opened under the splash belongs to the account the
  /// restore is about to name, and the null-to-id step that ends the restore
  /// must not clear it. The first account seen is not a change either.
  bool onAuthChanged(String? userId, {bool restoring = false}) {
    if (restoring) return false;
    final previous = _authUserId;
    _authUserId = userId;
    if (identical(previous, _kUnset) || previous == userId) return false;
    clear();
    return true;
  }

  /// Drops everything a load, and the session since, put in this notifier,
  /// leaving it as a freshly constructed one would be (issue #418).
  ///
  /// Every instance field of this class and its mixins is either reset here,
  /// in a method called from here, or on the allowlist of
  /// project_notifier_clear_scan_test.dart with the reason it survives — the
  /// test fails on a field that is neither.
  void clear() {
    _zoomRefetchTimer?.cancel();
    _loadedZoomBucket = null;
    _loadedGeoBox = null;
    _mapViewport = null;
    _stopPhotoPolling();
    stopDegradedRouteWatch();
    _lastDegradedRouteCount = null;
    degradedRouteUpgradeAvailable = false;
    ref = null;
    // The saved state the filters belong to: a held key would let the next
    // load of the same key keep filters this clear has just dropped.
    _heldStateKey = null;
    activities = [];
    items = [];
    people = [];
    groups = [];
    undecryptedFields.reset();
    geo = null;
    resetSegmentState();
    selectedActivityId = null;
    selectedSegmentId = null;
    selectedMemoryId = null;
    selectedJournalId = null;
    selectedDay = null;
    showJournals = true;
    resetFilters();
    tripStart = null;
    tripEnd = null;
    dayMeta = {};
    sleepingOptions = [];
    sleepingOptionGroups = {};
    counters = [];
    shareToken = null;
    shareTokenNoMemories = null;
    autoSyncEnabled = true;
    linkedPsTripId = null;
    lastStravaSyncAt = null;
    lastPsSyncAt = null;
    pendingSync = null;
    trackColor = _kDefaultTrackColor;
    trackSecondaryColor = null;
    trackWidth = _kDefaultTrackWidth;
    alternatingTrackColors = false;
    elevationChartColor = null;
    elevationChartShowLine = true;
    colorByType = false;
    typeStyles = {};
    languages = [];
    quotaError = null;
    polarstepsOverlaySteps = [];
    polarstepsOverlayLabel = null;
    _immichConnectedCheckedAt = null;
    _immichConnectedCached = false;
    // Memos keyed on the identity of the lists dropped above. They would
    // miss anyway, but holding them kept the last trip alive in memory.
    _dayStatsCache = null;
    _dayStatsCacheItems = null;
    _dayStatsCacheActivities = null;
    _orderedDayKeysCache = null;
    _orderedDayKeysCacheDayMeta = null;
    _orderedDayKeysCacheActivities = null;
    _orderedDayKeysCacheItems = null;
    members = [];
    pendingInvites = [];
    memberInviteToken = null;
    memberInviteRole = null;
    previewArcNotifier.value = null;
    elevationCursorNotifier.value = null;
    mapCursorDistNotifier.value = null;
    _fullTrack = const [];
    _perActivityTracks = const {};
    // Invalidate any _buildFullTrack() still in flight from before this
    // clear() — without this, a stale compute() resolving afterward would
    // pass the gen check and repopulate the track data this just wiped.
    _buildFullTrackGen++;
    // Same idea for any other in-flight top-level load/reload (issue #283) —
    // all three tracks, so nothing left over from before clear() can still
    // report itself as current afterward.
    _loadTrack.invalidate();
    _reloadTrack.invalidate();
    _detailsOnlyReloadTrack.invalidate();
    totalDistanceM = 0;
    totalMovingSeconds = 0;
    totalElevationGainM = 0;
    isLoading = false;
    error = null;
    loadErrorStatus = null;
    offlineFromCache = false;
    isMetaLoaded = true;
    isElevationLoaded = true;
    isGeoLoaded = true;
    isSyncMetaLoaded = true;
    notifyListeners();
  }

  Future<String?> renameProject(String newName) async {
    final ref = this.ref;
    if (ref == null) return null;
    try {
      final result = await api.put(
        ref.path(),
        {'new_name': newName},
      ) as Map<String, dynamic>;
      this.ref = ref.copyWith(name: result['name'] as String);
      notifyListeners();
      return projectName;
    } on Exception catch (e) {
      error = _msg(e);
      notifyListeners();
      return null;
    }
  }

  // ── API config pass-throughs ───────────────────────────────────────────────

  String get apiBaseUrl => api.baseUrl;
  String? get apiToken => api.tokenForUpload;

  String photoThumbUrl(String memId, String uuid) =>
      '${api.baseUrl}/api/memories/$memId/photos/$uuid/thumb';

  String photoFullUrl(String memId, String uuid) =>
      '${api.baseUrl}/api/memories/$memId/photos/$uuid';

  Map<String, String> get photoAuthHeaders {
    final token = api.tokenForUpload;
    return token != null ? {'Authorization': 'Bearer $token'} : {};
  }

  // ── Immich (photo-upgrade source, issue #33) ────────────────────────────────

  DateTime? _immichConnectedCheckedAt;
  bool _immichConnectedCached = false;

  /// How long [immichConnected]'s result is reused before re-checking the
  /// server — screens may call this on every rebuild, and the connection
  /// state rarely changes mid-session.
  @visibleForTesting
  Duration immichStatusCacheTtl = const Duration(seconds: 30);

  /// Whether this server has Immich configured and reachable — backs the
  /// photo-upgrade dialog's "browse Immich library" entry point. Cached
  /// briefly per instance (see [immichStatusCacheTtl]); a check failure is
  /// treated as "not connected" rather than thrown.
  Future<bool> immichConnected() async {
    final checkedAt = _immichConnectedCheckedAt;
    if (checkedAt != null &&
        DateTime.now().difference(checkedAt) < immichStatusCacheTtl) {
      return _immichConnectedCached;
    }
    try {
      final data = await api.get('/api/immich/status') as Map<String, dynamic>;
      _immichConnectedCached = data['connected'] == true;
    } catch (_) {
      _immichConnectedCached = false;
    }
    _immichConnectedCheckedAt = DateTime.now();
    return _immichConnectedCached;
  }

  // ── Auto-sync ─────────────────────────────────────────────────────────────

  Future<void> _loadSyncMeta(ProjectRef ref) async {
    try {
      final data = await api.get(ref.path('/sync-meta')) as Map<String, dynamic>;
      // Now fired in the background rather than awaited inside load(), so a
      // late response must not clobber a newer project's state if the user
      // has since navigated on to a different one.
      if (currentLoadKey != ref) return;
      autoSyncEnabled = data['auto_sync_enabled'] as bool? ?? true;
      linkedPsTripId = data['linked_ps_trip_id'] as int?;
      lastStravaSyncAt = (data['last_strava_sync_at'] as num?)?.toDouble();
      lastPsSyncAt = (data['last_ps_sync_at'] as num?)?.toDouble();
    } catch (_) {
      // Non-fatal — use defaults
    }
  }

  Future<void> _loadShareInfo(ProjectRef ref) async {
    try {
      final data = await api.get(ref.path('/share-info')) as Map<String, dynamic>;
      if (currentLoadKey != ref) return; // see _loadSyncMeta above
      shareToken = data['share_token'] as String?;
      shareTokenNoMemories = data['share_token_no_memories'] as String?;
    } catch (_) {
      // Non-fatal — use defaults
    }
  }

  Future<void> _backgroundSyncCheck(ProjectRef ref) async {
    try {
      final data = await api.get(ref.path('/sync/check')) as Map<String, dynamic>;
      final strava = (data['strava'] as List?)?.cast<Map<String, dynamic>>() ?? [];
      final ps = (data['polarsteps'] as List?)?.cast<Map<String, dynamic>>() ?? [];
      if ((strava.isNotEmpty || ps.isNotEmpty) && this.ref == ref) {
        pendingSync = (strava: strava, polarsteps: ps);
        notifyListeners();
      }
    } catch (_) {
      // Non-fatal — sync check failure is silent
    }
  }

  // ── Degraded-route upgrade watch (issue #207) ─────────────────────────────
  //
  // sweep_degraded_segments() retries a degraded (straight-line) segment on
  // its own hourly schedule, server-side, independent of whether anyone has
  // the project open. A tab already open at that moment has no live channel
  // telling it that happened, so this polls a cheap, already-cached endpoint
  // (/meta) on an interval — purely to detect the change. It never applies
  // the fresh data itself: only reloadForDegradedUpgrade() does that, and
  // only when the user asks for it, so a background tick can never rewrite
  // what someone is actively looking at or mid-edit on.
  //
  // Deliberately NOT started from load(): this notifier is reused as an
  // ambient/shared instance by screens that never render the banner (e.g.
  // ProjectStatsScreen reads it just for tags), and load() runs there too.
  // Starting a 15-minute Timer.periodic every time *anything* loads a project
  // — and only ever cancelling it in dispose(), which an ambient instance may
  // never see — leaks a pending timer for the lifetime of whatever's running.
  // Screens that actually show the banner (app_screen.dart, view_screen.dart)
  // call start/stop from their own State's initState/dispose instead, so the
  // timer's lifetime matches a mounted widget's, not the notifier's.

  @visibleForTesting
  Duration degradedRouteCheckInterval = const Duration(minutes: 15);

  void startDegradedRouteWatch(ProjectRef ref) {
    if (_isDisposed) return;
    _degradedRouteCheckTimer?.cancel();
    _lastDegradedRouteCount = null; // re-establish the baseline against fresh data
    _degradedRouteCheckTimer =
        Timer.periodic(degradedRouteCheckInterval, (_) => _checkDegradedRouteUpgrade(ref));
  }

  void stopDegradedRouteWatch() {
    _degradedRouteCheckTimer?.cancel();
    _degradedRouteCheckTimer = null;
  }

  @visibleForTesting
  Future<void> checkDegradedRouteUpgrade(ProjectRef ref) => _checkDegradedRouteUpgrade(ref);

  Future<void> _checkDegradedRouteUpgrade(ProjectRef ref) async {
    if (this.ref != ref) return;
    perfSpans.recordBackgroundRefresh('degraded_route_check');
    Map<String, dynamic> meta;
    try {
      meta = await _service.getDetailsMeta(ref);
    } on Exception {
      return; // transient — the next tick tries again
    }
    if (this.ref != ref) return;
    final count = _degradedSegmentCount(meta);
    final previous = _lastDegradedRouteCount;
    if (previous != null && count < previous) {
      degradedRouteUpgradeAvailable = true;
      notifyListeners();
    }
    _lastDegradedRouteCount = count;
  }

  int _degradedSegmentCount(Map<String, dynamic> meta) {
    final rawItems = meta['items'];
    if (rawItems is! List) return 0;
    var count = 0;
    for (final item in rawItems) {
      if (item is! Map) continue;
      if (item['item_type'] != 'segment') continue;
      final seg = item['segment'];
      if (seg is! Map) continue;
      if (seg['route_status'] == 'resolved' && seg['route_degraded'] == true) count++;
    }
    return count;
  }

  /// "Later" — hide the banner without touching any data.
  void dismissDegradedRouteUpgrade() {
    degradedRouteUpgradeAvailable = false;
    notifyListeners();
  }

  /// Hides the "showing last saved version" banner without retrying —
  /// [offlineFromCache] itself only ever clears via a fresh [load].
  void dismissOfflineBanner() {
    offlineFromCache = false;
    notifyListeners();
  }

  /// "Reload" — the only path that ever applies the fresher data.
  Future<void> reloadForDegradedUpgrade() async {
    final r = ref;
    if (r == null) return;
    degradedRouteUpgradeAvailable = false;
    notifyListeners();
    await load(r);
  }

  Future<void> saveSyncMeta({bool? autoSyncEnabled, int? linkedPsTripId, bool clearLinkedTrip = false}) async {
    final ref = this.ref;
    if (ref == null) return;
    if (autoSyncEnabled != null) this.autoSyncEnabled = autoSyncEnabled;
    if (clearLinkedTrip) {
      this.linkedPsTripId = null;
    } else if (linkedPsTripId != null) {
      this.linkedPsTripId = linkedPsTripId;
    }
    notifyListeners();
    try {
      final body = <String, dynamic>{
        if (autoSyncEnabled != null) 'auto_sync_enabled': autoSyncEnabled,
      };
      if (clearLinkedTrip) {
        body['linked_ps_trip_id'] = null;
      } else if (linkedPsTripId != null) {
        body['linked_ps_trip_id'] = linkedPsTripId;
      }
      await api.put(ref.path('/sync-meta'), body);
    } on Exception catch (e) {
      error = _msg(e);
      notifyListeners();
    }
  }

  Future<void> markSynced() async {
    final ref = this.ref;
    if (ref == null) return;
    final now = DateTime.now().millisecondsSinceEpoch / 1000.0;
    lastStravaSyncAt = now;
    lastPsSyncAt = now;
    pendingSync = null;
    notifyListeners();
    try {
      await api.put(
        ref.path('/sync-meta'),
        {'last_strava_sync_at': now, 'last_ps_sync_at': now},
      );
    } on Exception catch (e) {
      error = _msg(e);
      notifyListeners();
    }
  }

  // ── Share ──────────────────────────────────────────────────────────────────

  Future<void> createShareToken() async {
    final ref = this.ref;
    if (ref == null) throw Exception('No project open');
    try {
      final result = await api.post(ref.path('/share'), {}) as Map<String, dynamic>;
      shareToken = result['share_token'] as String?;
      notifyListeners();
    } on ApiException catch (e) {
      throw Exception(e.body);
    }
  }

  Future<void> revokeShareToken() async {
    final ref = this.ref;
    if (ref == null) return;
    try {
      await api.delete(ref.path('/share'));
      shareToken = null;
      notifyListeners();
    } on ApiException catch (e) {
      throw Exception(e.body);
    }
  }

  Future<void> createShareTokenNoMemories() async {
    final ref = this.ref;
    if (ref == null) throw Exception('No project open');
    try {
      final result = await api.post(ref.path('/share/no-memories'), {})
          as Map<String, dynamic>;
      shareTokenNoMemories = result['share_token_no_memories'] as String?;
      notifyListeners();
    } on ApiException catch (e) {
      throw Exception(e.body);
    }
  }

  Future<void> revokeShareTokenNoMemories() async {
    final ref = this.ref;
    if (ref == null) return;
    try {
      await api.delete(ref.path('/share/no-memories'));
      shareTokenNoMemories = null;
      notifyListeners();
    } on ApiException catch (e) {
      throw Exception(e.body);
    }
  }

  Future<Map<String, dynamic>> getShareVisitors() async {
    final ref = this.ref;
    if (ref == null) return {};
    try {
      return await api.get(ref.path('/share/visitors')) as Map<String, dynamic>;
    } on ApiException catch (e) {
      throw Exception(e.body);
    }
  }

  // ── Travel companions (issue #106) ─────────────────────────────────────────

  /// Members of the open project (owner first — server ordering). Loaded on
  /// demand by the settings screen's Travel companions section.
  List<ProjectMember> members = [];

  /// The project's invite token, once the owner/co-owner has created (or
  /// re-fetched) it this session. There is no GET endpoint for it — POST is
  /// idempotent and returns the existing token — so this stays null until
  /// [createMemberInvite] is called.
  String? memberInviteToken;

  /// The role [memberInviteToken] grants on accept — the actual role
  /// returned by the server, which can differ from what was last requested
  /// if an invite already existed (creation is idempotent).
  String? memberInviteRole;

  /// Invites emailed to someone who hasn't joined yet (issue #110). Co-owner+;
  /// empty for editors and viewers, who never fetch them.
  List<PendingInvite> pendingInvites = [];

  /// GET members into [members]. Throws ([ApiException] passes through) so
  /// the caller can show an inline error.
  Future<void> loadMembers() async {
    final ref = this.ref;
    if (ref == null) return;
    members = await _membersService.listMembers(ref);
    notifyListeners();
  }

  /// GET pending email invites into [pendingInvites] (issue #110). Co-owner+
  /// only, so a 403 for an editor/viewer is expected and leaves the list
  /// empty rather than surfacing as an error — the section simply has no
  /// pending block for them.
  Future<void> loadPendingInvites() async {
    final ref = this.ref;
    if (ref == null) return;
    try {
      pendingInvites = await _membersService.listPendingInvites(ref);
    } on ApiException catch (e) {
      if (e.statusCode != 403) rethrow;
      pendingInvites = [];
    }
    notifyListeners();
  }

  /// Revoke one pending invite (issue #110). Optimistic, mirroring
  /// [removeMember]: the row disappears immediately and is restored if the
  /// request fails (rethrown).
  Future<void> revokePendingInvite(int inviteId) async {
    final ref = this.ref;
    if (ref == null) return;
    final prev = pendingInvites;
    pendingInvites = [for (final p in pendingInvites) if (p.id != inviteId) p];
    notifyListeners();
    try {
      await _membersService.revokePendingInvite(ref, inviteId);
    } on Exception {
      pendingInvites = prev;
      notifyListeners();
      rethrow;
    }
  }

  /// Create (or re-fetch — idempotent) the invite token with the given
  /// [role]. Co-owner+; only the strict owner may request "co-owner".
  /// [email] (issue #113), when set, also queues the join link to be emailed
  /// to that address — pass it again on a later call to (re)send to a new
  /// address without creating a second invite. Rethrows [ApiException]
  /// unchanged — 409 means the account has E2EE enabled, 422 a malformed
  /// [email].
  Future<void> createMemberInvite({String role = 'editor', String? email}) async {
    final ref = this.ref;
    if (ref == null) throw Exception('No project open');
    final created = await _membersService.createInvite(ref, role: role, email: email);
    memberInviteToken = created.token;
    memberInviteRole = created.role;
    notifyListeners();
  }

  /// Revoke the invite link. Co-owner+. Existing members are unaffected.
  Future<void> revokeMemberInvite() async {
    final ref = this.ref;
    if (ref == null) return;
    await _membersService.revokeInvite(ref);
    memberInviteToken = null;
    memberInviteRole = null;
    notifyListeners();
  }

  /// Remove [userId] from the project — owner removes anyone; an editor may
  /// remove only themself (leave). Optimistic: the row disappears
  /// immediately and is restored if the request fails (rethrown).
  Future<void> removeMember(int userId) async {
    final ref = this.ref;
    if (ref == null) return;
    final prev = members;
    members = [for (final m in members) if (m.userId != userId) m];
    notifyListeners();
    try {
      await _membersService.removeMember(ref, userId);
    } on Exception {
      members = prev;
      notifyListeners();
      rethrow;
    }
  }

  /// Encrypt this project's currently-encrypted memories under a fresh
  /// per-share content key and upload the result (issue #28), so anonymous
  /// share-link viewers holding the key (in the URL fragment) can decrypt
  /// them. Explicit, one-shot, owner-triggered — NOT auto-synced on edits;
  /// calling again generates a NEW key and overwrites the previous envelopes
  /// (idempotent regeneration), so a previously copied link stops decrypting
  /// and the owner must re-share the freshly generated URL.
  ///
  /// Returns the base64 share key to embed in the share URL as `#key=...`,
  /// or null if the project has no encrypted memories to include. Requires
  /// [createShareToken] to have been called first — content is only ever
  /// attached to the "full" share token. See [ShareContentGenerator] for the
  /// actual (independently-testable) logic.
  Future<String?> generateShareContent() async {
    final ref = this.ref;
    if (ref == null) throw Exception('No project open');
    return ShareContentGenerator(api).generate(ref, items);
  }

  /// Fetches raw bytes for an export API path.
  /// Throws [Exception] with a user-readable message on API error.
  Future<http.Response> fetchExportBytes(String apiPath) async {
    try {
      return await api.getRaw(apiPath);
    } on ApiException catch (e) {
      throw Exception(e.body);
    }
  }

  /// Saves trip_start and trip_end in a single PUT. No-ops if neither changed.
  /// Optimistic update is applied immediately; no reload needed since the
  /// server only writes these two fields and returns them unchanged.
  Future<void> setTripDates(String? startStr, String? endStr) async {
    final ref = this.ref;
    if (ref == null) return;
    if (tripStart == startStr && tripEnd == endStr) return;
    tripStart = startStr;
    tripEnd = endStr;
    notifyListeners();
    try {
      await api.put(
        ref.path(),
        {'trip_start': startStr, 'trip_end': endStr},
      );
    } on Exception catch (e) {
      error = _msg(e);
      notifyListeners();
    }
  }

  // ── Photo polling (post-import background download) ──────────────────────

  /// Polls memory photos every 3 s for up to 3 min after a Polarsteps import.
  /// Updates only the `photos` list inside each memory item so map markers
  /// refresh without a full reload. [interval]/[maxTicks] are overridable for
  /// tests; production callers use the 3 s × 60 default.
  ///
  /// 60 s (the previous budget) was too short for a large bulk import: each
  /// photo is downloaded server-side in its own background task with up to a
  /// 30 s fetch timeout (see `_download_photo_from_url` in api/memories.py),
  /// and a big trip can queue far more of those than finish inside a minute.
  /// There's no pending-download-count signal from the server to poll against
  /// instead, so this widens the fixed budget rather than inferring one.
  void startPhotoPolling(ProjectRef ref,
      {Duration interval = const Duration(seconds: 3), int maxTicks = 60}) {
    _stopPhotoPolling();
    if (_isDisposed) return;
    var remainingTicks = maxTicks;
    _photoPollingTimer = Timer.periodic(interval, (_) async {
      if (remainingTicks <= 0 || this.ref != ref) {
        _stopPhotoPolling();
        return;
      }
      remainingTicks--;
      await _refreshMemoryPhotos(ref);
    });
  }

  void _stopPhotoPolling() {
    _photoPollingTimer?.cancel();
    _photoPollingTimer = null;
  }

  Future<void> _refreshMemoryPhotos(ProjectRef ref) async {
    perfSpans.recordBackgroundRefresh('photo_poll');
    try {
      final photosById = await _service.getMemoryPhotos(ref);
      if (this.ref != ref) return;
      // The merge below swaps a whole item in, so each candidate is the item
      // already on screen rebuilt around the server's photo list — the shape
      // the full details payload used to hand it, minus the 36 MB (issue #308).
      final freshById = <String, Map<String, dynamic>>{};
      for (final item in items) {
        if (item['item_type'] != 'memory') continue;
        final mem = item['memory'] as Map?;
        if (mem == null) continue;
        final id = mem['id']?.toString();
        if (id == null) continue;
        final photos = photosById[id];
        if (photos == null) continue;
        freshById[id] = {
          ...item,
          'memory': {...Map<String, dynamic>.from(mem), 'photos': photos},
        };
      }
      var changed = false;
      for (int i = 0; i < items.length; i++) {
        if (items[i]['item_type'] != 'memory') continue;
        final mem = items[i]['memory'] as Map?;
        if (mem == null) continue;
        final id = mem['id']?.toString();
        if (id == null) continue;
        final fresh = freshById[id];
        if (fresh == null) continue;
        final oldCount = (mem['photos'] as List?)?.length ?? 0;
        final newCount = ((fresh['memory'] as Map?)?['photos'] as List?)?.length ?? 0;
        if (newCount != oldCount) {
          items[i] = fresh;
          changed = true;
        }
      }
      if (changed) {
        // New list identity: MapPanel's marker cache invalidates via
        // `identical(items, _lastItems)`, which a same-object in-place
        // mutation would never trip.
        items = List.of(items);
        await _revealItems(items);
        notifyListeners();
      }
    } catch (_) {}
  }

  bool _isDisposed = false;

  /// Whether this notifier is still mounted (not disposed). Background tasks
  /// (e.g. segment route polling) check this before touching captured UI such
  /// as a ScaffoldMessenger.
  @override
  bool get isAlive => !_isDisposed;

  @override
  void notifyListeners() {
    if (!_isDisposed) super.notifyListeners();
  }

  /// Discarded — by its screen, or by the provider when the account changes
  /// (issue #418). Work still in flight lands here afterwards, so this leaves
  /// it nothing to do: its loads and reloads are invalidated, no timer runs,
  /// and the guards on [_saveUiState] and the timer starts keep it from
  /// writing shared state or starting another.
  @override
  void dispose() {
    _isDisposed = true;
    _loadTrack.invalidate();
    _reloadTrack.invalidate();
    _detailsOnlyReloadTrack.invalidate();
    _buildFullTrackGen++;
    _stopPhotoPolling();
    _zoomRefetchTimer?.cancel();
    for (final timeout in _cameraIdleTimeouts) {
      timeout.cancel();
    }
    _cameraIdleTimeouts.clear();
    // Released rather than left hanging: the waits resume, find their load
    // invalidated above, and return.
    final idle = _cameraIdleWaiter;
    _cameraIdleWaiter = null;
    if (idle != null && !idle.isCompleted) idle.complete();
    stopDegradedRouteWatch(); // usually already stopped by the owning screen's dispose()
    stopSegmentResolvePolling();
    previewArcNotifier.dispose();
    elevationCursorNotifier.dispose();
    mapCursorDistNotifier.dispose();
    super.dispose();
  }

  // ── Item management ────────────────────────────────────────────────────────

  /// Trigger an async Strava re-fetch of one activity and poll until it lands.
  ///
  /// The server marks the activity `refresh_status="pending"` and returns 202
  /// immediately — the two Strava calls run in a background task because they
  /// can take minutes, which used to blow past the client's 30 s HTTP timeout
  /// and report a failure for work that had actually succeeded (issue #148).
  ///
  /// Sets [error] on failure and leaves it null on success; callers read it to
  /// decide which toast to show.
  ///
  /// [pollInterval] and [pollTimeout] exist so tests don't wait real seconds.
  Future<void> refreshActivity(
    int activityId, {
    @visibleForTesting Duration pollInterval = const Duration(seconds: 3),
    @visibleForTesting Duration pollTimeout = const Duration(minutes: 5),
  }) async {
    final ref = this.ref;
    if (ref == null) return;
    // Cleared up front: the caller decides success/failure by reading `error`
    // after awaiting this, so a stale message from an earlier operation would
    // otherwise report a perfectly good re-fetch as a failure.
    error = null;
    try {
      await _service.refreshActivity(ref, activityId);
    } on Exception catch (e) {
      error = _loadErrorMessage(e);
      notifyListeners();
      return;
    }
    await _pollActivityRefresh(
      ref, activityId, interval: pollInterval, timeout: pollTimeout,
    );
  }

  /// Poll `/meta` until [activityId] flips from `pending` to `resolved`/`failed`,
  /// then reload the project data so the map and charts show the new track.
  ///
  /// Self-cancels if the user navigates to another project or the activity is
  /// deleted mid-refresh. Mirrors `pollSegmentResolution`.
  Future<void> _pollActivityRefresh(
    ProjectRef ref,
    int activityId, {
    Duration interval = const Duration(seconds: 3),
    Duration timeout = const Duration(minutes: 5),
  }) async {
    final deadline = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(deadline)) {
      await Future<void>.delayed(interval);
      if (this.ref != ref) return; // navigated away
      Map<String, dynamic> meta;
      try {
        meta = await _service.getDetailsMeta(ref);
      } on Exception {
        continue; // transient network error — retry on the next tick
      }
      if (this.ref != ref) return;

      final rawActivities = meta['activities'];
      final fresh = rawActivities is List
          ? rawActivities.cast<Map<String, dynamic>>()
          : <Map<String, dynamic>>[];
      final match = fresh.firstWhere(
        (a) => a['id'] == activityId,
        orElse: () => const {},
      );
      if (match.isEmpty) return; // deleted mid-refresh
      final stat = match['refresh_status'] as String?;

      if (stat == 'failed') {
        error = (match['refresh_error'] as String?) ?? 'Re-fetch failed';
        notifyListeners();
        return;
      }
      // Still pending → keep polling. A job orphaned by a server restart never
      // writes a verdict; [timeout] below is what bounds that case, and the
      // row's own refresh_started_at is what a later reopen would judge it by.
      if (stat == 'pending') continue;

      // Terminal and not failed → the row now holds fresh Strava data.
      await _applyRefreshedProject(ref);
      return;
    }
    error = 'Re-fetch is taking longer than expected. '
        'Reopen the project to see the result.';
    notifyListeners();
  }

  /// Reload the project after a successful re-fetch.
  ///
  /// Polling uses `/meta` because it is 10-15× smaller, but `/meta` omits
  /// summary_polyline and elevation_profile — adopting that payload would blank
  /// the very track the re-fetch just updated. So the verdict comes from the
  /// cheap poll and the data from one full fetch here.
  Future<void> _applyRefreshedProject(ProjectRef ref) async {
    // issue #283: on _reloadTrack (shared with _silentReload, not _loadTrack)
    // so a second concurrent top-level reload for the same ref — which a
    // bare `this.ref != ref` check can't tell apart from this call, since
    // ProjectRef.== is structural — still supersedes this call, without also
    // being able to cancel load()'s own unrelated in-flight Phase 2. Compares
    // against the ref this call started with (frozen at begin()), not the
    // live `this.ref` field _applyDetails-style code can legitimately
    // reassign mid-call — see _SupersessionTrack's doc for why that matters.
    final token = _reloadTrack.begin(ref);
    final Map<String, dynamic> details;
    try {
      details = await _service.getDetails(ref, bypassCache: true);
    } on Exception catch (e) {
      if (!_reloadTrack.isCurrent(token, ref)) return;
      error = _loadErrorMessage(e);
      notifyListeners();
      return;
    }
    if (!_reloadTrack.isCurrent(token, ref)) return;

    final rawActivities = details['activities'];
    activities = rawActivities is List
        ? rawActivities.cast<Map<String, dynamic>>()
        : [];
    await _revealActivities(activities);
    final rawItems = details['items'];
    items = rawItems is List ? rawItems.cast<Map<String, dynamic>>() : [];
    await _revealItems(items);
    if (!_reloadTrack.isCurrent(token, ref)) return;
    _updateStats();
    await _buildFullTrack();
    if (!_reloadTrack.isCurrent(token, ref)) return;
    // Refresh GeoJSON so the map polylines reflect the updated track.
    if (encryption.isUnlocked) {
      geo = client_geo.buildFullGeo(items, client_geo.activitiesById(activities));
    } else {
      final fetched = await fetchServerGeo(
          () => _service.getGeo(ref, bypassCache: true));
      if (!_reloadTrack.isCurrent(token, ref)) return;
      // Through the overlay like every other server geo, so a patch this
      // answer supersedes cannot come back at the next rebuild (I1-R2-2).
      reconcileSegmentOverlay(fetched.geo, requestedAt: fetched.requestedAt);
      geo = {
        'type': 'FeatureCollection',
        'features': mergePendingSegmentPatches(
            List<dynamic>.from(fetched.geo['features'] as List? ?? [])),
      };
    }
    if (!_reloadTrack.isCurrent(token, ref)) return;
    notifyListeners();
  }

  void removeItemLocally(int index) {
    if (index >= 0 && index < items.length) {
      // New list identity, not an in-place removeAt/removeWhere: map_panel's
      // marker/polyline cache and ProjectNotifier.dayStats both invalidate
      // via `identical(items/activities, _last...)`, which a same-object
      // in-place mutation would never trip (see the memory-refresh path
      // above for the same convention).
      final next = List.of(items);
      final removed = next.removeAt(index);
      items = next;
      if (removed['item_type'] == 'activity') {
        final actId = removed['activity_id']?.toString();
        activities = activities.where((a) => a['id']?.toString() != actId).toList();
      }
    }
    notifyListeners();
  }

  Future<void> removeItem(int index) async {
    final ref = this.ref;
    if (ref == null) return;
    removeItemLocally(index);
    try {
      await api.delete(ref.path('/items/$index'));
      await _silentReload(ref);
    } on Exception catch (e) {
      error = _msg(e);
      notifyListeners();
    }
  }

  /// API-only delete used by the undo-aware dismiss flow: the local removal
  /// has already happened via [removeItemLocally].
  Future<void> confirmRemoveItem(int index) async {
    final ref = this.ref;
    if (ref == null) return;
    try {
      await api.delete(ref.path('/items/$index'));
      await _silentReload(ref);
    } on Exception catch (e) {
      error = _msg(e);
      notifyListeners();
    }
  }

  Future<void> sortItemsByDate() async {
    final ref = this.ref;
    if (ref == null) return;
    try {
      await _service.sortItems(ref);
      await _silentReloadDetailsOnly(ref);
    } on Exception catch (e) {
      error = _msg(e);
      notifyListeners();
    }
  }

  Future<void> reorderItems(int fromIndex, int toIndex) async {
    final ref = this.ref;
    if (ref == null) return;
    // Immediate local update so the list responds without a blank flash. New
    // list object, not an in-place removeAt/insert: map_panel's marker cache
    // and dayStats/orderedDayKeys above invalidate via
    // identical(items, _last...), which a same-object mutation never trips.
    final newItems = List.of(items);
    final moved = newItems.removeAt(fromIndex);
    newItems.insert(toIndex, moved);
    items = newItems;
    notifyListeners();
    try {
      await api.put(
        ref.path('/items/reorder'),
        {'from_index': fromIndex, 'to_index': toIndex},
      );
      await _silentReloadDetailsOnly(ref);
    } on Exception catch (e) {
      error = _msg(e);
      notifyListeners();
    }
  }

  // ── Activity track editing (issue #31) ─────────────────────────────────────

  /// Fetch a single activity's geometry (with `map.summary_polyline` and
  /// `elevation_profile`) for [activityId] — the editor needs the geometry that
  /// the lightweight meta/list load omits. Uses the per-activity endpoint so the
  /// editor doesn't download the whole project. Returns null if not found.
  Future<Map<String, dynamic>?> fetchActivityForEdit(int activityId) async {
    final ref = this.ref;
    if (ref == null) return null;
    return _service.getActivityTrack(ref, activityId);
  }

  /// Save an edited track (trim/add/remove) for [activityId]. [payload] is
  /// [TrackEditModel.toSavePayload]. [lockVersion], when given, is the
  /// project's lock_version the editor last saw — a 409 means the activity
  /// changed elsewhere since (issue #31); see ActivityEditorPage._save.
  /// Reloads geometry on success. Rethrows so the editor page can surface the
  /// failure and keep the user's edits.
  Future<void> saveActivityTrack(
    int activityId, Map<String, dynamic> payload, {int? lockVersion}) async {
    final ref = this.ref;
    if (ref == null) return;
    await _service.saveActivityTrack(ref, activityId, payload,
        lockVersion: lockVersion);
    await _silentReload(ref);
  }

  /// Reset [activityId]'s track to the original Strava geometry.
  Future<void> resetActivityTrack(int activityId) async {
    final ref = this.ref;
    if (ref == null) return;
    await _service.resetActivityTrack(ref, activityId);
    await _silentReload(ref);
  }

  /// Split [activityId] at [splitIndex]; the tail becomes a new local activity.
  /// When [dropBoundary] is true, the tail excludes the shared boundary point
  /// (#104 — used when a transportation segment will bridge the cut).
  ///
  /// [payload] is the editor's TrackEditModel.toSavePayload, i.e. the point list
  /// [splitIndex] indexes into, so pending edits are cut along with the track
  /// rather than discarded (#127).
  ///
  /// [lockVersion], when given, is the project's lock_version the editor last
  /// saw — a 409 means the activity changed elsewhere since (issue #31); see
  /// ActivityEditorPage._confirmSplit.
  Future<void> splitActivity(
    int activityId,
    int splitIndex, {
    bool dropBoundary = false,
    Map<String, dynamic>? payload,
    int? lockVersion,
  }) async {
    final ref = this.ref;
    if (ref == null) return;
    await _service.splitActivity(ref, activityId, splitIndex,
        dropBoundary: dropBoundary, payload: payload, lockVersion: lockVersion);
    await _silentReload(ref);
  }

  /// Delete a local (split-tail, negative-id) [activityId].
  ///
  /// The server may refuse — a companion deleting someone else's piece of a
  /// Strava activity gets a 403. The panel has already removed the item
  /// optimistically, so a refusal reloads to bring it back and sets [error]
  /// to say why, then rethrows for callers with their own failure message.
  Future<void> deleteLocalActivity(int activityId) async {
    final ref = this.ref;
    if (ref == null) return;
    try {
      await _service.deleteLocalActivity(ref, activityId);
    } on Exception catch (e) {
      await _silentReload(ref);
      error = _msg(e);
      notifyListeners();
      rethrow;
    }
    await _silentReload(ref);
  }

  // ── Internal helpers ───────────────────────────────────────────────────────

  /// Saves only the day-meta that changed (issue #397): [days] are set,
  /// [delete] are removed, every other day is left to the server's copy.
  /// Sleeping options, groups and counters go along only when given.
  ///
  /// The edit shows at once. On 200 the server's merged map is adopted, so days
  /// another device changed arrive here; on failure day-meta is reloaded
  /// instead of keeping a copy the server never accepted.
  Future<void> saveDayMeta({
    Map<String, Map<String, dynamic>> days = const {},
    List<String> delete = const [],
    List<String>? newSleepingOptions,
    Map<String, String>? newSleepingOptionGroups,
    List<Map<String, dynamic>>? newCounters,
  }) async {
    final ref = this.ref;
    if (ref == null) return;
    if (days.isEmpty &&
        delete.isEmpty &&
        newSleepingOptions == null &&
        newSleepingOptionGroups == null &&
        newCounters == null) {
      return;
    }
    // The whole map, built now: the 405 fallback below runs after an await,
    // by which time another trip may have replaced dayMeta. The account too,
    // for the same fallback: by then another one may hold the token.
    final account = api.tokenUserId;
    final merged = {
      for (final e in dayMeta.entries)
        if (!delete.contains(e.key)) e.key: e.value,
      ...days,
    };
    dayMeta = {...merged};
    _autoFillDaysToToday();
    if (newSleepingOptions != null) sleepingOptions = newSleepingOptions;
    if (newSleepingOptionGroups != null) sleepingOptionGroups = newSleepingOptionGroups;
    if (newCounters != null) counters = newCounters;
    notifyListeners();
    try {
      Object? res;
      try {
        res = await api.patch(ref.path('/day-meta'), {
          'days': days,
          'delete': delete,
          if (newSleepingOptions != null) 'sleeping_options': newSleepingOptions,
          if (newSleepingOptionGroups != null) 'sleeping_option_groups': newSleepingOptionGroups,
          if (newCounters != null) 'counters': newCounters,
        });
      } on ApiException catch (e) {
        if (e.statusCode != 405) rethrow;
        // A server that predates the PATCH (the tag publishes the APK before
        // the server is deployed): its PUT still takes the whole map. No trip
        // check here: [merged] and [ref] are this trip's, captured before the
        // await, so a trip opened since must not cost this save (I1-R2-1).
        // An account change does: the PUT would carry the next account's
        // token to its trip of the same name and replace its days (I1-R3-2).
        if (api.tokenUserId != account) return;
        await api.put(ref.path('/day-meta'), {
          'day_meta': merged,
          if (newSleepingOptions != null) 'sleeping_options': newSleepingOptions,
          if (newSleepingOptionGroups != null) 'sleeping_option_groups': newSleepingOptionGroups,
          if (newCounters != null) 'counters': newCounters,
        });
        return;
      }
      if (this.ref != ref) return;
      final raw = res is Map ? res['day_meta'] : null;
      if (raw is Map) {
        final server = raw.map(
            (k, v) => MapEntry(k as String, Map<String, dynamic>.from(v as Map)));
        // The server's value wins for every day it returns. A day it no longer
        // has stays only when it is an empty gap-fill day, which exists in
        // memory alone.
        dayMeta = {
          for (final e in dayMeta.entries)
            if (!server.containsKey(e.key) && e.value.isEmpty) e.key: e.value,
          ...server,
        };
        _autoFillDaysToToday();
        notifyListeners();
      }
    } on Exception catch (e) {
      await _reloadDayMeta(ref);
      if (this.ref != ref) return;
      error = _msg(e);
      notifyListeners();
    }
  }

  /// Restores what a rejected [saveDayMeta] had applied optimistically.
  Future<void> _reloadDayMeta(ProjectRef ref) async {
    try {
      final details = await _service.getDetailsMeta(ref);
      if (this.ref != ref) return;
      final rawDm = details['day_meta'];
      dayMeta = rawDm is Map
          ? rawDm.map((k, v) => MapEntry(k as String, Map<String, dynamic>.from(v as Map)))
          : {};
      final rawOpts = details['sleeping_options'];
      final optList = rawOpts is List ? List<String>.from(rawOpts) : <String>[];
      sleepingOptions = optList.isNotEmpty ? optList : List<String>.from(_defaultSleepingOptions);
      final rawGroups = details['sleeping_option_groups'];
      sleepingOptionGroups = rawGroups is Map
          ? Map<String, String>.from(rawGroups.cast<String, String>())
          : { for (final n in sleepingOptions) n: _defaultSleepingGroups[n] ?? 'Other' };
      final rawCounters = details['counters'];
      counters = rawCounters is List
          ? rawCounters.map((c) => Map<String, dynamic>.from(c as Map)).toList()
          : [];
      _autoFillDaysToToday();
    } on Exception {
      // The caller reports the save failure; a failed reload leaves the
      // optimistic copy, as before.
    }
  }


  /// Fills missing dayMeta entries from the earliest known date up to today,
  /// in memory only — does NOT write to the backend.  Real data is only
  /// persisted when the user edits a day or saves project settings.
  void _autoFillDaysToToday() {
    if (!_tripIsActive) return;

    final now = DateTime.now();
    final todayDate = DateTime(now.year, now.month, now.day);

    String? earliest = tripStart;
    if (dayMeta.isNotEmpty) {
      final minKey = (dayMeta.keys.toList()..sort()).first;
      if (earliest == null || minKey.compareTo(earliest) < 0) earliest = minKey;
    }
    for (final a in activities) {
      final d = a['start_date_local'] as String?;
      if (d != null && d.length >= 10) {
        final dk = d.substring(0, 10);
        if (earliest == null || dk.compareTo(earliest) < 0) earliest = dk;
      }
    }
    earliest ??= '${todayDate.year.toString().padLeft(4, '0')}-'
                 '${todayDate.month.toString().padLeft(2, '0')}-'
                 '${todayDate.day.toString().padLeft(2, '0')}';

    final startDate = DateTime.tryParse(earliest);
    if (startDate == null || startDate.isAfter(todayDate)) return;

    final updated = Map<String, Map<String, dynamic>>.from(dayMeta);
    bool changed = false;
    DateTime cursor = DateTime(startDate.year, startDate.month, startDate.day);
    while (!cursor.isAfter(todayDate)) {
      final key =
          '${cursor.year.toString().padLeft(4, '0')}-'
          '${cursor.month.toString().padLeft(2, '0')}-'
          '${cursor.day.toString().padLeft(2, '0')}';
      if (!updated.containsKey(key)) {
        updated[key] = {};
        changed = true;
      }
      // Advance by date arithmetic, not by 24 h, so DST spring-forwards
      // don't leave cursor at 01:00 and cause today to be skipped.
      cursor = DateTime(cursor.year, cursor.month, cursor.day + 1);
    }
    if (!changed) return;
    dayMeta = updated;
  }

  /// Full reload: details + geo. Use when a mutation can change map geometry
  /// (remove item, add/update/delete segment, refresh activity).
  Future<void> _silentReload(ProjectRef ref) async {
    // issue #283: on _reloadTrack (shared with _applyRefreshedProject, not
    // _loadTrack) so a second concurrent reload for the same ref — which a
    // bare `this.ref != ref` check can't tell apart from this call, since
    // ProjectRef.== is structural — still supersedes this call, without also
    // being able to cancel load()'s own unrelated in-flight Phase 2. Compares
    // against the ref this call started with (frozen at begin()), not the
    // live `this.ref` field _applyDetails reassigns mid-call — see
    // _SupersessionTrack's doc for why that matters.
    //
    // A reload for a trip the user has left would make it the open trip
    // again (I1-R5-1): not started once another trip is open, and dropped if
    // one opens during any of its awaits. load() invalidates this track, but
    // a reload begun after it gets a fresh token, hence the trip check too.
    if (!isOpenTrip(ref)) return;
    final token = _reloadTrack.begin(ref);
    bool stale() => !_reloadTrack.isCurrent(token, ref) || !isOpenTrip(ref);
    // Set false only by the staleness checks below, so a navigation that
    // lands during an await (issue #276 follow-up) suppresses the notify —
    // every other path (success or the catch below) keeps notifying exactly
    // as before.
    var notify = true;
    try {
      if (encryption.isUnlocked) {
        // The server can't build geo for encrypted activities (issue #29) —
        // build it client-side from the just-reloaded, decrypted activities.
        final details = await _service.getDetailsMeta(ref);
        if (!await _applyDetails(details, ref, stale)) {
          notify = false;
          return;
        }
        _autoFillDaysToToday();
        geo = client_geo.buildFullGeo(items, client_geo.activitiesById(activities));
      } else {
        final results = await Future.wait<Object>([
          _service.getDetailsMeta(ref),
          fetchServerGeo(() => _service.getGeo(ref, bypassCache: true)),
        ]);
        final details = results[0] as Map<String, dynamic>;
        final fetched =
            results[1] as ({Map<String, dynamic> geo, int requestedAt});
        if (!await _applyDetails(details, ref, stale)) {
          notify = false;
          return;
        }
        _autoFillDaysToToday();
        // Through the overlay like every other server geo: assigned as is, it
        // showed the server's route but left a patch it supersedes to come
        // back at the next rebuild (I1-R2-2).
        reconcileSegmentOverlay(fetched.geo, requestedAt: fetched.requestedAt);
        geo = {
          'type': 'FeatureCollection',
          'features': mergePendingSegmentPatches(
              List<dynamic>.from(fetched.geo['features'] as List? ?? [])),
        };
      }
      _updateStats();
      await _buildFullTrack();
      if (stale()) {
        notify = false;
        return;
      }
    } on Exception catch (e) {
      // issue #283 review finding: this used to set `error`/notify
      // unconditionally on the exception path — the staleness guard above
      // only covered the success path — so a stale, superseded call's
      // failure (e.g. a network error arriving after a second concurrent
      // _silentReload/load for the same ref already succeeded) could
      // overwrite fresh state with a stale error and fire a spurious notify.
      if (stale()) {
        notify = false;
      } else {
        error = _msg(e);
      }
    } finally {
      if (notify) notifyListeners();
    }
  }

  /// Details-only reload: skips the heavy GeoJSON fetch. Use when a mutation
  /// cannot change map geometry (reorder, trip-start, memory CRUD).
  Future<void> _silentReloadDetailsOnly(ProjectRef ref) async {
    // issue #283: this reload had no staleness guard at all originally — a
    // second concurrent call (or a navigation away) could clobber the
    // outcome of a later, current one landing first. On _detailsOnlyReloadTrack
    // (see its doc above) rather than _loadTrack/_reloadTrack, since this
    // reload has no geo/details of its own to offer in exchange for
    // superseding whoever it cancels.
    //
    // The open-trip check is _silentReload's (I1-R5-2): a memory, journal or
    // people save, a sort or a reorder can return after another trip opened,
    // and its reload then begins after load() invalidated this track.
    if (!isOpenTrip(ref)) return;
    final token = _detailsOnlyReloadTrack.begin(ref);
    bool stale() =>
        !_detailsOnlyReloadTrack.isCurrent(token, ref) || !isOpenTrip(ref);
    try {
      final details = await _service.getDetailsMeta(ref);
      // Checked before _applyDetails mutates activities/items/dayMeta/ref,
      // not just before the trailing notify/error below — a stale call used
      // to run this unconditionally and only skip the notify, leaving a
      // second concurrent call's fresher result silently overwritten by a
      // slower, superseded one (the same bug class applyFullActivities was
      // fixed for — issue #283 review finding).
      if (stale()) return;
      if (!await _applyDetails(details, ref, stale)) return;
      _autoFillDaysToToday();
      _updateStats();
    } on Exception catch (e) {
      if (stale()) return;
      error = _msg(e);
    } finally {
      if (!stale()) notifyListeners();
    }
  }

  /// Decrypt in-scope memory/journal text in [list] in place (issue #26), and
  /// record in [undecryptedFields] each field it leaves as ciphertext: locked,
  /// or another account's key (#466). It resets that record, so pass the whole
  /// item list. Idempotent: text already revealed is not a well-formed
  /// envelope, so it is neither decrypted nor marked again.
  Future<void> _revealItems(List<Map<String, dynamic>> list) async =>
      _recordUndecrypted(await _revealItemText(list));

  /// [_revealItems] without the record: decrypts [list] in place and returns
  /// the fields it left as ciphertext, touching no notifier state, for a
  /// caller that may still drop [list] after the awaits (see [_applyDetails]).
  Future<List<(String, String, String)>> _revealItemText(
      List<Map<String, dynamic>> list) async {
    final undecrypted = <(String, String, String)>[];
    for (final item in list) {
      switch (item['item_type']) {
        case 'memory':
          final m = item['memory'];
          if (m is Map) {
            m['name'] = await _revealField('memory', m, 'name', undecrypted);
            m['description'] =
                await _revealField('memory', m, 'description', undecrypted);
          }
        case 'journal':
          final j = item['journal'];
          if (j is Map) {
            j['description'] =
                await _revealField('journal', j, 'description', undecrypted);
          }
      }
    }
    return undecrypted;
  }

  /// Replaces [undecryptedFields] with [undecrypted], as [_revealItemText]
  /// returned it.
  void _recordUndecrypted(List<(String, String, String)> undecrypted) {
    undecryptedFields.reset();
    for (final (kind, id, field) in undecrypted) {
      undecryptedFields.mark(kind, id, field);
    }
  }

  Future<String?> _revealField(String kind, Map entry, String field,
      List<(String, String, String)> undecrypted) async {
    final stored = entry[field] as String?;
    final revealed = await encryption.reveal(stored);
    final id = entry['id']?.toString();
    if (id != null &&
        stored != null &&
        revealed == stored &&
        EncryptedField.isWellFormed(stored)) {
      undecrypted.add((kind, id, field));
    }
    return revealed;
  }

  /// Decrypt in-scope activity fields in [list] in place (issue #29). `name`
  /// and `map.summary_polyline` are plain string fields — reveal() swaps
  /// ciphertext for plaintext directly, same as memory name/description.
  /// `start_latlng`, `end_latlng` and `elevation_profile` can't carry a
  /// ciphertext *string* in their normal (list / distance-elevation-pairs)
  /// shape, so the server sends their ciphertext via the sibling
  /// `start_latlng_enc` / `end_latlng_enc` / `elevation_profile_enc` keys
  /// instead (see ActivityMixin._row_to_activity and Activity.to_strava_dict
  /// on the server) — decrypt those, JSON-decode the recovered plaintext, and
  /// write the result into the normal key so every existing consumer (map,
  /// elevation chart, _buildFullTrack) needs no changes. No-op when
  /// encryption is locked/off; idempotent, so it's safe to call after any
  /// activities load.
  Future<void> _revealActivities(List<Map<String, dynamic>> list) async {
    if (!encryption.isUnlocked) return;
    for (final a in list) {
      a['name'] = await encryption.reveal(a['name'] as String?);
      final map = a['map'];
      if (map is Map) {
        map['summary_polyline'] =
            await encryption.reveal(map['summary_polyline'] as String?);
      }

      final startEnc = a['start_latlng_enc'] as String?;
      if (startEnc != null) {
        final revealed = await encryption.reveal(startEnc);
        if (revealed != null && revealed != startEnc) {
          try {
            a['start_latlng'] = jsonDecode(revealed);
          } catch (_) {
            // Wrong key / corrupt — leave start_latlng as the None the server
            // already sent for this encrypted field.
          }
        }
      }
      final endEnc = a['end_latlng_enc'] as String?;
      if (endEnc != null) {
        final revealed = await encryption.reveal(endEnc);
        if (revealed != null && revealed != endEnc) {
          try {
            a['end_latlng'] = jsonDecode(revealed);
          } catch (_) {}
        }
      }
      final epEnc = a['elevation_profile_enc'] as String?;
      if (epEnc != null) {
        final revealed = await encryption.reveal(epEnc);
        if (revealed != null && revealed != epEnc) {
          try {
            final decoded = jsonDecode(revealed) as Map<String, dynamic>;
            final d = (decoded['distances_km'] as List).cast<num>();
            final e = (decoded['elevations_m'] as List).cast<num>();
            final n = d.length < e.length ? d.length : e.length;
            a['elevation_profile'] = [
              for (int i = 0; i < n; i++) [d[i], e[i]],
            ];
          } catch (_) {}
        }
      }
    }
  }

  /// Adopts [details] for [ref] unless [stale] says it no longer should, and
  /// returns whether it did. Everything that awaits — the reveals — runs on
  /// the response's own lists first; [stale] is checked once they are done,
  /// and the open trip, its items, people and day meta are then all assigned
  /// with no await between. Assigning `ref` before the reveals left a window
  /// in which the open trip was the reload's but the items still another
  /// trip's, so an index-addressed save such as reorderItems or removeItem
  /// went to the wrong trip (I1-R5-1).
  Future<bool> _applyDetails(
      dynamic details, ProjectRef ref, bool Function() stale) async {
    final rawActivities = details['activities'];
    final List<Map<String, dynamic>> nextActivities = rawActivities is List
        ? rawActivities.cast<Map<String, dynamic>>()
        : [];
    await _revealActivities(nextActivities);
    final rawItems = details['items'];
    final List<Map<String, dynamic>> nextItems = rawItems is List
        ? rawItems.cast<Map<String, dynamic>>()
        : [];
    final undecrypted = await _revealItemText(nextItems);
    if (stale()) return false;
    this.ref = ref.copyWith(
      name: details['name'] as String? ?? ref.name,
      role: details['caller_role'] as String? ?? ref.role,
    );
    tripStart   = details['trip_start'] as String?;
    final rawColor = details['track_color'] as String?;
    if (rawColor != null && rawColor.length == 7 && rawColor.startsWith('#')) {
      trackColor = Color(int.parse(rawColor.substring(1), radix: 16) | 0xFF000000);
    }
    final rawWidth = details['track_width'] as num?;
    if (rawWidth != null) trackWidth = rawWidth.toDouble();
    final rawAlt = details['alternating_track_colors'] as bool?;
    if (rawAlt != null) alternatingTrackColors = rawAlt;
    final rawElColor = details['elevation_chart_color'] as String?;
    elevationChartColor = (rawElColor != null && rawElColor.length == 7 && rawElColor.startsWith('#'))
        ? Color(int.parse(rawElColor.substring(1), radix: 16) | 0xFF000000)
        : null;
    final rawElLine = details['elevation_chart_show_line'] as bool?;
    if (rawElLine != null) elevationChartShowLine = rawElLine;
    final rawLangs = details['languages'];
    if (rawLangs is List) languages = rawLangs.cast<String>();
    final rawColorByType = details['color_by_type'] as bool?;
    if (rawColorByType != null) colorByType = rawColorByType;
    final rawTypeStyles = details['type_styles'];
    typeStyles = rawTypeStyles is Map
        ? rawTypeStyles.map((k, v) =>
            MapEntry(k as String, Map<String, dynamic>.from(v as Map)))
        : {};
    tripEnd     = details['trip_end']   as String?;
    activities = nextActivities;
    items = nextItems;
    _recordUndecrypted(undecrypted);
    final rawPeople = details['people'];
    people = rawPeople is List
        ? rawPeople.cast<Map<String, dynamic>>()
        : [];
    final rawPeopleGroups = details['groups'];
    groups = rawPeopleGroups is List
        ? rawPeopleGroups.cast<Map<String, dynamic>>()
        : [];
    final rawDm = details['day_meta'];
    dayMeta = rawDm is Map
        ? rawDm.map((k, v) => MapEntry(k as String, Map<String, dynamic>.from(v as Map)))
        : {};
    final rawOpts = details['sleeping_options'];
    sleepingOptions = rawOpts is List
        ? List<String>.from(rawOpts)
        : List<String>.from(_defaultSleepingOptions);
    return true;
  }

  // ── Mixin delegates (forward private helpers to ProjectMemoryCrudMixin) ────

  @override
  ProjectService get service => _service;

  @override
  ProjectRef? get projectRef => ref;

  @override
  Future<void> reloadDetailsOnly(ProjectRef ref) => _silentReloadDetailsOnly(ref);

  @override
  String errorMessage(Exception e) => _msg(e);

  String _msg(Exception e) {
    final s = e.toString();
    final m = RegExp(r'"detail"\s*:\s*"([^"]+)"').firstMatch(s);
    return m?.group(1) ?? s.replaceFirst('Exception: ', '');
  }

  /// A message for a background project fetch that failed — the initial load,
  /// the full-res geo upgrade, or an activity re-fetch (issues #178, #190).
  ///
  /// [_msg] is right for the action-triggered failures it was written for — it
  /// unwraps the server's `detail` — but a raw
  /// `TimeoutException after 0:00:30.000000: Future not completed` sitting in
  /// the middle of the activity panel is a stack-trace fragment, not a message.
  /// Anything the server actually explained is still passed through verbatim;
  /// only the transport failures get plain language.
  String _loadErrorMessage(Object e) {
    // Checked before the general Exception case below: TimeoutException
    // implements Exception, and its toString has no `detail` to unwrap.
    if (e is TimeoutException) {
      return 'The server took too long to answer. It may still be catching up '
          '— reopen the trip in a moment.';
    }
    if (e is Exception) return _msg(e);
    return "Couldn't load this trip. Check your connection and reopen it.";
  }

}
