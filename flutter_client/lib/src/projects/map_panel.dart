library;

import 'dart:math' show pow;
import 'dart:typed_data' show Uint8List;
import 'dart:ui' as ui show Path, PathFillType;

import 'package:flutter/foundation.dart' show compute;
import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:http/http.dart' as http;
import 'package:latlong2/latlong.dart';
import 'package:vector_map_tiles/vector_map_tiles.dart';

import '../core/brand.dart';
import '../core/concurrency_gate.dart';
import '../core/design_tokens.dart'
    show kAccent, kShadow2, monoStyle, activityTypeBucket, segmentTypeBucket,
        resolveTypeStyle, LineStyleKind;
import '../core/perf_subtree.dart';
import '../core/perf_timing.dart' show kPerfTiming, perfSpans;
import '../map/geo_point.dart';
import 'activity_panel.dart';
import 'basemaps.dart';
import 'bounded_byte_cache.dart';
import 'map_geometry_memo.dart';
import 'polyline_decimation.dart';
import 'memory_detail_modal.dart';
import 'people_screen.dart' show showGroupDetailSheet, showPersonDetailSheet;
import 'people_search.dart' show classifyEncounterPin;
import 'photo_thumb_cache.dart';
import 'project_notifier.dart';

// Existing callers (and map_geometry_memo_test.dart) import this helper from
// map_panel.dart, where it used to live; keep that entry point.
export 'map_geometry_memo.dart' show memoArcMidpoint, memoCoordsToLatLng;
export 'polyline_decimation.dart'
    show decimatePolylinePoints, kMaxTotalPolylinePoints, totalPolylinePoints;
LatLng _ll(GeoPoint p) => LatLng(p.lat, p.lon);

IconData _iconForActivityType(String? type) => switch (type?.toLowerCase()) {
  'run' || 'virtualrun'                  => Icons.directions_run,
  'ride' || 'virtualride' || 'ebikeride' => Icons.directions_bike,
  'hike' || 'walk'                       => Icons.hiking,
  _                                      => Icons.map_outlined,
};

// Start (green) / end (red) markers for the selected activity (issue #19).
const Color _kStartMarkerColor = Color(0xFF22C55E); // green-500
const Color _kEndMarkerColor   = Color(0xFFEF4444); // red-500

// Focused-location marker (issue #72) — tapping an encounter's place icon
// zooms in and drops this pin, distinct from the (red) encounter pins.
const Color _kFocusMarkerColor = Color(0xFFF59E0B); // amber-500

// A degraded segment (route_degraded=true — the server fell back to a
// straight endpoint chord because it couldn't find real track geometry, see
// api/segments.py) is drawn in this muted grey regardless of type colouring
// or selection, so it reads as "approximate" on the map itself rather than
// only in the tile list (issue #207). Line *style* (dashed/solid) is left
// alone — colour is the only signal.
const Color _kDegradedRouteColor = Color(0xFF64748B); // slate-500

// A segment whose route resolution ended in route_status="failed" (e.g. no
// ferry route exists, or the job never got a verdict) otherwise renders as an
// ordinary great-circle line forever — indistinguishable from an intentional
// straight segment. Drawn in this red so it reads as
// "needs attention" on the map itself, same rationale as the degraded colour
// above; retrying is still done from the tile list's "tap to retry" row.
const Color _kFailedRouteColor = Color(0xFFDC2626); // red-600

/// A single highlighted pin at [point] — rendered by both map widgets when
/// [MapPanel.focusedLatLng] / [ManageMapPanel.focusedLatLng] is set (issue #72).
Marker focusedLocationMarker(LatLng point) => Marker(
      point: point,
      width: 30,
      height: 30,
      alignment: Alignment.center,
      child: Container(
        decoration: BoxDecoration(
          color: _kFocusMarkerColor,
          shape: BoxShape.circle,
          border: Border.all(color: Colors.white, width: 3),
        ),
        child: const Icon(Icons.place, size: 16, color: Colors.white),
      ),
    );

// "You are here" marker (issue #88) — dropped by the locate-me button, a
// plain dot (no icon glyph) so it's visually distinct from the amber
// focused-location pin above and from the locate-me button itself, which
// both use icon-in-circle styling.
const Color _kHereMarkerColor = Color(0xFF2563EB); // blue-600

/// The device-location pin dropped by the locate-me button (issue #88),
/// rendered by both map widgets when [MapPanel.hereLatLng] /
/// [ManageMapPanel.hereLatLng] is set.
Marker youAreHereMarker(LatLng point) => Marker(
      point: point,
      width: 20,
      height: 20,
      alignment: Alignment.center,
      child: Container(
        decoration: BoxDecoration(
          color: _kHereMarkerColor,
          shape: BoxShape.circle,
          border: Border.all(color: Colors.white, width: 3),
        ),
      ),
    );

/// Locate-me button (issue #88) — overlaid on the map, shared by [MapPanel]
/// and [ManageMapPanel]. Purely presentational: the async device-location
/// fetch and camera pan are owned by the parent screen (mirrors how
/// [focusedLocationMarker] taps bubble up via `onLocationTap` instead of
/// being handled inside this file), so this widget just renders the button
/// and reports taps via [onPressed].
class LocateMeButton extends StatelessWidget {
  final bool locating;
  final VoidCallback onPressed;

  const LocateMeButton({super.key, required this.locating, required this.onPressed});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Material(
      color: cs.surface.withValues(alpha: 0.94),
      shape: const CircleBorder(),
      elevation: 0,
      child: Container(
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          boxShadow: kShadow2(Theme.of(context).brightness),
        ),
        child: IconButton(
          icon: locating
              ? SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(
                      strokeWidth: 2, color: cs.onSurfaceVariant),
                )
              : Icon(Icons.my_location, color: cs.onSurfaceVariant),
          tooltip: 'Center on my location',
          onPressed: locating ? null : onPressed,
        ),
      ),
    );
  }
}

LatLng? _coordToLatLng(dynamic c) {
  if (c is! List || c.length < 2) return null;
  return LatLng((c[1] as num).toDouble(), (c[0] as num).toDouble());
}

// Per-feature geometry memoization (coordinate→LatLng and arc midpoint) lives
// in map_geometry_memo.dart so the decode path can seed both without importing
// UI code (issue #293). Both are re-exported above, so this library stays the
// single import point for callers.
/// Activity ids that begin a new day, in trip order: the first dated activity
/// of each distinct consecutive `start_date_local` date. Used to drop a day
/// breakpoint node at the start of each day on the map so days are visually
/// separable (issue #19). Non-activity items and dateless activities are
/// ignored; mirrors the activity panel's day-grouping (only activities advance
/// the running date).
Set<String> dayStartActivityIds(
  List<Map<String, dynamic>> items,
  Map<dynamic, Map<String, dynamic>> activityById,
) {
  final starts = <String>{};
  String? lastDate;
  for (final item in items) {
    if (item['item_type'] != 'activity') continue;
    final actId = item['activity_id'];
    final date =
        (activityById[actId]?['start_date_local'] as String?)?.split('T').first;
    if (date == null) continue;
    if (date != lastDate) {
      final id = actId?.toString();
      if (id != null) starts.add(id);
      lastDate = date;
    }
  }
  return starts;
}

/// Every day's activity_ids and segment_ids, in one pass over [items].
///
/// The day carousel (issue #199) selects a day via `notifier.selectDays()`,
/// which used to trigger a *full* re-derivation of the map: this index was
/// recomputed with one O(items) scan per selected day (via the old
/// `_dayItemIds` helper), on top of re-parsing every GeoJSON feature and
/// re-resolving every marker's type/icon/colour from scratch — all synchronous
/// work on the UI isolate. A single tap was slow but tolerable; a second tap
/// shortly after (still well within Android's ANR window) stacked another
/// full pass on top and blocked the UI isolate long enough to trip an ANR.
///
/// This index and the geometry specs in [_buildPolylineSpecs] /
/// [_buildActivityMarkerSpecs] / [_buildSegmentMarkerSpecs] depend only on
/// [items] (and, for the specs, geo/track style) — never on which day is
/// currently selected — so callers cache them and only rebuild when those
/// actually change. A selection change alone then only needs an O(1) lookup
/// per selected day here, plus the cheap restyle passes in
/// [_stylePolylines] / [_styleActivityMarkers] / [_styleSegmentMarkers].
Map<String, ({Set<String> actIds, Set<String> segIds})> buildDayIndex(
  List<Map<String, dynamic>> items,
  Map<dynamic, Map<String, dynamic>> activityById,
) {
  final index = <String, ({Set<String> actIds, Set<String> segIds})>{};
  String? lastDate;
  for (final item in items) {
    if (item['item_type'] == 'activity') {
      final a = activityById[item['activity_id']];
      final ds = (a?['start_date_local'] as String?)?.split('T').first;
      if (ds != null) lastDate = ds;
      final day = ds ?? lastDate;
      final id = item['activity_id']?.toString();
      if (day == null || id == null) continue;
      index.putIfAbsent(day, () => (actIds: <String>{}, segIds: <String>{}))
          .actIds
          .add(id);
    } else {
      final day = item['segment']?['date'] as String? ?? lastDate;
      final id = item['segment']?['id']?.toString();
      if (day == null || id == null) continue;
      index.putIfAbsent(day, () => (actIds: <String>{}, segIds: <String>{}))
          .segIds
          .add(id);
    }
  }
  return index;
}

/// The day key ("YYYY-MM-DD") a selected activity/segment belongs to, or null
/// when it belongs to no day at all.
///
/// Deliberately routed through [buildDayIndex] instead of reading the
/// activity's own `start_date_local`: the day the carousel follows a map tap
/// to (issue #322) is then by construction the same day whose highlight the
/// map draws when that day is selected, including buildDayIndex's
/// carry-forward rule for an activity/segment with no date of its own.
/// Building the whole index per lookup is deliberate too — this runs once per
/// map tap, next to a hit-test that already scans every coordinate in the
/// trip, and a cheaper direct scan would mean a second copy of that rule.
String? dayForSelection(
  List<Map<String, dynamic>> items,
  List<Map<String, dynamic>> activities, {
  dynamic activityId,
  String? segmentId,
}) {
  final actId = activityId?.toString();
  if (actId == null && segmentId == null) return null;
  final index =
      buildDayIndex(items, {for (final a in activities) a['id']: a});
  for (final entry in index.entries) {
    if (actId != null && entry.value.actIds.contains(actId)) return entry.key;
    if (segmentId != null && entry.value.segIds.contains(segmentId)) {
      return entry.key;
    }
  }
  return null;
}

/// A solid coloured disc with a white ring — used for the selected activity's
/// start (green) / end (red) markers so they read on any basemap.
Marker _dotMarker(LatLng point, Color color, {double size = 16}) => Marker(
      point: point,
      width: size,
      height: size,
      alignment: Alignment.center,
      child: Container(
        decoration: BoxDecoration(
          color: color,
          shape: BoxShape.circle,
          border: Border.all(color: Colors.white, width: 2),
        ),
      ),
    );

/// A white "bead" with a thick coloured ring — the day breakpoint node. The
/// inverse fill (vs the solid sport-icon markers) makes it read as a joint that
/// breaks the line between days rather than another activity marker (#19).
Marker _dayNodeMarker(LatLng point, Color ringColor, {double size = 15}) =>
    Marker(
      point: point,
      width: size,
      height: size,
      alignment: Alignment.center,
      child: Container(
        decoration: BoxDecoration(
          color: Colors.white,
          shape: BoxShape.circle,
          border: Border.all(color: ringColor, width: 3),
        ),
      ),
    );

/// Day breakpoint node markers for [geo] (#19): one bead at the start of each
/// day-start activity (see [dayStartActivityIds]). Shared by the live map and
/// the image export so both render days the same way. [ringColor] should match
/// the colour the track line is drawn with in that context.
List<Marker> buildDayBreakpointMarkers(
  Map<String, dynamic> geo,
  Set<String> dayStartIds,
  Color ringColor,
) {
  if (dayStartIds.isEmpty) return const [];
  final features = geo['features'];
  if (features is! List) return const [];
  final markers = <Marker>[];
  for (final feature in features) {
    if (feature is! Map) continue;
    final props = feature['properties'] as Map? ?? {};
    if (props['type'] != 'activity') continue;
    final actId = props['activity_id']?.toString();
    if (actId == null || !dayStartIds.contains(actId)) continue;
    final coords = (feature['geometry'] as Map? ?? {})['coordinates'];
    if (coords is! List || coords.isEmpty) continue;
    final start = _coordToLatLng(coords.first);
    if (start != null) markers.add(_dayNodeMarker(start, ringColor));
  }
  return markers;
}

/// Everything about an activity marker that doesn't depend on selection:
/// point, icon, and base (pre-highlight) colour. See [_buildActivityMarkerSpecs]
/// / [_styleActivityMarkers].
class _ActivityMarkerSpec {
  final LatLng point;
  final String? actId;
  final IconData icon;
  final Color baseColor;
  final LatLng? startPoint;
  final LatLng? endPoint;
  const _ActivityMarkerSpec({
    required this.point,
    required this.actId,
    required this.icon,
    required this.baseColor,
    required this.startPoint,
    required this.endPoint,
  });
}

/// Builds one spec per activity feature in [geo] — the geometry/icon/colour
/// resolution work that used to run again, from scratch, on every selection
/// change (see [buildDayIndex]'s doc comment). Independent of selection;
/// callers cache this and only rerun it when geo/items/style change.
List<_ActivityMarkerSpec> _buildActivityMarkerSpecs(
  Map<String, dynamic> geo,
  Color trackColor, {
  bool colorByType = false,
  Map<String, Map<String, dynamic>> typeStyles = const {},
}) {
  final features = geo['features'];
  if (features is! List) return const [];
  final specs = <_ActivityMarkerSpec>[];
  for (final feature in features) {
    if (feature is! Map) continue;
    final props = feature['properties'] as Map? ?? {};
    if (props['type'] != 'activity') continue;
    final coords = (feature['geometry'] as Map? ?? {})['coordinates'];
    if (coords is! List || coords.length < 2) continue;

    final point = memoArcMidpoint(coords);
    if (point == null) continue;

    final sportType = props['sport_type'] as String?;
    final baseColor = colorByType
        ? resolveTypeStyle(activityTypeBucket(sportType), isSegment: false,
            overrides: typeStyles[activityTypeBucket(sportType)]).color
        : trackColor;

    specs.add(_ActivityMarkerSpec(
      point: point,
      actId: props['activity_id']?.toString(),
      icon: _iconForActivityType(sportType),
      baseColor: baseColor,
      startPoint: _coordToLatLng(coords.first),
      endPoint: _coordToLatLng(coords.last),
    ));
  }
  return specs;
}

/// Cheap per-selection restyle over [specs] — just choosing a colour and
/// whether to add the selected activity's start/end dots, no GeoJSON parsing
/// or type-bucket resolution. This is what a selection change should cost.
List<Marker> _styleActivityMarkers(
  List<_ActivityMarkerSpec> specs,
  dynamic selectedActivityId,
  bool hasSelection, {
  required List<Marker> dayBreakpointMarkers,
}) {
  final markers = <Marker>[];
  for (final spec in specs) {
    final isSelected = selectedActivityId != null &&
        spec.actId == selectedActivityId.toString();
    final bgColor = isSelected
        ? spec.baseColor
        : hasSelection
            ? spec.baseColor.withAlpha(0x60)
            : spec.baseColor;

    markers.add(Marker(
      key: spec.actId != null ? ValueKey('activity-${spec.actId}') : null,
      point: spec.point,
      width: 22,
      height: 22,
      alignment: Alignment.center,
      child: Container(
        decoration: BoxDecoration(color: bgColor, shape: BoxShape.circle),
        child: Icon(spec.icon, color: Colors.white, size: 13),
      ),
    ));

    // Start (green) / end (red) markers for the selected activity (#19).
    if (isSelected) {
      if (spec.startPoint != null) {
        markers.add(_dotMarker(spec.startPoint!, _kStartMarkerColor, size: 18));
      }
      if (spec.endPoint != null) {
        markers.add(_dotMarker(spec.endPoint!, _kEndMarkerColor, size: 18));
      }
    }
  }
  // Day breakpoints sit beneath the sport icons / selected endpoints (so prepend).
  return [...dayBreakpointMarkers, ...markers];
}

IconData _iconForSegmentType(String? type) {
  switch (type?.toLowerCase()) {
    case 'flight': return Icons.flight;
    case 'train':  return Icons.train;
    case 'bus':    return Icons.directions_bus;
    case 'boat':   return Icons.directions_boat;
    default:       return Icons.route;
  }
}

/// Everything about a segment marker that doesn't depend on selection — see
/// [_ActivityMarkerSpec], which this mirrors.
class _SegmentMarkerSpec {
  final LatLng point;
  final String? segId;
  final IconData icon;
  final Color baseColor;
  const _SegmentMarkerSpec({
    required this.point,
    required this.segId,
    required this.icon,
    required this.baseColor,
  });
}

// Shared by _MapPanelState and ManageMapPanelState (mirrors
// _buildActivityMarkerSpecs above).
List<_SegmentMarkerSpec> _buildSegmentMarkerSpecs(
  Map<String, dynamic> geo,
  Color trackColor, {
  bool colorByType = false,
  Map<String, Map<String, dynamic>> typeStyles = const {},
}) {
  final features = geo['features'];
  if (features is! List) return const [];
  final specs = <_SegmentMarkerSpec>[];
  for (final feature in features) {
    if (feature is! Map) continue;
    final props = feature['properties'] as Map? ?? {};
    if (props['type'] != 'segment') continue;
    final coords = (feature['geometry'] as Map? ?? {})['coordinates'];
    if (coords is! List || coords.isEmpty) continue;

    final point = memoArcMidpoint(coords);
    if (point == null) continue;

    final segType = (props['segment_type'] as String?) ??
        (props['route_mode'] == 'rail' ? 'train' : null);
    final baseColor = colorByType
        ? resolveTypeStyle(segmentTypeBucket(segType), isSegment: true,
            overrides: typeStyles[segmentTypeBucket(segType)]).color
        : trackColor;

    specs.add(_SegmentMarkerSpec(
      point: point,
      segId: props['segment_id']?.toString(),
      icon: _iconForSegmentType(segType),
      baseColor: baseColor,
    ));
  }
  return specs;
}

/// Cheap per-selection restyle over [specs] — see [_styleActivityMarkers].
List<Marker> _styleSegmentMarkers(
  List<_SegmentMarkerSpec> specs,
  dynamic selectedSegmentId,
  bool hasSelection,
) {
  final markers = <Marker>[];
  for (final spec in specs) {
    final isSelected = selectedSegmentId != null &&
        spec.segId == selectedSegmentId.toString();
    final bgColor = isSelected
        ? spec.baseColor
        : hasSelection
            ? spec.baseColor.withAlpha(0x60)
            : spec.baseColor;

    markers.add(Marker(
      key: spec.segId != null ? ValueKey('segment-${spec.segId}') : null,
      point: spec.point,
      width: 22,
      height: 22,
      child: Container(
        decoration: BoxDecoration(color: bgColor, shape: BoxShape.circle),
        child: Icon(spec.icon, color: Colors.white, size: 13),
      ),
    ));
  }
  return markers;
}

/// Everything about a journal marker that doesn't depend on selection: point
/// and the raw journal map (needed by the tap handler). See
/// _styleJournalMarkers.
class _JournalMarkerSpec {
  final LatLng point;
  final String jId;
  final Map<String, dynamic> j;
  const _JournalMarkerSpec({required this.point, required this.jId, required this.j});
}

// Shared by _MapPanelState and ManageMapPanelState — unlike memory markers,
// journal-marker rendering (including the tap handler) has no mode-specific
// affordance, so this stayed byte-for-byte identical between the two classes
// and is hoisted here rather than duplicated.
List<_JournalMarkerSpec> _buildJournalMarkerSpecs(List<Map<String, dynamic>> items) {
  final specs = <_JournalMarkerSpec>[];
  for (final item in items) {
    if (item['item_type'] != 'journal') continue;
    final j = item['journal'] as Map<String, dynamic>?;
    if (j == null) continue;
    final lat = (j['lat'] as num?)?.toDouble();
    final lon = (j['lon'] as num?)?.toDouble();
    if (lat == null || lon == null) continue;
    specs.add(_JournalMarkerSpec(
      point: LatLng(lat, lon),
      jId: j['id']?.toString() ?? '',
      j: j,
    ));
  }
  return specs;
}

/// Cheap per-selection restyle over [specs] — just the bgColor/border pick;
/// no item-list scan or field extraction, which is what a selection change
/// used to redo here on every rebuild (see map_panel.dart's buildDayIndex
/// doc comment for the ANR this pattern fixes generally).
List<Marker> _styleJournalMarkers(
  List<_JournalMarkerSpec> specs,
  dynamic selectedJournalId,
  bool hasSelection,
  ProjectNotifier notifier,
) {
  final markers = <Marker>[];
  for (final spec in specs) {
    final isSelected = selectedJournalId?.toString() == spec.jId;
    const size = 22.0;
    final bgColor = isSelected
        ? const Color(0xFF44AAFF)
        : hasSelection
            ? const Color(0xA064748B)
            : const Color(0xFF64748B);
    markers.add(Marker(
      key: ValueKey('journal-${spec.jId}'),
      point: spec.point,
      width: size,
      height: size,
      child: GestureDetector(
        onTap: () => notifier.selectJournal(spec.j['id']),
        child: Container(
          decoration: BoxDecoration(
            color: bgColor,
            shape: BoxShape.circle,
            border: isSelected ? Border.all(color: Colors.white, width: 2) : null,
          ),
          child: const Center(
            child: Icon(Icons.book_outlined, size: 12, color: Colors.white),
          ),
        ),
      ),
    ));
  }
  return markers;
}

/// Everything about a memory marker that doesn't depend on selection: point,
/// thumbnail URL, the day it belongs to (for the day-highlight check), and
/// the raw memory map (needed by the tap handler). See _styleMemoryMarkers.
class _MemoryMarkerSpec {
  final LatLng point;
  final String memId;
  final String? thumbUrl;
  final String? memDate;
  final Map<String, dynamic> mem;
  const _MemoryMarkerSpec({
    required this.point,
    required this.memId,
    required this.thumbUrl,
    required this.memDate,
    required this.mem,
  });
}

// Shared by _MapPanelState and ManageMapPanelState — the two modes only
// differ in what tapping a marker opens (view mode's memory detail is
// read-only), so that's a callback ([showDetail]) rather than two
// near-duplicate copies of this loop.
List<_MemoryMarkerSpec> _buildMemoryMarkerSpecs(
  List<Map<String, dynamic>> items,
  ProjectNotifier notifier,
) {
  final specs = <_MemoryMarkerSpec>[];
  for (final item in items) {
    if (item['item_type'] != 'memory') continue;
    final mem = item['memory'] as Map<String, dynamic>?;
    if (mem == null) continue;
    final lat = (mem['lat'] as num?)?.toDouble();
    final lon = (mem['lon'] as num?)?.toDouble();
    if (lat == null || lon == null) continue;
    final memId = mem['id']?.toString() ?? '';
    final photos = (mem['photos'] as List?)?.cast<String>() ?? [];
    specs.add(_MemoryMarkerSpec(
      point: LatLng(lat, lon),
      memId: memId,
      thumbUrl: photos.isNotEmpty ? notifier.photoThumbUrl(memId, photos.first) : null,
      memDate: mem['date'] as String?,
      mem: mem,
    ));
  }
  return specs;
}

/// Cheap per-selection restyle over [specs]: only bgColor/size/border/
/// day-highlight change with selection. The thumbnail widget keeps its
/// stable `ValueKey(thumbUrl)`, so Flutter's reconciliation reuses it across
/// a resize instead of re-fetching (see _MarkerThumbImage). [showDetail]
/// lets each mode wire its own memory-detail affordance without duplicating
/// this loop — see map_panel.dart's buildDayIndex doc comment for the ANR
/// this general pattern (full rebuild on every selection change) fixes.
List<Marker> _styleMemoryMarkers(
  List<_MemoryMarkerSpec> specs,
  dynamic selectedMemoryId,
  bool hasSelection,
  Set<String> effectiveDays,
  Map<String, String> authHeaders,
  ProjectNotifier notifier,
  void Function(Map<String, dynamic> mem) showDetail,
) {
  final markers = <Marker>[];
  for (final spec in specs) {
    final isSelected = selectedMemoryId?.toString() == spec.memId;
    final isDayHighlighted = effectiveDays.isEmpty ||
        (spec.memDate != null && effectiveDays.contains(spec.memDate));
    final size = isSelected ? 34.0 : 28.0;
    final bgColor = isSelected
        ? const Color(0xFF333333)
        : (hasSelection && !isDayHighlighted)
            ? const Color(0xA0000000)
            : Colors.black;
    final thumbUrl = spec.thumbUrl;
    final Widget inner = thumbUrl != null
        ? ClipOval(
            child: _MarkerThumbImage(
              key: ValueKey(thumbUrl),
              url: thumbUrl,
              headers: authHeaders,
              size: size,
            ),
          )
        : Icon(Icons.photo_camera, size: size * 0.45, color: Colors.white);
    markers.add(Marker(
      key: ValueKey('memory-${spec.memId}'),
      point: spec.point,
      width: size,
      height: size,
      child: GestureDetector(
        onTap: () {
          notifier.selectMemory(spec.mem['id']);
          showDetail(spec.mem);
        },
        child: Container(
          decoration: BoxDecoration(
            color: bgColor,
            shape: BoxShape.circle,
            border: isSelected ? Border.all(color: Colors.white, width: 2) : null,
          ),
          child: Center(child: inner),
        ),
      ),
    ));
  }
  return markers;
}

/// Owner-only encounter pins (issue #40). Tapping opens the person/group
/// sheet. Shared by [ManageMapPanelState] and the (owner-only, gated)
/// view-mode [_MapPanelState] (issue #71) so the pin-classification logic
/// isn't duplicated between the two maps.
List<Marker> buildEncounterMarkers(
  List<Map<String, dynamic>> items,
  BuildContext context,
  ProjectNotifier notifier, {
  /// Invoked when the opened sheet's encounter-location icon is tapped
  /// (issue #72). The pin's own sheet is closed first, then this fires so the
  /// caller can re-focus/zoom the map to that point.
  void Function(double lat, double lon)? onLocationTap,
}) {
  // Lookups for pin classification: a grouped person's encounter shows the
  // group ("People") icon (the individual is masked); an ungrouped person
  // shows a person icon (issue #50).
  final peopleById = {
    for (final p in notifier.people)
      if (p['id'] is int) p['id'] as int: p,
  };
  final groupsById = {
    for (final g in notifier.groups)
      if (g['id'] is int) g['id'] as int: g,
  };

  final markers = <Marker>[];
  for (final item in items) {
    if (item['item_type'] != 'encounter') continue;
    final e = item['encounter'] as Map<String, dynamic>?;
    if (e == null) continue;
    final lat = (e['lat'] as num?)?.toDouble();
    final lon = (e['lon'] as num?)?.toDouble();
    if (lat == null || lon == null) continue;

    final pin = classifyEncounterPin(
        (e['person_id'] as num?)?.toInt(), (e['group_id'] as num?)?.toInt(),
        peopleById, groupsById);
    final isGroup = pin?.kind == 'group';
    final sheetLocationTap = onLocationTap == null
        ? null
        : (double lat2, double lon2) {
            Navigator.of(context).pop();
            onLocationTap(lat2, lon2);
          };
    final encId = e['id']?.toString();
    markers.add(Marker(
      key: encId != null ? ValueKey('encounter-$encId') : null,
      point: LatLng(lat, lon),
      width: 22,
      height: 22,
      child: GestureDetector(
        onTap: pin == null
            ? null
            : () {
                if (isGroup) {
                  showGroupDetailSheet(context, notifier, pin.entity,
                      onLocationTap: sheetLocationTap);
                } else {
                  showPersonDetailSheet(context, notifier, pin.entity,
                      onLocationTap: sheetLocationTap);
                }
              },
        child: Container(
          decoration: BoxDecoration(
            color: kAccent,
            shape: BoxShape.circle,
            border: Border.all(color: Colors.white, width: 2),
          ),
          child: Center(
            child: Icon(isGroup ? Icons.groups : Icons.person,
                size: 12, color: Colors.white),
          ),
        ),
      ),
    ));
  }
  return markers;
}

// ── Polarsteps trip overlay (issue #40) ──────────────────────────────────────
//
// A person's shared Polarsteps trip, shown on demand from the People directory
// and never persisted. Shared by MapPanel (view mode) and ManageMapPanel — both
// read the same ProjectNotifier overlay state.

const Color _kPolarstepsOverlay = Color(0xFF7C3AED);

/// Map points of the overlay [steps], in step order. Steps without coordinates
/// are already filtered out by `showPersonPolarstepsTrip`.
List<LatLng> polarstepsOverlayPoints(List<Map<String, dynamic>> steps) => [
      for (final s in steps)
        LatLng((s['lat'] as num).toDouble(), (s['lon'] as num).toDouble()),
    ];

/// Route line + step dots for the current overlay; empty when no trip is shown.
List<Widget> polarstepsOverlayLayers(ProjectNotifier notifier) {
  final points = polarstepsOverlayPoints(notifier.polarstepsOverlaySteps);
  if (points.isEmpty) return const [];
  return [
    PolylineLayer(
      polylines: [
        Polyline(
            points: points, color: _kPolarstepsOverlay, strokeWidth: 3),
      ],
    ),
    MarkerLayer(
      markers: [
        for (final p in points)
          Marker(
            point: p,
            width: 12,
            height: 12,
            child: const DecoratedBox(
              decoration: BoxDecoration(
                color: _kPolarstepsOverlay,
                shape: BoxShape.circle,
              ),
            ),
          ),
      ],
    ),
  ];
}

/// Top-of-map banner naming the trip on show, with a button to clear it.
Widget polarstepsOverlayBanner(ProjectNotifier notifier) {
  return Positioned(
    top: 12,
    left: 12,
    right: 12,
    child: Align(
      alignment: Alignment.topCenter,
      child: Material(
        color: _kPolarstepsOverlay,
        borderRadius: BorderRadius.circular(20),
        child: Padding(
          padding: const EdgeInsets.only(left: 14, right: 4),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.travel_explore, size: 16, color: Colors.white),
              const SizedBox(width: 8),
              Flexible(
                child: Text(
                  notifier.polarstepsOverlayLabel ?? 'Polarsteps trip',
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(color: Colors.white),
                ),
              ),
              IconButton(
                icon: const Icon(Icons.close, size: 16, color: Colors.white),
                visualDensity: VisualDensity.compact,
                tooltip: 'Clear overlay',
                onPressed: notifier.clearPolarstepsOverlay,
              ),
            ],
          ),
        ),
      ),
    ),
  );
}

/// Frames [points] on the map. A trip is usually nowhere near the project's own
/// track, so without this the overlay is drawn off-screen and picking a trip
/// looks like it did nothing (#123).
void fitCameraToPolarstepsOverlay(
    AnimatedMapController controller, List<LatLng> points) {
  if (points.isEmpty) return;
  if (points.length == 1) {
    controller.centerOnPoint(points.first, zoom: 12);
    return;
  }
  controller.animatedFitCamera(
    cameraFit: CameraFit.bounds(
      bounds: LatLngBounds.fromPoints(points),
      padding: const EdgeInsets.all(32),
    ),
    curve: Curves.easeInOut,
  );
}

/// Fits the camera once per newly-shown Polarsteps overlay. Driven from the map
/// panels' `build` rather than from the call site that loaded the trip: the
/// People directory is a full-screen route on top of the map, so the map only
/// rebuilds (and can only sensibly animate) while the overlay state changes
/// underneath it.
mixin _PolarstepsOverlayFit<T extends StatefulWidget> on State<T> {
  List<Map<String, dynamic>>? _fittedPolarstepsOverlay;

  void fitPolarstepsOverlayOnce(
      ProjectNotifier notifier, AnimatedMapController controller) {
    final steps = notifier.polarstepsOverlaySteps;
    // The notifier assigns a fresh list per overlay, so identity is the change
    // signal — re-showing the same trip after clearing it fits again.
    if (identical(steps, _fittedPolarstepsOverlay)) return;
    _fittedPolarstepsOverlay = steps;
    final points = polarstepsOverlayPoints(steps);
    if (points.isEmpty) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      fitCameraToPolarstepsOverlay(controller, points);
    });
  }
}

// ── Selection stats overlay (issue #74) ──────────────────────────────────────
//
// Shows the distance/climb/day-number of the current activity or day(s)
// selection on top of the map. Shared by MapPanel (view mode) and
// ManageMapPanel (manage mode) — both read the same ProjectNotifier selection
// state (selectedActivityId / selectedDays, mutually exclusive by construction).

/// Distance/climb + a day label for the current map selection, or null when
/// nothing is selected (the overlay is hidden in that case).
@visibleForTesting
class SelectionStatsData {
  final double distanceKm;
  final double elevationM;
  /// "Day N" for a single activity/day, "Days N–M" for a contiguous multi-day
  /// selection, or "N days selected" for a non-contiguous one.
  final String dayLabel;

  const SelectionStatsData({
    required this.distanceKm,
    required this.elevationM,
    required this.dayLabel,
  });
}

/// Computes the overlay content for the notifier's current selection. Mirrors
/// [ProjectNotifier.dayStats]'s field names/units/rounding conventions for the
/// single-activity case (distance in metres → km, elevation already in metres).
@visibleForTesting
SelectionStatsData? computeSelectionStats(ProjectNotifier notifier) {
  final selActId = notifier.selectedActivityId;
  // Multi-select takes priority over single-day selection, same as the
  // polyline-highlighting "effectiveDays" logic below.
  final selDays = notifier.selectedDays.isNotEmpty
      ? notifier.selectedDays
      : (notifier.selectedDay != null ? {notifier.selectedDay!} : <String>{});
  if (selActId == null && selDays.isEmpty) return null;

  final orderedDays = notifier.orderedDayKeys();

  if (selDays.isEmpty) {
    // Single activity selected.
    Map<String, dynamic>? activity;
    for (final a in notifier.activities) {
      if (a['id']?.toString() == selActId.toString()) {
        activity = a;
        break;
      }
    }
    if (activity == null) return null;
    final distanceKm = (activity['distance'] as num? ?? 0).toDouble() / 1000.0;
    final elevationM = (activity['total_elevation_gain'] as num? ?? 0).toDouble();
    final dateKey = (activity['start_date_local'] as String?)?.split('T').first;
    final dayLabel = dateKey == null
        ? ''
        : 'Day ${dayTripNumbering(dateKey, orderedDays, notifier.tripStart).dayNumber}';
    return SelectionStatsData(
        distanceKm: distanceKm, elevationM: elevationM, dayLabel: dayLabel);
  }

  if (selDays.length == 1) {
    final dateKey = selDays.first;
    final stats = notifier.dayStats(dateKey);
    final n = dayTripNumbering(dateKey, orderedDays, notifier.tripStart);
    return SelectionStatsData(
        distanceKm: stats.distanceKm,
        elevationM: stats.elevationM,
        dayLabel: 'Day ${n.dayNumber}');
  }

  // Multiple days selected: sum distance/climb across every selected date.
  double distanceKm = 0, elevationM = 0;
  for (final d in selDays) {
    final s = notifier.dayStats(d);
    distanceKm += s.distanceKm;
    elevationM += s.elevationM;
  }
  final sortedDays = selDays.toList()..sort();
  final indices = sortedDays.map((d) => orderedDays.indexOf(d)).toList()..sort();
  final contiguous = !indices.contains(-1) &&
      indices.last - indices.first + 1 == indices.length;
  final String dayLabel;
  if (contiguous) {
    final first = dayTripNumbering(sortedDays.first, orderedDays, notifier.tripStart).dayNumber;
    final last = dayTripNumbering(sortedDays.last, orderedDays, notifier.tripStart).dayNumber;
    dayLabel = 'Days $first–$last';
  } else {
    dayLabel = '${selDays.length} days selected';
  }
  return SelectionStatsData(distanceKm: distanceKm, elevationM: elevationM, dayLabel: dayLabel);
}

/// Small mono-numeral "LABEL / value+unit" cell, matching the day-meta
/// editor's stat styling (see `_EDStat` in day_meta_editor.dart).
class _StatCell extends StatelessWidget {
  final String label;
  final String value;
  final String unit;

  const _StatCell({required this.label, required this.value, required this.unit});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(label,
            style: monoStyle(
              fontSize: 9.5, fontWeight: FontWeight.w600,
              color: cs.onSurfaceVariant, letterSpacing: 1.2,
            )),
        Row(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.baseline,
          textBaseline: TextBaseline.alphabetic,
          children: [
            Text(value,
                style: monoStyle(
                  fontSize: 15, fontWeight: FontWeight.w600, color: cs.onSurface,
                )),
            const SizedBox(width: 1),
            Text(unit, style: monoStyle(fontSize: 9.5, color: cs.onSurfaceVariant)),
          ],
        ),
      ],
    );
  }
}

/// Positioned badge (top-right corner) showing distance/climb/day for the
/// current activity or day(s) selection. Hidden entirely when nothing is
/// selected — see [computeSelectionStats].
class SelectionStatsOverlay extends StatelessWidget {
  final ProjectNotifier notifier;

  const SelectionStatsOverlay({super.key, required this.notifier});

  @override
  Widget build(BuildContext context) {
    final stats = computeSelectionStats(notifier);
    if (stats == null) return const SizedBox.shrink();
    final cs = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
      decoration: BoxDecoration(
        color: cs.surface.withValues(alpha: 0.94),
        borderRadius: BorderRadius.circular(12),
        boxShadow: kShadow2(Theme.of(context).brightness),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (stats.dayLabel.isNotEmpty) ...[
            Icon(Icons.today, size: 14, color: cs.onSurfaceVariant),
            const SizedBox(width: 6),
            Text(stats.dayLabel,
                style: monoStyle(
                  fontSize: 12.5, fontWeight: FontWeight.w600, color: cs.onSurface,
                )),
            const SizedBox(width: 12),
            Container(width: 1, height: 22, color: cs.outlineVariant),
            const SizedBox(width: 12),
          ],
          _StatCell(
              label: 'DIST', value: stats.distanceKm.round().toString(), unit: 'km'),
          const SizedBox(width: 14),
          _StatCell(
              label: 'CLIMB', value: stats.elevationM.round().toString(), unit: 'm'),
        ],
      ),
    );
  }
}

// Shared by _MapPanelState and ManageMapPanelState: a keyed MarkerLayer for
// [markers], built only when [visible] and non-empty, else omitted entirely.
// The key is what stops the marker-storm ANR (see the fix this refactor
// follows) — every one of the five marker layers in both classes needs it, so
// it's centralised here rather than left as five copy-pasted `if` blocks per
// class that could silently drift back to unkeyed.
// Wrapped in a PerfSubtree so the five layers' combined per-frame layout and
// paint is a named number (issue #276). flutter_map repositions every marker
// on every camera frame, 586 of them on the measured trip, and nothing in this
// app could see that cost — `buildDuration` lumps it in with build, and every
// blocking() span wraps a build callback. The wrapper is a RenderProxyBox:
// same constraints, same size, same paint offset. All five share one span
// name, so the report reads "what do markers cost this frame", not five lines.
List<Widget> _keyedMarkerLayer(String key, bool visible, List<Marker> markers) =>
    visible && markers.isNotEmpty
        ? [
            PerfSubtree(
              key: ValueKey('perf-$key'),
              name: 'map_markers',
              child: MarkerLayer(key: ValueKey(key), markers: markers),
            )
          ]
        : const [];

// ── MapPanel ──────────────────────────────────────────────────────────────────

class MapPanel extends StatefulWidget {
  final ProjectNotifier notifier;
  final AnimatedMapController mapController;
  final String basemapUrl;
  final List<String> basemapSubdomains;
  final String? labelsUrl;
  /// Mapbox vector style URI (e.g. `mapbox://styles/mapbox/satellite-streets-v12`).
  /// When set and non-null, a VectorTileLayer replaces the raster TileLayer.
  final String? basemapStyleUri;

  /// When set, a raster TileLayer is added for the track base layer and
  /// the PolylineLayer is filtered to the selected item only (for highlights).
  /// Template variables `{z}`, `{x}`, `{y}` are filled in by flutter_map.
  final String? trackTileUrlTemplate;

  /// When true, changing the selection zooms the map to the selected
  /// activity/segment instead of leaving the viewport as-is (view-mode
  /// "auto-zoom to selection", issue #34).
  final bool autoZoom;

  /// Initial camera position (carried over from the other mode when
  /// switching view/edit, so the viewport doesn't reset to fit-all-bounds).
  /// When set, the initial full-track auto-fit is skipped.
  final double? initialLat;
  final double? initialLng;
  final double? initialZoom;

  /// Owner-only encounter pins + toggle (issue #71). Defaults to `false` so
  /// the public/shared map (which reuses this same widget) never renders
  /// encounters — they are strictly owner-only PII (see docs/ENCOUNTERS.md).
  /// Only the owner-authenticated view-mode screen should ever pass `true`.
  final bool showEncounters;

  /// A single point to highlight with [focusedLocationMarker] (issue #72),
  /// set by the parent screen when the user taps an encounter's place icon.
  final LatLng? focusedLatLng;

  /// Invoked when an encounter pin's sheet's place icon is tapped (issue #72)
  /// — the parent screen re-focuses/zooms the map to that point.
  final void Function(double lat, double lon)? onLocationTap;

  /// Invoked on any other map tap/selection, so the parent can clear
  /// [focusedLatLng] (issue #72) — no timer, cleared on the next interaction.
  final VoidCallback? onClearFocusedLocation;

  /// Owner-only locate-me button (issue #88). Defaults to `false` so the
  /// public/shared map (which reuses this same widget) never prompts
  /// anonymous visitors for their location — mirrors [showEncounters].
  /// Only the owner-authenticated view-mode screen should ever pass `true`.
  final bool showLocateMe;

  /// The device-location pin to render via [youAreHereMarker] (issue #88),
  /// set by the parent screen after a successful locate-me fetch.
  final LatLng? hereLatLng;

  /// True while the parent screen is fetching the device's location, so the
  /// locate-me button can show a busy state.
  final bool locatingHere;

  /// Invoked when the locate-me button is tapped — the parent screen owns
  /// the actual device-location fetch and camera pan, mirroring how
  /// [onLocationTap] is owned by the parent rather than this widget.
  final VoidCallback? onLocateMe;

  const MapPanel({
    super.key,
    required this.notifier,
    required this.mapController,
    required this.basemapUrl,
    this.basemapSubdomains = const [],
    this.labelsUrl,
    this.basemapStyleUri,
    this.trackTileUrlTemplate,
    this.autoZoom = false,
    this.initialLat,
    this.initialLng,
    this.initialZoom,
    this.showEncounters = false,
    this.focusedLatLng,
    this.onLocationTap,
    this.onClearFocusedLocation,
    this.showLocateMe = false,
    this.hereLatLng,
    this.locatingHere = false,
    this.onLocateMe,
  });

  @override
  State<MapPanel> createState() => _MapPanelState();
}

/// Result of [hitTestMapTap]: either an activity/segment hit (select it), or
/// — when nothing was within the tap threshold — a point on the full track
/// to park the elevation cursor at, or neither (empty tap, empty track).
typedef MapTapHitTest = ({
  dynamic hitActivityId,
  String? hitSegmentId,
  GeoPoint? cursorPoint,
  double? cursorDist,
});

/// Pure activity/segment + elevation-cursor hit-test for a map tap — every
/// tap, not just a marker tap, used to run this synchronously on the UI
/// isolate: a single pass over every point of every activity/segment
/// feature in [geo], and — if nothing was within [thresholdSq] (squared
/// degrees) — a second full pass over [track] to place the elevation
/// cursor. Neither scan has a spatial index or a viewport prefilter, so
/// cost is O(every coordinate in the trip) on every tap, including taps
/// that hit nothing (open water, a gap between tracks): the most common
/// post-load interaction, paying the same "big computation on the UI
/// isolate" cost the day-carousel ANR fix addressed elsewhere in this file
/// (see buildDayIndex's doc comment) — just per tap instead of per
/// selection. Pure and top-level so it can run via [compute] on a
/// background isolate for a large trip; see totalMapTapPoints and the
/// _kInlineHitTestThreshold split in _MapPanelState/ManageMapPanelState's
/// _onMapTap for when that's actually worth it.
MapTapHitTest hitTestMapTap(({
  Map<String, dynamic>? geo,
  double tapLat,
  double tapLon,
  double thresholdSq,
  List<(double, GeoPoint)> track,
}) args) {
  final geo = args.geo;
  if (geo != null) {
    final features = geo['features'];
    if (features is List) {
      double minHit = args.thresholdSq;
      dynamic hitActivityId;
      String? hitSegmentId;
      for (final f in features) {
        if (f is! Map) continue;
        final props = f['properties'] as Map? ?? {};
        final type = props['type'] as String?;
        if (type != 'activity' && type != 'segment') continue;
        final coords = (f['geometry'] as Map? ?? {})['coordinates'];
        if (coords is! List) continue;
        for (final c in coords) {
          if (c is! List || c.length < 2) continue;
          final dLat = (c[1] as num).toDouble() - args.tapLat;
          final dLon = (c[0] as num).toDouble() - args.tapLon;
          final d = dLat * dLat + dLon * dLon;
          if (d < minHit) {
            minHit = d;
            if (type == 'activity') {
              hitActivityId = props['activity_id'];
              hitSegmentId = null;
            } else {
              hitSegmentId = props['segment_id']?.toString();
              hitActivityId = null;
            }
          }
        }
      }
      if (hitActivityId != null || hitSegmentId != null) {
        return (
          hitActivityId: hitActivityId,
          hitSegmentId: hitSegmentId,
          cursorPoint: null,
          cursorDist: null,
        );
      }
    }
  }

  final track = args.track;
  if (track.isEmpty) {
    return (hitActivityId: null, hitSegmentId: null, cursorPoint: null, cursorDist: null);
  }
  int nearest = 0;
  double minDist = double.infinity;
  for (int i = 0; i < track.length; i++) {
    final dLat = track[i].$2.lat - args.tapLat;
    final dLon = track[i].$2.lon - args.tapLon;
    final d = dLat * dLat + dLon * dLon;
    if (d < minDist) { minDist = d; nearest = i; }
  }
  return (
    hitActivityId: null,
    hitSegmentId: null,
    cursorPoint: track[nearest].$2,
    cursorDist: track[nearest].$1,
  );
}

/// Cheap O(features) precheck for whether [hitTestMapTap]'s cost is worth
/// moving off the UI isolate — reads `coordinates.length` per feature plus
/// `track.length`, no per-point math.
int totalMapTapPoints(Map<String, dynamic>? geo, List<(double, GeoPoint)> track) {
  var total = track.length;
  final features = geo?['features'];
  if (features is List) {
    for (final f in features) {
      if (f is! Map) continue;
      final coords = (f['geometry'] as Map? ?? {})['coordinates'];
      if (coords is List) total += coords.length;
    }
  }
  return total;
}

/// Above this many combined geo + track points, _onMapTap moves the hit
/// test to a background isolate instead of running it inline. Below it, the
/// isolate hop isn't worth the tap-to-selection latency it adds — most
/// trips never get close to this, so the overwhelming majority of taps stay
/// perfectly synchronous/instant.
const _kInlineHitTestThreshold = 20000;

class _MapPanelState extends State<MapPanel> with _PolarstepsOverlayFit {
  // Seeded true when an initial camera position was carried over from the
  // other mode (view/edit toggle) — skips the fit-all-bounds animation.
  // Seeded in initState, NOT lazily: `_fitBoundsOnce` isn't called until the
  // project finishes loading, and the camera→URL sync can add lat/lng to the
  // route in the meantime. A `late` initialiser would then read that
  // self-written param as "the user arrived with an explicit viewport" and
  // silently skip the fit. The answer is about arrival, so capture it once.
  late bool _fittedBounds;
  // Points to auto-zoom to after the next full-track fit (issue #34). Set when
  // the selection changes and autoZoom is on; consumed once in build().
  List<LatLng>? _pendingAutoZoomPts;
  // Polyline + bounds cache — only rebuilt when geo, selection, or style changes.
  Map<String, dynamic>? _lastGeo;
  dynamic _lastSelectedId = _sentinel;
  dynamic _lastSelectedSegId = _sentinel;
  String? _lastSelectedDay = '';   // '' = sentinel (distinct from null)
  Set<String> _lastSelectedDays = const {};
  dynamic _lastSelectedMemId = _sentinel;
  dynamic _lastSelectedJournalId = _sentinel;
  List<Map<String, dynamic>>? _lastItems;
  Color? _lastTrackColor;
  double? _lastTrackWidth;
  bool? _lastAlternating;
  bool? _lastShowJournals;
  bool? _lastColorByType;
  Map<String, Map<String, dynamic>>? _lastTypeStyles;
  // Selection-independent geometry/style specs — rebuilt only when geo,
  // items, or track style changes (never by a day/activity/segment/memory
  // selection alone). See buildDayIndex's doc comment for why this split
  // exists: two day-carousel taps in a row used to redo this parsing work
  // twice back to back, blocking the UI isolate long enough to ANR.
  List<_PolylineSpec> _polylineSpecs = const [];
  List<_ActivityMarkerSpec> _activityMarkerSpecs = const [];
  List<_SegmentMarkerSpec> _segmentMarkerSpecs = const [];
  List<_MemoryMarkerSpec> _memoryMarkerSpecs = const [];
  List<_JournalMarkerSpec> _journalMarkerSpecs = const [];
  List<Marker> _cachedDayBreakpointMarkers = const [];
  Map<String, ({Set<String> actIds, Set<String> segIds})> _dayIndex = const {};
  List<Polyline> _cachedPolylines = [];
  List<Marker> _cachedActivityMarkers = [];
  List<Marker> _cachedSegmentMarkers = [];
  List<Marker> _cachedMemoryMarkers = [];
  List<Marker> _cachedJournalMarkers = [];
  List<Marker> _cachedEncounterMarkers = [];
  // Encounter markers have no selection-dependent styling at all (unlike
  // every other marker type above), so they get their own narrower cache
  // check — see the encounter-cache block in build() — instead of riding
  // along with geoOrStyleChanged/selectionChanged, which used to rebuild
  // them on every selection change for no reason.
  List<Map<String, dynamic>>? _lastEncounterItems;
  List<Map<String, dynamic>>? _lastEncounterPeople;
  List<Map<String, dynamic>>? _lastEncounterGroups;
  bool? _lastEncounterShowEncounters;
  bool _showMemories = true;
  bool _showEncounters = true;
  bool _showActivities = true;
  NetworkTileProvider? _tileProvider;
  Style? _vectorStyle;

  static const _sentinel = Object(); // distinct from null

  @override
  void initState() {
    super.initState();
    _fittedBounds = widget.initialLat != null;
    if (widget.basemapStyleUri != null) {
      () async {
        try {
          final s = await StyleReader(
                  uri: widget.basemapStyleUri!,
                  apiKey: kMapboxToken)
              .read();
          if (!mounted) return;
          setState(() => _vectorStyle = s);
        } catch (e) {
          debugPrint('[MapPanel] StyleReader error: $e');
        }
      }();
    } else {
      _tileProvider = NetworkTileProvider();
    }
  }

  static Color _alternateColor(Color base) {
    final hsl = HSLColor.fromColor(base);
    return hsl
        .withSaturation((hsl.saturation * 0.42).clamp(0.0, 1.0))
        .withLightness((hsl.lightness * 1.18).clamp(0.0, 1.0))
        .toColor();
  }

  List<LatLng> _allPoints(List<Polyline> polylines) {
    return polylines.expand((p) => p.points).toList();
  }

  void _fitBoundsOnce(List<LatLng> points) {
    if (_fittedBounds || points.isEmpty) return;
    _fittedBounds = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      double minLat = points.first.latitude;
      double maxLat = points.first.latitude;
      double minLon = points.first.longitude;
      double maxLon = points.first.longitude;
      for (final p in points) {
        if (p.latitude < minLat) minLat = p.latitude;
        if (p.latitude > maxLat) maxLat = p.latitude;
        if (p.longitude < minLon) minLon = p.longitude;
        if (p.longitude > maxLon) maxLon = p.longitude;
      }
      widget.mapController.animatedFitCamera(
        cameraFit: CameraFit.bounds(
          bounds: LatLngBounds(
            LatLng(minLat, minLon),
            LatLng(maxLat, maxLon),
          ),
          // View mode lays the chart out as a sibling below the map (a Column),
          // so the map viewport already excludes it — no chart-clearance inset
          // here, unlike ManageMapPanel where the chart overlays the map.
          padding: const EdgeInsets.all(32),
        ),
        curve: Curves.easeInOut,
      );
    });
  }

  @override
  void didUpdateWidget(MapPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Reset fit flag when a different notifier instance is passed (new project).
    if (oldWidget.notifier != widget.notifier) {
      _fittedBounds = false;
    }
  }

  // Bumped on every _onMapTap call; guards a stale async hit-test result
  // (from a superseded tap — e.g. two rapid taps) from overwriting a newer
  // one, or from landing after the widget is gone.
  int _hitTestGen = 0;

  // Guards a stale/duplicate async polyline decimation the same way — see
  // _maybeDecimatePolylines.
  int _polylineDecimateGen = 0;
  List<_PolylineSpec>? _decimateSourceSpecs;

  /// When [specs]' combined point count exceeds [kMaxTotalPolylinePoints],
  /// asynchronously decimates them (off the UI isolate, since a large trip's
  /// combined track can be substantial — see kMaxTotalPolylinePoints' doc)
  /// and swaps _polylineSpecs to the reduced result once it lands. A no-op
  /// below the threshold, and a no-op if already decimating/decimated this
  /// exact spec list (identity-guarded, mirroring every other cache in this
  /// file) — geo/style genuinely changing produces a new list and retriggers.
  void _maybeDecimatePolylines(List<_PolylineSpec> specs) {
    var total = 0;
    for (final s in specs) { total += s.points.length; }
    if (total <= kMaxTotalPolylinePoints) return;
    if (identical(specs, _decimateSourceSpecs)) return;
    _decimateSourceSpecs = specs;

    // Fast path: the decode hop already decimated this geometry on a worker
    // (issue #299). Taking it costs one lookup per feature instead of
    // marshalling every point into records and paying compute() to copy them
    // across — measured on a real device at 788 ms of marshalling plus ~1.6 s
    // of argument copy, a 2.4 s stall that landed mid-pan and tripped
    // Android's ANR watchdog. Assigning synchronously is safe: build() reads
    // _polylineSpecs only after this returns.
    final seeded = <List<LatLng>>[];
    for (final s in specs) {
      final coords = s.rawCoords;
      final d = coords == null ? null : decimatedLatLng(coords);
      if (d == null) break;
      seeded.add(d);
    }
    if (seeded.length == specs.length) {
      _polylineSpecs = [
        for (var i = 0; i < specs.length; i++) specs[i].withPoints(seeded[i]),
      ];
      return;
    }

    // Fallback for geometry the decode hop never saw: client-built E2EE geo,
    // and locally patched segments merged in after a load.
    final gen = ++_polylineDecimateGen;
    final lines = perfSpans.blocking('decimate_marshal', () => [
      for (final s in specs)
        [for (final p in s.points) (p.latitude, p.longitude)],
    ]);
    compute(decimatePolylinePoints,
            (lines: lines, budget: kMaxTotalPolylinePoints))
        .then((decimated) {
      if (!mounted || gen != _polylineDecimateGen) return;
      setState(() {
        _polylineSpecs = [
          for (var i = 0; i < specs.length; i++)
            specs[i].withPoints(
                [for (final (lat, lon) in decimated[i]) LatLng(lat, lon)]),
        ];
      });
    });
  }

  void _onMapTap(LatLng latlng) {
    widget.onClearFocusedLocation?.call();
    final geo = widget.notifier.geo;
    final zoom = widget.mapController.mapController.camera.zoom;
    final pixelDeg = 360.0 / (pow(2.0, zoom) * 256.0);
    final args = (
      geo: geo,
      tapLat: latlng.latitude,
      tapLon: latlng.longitude,
      thresholdSq: pow(15.0 * pixelDeg, 2).toDouble(),
      track: widget.notifier.fullTrack,
    );
    // See hitTestMapTap's doc comment: most taps are cheap enough to just
    // handle inline (instant selection/cursor feedback); only a trip large
    // enough to make the scan itself slow needs the isolate hop.
    if (totalMapTapPoints(geo, args.track) <= _kInlineHitTestThreshold) {
      _applyMapTapHit(hitTestMapTap(args));
      return;
    }
    final gen = ++_hitTestGen;
    compute(hitTestMapTap, args).then((result) {
      if (!mounted || gen != _hitTestGen) return;
      _applyMapTapHit(result);
    });
  }

  void _applyMapTapHit(MapTapHitTest hit) {
    if (hit.hitActivityId != null) {
      widget.notifier.selectActivity(hit.hitActivityId);
      return;
    }
    if (hit.hitSegmentId != null) {
      widget.notifier.selectSegment(hit.hitSegmentId!);
      return;
    }
    final cursorPoint = hit.cursorPoint;
    if (cursorPoint != null) {
      widget.notifier.elevationCursorNotifier.value = cursorPoint;
      widget.notifier.mapCursorDistNotifier.value = hit.cursorDist;
    }
  }

  @override
  Widget build(BuildContext context) {
    final notifier = widget.notifier;

    // Recompute polylines only when geo, selection, or track style changes.
    final geo = notifier.geo;
    final selActId = notifier.selectedActivityId;
    final selSegId = notifier.selectedSegmentId;
    final selDay = notifier.selectedDay;
    final selDays = notifier.selectedDays;
    final selMemId = notifier.selectedMemoryId;
    final selJournalId = notifier.selectedJournalId;
    final showJournals = notifier.showJournals;
    final items = notifier.items;
    final trackColor = notifier.trackColor;
    final trackSecondaryColor = notifier.trackSecondaryColor;
    final trackWidth = notifier.trackWidth;
    final alternating = notifier.alternatingTrackColors;
    final selectionChanged = selActId != _lastSelectedId ||
        selSegId?.toString() != _lastSelectedSegId?.toString() ||
        selDay != _lastSelectedDay ||
        !ManageMapPanelState.setEquals(selDays, _lastSelectedDays) ||
        selMemId?.toString() != _lastSelectedMemId?.toString() ||
        selJournalId?.toString() != _lastSelectedJournalId?.toString();
    final styleChanged = trackColor != _lastTrackColor ||
        trackWidth != _lastTrackWidth || alternating != _lastAlternating ||
        notifier.colorByType != _lastColorByType ||
        !identical(notifier.typeStyles, _lastTypeStyles);
    // Geometry (points, base colours, icons — everything a day/activity/
    // segment/memory selection doesn't change) only needs rebuilding when
    // geo, items, or track style actually change. A selection change alone
    // now only re-styles the specs already cached below — see buildDayIndex's
    // doc comment for why this split exists.
    final geoOrStyleChanged = !identical(geo, _lastGeo) || styleChanged ||
        !identical(items, _lastItems) || showJournals != _lastShowJournals;
    if (geoOrStyleChanged || selectionChanged) {
      _lastGeo = geo;
      _lastSelectedId = selActId;
      _lastSelectedSegId = selSegId;
      _lastSelectedDay = selDay;
      _lastSelectedDays = Set.from(selDays);
      _lastSelectedMemId = selMemId;
      _lastSelectedJournalId = selJournalId;
      _lastItems = items;
      _lastTrackColor = trackColor;
      _lastTrackWidth = trackWidth;
      _lastAlternating = alternating;
      _lastShowJournals = showJournals;
      _lastColorByType = notifier.colorByType;
      _lastTypeStyles = notifier.typeStyles;
      final tilesActive = widget.trackTileUrlTemplate != null;
      // Multi-select takes priority over single-day selection, mirroring
      // ManageMapPanel's day-highlighting (issue #199 view-mode carousel).
      final effectiveDays = selDays.isNotEmpty
          ? selDays
          : (selDay != null ? {selDay} : <String>{});
      if (geoOrStyleChanged) {
        perfSpans.blocking('build_specs', () {
        final actById = <dynamic, Map<String, dynamic>>{
          for (final a in notifier.activities) a['id']: a
        };
        _dayIndex = buildDayIndex(items, actById);
        _polylineSpecs = geo != null
            ? _buildPolylineSpecs(geo, items, trackColor,
                trackSecondaryColor: trackSecondaryColor, alternating: alternating,
                colorByType: notifier.colorByType, typeStyles: notifier.typeStyles)
            : const [];
        _maybeDecimatePolylines(_polylineSpecs);
        _activityMarkerSpecs = geo != null
            ? _buildActivityMarkerSpecs(geo, trackColor,
                colorByType: notifier.colorByType, typeStyles: notifier.typeStyles)
            : const [];
        _segmentMarkerSpecs = geo != null
            ? _buildSegmentMarkerSpecs(geo, trackColor,
                colorByType: notifier.colorByType, typeStyles: notifier.typeStyles)
            : const [];
        _cachedDayBreakpointMarkers = geo != null
            ? buildDayBreakpointMarkers(
                geo, dayStartActivityIds(items, actById), trackColor)
            : const [];
        _memoryMarkerSpecs = _buildMemoryMarkerSpecs(items, notifier);
        _journalMarkerSpecs = _buildJournalMarkerSpecs(items);
        });
      }
      Set<String>? dayActIds;
      Set<String>? daySegIds;
      if (effectiveDays.isNotEmpty) {
        dayActIds = {};
        daySegIds = {};
        for (final dk in effectiveDays) {
          final r = _dayIndex[dk];
          if (r == null) continue;
          dayActIds.addAll(r.actIds);
          daySegIds.addAll(r.segIds);
        }
      }
      perfSpans.blocking('style_markers', () {
      _cachedPolylines = _stylePolylines(_polylineSpecs, selActId, selSegId, trackWidth,
          selectedOnly: tilesActive, dayActIds: dayActIds, daySegIds: daySegIds);
      final hasSelection = selActId != null || selSegId != null ||
          effectiveDays.isNotEmpty || selMemId != null || selJournalId != null;
      _cachedActivityMarkers = _styleActivityMarkers(
          _activityMarkerSpecs, selActId, hasSelection,
          dayBreakpointMarkers: _cachedDayBreakpointMarkers);
      _cachedSegmentMarkers =
          _styleSegmentMarkers(_segmentMarkerSpecs, selSegId, hasSelection);
      _cachedMemoryMarkers = _styleMemoryMarkers(
          _memoryMarkerSpecs, selMemId, hasSelection, effectiveDays,
          notifier.photoAuthHeaders, notifier,
          (mem) => showMemoryDetail(context, notifier, mem,
              readOnly: true, shareContentKey: notifier.shareContentKey));
      _cachedJournalMarkers =
          _styleJournalMarkers(_journalMarkerSpecs, selJournalId, hasSelection, notifier);
      });
      // Auto-zoom to selection (issue #34). Previously this always did
      // `_fittedBounds = false` on any selection change, which re-fit the map to
      // the WHOLE trip every time — so view mode "reset to full trip zoom"
      // instead of zooming to the picked item. Now: only when auto-zoom is on
      // and something is selected do we queue a zoom to that item; with
      // auto-zoom off, selection leaves the viewport untouched.
      if (selectionChanged && widget.autoZoom && geo != null &&
          (effectiveDays.isNotEmpty || selActId != null || selSegId != null)) {
        _pendingAutoZoomPts = ManageMapPanelState.extractSelectedPoints(
            geo, selActId, selSegId, dayActIds, daySegIds);
      } else if (selectionChanged) {
        _pendingAutoZoomPts = null;
      }
    }
    // Encounter markers have no selection-dependent styling (unlike every
    // other marker type above) — checked independently of
    // geoOrStyleChanged/selectionChanged so a day/activity/segment/memory
    // selection never redoes this classification pass for nothing.
    if (!identical(items, _lastEncounterItems) ||
        !identical(notifier.people, _lastEncounterPeople) ||
        !identical(notifier.groups, _lastEncounterGroups) ||
        widget.showEncounters != _lastEncounterShowEncounters) {
      _lastEncounterItems = items;
      _lastEncounterPeople = notifier.people;
      _lastEncounterGroups = notifier.groups;
      _lastEncounterShowEncounters = widget.showEncounters;
      _cachedEncounterMarkers = widget.showEncounters
          ? buildEncounterMarkers(items, context, notifier,
              onLocationTap: widget.onLocationTap)
          : const [];
    }
    final polylines = _cachedPolylines;

    // What the renderer is being handed. flutter_map re-walks every point of
    // every polyline overlapping the viewport, and repositions every marker,
    // on every camera frame — so these two counts, not any load timing, are
    // what a pan actually costs (issue #276).
    if (perfSpans.enabled) {
      var pts = 0;
      for (final p in polylines) {
        pts += p.points.length;
      }
      perfSpans.note('rendered_points', '$pts');
      perfSpans.note(
          'markers',
          '${_cachedActivityMarkers.length + _cachedSegmentMarkers.length + _cachedMemoryMarkers.length + _cachedJournalMarkers.length + _cachedEncounterMarkers.length}');
      // Decoded bitmaps live in NATIVE memory, outside the Dart heap and
      // outside every span in this app — which is why an ANR with no
      // measurable UI-isolate work stayed invisible for so long (#276).
      final imgCache = PaintingBinding.instance.imageCache;
      perfSpans.note('image_cache',
          '${(imgCache.currentSizeBytes / (1024 * 1024)).toStringAsFixed(1)} MB'
          ' in ${imgCache.currentSize} images (${imgCache.liveImageCount} live)');
      perfSpans.note('thumb_bytes',
          '${(_MarkerThumbImage._cache.bytes / (1024 * 1024)).toStringAsFixed(1)} MB');
    }

    // Guard before flattening every polyline's points: this list is consumed
    // only by the one-shot _fitBoundsOnce, so once the map has been fitted
    // building it is pure waste. It used to be rebuilt on every day/activity
    // tap — measured at ~24 ms per selection on a 500k-point trip, over the
    // frame budget on its own (issue #294). Mirrors the identical guard
    // ManageMapPanel already had. Uses the full geo, not _cachedPolylines,
    // which may be decimated for rendering (see _maybeDecimatePolylines).
    if (!notifier.isLoading && !_fittedBounds) {
      perfSpans.blocking('all_points', () {
        _fitBoundsOnce(geo != null
            ? _allPointsFromGeo(geo)
            : _allPoints(polylines));
      });
    }

    // Auto-zoom to the selected item (issue #34), scheduled after the
    // full-track fit so it wins over it.
    final pendingPts = _pendingAutoZoomPts;
    if (pendingPts != null && pendingPts.isNotEmpty) {
      _pendingAutoZoomPts = null;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        double minLat = pendingPts.first.latitude, maxLat = pendingPts.first.latitude;
        double minLon = pendingPts.first.longitude, maxLon = pendingPts.first.longitude;
        for (final p in pendingPts) {
          if (p.latitude < minLat) minLat = p.latitude;
          if (p.latitude > maxLat) maxLat = p.latitude;
          if (p.longitude < minLon) minLon = p.longitude;
          if (p.longitude > maxLon) maxLon = p.longitude;
        }
        widget.mapController.animatedFitCamera(
          cameraFit: CameraFit.bounds(
            bounds: LatLngBounds(
                LatLng(minLat, minLon), LatLng(maxLat, maxLon)),
            padding: const EdgeInsets.all(48),
          ),
          curve: Curves.easeInOut,
        );
      });
    }

    // Last, so picking a person's trip wins over the fits scheduled above.
    if (widget.showEncounters) {
      fitPolarstepsOverlayOnce(notifier, widget.mapController);
    }
    final showPolarstepsBanner =
        widget.showEncounters && notifier.polarstepsOverlaySteps.isNotEmpty;

    // FlutterMap stays mounted throughout loading so the MapController stays
    // attached and tiles don't get torn down on every load.  A spinner is
    // overlaid on top while data is in flight.
    return Stack(
      children: [
        // The whole map subtree, timed (issue #276). `map_lines` and
        // `map_markers` below are nested inside this, so `map` is the total
        // and the difference is the basemap plus flutter_map's own per-frame
        // work — which is the part no instrument in this app could see.
        PerfSubtree(
          name: 'map',
          child: FlutterMap(
          mapController: widget.mapController.mapController,
          options: MapOptions(
            initialCenter: widget.initialLat != null
                ? LatLng(widget.initialLat!, widget.initialLng!)
                : const LatLng(0, 0),
            initialZoom: widget.initialZoom ?? 2,
            maxZoom: kMaxMapZoom,
            interactionOptions: const InteractionOptions(
              flags: InteractiveFlag.all & ~InteractiveFlag.rotate,
            ),
            onTap: (_, latlng) => _onMapTap(latlng),
          ),
          children: [
            if (_vectorStyle != null)
              ValueListenableBuilder<VectorTileLayerMode>(
                valueListenable: mapTileModeNotifier,
                builder: (_, tileMode, __) => VectorTileLayer(
                  // Keyed by mode: VectorTileLayer caches rasterised tiles
                  // internally, so it has to be rebuilt from scratch rather
                  // than updated in place when the mode changes.
                  key: ValueKey(tileMode),
                  tileProviders: _vectorStyle!.providers,
                  theme: _vectorStyle!.theme,
                  sprites: _vectorStyle!.sprites,
                  tileOffset: TileOffset.mapbox,
                  layerMode: tileMode,
                  maximumZoom: kMaxMapZoom,
                ),
              )
            else if (_tileProvider != null) ...[
              TileLayer(
                urlTemplate: widget.basemapUrl,
                subdomains: widget.basemapSubdomains,
                userAgentPackageName: kAppPackageId,
                tileProvider: _tileProvider!,
                maxNativeZoom: 22,
                retinaMode: RetinaMode.isHighDensity(context),
              ),
              // ESRI fallback path only — the Mapbox path's labels are baked
              // directly into its own satellite style now (issue #75), so
              // widget.labelsUrl is null there and this never renders.
              if (widget.labelsUrl != null)
                TileLayer(
                  urlTemplate: widget.labelsUrl!,
                  subdomains: kActiveViewLabelsSubdomains,
                  userAgentPackageName: kAppPackageId,
                  tileProvider: _tileProvider!,
                  maxNativeZoom: 22,
                  // Bundles country/state/city labels in one raster image —
                  // hide it below city scale to avoid region-name clutter.
                  minZoom: 10,
                ),
            ],
            if (widget.trackTileUrlTemplate != null)
              TileLayer(
                urlTemplate: widget.trackTileUrlTemplate!,
                userAgentPackageName: kAppPackageId,
                maxNativeZoom: 15,
              ),
            if (polylines.isNotEmpty)
              // Timed for the same reason as the marker layers: flutter_map
              // re-walks every point of every polyline overlapping the
              // viewport on every camera frame, and `rendered_points` in the
              // report below is meaningless without the milliseconds it buys.
              PerfSubtree(
                name: 'map_lines',
                child: PolylineLayer(
                  polylines: polylines,
                  // Reduce GPU path vertices at low zoom — detail preserved when zoomed in.
                  simplificationTolerance: 0.5,
                ),
              ),
            ..._keyedMarkerLayer(
                'activities-layer', _showActivities, _cachedActivityMarkers),
            ..._keyedMarkerLayer('segment-layer', true, _cachedSegmentMarkers),
            ..._keyedMarkerLayer(
                'memories-layer', _showMemories, _cachedMemoryMarkers),
            ..._keyedMarkerLayer('journal-layer', notifier.showJournals,
                _cachedJournalMarkers),
            ..._keyedMarkerLayer(
                'encounters-layer',
                widget.showEncounters && _showEncounters,
                _cachedEncounterMarkers),
            if (widget.focusedLatLng != null)
              MarkerLayer(markers: [focusedLocationMarker(widget.focusedLatLng!)]),
            if (widget.hereLatLng != null)
              MarkerLayer(markers: [youAreHereMarker(widget.hereLatLng!)]),
            // A person's Polarsteps trip (#40), shown from the People
            // directory. Gated on [showEncounters]: same owner-only PII, and
            // the same single screen is the only one that can populate it.
            if (widget.showEncounters) ...polarstepsOverlayLayers(notifier),
            // Preview arc uses ValueListenableBuilder so only this layer rebuilds
            // when the segment dialog updates coordinates — not the whole map.
            ValueListenableBuilder<List<GeoPoint>?>(
              valueListenable: notifier.previewArcNotifier,
              builder: (_, arc, __) {
                if (arc == null) return const SizedBox.shrink();
                return PolylineLayer(
                  polylines: [
                    Polyline(
                      points: arc.map(_ll).toList(),
                      color: const Color(0xCC6366F1),
                      strokeWidth: 2.5,
                    ),
                  ],
                );
              },
            ),
            // Elevation cursor — driven by chart hover/tap and by map taps.
            ValueListenableBuilder<GeoPoint?>(
              valueListenable: notifier.elevationCursorNotifier,
              builder: (_, cursor, __) {
                if (cursor == null) return const SizedBox.shrink();
                return MarkerLayer(
                  markers: [
                    Marker(
                      point: _ll(cursor),
                      width: 16,
                      height: 16,
                      child: Container(
                        decoration: BoxDecoration(
                          color: Colors.black,
                          shape: BoxShape.circle,
                          border: Border.all(color: Colors.white, width: 2),
                        ),
                      ),
                    ),
                  ],
                );
              },
            ),
          ],
        )),
        if (notifier.isLoading)
          const Center(child: CircularProgressIndicator()),
        if (showPolarstepsBanner) polarstepsOverlayBanner(notifier),
        if (_cachedActivityMarkers.isNotEmpty ||
            _cachedMemoryMarkers.isNotEmpty ||
            (widget.showEncounters && _cachedEncounterMarkers.isNotEmpty))
          Positioned(
            // Drop below the full-width Polarsteps trip banner so the two
            // never overlap on a narrow screen.
            top: showPolarstepsBanner ? 64 : 12,
            left: 12,
            // Stacked in a column, not a row (issue #215) — three toggles side
            // by side ran out of horizontal room on narrow/mobile screens.
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (_cachedActivityMarkers.isNotEmpty)
                  _MapToggleRow(
                    label: 'Activities',
                    value: _showActivities,
                    onChanged: (v) => setState(() => _showActivities = v),
                  ),
                if (_cachedMemoryMarkers.isNotEmpty)
                  _MapToggleRow(
                    label: 'Memories',
                    value: _showMemories,
                    onChanged: (v) => setState(() => _showMemories = v),
                  ),
                if (widget.showEncounters && _cachedEncounterMarkers.isNotEmpty)
                  _MapToggleRow(
                    label: 'Encounters',
                    value: _showEncounters,
                    onChanged: (v) => setState(() => _showEncounters = v),
                  ),
              ],
            ),
          ),
        Positioned(
          top: showPolarstepsBanner ? 64 : 12,
          right: 12,
          child: SelectionStatsOverlay(notifier: notifier),
        ),
        if (widget.showLocateMe && widget.onLocateMe != null)
          Positioned(
            bottom: 16,
            right: 12,
            child: LocateMeButton(
              locating: widget.locatingHere,
              onPressed: widget.onLocateMe!,
            ),
          ),
      ],
    );
  }
}

/// One label+switch row for the map's layer-visibility toggles (issue #215).
class _MapToggleRow extends StatelessWidget {
  final String label;
  final bool value;
  final ValueChanged<bool> onChanged;

  const _MapToggleRow({
    required this.label,
    required this.value,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          label,
          style: Theme.of(context)
              .textTheme
              .bodySmall
              ?.copyWith(color: Colors.white),
        ),
        Transform.scale(
          scale: 0.7, // Adjust this value to set the size
          child: Switch(
            value: value,
            onChanged: onChanged,
            materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
          ),
        ),
      ],
    );
  }
}

/// Everything about a polyline that doesn't depend on selection: points,
/// base (pre-highlight) colour, and line style. See [_buildPolylineSpecs] /
/// [_stylePolylines] — the split that fixed the day-carousel ANR (see
/// [buildDayIndex]'s doc comment).
class _PolylineSpec {
  final List<LatLng> points;
  /// The raw GeoJSON coordinates this spec was built from — carried purely as
  /// the identity key for the geometry caches in map_geometry_memo.dart, so
  /// _maybeDecimatePolylines can find the decimation the decode hop already
  /// produced. Never read as data.
  final List? rawCoords;
  final bool isSegment;
  final String? featureId;
  final Color baseColor;
  final LineStyleKind lineStyle;
  const _PolylineSpec({
    required this.points,
    required this.isSegment,
    required this.featureId,
    required this.baseColor,
    required this.lineStyle,
    this.rawCoords,
  });

  _PolylineSpec withPoints(List<LatLng> newPoints) => _PolylineSpec(
        points: newPoints,
        rawCoords: rawCoords,
        isSegment: isSegment,
        featureId: featureId,
        baseColor: baseColor,
        lineStyle: lineStyle,
      );
}

/// Every point across every feature in [geo], used for bounds-fitting —
/// deliberately independent of [_stylePolylines]'s output so a decimated
/// render (see [decimatePolylinePoints]) never throws off the initial
/// fit-to-bounds animation.
List<LatLng> _allPointsFromGeo(Map<String, dynamic> geo) {
  final features = geo['features'];
  if (features is! List) return [];
  final pts = <LatLng>[];
  for (final f in features) {
    if (f is! Map) continue;
    final coords = (f['geometry'] as Map? ?? {})['coordinates'];
    if (coords is! List) continue;
    pts.addAll(memoCoordsToLatLng(coords));
  }
  return pts;
}

// half of each polyline — everything except which one is highlighted, which
// depends only on selection and is applied separately by [_stylePolylines].
// Independent of selection; callers cache this and only rerun it when
// geo/items/style change.
List<_PolylineSpec> _buildPolylineSpecs(
  Map<String, dynamic> geo,
  List<Map<String, dynamic>> items,
  Color trackColor, {
  Color? trackSecondaryColor,
  required bool alternating,
  bool colorByType = false,
  Map<String, Map<String, dynamic>> typeStyles = const {},
}) {
  final features = geo['features'];
  if (features is! List) return const [];

  // Build activity-index map for alternating colours.
  final actIdx = <String, int>{};
  int ai = 0;
  for (final item in items) {
    if (item['item_type'] == 'activity') {
      final id = item['activity_id']?.toString();
      if (id != null) actIdx[id] = ai++;
    }
  }
  final altColor = trackSecondaryColor ?? _MapPanelState._alternateColor(trackColor);

  final specs = <_PolylineSpec>[];
  for (final feature in features) {
    if (feature is! Map) continue;
    final props = feature['properties'] as Map? ?? {};
    final geometry = feature['geometry'] as Map? ?? {};
    final coords = geometry['coordinates'];
    if (coords is! List) continue;

    final points = memoCoordsToLatLng(coords);
    // A LineString needs ≥2 points; a single-point polyline can throw deep in
    // flutter_map's paint path, so skip it (and empty ones) defensively.
    if (points.length < 2) continue;

    final isSegment = props['type'] == 'segment';
    final featureId = isSegment
        ? props['segment_id']?.toString()
        : props['activity_id']?.toString();

    // Alternating is a stand-in for per-type colour, so it's superseded once
    // a project opts into colorByType (issue #95).
    final isOdd = !colorByType && alternating && !isSegment && featureId != null &&
        (actIdx[featureId] ?? 0).isOdd;

    final bucket = isSegment
        ? segmentTypeBucket(props['segment_type'] as String?)
        : activityTypeBucket(props['sport_type'] as String?);
    final Color baseColor;
    final LineStyleKind lineStyle;
    if (colorByType) {
      final resolved = resolveTypeStyle(bucket,
          isSegment: isSegment, overrides: typeStyles[bucket]);
      baseColor = resolved.color;
      lineStyle = resolved.style;
    } else {
      baseColor = isOdd ? altColor : trackColor;
      lineStyle = isSegment ? LineStyleKind.dashed : LineStyleKind.solid;
    }
    // Degraded/failed override type/track colouring but not line style — the
    // tile list carries the same signal via _segmentStatusLine (issue #207).
    final effectiveBaseColor = (isSegment && props['route_degraded'] == true)
        ? _kDegradedRouteColor
        : (isSegment && props['route_status'] == 'failed')
            ? _kFailedRouteColor
            : baseColor;

    specs.add(_PolylineSpec(
      points: points,
      rawCoords: coords,
      isSegment: isSegment,
      featureId: featureId,
      baseColor: effectiveBaseColor,
      lineStyle: lineStyle,
    ));
  }
  return specs;
}

/// Cheap per-selection restyle over [specs] — highlighting is either a
/// single selected activity/segment, or (dayActIds/daySegIds set) the union
/// of items across one or more selected days. No GeoJSON parsing or
/// type-bucket resolution here — just picking a colour/width per spec, which
/// is what a selection change should cost.
List<Polyline> _stylePolylines(
  List<_PolylineSpec> specs,
  dynamic selectedActivityId,
  dynamic selectedSegmentId,
  double trackWidth, {
  bool selectedOnly = false,
  Set<String>? dayActIds,
  Set<String>? daySegIds,
}) {
  final usingDaySelection = dayActIds != null || daySegIds != null;
  final hasSelection = selectedActivityId != null ||
      selectedSegmentId != null ||
      usingDaySelection;
  final polylines = <Polyline>[];
  for (final spec in specs) {
    final bool isHighlighted;
    if (usingDaySelection) {
      isHighlighted = spec.isSegment
          ? (daySegIds?.contains(spec.featureId) ?? false)
          : (dayActIds?.contains(spec.featureId) ?? false);
    } else if (spec.isSegment) {
      isHighlighted = selectedSegmentId != null &&
          spec.featureId == selectedSegmentId.toString();
    } else {
      isHighlighted = selectedActivityId != null &&
          spec.featureId == selectedActivityId.toString();
    }

    // When tile layer handles the base rendering, only draw the selected item.
    if (selectedOnly && !isHighlighted) continue;

    final Color color;
    final double strokeWidth;
    if (isHighlighted) {
      color = spec.baseColor;
      strokeWidth = (trackWidth * 1.9).clamp(4.0, 8.0);
    } else if (hasSelection) {
      color = spec.baseColor.withAlpha(0x60);
      strokeWidth = trackWidth;
    } else {
      color = spec.baseColor;
      strokeWidth = trackWidth;
    }
    polylines.add(Polyline(
      points: spec.points,
      color: color,
      strokeWidth: strokeWidth,
      pattern: switch (spec.lineStyle) {
        LineStyleKind.dashed => StrokePattern.dashed(segments: const [12, 8]),
        LineStyleKind.dotted => const StrokePattern.dotted(),
        LineStyleKind.solid  => const StrokePattern.solid(),
      },
    ));
  }
  return polylines;
}

// ── ManageMapPanel — bare TileLayer, no controller, no polylines ─────────────

class ManageMapPanel extends StatefulWidget {
  final ProjectNotifier notifier;
  final AnimatedMapController mapController;
  final bool autoZoom;
  final String basemapUrl;
  final List<String> basemapSubdomains;
  final ValueNotifier<bool> fittedNotifier;
  /// Mapbox vector style URI. When set, a VectorTileLayer replaces the raster TileLayer.
  final String? basemapStyleUri;

  /// Initial camera position (carried over from the other mode when
  /// switching view/edit). The caller is expected to seed [fittedNotifier]
  /// to `true` when these are set, so the fit-all-bounds animation is skipped.
  final double? initialLat;
  final double? initialLng;
  final double? initialZoom;

  /// A single point to highlight with [focusedLocationMarker] (issue #72),
  /// set by the parent screen when the user taps an encounter's place icon.
  final LatLng? focusedLatLng;

  /// Invoked when an encounter pin's sheet's place icon is tapped (issue #72)
  /// — the parent screen re-focuses/zooms the map to that point.
  final void Function(double lat, double lon)? onLocationTap;

  /// Invoked on any other map tap/selection, so the parent can clear
  /// [focusedLatLng] (issue #72) — no timer, cleared on the next interaction.
  final VoidCallback? onClearFocusedLocation;

  /// The device-location pin to render via [youAreHereMarker] (issue #88),
  /// set by the parent screen after a successful locate-me fetch.
  final LatLng? hereLatLng;

  /// True while the parent screen is fetching the device's location, so the
  /// locate-me button can show a busy state.
  final bool locatingHere;

  /// Invoked when the locate-me button is tapped — the parent screen owns
  /// the actual device-location fetch and camera pan. Edit mode is always
  /// owner-only, so unlike [MapPanel.showLocateMe] there's no separate gate:
  /// the button renders whenever this is non-null.
  final VoidCallback? onLocateMe;

  const ManageMapPanel({
    super.key,
    required this.notifier,
    required this.mapController,
    required this.basemapUrl,
    required this.fittedNotifier,
    this.autoZoom = false,
    this.basemapSubdomains = const [],
    this.basemapStyleUri,
    this.initialLat,
    this.initialLng,
    this.initialZoom,
    this.focusedLatLng,
    this.onLocationTap,
    this.onClearFocusedLocation,
    this.hereLatLng,
    this.locatingHere = false,
    this.onLocateMe,
  });

  @override
  State<ManageMapPanel> createState() => ManageMapPanelState();
}

class ManageMapPanelState extends State<ManageMapPanel>
    with _PolarstepsOverlayFit {
  NetworkTileProvider? _tileProvider;
  Style? _vectorStyle;

  // Polyline + marker cache — only rebuilt when geo or selection changes.
  Map<String, dynamic>? _lastGeo;
  dynamic _lastSelectedId = _sentinel;
  dynamic _lastSelectedSegId = _sentinel;
  String? _lastSelectedDay = '';   // '' = sentinel (distinct from null)
  Set<String> _lastSelectedDays = const {};
  dynamic _lastSelectedMemId = _sentinel;
  dynamic _lastSelectedJournalId = _sentinel;
  List<Map<String, dynamic>>? _lastItems;
  // Selection-independent geometry/style specs — see the matching fields on
  // _MapPanelState and buildDayIndex's doc comment for why this is split
  // from the final, selection-styled Polyline/Marker lists below.
  List<_PolylineSpec> _polylineSpecs = const [];
  List<_ActivityMarkerSpec> _activityMarkerSpecs = const [];
  List<_SegmentMarkerSpec> _segmentMarkerSpecs = const [];
  List<_MemoryMarkerSpec> _memoryMarkerSpecs = const [];
  List<_JournalMarkerSpec> _journalMarkerSpecs = const [];
  List<Marker> _cachedDayBreakpointMarkers = const [];
  Map<String, ({Set<String> actIds, Set<String> segIds})> _dayIndex = const {};
  List<Polyline> _cachedPolylines = [];
  List<Marker> _cachedActivityMarkers = [];
  List<Marker> _cachedSegmentMarkers = [];
  List<Marker> _cachedMemoryMarkers = [];
  List<Marker> _cachedJournalMarkers = [];
  List<Marker> _cachedEncounterMarkers = [];
  // Encounter markers have no selection-dependent styling at all (unlike
  // every other marker type above), so they get their own narrower cache
  // check — see the encounter-cache block in build() — instead of riding
  // along with geoOrStyleChanged2/selectionChanged2.
  List<Map<String, dynamic>>? _lastEncounterItems;
  List<Map<String, dynamic>>? _lastEncounterPeople;
  List<Map<String, dynamic>>? _lastEncounterGroups;
  bool _showMemories = true;
  // Points queued for auto-zoom on the next frame; null = nothing pending.
  List<LatLng>? _pendingAutoZoomPts;
  // Track-style cache fields.
  Color? _lastTrackColor;
  double? _lastTrackWidth;
  bool? _lastAlternating;
  bool? _lastShowJournals;
  bool? _lastColorByType;
  Map<String, Map<String, dynamic>>? _lastTypeStyles;

  static const _sentinel = Object();

  static bool setEquals(Set<String> a, Set<String> b) =>
      a.length == b.length && a.containsAll(b);

  @override
  void initState() {
    super.initState();
    if (widget.basemapStyleUri != null) {
      () async {
        try {
          final s = await StyleReader(
                  uri: widget.basemapStyleUri!,
                  apiKey: kMapboxToken)
              .read();
          if (!mounted) return;
          setState(() => _vectorStyle = s);
        } catch (e) {
          debugPrint('[ManageMapPanel] StyleReader error: $e');
        }
      }();
    } else {
      _tileProvider = NetworkTileProvider();
    }
    // Initialise "last" selection state from the current notifier values so that
    // a spurious selectionChanged2=true (which resets the fit flag) is never
    // triggered when this state is (re)created while a fit has already happened.
    _lastSelectedId = widget.notifier.selectedActivityId;
    _lastSelectedSegId = widget.notifier.selectedSegmentId;
    _lastSelectedDay = widget.notifier.selectedDay;
    _lastSelectedDays = Set.from(widget.notifier.selectedDays);
    _lastSelectedMemId = widget.notifier.selectedMemoryId;
  }

  @override
  void dispose() {
    super.dispose();
  }

  void _fitBoundsOnce(List<LatLng> points) {
    if (widget.fittedNotifier.value || points.isEmpty) return;
    widget.fittedNotifier.value = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      double minLat = points.first.latitude, maxLat = points.first.latitude;
      double minLon = points.first.longitude, maxLon = points.first.longitude;
      for (final p in points) {
        if (p.latitude < minLat) minLat = p.latitude;
        if (p.latitude > maxLat) maxLat = p.latitude;
        if (p.longitude < minLon) minLon = p.longitude;
        if (p.longitude > maxLon) maxLon = p.longitude;
      }
      widget.mapController.animatedFitCamera(
        cameraFit: CameraFit.bounds(
          bounds: LatLngBounds(LatLng(minLat, minLon), LatLng(maxLat, maxLon)),
          padding: const EdgeInsets.fromLTRB(32, 32, 32, 32 + 160),
        ),
        curve: Curves.easeInOut,
      );
    });
  }

  // Bumped on every _onMapTap call; guards a stale async hit-test result
  // (from a superseded tap — e.g. two rapid taps) from overwriting a newer
  // one, or from landing after the widget is gone.
  int _hitTestGen = 0;

  // Guards a stale/duplicate async polyline decimation the same way — see
  // _maybeDecimatePolylines.
  int _polylineDecimateGen = 0;
  List<_PolylineSpec>? _decimateSourceSpecs;

  /// When [specs]' combined point count exceeds [kMaxTotalPolylinePoints],
  /// asynchronously decimates them (off the UI isolate, since a large trip's
  /// combined track can be substantial — see kMaxTotalPolylinePoints' doc)
  /// and swaps _polylineSpecs to the reduced result once it lands. A no-op
  /// below the threshold, and a no-op if already decimating/decimated this
  /// exact spec list (identity-guarded, mirroring every other cache in this
  /// file) — geo/style genuinely changing produces a new list and retriggers.
  void _maybeDecimatePolylines(List<_PolylineSpec> specs) {
    var total = 0;
    for (final s in specs) { total += s.points.length; }
    if (total <= kMaxTotalPolylinePoints) return;
    if (identical(specs, _decimateSourceSpecs)) return;
    _decimateSourceSpecs = specs;

    // Fast path: the decode hop already decimated this geometry on a worker
    // (issue #299). Taking it costs one lookup per feature instead of
    // marshalling every point into records and paying compute() to copy them
    // across — measured on a real device at 788 ms of marshalling plus ~1.6 s
    // of argument copy, a 2.4 s stall that landed mid-pan and tripped
    // Android's ANR watchdog. Assigning synchronously is safe: build() reads
    // _polylineSpecs only after this returns.
    final seeded = <List<LatLng>>[];
    for (final s in specs) {
      final coords = s.rawCoords;
      final d = coords == null ? null : decimatedLatLng(coords);
      if (d == null) break;
      seeded.add(d);
    }
    if (seeded.length == specs.length) {
      _polylineSpecs = [
        for (var i = 0; i < specs.length; i++) specs[i].withPoints(seeded[i]),
      ];
      return;
    }

    // Fallback for geometry the decode hop never saw: client-built E2EE geo,
    // and locally patched segments merged in after a load.
    final gen = ++_polylineDecimateGen;
    final lines = perfSpans.blocking('decimate_marshal', () => [
      for (final s in specs)
        [for (final p in s.points) (p.latitude, p.longitude)],
    ]);
    compute(decimatePolylinePoints,
            (lines: lines, budget: kMaxTotalPolylinePoints))
        .then((decimated) {
      if (!mounted || gen != _polylineDecimateGen) return;
      setState(() {
        _polylineSpecs = [
          for (var i = 0; i < specs.length; i++)
            specs[i].withPoints(
                [for (final (lat, lon) in decimated[i]) LatLng(lat, lon)]),
        ];
      });
    });
  }

  void _onMapTap(LatLng latlng) {
    widget.onClearFocusedLocation?.call();
    final geo = widget.notifier.geo;
    final zoom = widget.mapController.mapController.camera.zoom;
    final pixelDeg = 360.0 / (pow(2.0, zoom) * 256.0);
    final args = (
      geo: geo,
      tapLat: latlng.latitude,
      tapLon: latlng.longitude,
      thresholdSq: pow(15.0 * pixelDeg, 2).toDouble(),
      track: widget.notifier.fullTrack,
    );
    // See hitTestMapTap's doc comment: most taps are cheap enough to just
    // handle inline (instant selection/cursor feedback); only a trip large
    // enough to make the scan itself slow needs the isolate hop.
    if (totalMapTapPoints(geo, args.track) <= _kInlineHitTestThreshold) {
      _applyMapTapHit(hitTestMapTap(args));
      return;
    }
    final gen = ++_hitTestGen;
    compute(hitTestMapTap, args).then((result) {
      if (!mounted || gen != _hitTestGen) return;
      _applyMapTapHit(result);
    });
  }

  void _applyMapTapHit(MapTapHitTest hit) {
    if (hit.hitActivityId != null) {
      widget.notifier.selectActivity(hit.hitActivityId);
      return;
    }
    if (hit.hitSegmentId != null) {
      widget.notifier.selectSegment(hit.hitSegmentId!);
      return;
    }
    final cursorPoint = hit.cursorPoint;
    if (cursorPoint != null) {
      widget.notifier.elevationCursorNotifier.value = cursorPoint;
      widget.notifier.mapCursorDistNotifier.value = hit.cursorDist;
    }
  }

  /// Points of the currently-selected activity/segment (or all activities/
  /// segments of the selected day(s)), used to compute the auto-zoom target.
  /// Public + static so the view-mode [MapPanel] and tests can reuse it.
  static List<LatLng> extractSelectedPoints(
    Map<String, dynamic> geo,
    dynamic selActId,
    dynamic selSegId,
    Set<String>? dayActIds,
    Set<String>? daySegIds,
  ) {
    final points = <LatLng>[];
    final features = geo['features'];
    if (features is! List) return points;
    for (final feature in features) {
      if (feature is! Map) continue;
      final props = feature['properties'] as Map? ?? {};
      final isSegment = props['type'] == 'segment';
      final featureId = isSegment
          ? props['segment_id']?.toString()
          : props['activity_id']?.toString();
      final bool match;
      if (dayActIds != null || daySegIds != null) {
        match = isSegment
            ? (daySegIds?.contains(featureId) ?? false)
            : (dayActIds?.contains(featureId) ?? false);
      } else if (isSegment) {
        match = selSegId != null && featureId == selSegId.toString();
      } else {
        match = selActId != null && featureId == selActId.toString();
      }
      if (!match) continue;
      final coords = (feature['geometry'] as Map? ?? {})['coordinates'];
      if (coords is! List) continue;
      points.addAll(memoCoordsToLatLng(coords));
    }
    return points;
  }

  @override
  Widget build(BuildContext context) {
    // Dev-only build timer (PERF_TIMING) — pins whether map rebuilds are the
    // source of the build-thread storms, and what triggers them.
    final perfSw = kPerfTiming ? (Stopwatch()..start()) : null;
    var perfRebuiltLayers = false;
    final notifier = widget.notifier;
    final geo = notifier.geo;
    final selActId = notifier.selectedActivityId;
    final selSegId = notifier.selectedSegmentId;
    final selDay = notifier.selectedDay;
    final selDays = notifier.selectedDays;
    final selMemId = notifier.selectedMemoryId;
    final selJournalId2 = notifier.selectedJournalId;
    final showJournals2 = notifier.showJournals;
    final items = notifier.items;
    final trackColor = notifier.trackColor;
    final trackSecondaryColor2 = notifier.trackSecondaryColor;
    final trackWidth = notifier.trackWidth;
    final alternating = notifier.alternatingTrackColors;
    final selectionChanged2 = selActId != _lastSelectedId ||
        selSegId?.toString() != _lastSelectedSegId?.toString() ||
        selDay != _lastSelectedDay ||
        !setEquals(selDays, _lastSelectedDays) ||
        selMemId?.toString() != (_lastSelectedMemId as dynamic)?.toString() ||
        selJournalId2?.toString() != _lastSelectedJournalId?.toString();
    final styleChanged2 = trackColor != _lastTrackColor ||
        trackWidth != _lastTrackWidth || alternating != _lastAlternating ||
        notifier.colorByType != _lastColorByType ||
        !identical(notifier.typeStyles, _lastTypeStyles);
    final perfGeoChg = !identical(geo, _lastGeo);
    final perfItemsChg = !identical(items, _lastItems);
    final perfJournalsChg = showJournals2 != _lastShowJournals;
    // Geometry (points, base colours, icons) only needs rebuilding when geo,
    // items, or track style actually change — a selection change alone just
    // re-styles the specs already cached below. See buildDayIndex's doc
    // comment for why this split exists.
    final geoOrStyleChanged2 =
        perfGeoChg || perfItemsChg || styleChanged2 || perfJournalsChg;
    if (geoOrStyleChanged2 || selectionChanged2) {
      perfRebuiltLayers = true;
      if (selectionChanged2 && widget.autoZoom) widget.fittedNotifier.value = false;
      _lastGeo = geo;
      _lastSelectedId = selActId;
      _lastSelectedSegId = selSegId;
      _lastSelectedDay = selDay;
      _lastSelectedDays = Set.from(selDays);
      _lastSelectedMemId = selMemId;
      _lastSelectedJournalId = selJournalId2;
      _lastItems = items;
      _lastTrackColor = trackColor;
      _lastTrackWidth = trackWidth;
      _lastAlternating = alternating;
      _lastShowJournals = showJournals2;
      _lastColorByType = notifier.colorByType;
      _lastTypeStyles = notifier.typeStyles;
      // Multi-select takes priority over single-day selection.
      final effectiveDays = selDays.isNotEmpty
          ? selDays
          : (selDay != null ? {selDay} : <String>{});
      if (geoOrStyleChanged2) {
        // Build activityById for the day index below.
        final actById = <dynamic, Map<String, dynamic>>{
          for (final a in notifier.activities) a['id']: a
        };
        _dayIndex = buildDayIndex(items, actById);
        _polylineSpecs = geo != null
            ? _buildPolylineSpecs(geo, items, trackColor,
                trackSecondaryColor: trackSecondaryColor2, alternating: alternating,
                colorByType: notifier.colorByType, typeStyles: notifier.typeStyles)
            : const [];
        _maybeDecimatePolylines(_polylineSpecs);
        _activityMarkerSpecs = geo != null
            ? _buildActivityMarkerSpecs(geo, trackColor,
                colorByType: notifier.colorByType, typeStyles: notifier.typeStyles)
            : const [];
        _segmentMarkerSpecs = geo != null
            ? _buildSegmentMarkerSpecs(geo, trackColor,
                colorByType: notifier.colorByType, typeStyles: notifier.typeStyles)
            : const [];
        _cachedDayBreakpointMarkers = geo != null
            ? buildDayBreakpointMarkers(
                geo, dayStartActivityIds(items, actById), trackColor)
            : const [];
        _memoryMarkerSpecs = _buildMemoryMarkerSpecs(items, notifier);
        _journalMarkerSpecs = _buildJournalMarkerSpecs(items);
      }
      // For day selection, union ids across all selected days.
      Set<String>? dayActIds;
      Set<String>? daySegIds;
      if (effectiveDays.isNotEmpty) {
        dayActIds = {};
        daySegIds = {};
        for (final dk in effectiveDays) {
          final r = _dayIndex[dk];
          if (r == null) continue;
          dayActIds.addAll(r.actIds);
          daySegIds.addAll(r.segIds);
        }
      }
      _cachedPolylines = _stylePolylines(_polylineSpecs, selActId, selSegId, trackWidth,
          dayActIds: dayActIds, daySegIds: daySegIds);
      final hasSelection = selActId != null || selSegId != null ||
          effectiveDays.isNotEmpty || selMemId != null || selJournalId2 != null;
      _cachedActivityMarkers = _styleActivityMarkers(
          _activityMarkerSpecs, selActId, hasSelection,
          dayBreakpointMarkers: _cachedDayBreakpointMarkers);
      _cachedSegmentMarkers =
          _styleSegmentMarkers(_segmentMarkerSpecs, selSegId, hasSelection);
      _cachedMemoryMarkers = _styleMemoryMarkers(
          _memoryMarkerSpecs, selMemId, hasSelection, effectiveDays,
          notifier.photoAuthHeaders, notifier,
          (mem) => showMemoryDetail(context, notifier, mem));
      _cachedJournalMarkers = _styleJournalMarkers(
          _journalMarkerSpecs, selJournalId2, hasSelection, notifier);

      // Queue auto-zoom only when selection genuinely changed (not on geo updates
      // from progressive loading) so it doesn't fight _fitBoundsOnce mid-load.
      if (selectionChanged2 && widget.autoZoom && geo != null &&
          (effectiveDays.isNotEmpty || selActId != null || selSegId != null)) {
        _pendingAutoZoomPts = extractSelectedPoints(
            geo, selActId, selSegId, dayActIds, daySegIds);
      } else if (selectionChanged2) {
        _pendingAutoZoomPts = null;
      }
    }
    // Encounter markers have no selection-dependent styling (unlike every
    // other marker type above) — checked independently of
    // geoOrStyleChanged2/selectionChanged2 so a day/activity/segment/memory
    // selection never redoes this classification pass for nothing.
    if (!identical(items, _lastEncounterItems) ||
        !identical(notifier.people, _lastEncounterPeople) ||
        !identical(notifier.groups, _lastEncounterGroups)) {
      _lastEncounterItems = items;
      _lastEncounterPeople = notifier.people;
      _lastEncounterGroups = notifier.groups;
      _cachedEncounterMarkers = buildEncounterMarkers(items, context,
          widget.notifier, onLocationTap: widget.onLocationTap);
    }

    // Guard on fittedNotifier before flattening every polyline's points: once
    // the map has been fitted, _fitBoundsOnce early-returns, so building this
    // (potentially huge) point list on every build was pure waste/GC churn.
    // Uses the full geo (not _cachedPolylines, which may be decimated for
    // rendering — see _maybeDecimatePolylines) so the fit stays accurate.
    if (!notifier.isLoading && !widget.fittedNotifier.value) {
      _fitBoundsOnce(geo != null
          ? _allPointsFromGeo(geo)
          : _cachedPolylines.expand((p) => p.points).toList());
    }

    // Schedule auto-zoom AFTER _fitBoundsOnce so its postFrameCallback runs
    // last and takes priority over the whole-track fit.
    final pendingPts = _pendingAutoZoomPts;
    if (pendingPts != null && pendingPts.isNotEmpty) {
      _pendingAutoZoomPts = null;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        double minLat = pendingPts.first.latitude, maxLat = pendingPts.first.latitude;
        double minLon = pendingPts.first.longitude, maxLon = pendingPts.first.longitude;
        for (final p in pendingPts) {
          if (p.latitude < minLat) minLat = p.latitude;
          if (p.latitude > maxLat) maxLat = p.latitude;
          if (p.longitude < minLon) minLon = p.longitude;
          if (p.longitude > maxLon) maxLon = p.longitude;
        }
        widget.mapController.animatedFitCamera(
          cameraFit: CameraFit.bounds(
            bounds: LatLngBounds(
                LatLng(minLat, minLon), LatLng(maxLat, maxLon)),
            padding: const EdgeInsets.fromLTRB(48, 48, 48, 48 + 160),
          ),
          curve: Curves.easeInOut,
        );
      });
    }

    // Last, so picking a person's trip wins over the fits scheduled above.
    fitPolarstepsOverlayOnce(notifier, widget.mapController);

    final perfBuilt = Stack(
      children: [
        // The whole map subtree, timed (issue #276). `map_lines` and
        // `map_markers` below are nested inside this, so `map` is the total
        // and the difference is the basemap plus flutter_map's own per-frame
        // work — which is the part no instrument in this app could see.
        PerfSubtree(
          name: 'map',
          child: FlutterMap(
          mapController: widget.mapController.mapController,
          options: MapOptions(
            initialCenter: widget.initialLat != null
                ? LatLng(widget.initialLat!, widget.initialLng!)
                : const LatLng(48.0, 10.0),
            initialZoom: widget.initialZoom ?? 4,
            maxZoom: kMaxMapZoom,
            interactionOptions: const InteractionOptions(
              flags: InteractiveFlag.all & ~InteractiveFlag.rotate,
            ),
            onTap: (_, latlng) => _onMapTap(latlng),
          ),
          children: [
            if (_vectorStyle != null)
              ValueListenableBuilder<VectorTileLayerMode>(
                valueListenable: mapTileModeNotifier,
                builder: (_, tileMode, __) => VectorTileLayer(
                  // Keyed by mode: VectorTileLayer caches rasterised tiles
                  // internally, so it has to be rebuilt from scratch rather
                  // than updated in place when the mode changes.
                  key: ValueKey(tileMode),
                  tileProviders: _vectorStyle!.providers,
                  theme: _vectorStyle!.theme,
                  sprites: _vectorStyle!.sprites,
                  tileOffset: TileOffset.mapbox,
                  layerMode: tileMode,
                  maximumZoom: kMaxMapZoom,
                ),
              )
            else if (_tileProvider != null)
              TileLayer(
                urlTemplate: widget.basemapUrl,
                subdomains: widget.basemapSubdomains,
                userAgentPackageName: kAppPackageId,
                tileProvider: _tileProvider!,
                maxNativeZoom: 22,
              ),
            if (_cachedPolylines.isNotEmpty)
              PerfSubtree(
                name: 'map_lines',
                child: PolylineLayer(
                  polylines: _cachedPolylines,
                  simplificationTolerance: 0.5,
                ),
              ),
            ..._keyedMarkerLayer(
                'activities-layer', true, _cachedActivityMarkers),
            ..._keyedMarkerLayer('segment-layer', true, _cachedSegmentMarkers),
            ..._keyedMarkerLayer(
                'memories-layer', _showMemories, _cachedMemoryMarkers),
            ..._keyedMarkerLayer('journal-layer', notifier.showJournals,
                _cachedJournalMarkers),
            ..._keyedMarkerLayer(
                'encounters-layer', true, _cachedEncounterMarkers),
            if (widget.focusedLatLng != null)
              MarkerLayer(markers: [focusedLocationMarker(widget.focusedLatLng!)]),
            if (widget.hereLatLng != null)
              MarkerLayer(markers: [youAreHereMarker(widget.hereLatLng!)]),
            // Owner-only, view-only Polarsteps trip overlay for a person (#40).
            ...polarstepsOverlayLayers(notifier),
            ValueListenableBuilder<List<GeoPoint>?>(
              valueListenable: notifier.previewArcNotifier,
              builder: (_, arc, __) {
                if (arc == null) return const SizedBox.shrink();
                return PolylineLayer(
                  polylines: [
                    Polyline(
                      points: arc.map(_ll).toList(),
                      color: const Color(0xCC6366F1),
                      strokeWidth: 2.5,
                    ),
                  ],
                );
              },
            ),
            ValueListenableBuilder<GeoPoint?>(
              valueListenable: notifier.elevationCursorNotifier,
              builder: (_, cursor, __) {
                if (cursor == null) return const SizedBox.shrink();
                return MarkerLayer(
                  markers: [
                    Marker(
                      point: _ll(cursor),
                      width: 16,
                      height: 16,
                      child: Container(
                        decoration: BoxDecoration(
                          color: Colors.black,
                          shape: BoxShape.circle,
                          border: Border.all(color: Colors.white, width: 2),
                        ),
                      ),
                    ),
                  ],
                );
              },
            ),
          ],
        )),
        if (notifier.isLoading)
          const Center(child: CircularProgressIndicator()),
        if (_cachedMemoryMarkers.isNotEmpty)
          Positioned(
            top: 12,
            left: 12,
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'Memories',
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: Colors.white,
                  ),
                ),
                Transform.scale(
                  scale: 0.7,
                  child: Switch(
                    value: _showMemories,
                    onChanged: (v) => setState(() => _showMemories = v),
                    materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                  ),
                ),
              ],
            ),
          ),
        if (notifier.polarstepsOverlaySteps.isNotEmpty)
          polarstepsOverlayBanner(notifier),
        Positioned(
          // Drop below the Polarsteps trip banner (also top:12, full-width)
          // so the two never overlap — the banner's close button otherwise
          // sits under this badge's top-right corner.
          top: notifier.polarstepsOverlaySteps.isNotEmpty ? 64 : 12,
          right: 12,
          child: SelectionStatsOverlay(notifier: notifier),
        ),
        if (widget.onLocateMe != null)
          Positioned(
            bottom: 16,
            right: 12,
            child: LocateMeButton(
              locating: widget.locatingHere,
              onPressed: widget.onLocateMe!,
            ),
          ),
      ],
    );
    if (perfSw != null) {
      perfSw.stop();
      final ms = perfSw.elapsedMilliseconds;
      if (ms >= 16) {
        final markers = _cachedActivityMarkers.length +
            _cachedSegmentMarkers.length +
            _cachedMemoryMarkers.length +
            _cachedJournalMarkers.length;
        debugPrint('[perf] ManageMapPanel.build ${ms}ms '
            'rebuilt=$perfRebuiltLayers geoChg=$perfGeoChg selChg=$selectionChanged2 '
            'itemsChg=$perfItemsChg styleChg=$styleChanged2 jrnChg=$perfJournalsChg '
            'markers=$markers polys=${_cachedPolylines.length}');
      }
    }
    return perfBuilt;
  }
}

// ── _StatChip ─────────────────────────────────────────────────────────────────

// ── Poster frame picker (issue #14, unit F) ──────────────────────────────────
//
// A fixed-aspect-ratio frame overlaid on the map to preview what an A0 poster
// export will capture. The user pans/zooms the real map underneath the frame
// using normal map gestures — the frame itself is wrapped in [IgnorePointer]
// and never intercepts touches or drags. Confirming ("Next") reads the map's
// current viewport bounds (`camera.visibleBounds`) as the capture region (see
// [posterBoundsFromLatLngBounds]) — the map viewport is generally a different
// aspect ratio than the drawn frame, so this is a v1 approximation rather than
// a pixel-exact crop of the frame rectangle.

/// A0 paper aspect ratio (width / height) in portrait orientation (841×1189 mm).
/// Every ISO 216 A-series size (A0-A4) shares this same aspect ratio (1:√2),
/// so the on-screen frame doesn't need to change per paper size — only the
/// paper size value sent downstream does.
const double kA0PortraitAspect = 841 / 1189;

/// Paper sizes offered by the frame picker's paper-size selector, in the
/// order shown. Matches `PosterRequest.paper_size` in `api/poster.py`.
const List<String> kPosterPaperSizes = ['A0', 'A1', 'A2', 'A3', 'A4'];

/// The frame rectangle centered in [size] with the given orientation's A0
/// aspect ratio, inset by [padding] on all sides (shrunk to fit if needed).
@visibleForTesting
Rect frameRectFor(Size size, String orientation, {double padding = 32}) {
  final aspect =
      orientation == 'portrait' ? kA0PortraitAspect : 1 / kA0PortraitAspect;
  final maxW = (size.width - padding * 2).clamp(0.0, size.width);
  final maxH = (size.height - padding * 2).clamp(0.0, size.height);
  double w = maxW;
  double h = w / aspect;
  if (h > maxH) {
    h = maxH;
    w = h * aspect;
  }
  final left = (size.width - w) / 2;
  final top = (size.height - h) / 2;
  return Rect.fromLTWH(left, top, w, h);
}

/// Converts the map's current viewport bounds to the `{north, south, east,
/// west}` shape the poster API's `bounds` request field expects. Used by
/// `app_screen.dart` when the frame picker's "Next" is confirmed.
Map<String, double> posterBoundsFromLatLngBounds(LatLngBounds bounds) => {
      'north': bounds.north,
      'south': bounds.south,
      'east': bounds.east,
      'west': bounds.west,
    };

class _FrameMaskPainter extends CustomPainter {
  final Rect frameRect;
  final Color maskColor;
  final Color borderColor;

  const _FrameMaskPainter({
    required this.frameRect,
    required this.maskColor,
    required this.borderColor,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final path = ui.Path()
      ..addRect(Rect.fromLTWH(0, 0, size.width, size.height))
      ..addRect(frameRect)
      ..fillType = ui.PathFillType.evenOdd;
    canvas.drawPath(path, Paint()..color = maskColor);
    canvas.drawRect(
      frameRect,
      Paint()
        ..color = borderColor
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2,
    );
  }

  @override
  bool shouldRepaint(covariant _FrameMaskPainter oldDelegate) =>
      oldDelegate.frameRect != frameRect ||
      oldDelegate.maskColor != maskColor ||
      oldDelegate.borderColor != borderColor;
}

/// Dimmed-mask + fixed-aspect-ratio frame overlay for picking the poster
/// capture region (issue #14). Placed as a `Stack` sibling on top of a live
/// [ManageMapPanel] by the parent screen (`app_screen.dart`) when the poster
/// flow is active — see that file for the on/off toggle wiring.
class FramePickerOverlay extends StatefulWidget {
  final AnimatedMapController mapController;

  /// Invoked with the map's current viewport bounds, the chosen orientation
  /// ('landscape'/'portrait') and the chosen paper size (see
  /// [kPosterPaperSizes]) when the user taps "Next".
  final void Function(LatLngBounds bounds, String orientation, String paperSize) onNext;
  final VoidCallback onCancel;

  const FramePickerOverlay({
    super.key,
    required this.mapController,
    required this.onNext,
    required this.onCancel,
  });

  @override
  State<FramePickerOverlay> createState() => _FramePickerOverlayState();
}

class _FramePickerOverlayState extends State<FramePickerOverlay> {
  String _orientation = 'landscape';
  String _paperSize = 'A0';

  void _toggleOrientation() => setState(() =>
      _orientation = _orientation == 'landscape' ? 'portrait' : 'landscape');

  void _confirm() {
    final bounds = widget.mapController.mapController.camera.visibleBounds;
    widget.onNext(bounds, _orientation, _paperSize);
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return LayoutBuilder(builder: (context, constraints) {
      final size = Size(constraints.maxWidth, constraints.maxHeight);
      final frameRect = frameRectFor(size, _orientation);
      return Stack(
        children: [
          Positioned.fill(
            child: IgnorePointer(
              child: CustomPaint(
                painter: _FrameMaskPainter(
                  frameRect: frameRect,
                  maskColor: const Color(0x99000000),
                  borderColor: Colors.white,
                ),
              ),
            ),
          ),
          Positioned(
            bottom: 24,
            left: 0,
            right: 0,
            child: Center(
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                decoration: BoxDecoration(
                  color: cs.surface.withValues(alpha: 0.95),
                  borderRadius: BorderRadius.circular(12),
                  boxShadow: kShadow2(Theme.of(context).brightness),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    IconButton(
                      tooltip: _orientation == 'landscape'
                          ? 'Switch to portrait'
                          : 'Switch to landscape',
                      icon: Icon(_orientation == 'landscape'
                          ? Icons.crop_landscape
                          : Icons.crop_portrait),
                      onPressed: _toggleOrientation,
                    ),
                    const SizedBox(width: 4),
                    DropdownButton<String>(
                      value: _paperSize,
                      underline: const SizedBox.shrink(),
                      items: [
                        for (final size in kPosterPaperSizes)
                          DropdownMenuItem(value: size, child: Text(size)),
                      ],
                      onChanged: (value) {
                        if (value != null) setState(() => _paperSize = value);
                      },
                    ),
                    const SizedBox(width: 4),
                    TextButton(
                      onPressed: widget.onCancel,
                      child: const Text('Cancel'),
                    ),
                    const SizedBox(width: 4),
                    FilledButton(
                      onPressed: _confirm,
                      child: const Text('Next'),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ],
      );
    });
  }
}

// ── MobileActivityPanelOverlay ───────────────────────────────────────────────
// Slide-in activity panel for narrow (mobile) layout. Rendered as a Material
// surface with elevation so it casts a shadow over the map behind it.

class MobileActivityPanelOverlay extends StatelessWidget {
  final ProjectNotifier notifier;
  final AnimatedMapController mapController;
  final double height;

  /// Drives the panel's list scrolling. Without it the panel can't centre a
  /// selection (issue #21) — `_scrollToSegment` no-ops on a null controller.
  final ScrollController? scrollController;

  /// Whether the overlay is currently open (slid on-screen). Forwarded to
  /// [ActivityPanel.panelVisible] so it reveals the current selection on open.
  final bool isVisible;

  /// Forwarded to [ActivityPanel.onLocationTap] (issue #72).
  final void Function(double lat, double lon)? onLocationTap;

  const MobileActivityPanelOverlay({
    super.key,
    required this.notifier,
    required this.mapController,
    required this.height,
    this.scrollController,
    this.isVisible = true,
    this.onLocationTap,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      elevation: 4,
      color: Theme.of(context).colorScheme.surface,
      child: SizedBox(
        width: 280,
        height: height,
        child: ActivityPanel(
          notifier: notifier,
          mapController: mapController,
          scrollController: scrollController,
          panelVisible: isVisible,
          onLocationTap: onLocationTap,
        ),
      ),
    );
  }
}

/// One memory marker's photo thumbnail. `flutter_map` builds every marker
/// up front rather than only the visible ones, so a plain `Image.network`
/// per marker meant opening the map for a photo-heavy trip fired one HTTP
/// request per memory simultaneously — enough to repeatedly crash the
/// production API. Fetches are throttled through [_gate] and cached by URL
/// so panning/zooming rebuilds don't refetch.
class _MarkerThumbImage extends StatefulWidget {
  const _MarkerThumbImage({
    super.key,
    required this.url,
    required this.headers,
    required this.size,
  });

  final String url;
  final Map<String, String> headers;
  final double size;

  static final ConcurrencyGate _gate = ConcurrencyGate(8);

  /// Encoded thumbnail bytes by URL, bounded (issue #276).
  ///
  /// Was an unbounded static Map: a long trip accumulated every thumbnail the
  /// session ever showed and released none, which is also why hiding the
  /// memories layer freed nothing. The decoded bitmaps are the expensive half
  /// and are bounded separately, by cacheWidth/cacheHeight in build().
  static final BoundedByteCache _cache = BoundedByteCache(24 * 1024 * 1024);

  @override
  State<_MarkerThumbImage> createState() => _MarkerThumbImageState();
}

class _MarkerThumbImageState extends State<_MarkerThumbImage> {
  Uint8List? _bytes;
  bool _failed = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant _MarkerThumbImage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.url != widget.url) {
      _bytes = null;
      _failed = false;
      _load();
    }
  }

  Future<void> _load() async {
    final cached = _MarkerThumbImage._cache[widget.url];
    if (cached != null) {
      setState(() => _bytes = cached);
      return;
    }
    // On-disk L2 (photo_thumb_cache.dart): survives an app restart, unlike
    // the in-memory _cache above, which is why marker thumbnails used to
    // refetch from scratch on every cold start.
    final onDisk = await photoThumbCache.read(widget.url);
    if (onDisk != null) {
      _MarkerThumbImage._cache.put(widget.url, onDisk);
      if (mounted) setState(() => _bytes = onDisk);
      return;
    }
    try {
      final bytes = await _MarkerThumbImage._gate.run(() async {
        final res = await http.get(Uri.parse(widget.url), headers: widget.headers);
        if (res.statusCode != 200) {
          throw Exception('thumb fetch failed: ${res.statusCode}');
        }
        return res.bodyBytes;
      });
      _MarkerThumbImage._cache.put(widget.url, bytes);
      photoThumbCache.write(widget.url, bytes);
      if (mounted) setState(() => _bytes = bytes);
    } catch (_) {
      if (mounted) setState(() => _failed = true);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_failed) {
      return Icon(Icons.photo_camera, size: widget.size * 0.45, color: Colors.white);
    }
    final bytes = _bytes;
    if (bytes == null) return const SizedBox.shrink();
    // cacheWidth/cacheHeight, not just width/height: the latter are layout
    // constraints and do NOT affect decoding. Without them Flutter decodes at
    // the image's native resolution — and the server produces 400x400
    // thumbnails (api/memories.py's _THUMB_SIZE) for a marker drawn at ~30
    // logical pixels. That is ~640 KB of NATIVE memory per marker; on a
    // 180-day trip with ~600 memory markers, ~375 MB, none of it visible to
    // the Dart heap or to any span in this app. It is the leading explanation
    // for issue #276's ANRs with no measurable UI-isolate work and no user
    // interaction at all.
    final px = (widget.size * MediaQuery.devicePixelRatioOf(context)).round();
    return Image.memory(
      bytes,
      width: widget.size,
      height: widget.size,
      cacheWidth: px,
      cacheHeight: px,
      fit: BoxFit.cover,
    );
  }
}
