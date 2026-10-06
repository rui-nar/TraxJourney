library;

import 'dart:typed_data';

import '../core/perf_timing.dart' show perfSpans;
import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/foundation.dart' show compute, kIsWeb, visibleForTesting;
import 'package:flutter/material.dart';
import '../map/geo_point.dart';
import '../map/polyline_decoder.dart';
import 'facets/project_facet.dart' show ElevationFacet;

/// Maximum number of FlSpot points rendered by fl_chart.
/// LTTB downsampling preserves visual shape; cursor uses the full-resolution
/// [ElevationChart.track] so accuracy is unaffected.
const _kMaxChartPoints = 300;

/// Above this many raw (pre-downsample) elevation points, _compute moves the
/// concatenate+LTTB work to a background isolate instead of running it
/// inline. Below it, the isolate hop (and the single frame of "No elevation
/// data" before the result lands) isn't worth it — comfortably above
/// [_kMaxChartPoints] and typical single/few-activity cases, comfortably
/// below what makes the synchronous path itself take user-visible time.
const _kInlineComputeThreshold = 5000;

/// Cheap O(activities) precheck for [_kInlineComputeThreshold] — just reads
/// `elevation_profile.length`, no per-point work.
int _totalProfilePoints(List<Map<String, dynamic>> activities) {
  var total = 0;
  for (final a in activities) {
    final profile = a['elevation_profile'];
    if (profile is List) total += profile.length;
  }
  return total;
}

/// A trip's elevation profiles flattened into typed buffers, for crossing an
/// isolate boundary.
///
/// [compute] *copies its argument*. A profile shaped `[[distKm, elevM], …]`
/// is one Dart list object per sample, so a long trip's profiles are on the
/// order of a million small objects and copying them was measured as a 2.4 s
/// frame — the cost landing on the UI isolate that the hop exists to keep it
/// off (issue #276). The same samples in a [Float64List] copy as one buffer.
typedef FlatProfiles
    = List<({String? id, Float64List points, double elevTotalKm})>;

/// Flattens every activity's `elevation_profile` into [FlatProfiles].
///
/// Skips exactly what the nested walk below used to skip inline — activities
/// with no profile or an empty one, and individual malformed samples — so the
/// series is unchanged. `elevTotalKm` is read from the raw last entry, before
/// that filtering, to keep the per-activity distance offsets identical.
@visibleForTesting
FlatProfiles flattenProfiles(List<Map<String, dynamic>> activities) {
  final out = <({String? id, Float64List points, double elevTotalKm})>[];
  for (final a in activities) {
    final profile = a['elevation_profile'];
    if (profile is! List || profile.isEmpty) continue;
    final lastPt = profile.last;
    final buf = Float64List(profile.length * 2);
    var n = 0;
    for (int i = 0; i < profile.length; i++) {
      final point = profile[i];
      if (point is! List || point.length < 2) continue;
      buf[n++] = (point[0] as num).toDouble();
      buf[n++] = (point[1] as num).toDouble();
    }
    out.add((
      id: a['id']?.toString(),
      points: n == buf.length ? buf : buf.sublist(0, n),
      elevTotalKm: (lastPt is List && lastPt.isNotEmpty)
          ? (lastPt[0] as num).toDouble()
          : 0.0,
    ));
  }
  return out;
}

/// Nested-input form of [computeElevationSpotsFlat], kept as the readable
/// contract this computation is tested through.
///
/// Filters by [args.selectedId] *before* flattening, which is the whole
/// ordering. Flattening first would walk — and allocate a [Float64List] for —
/// every activity in the trip only to discard all but one, turning the cheap
/// bounded selected-activity path into the single most expensive path there
/// is. That path runs inline with no size threshold (see
/// `_ElevationChartState._compute`), so the cost would land straight in the
/// build phase this whole exercise is about. It would also reach malformed
/// samples in activities the caller never asked about, throwing where the
/// filtered walk returned cleanly.
@visibleForTesting
({List<FlSpot> spots, double minY, double maxY}) computeElevationSpots(
  ({List<Map<String, dynamic>> activities, dynamic selectedId}) args,
) {
  final source = args.selectedId == null
      ? args.activities
      : args.activities
          .where((a) => a['id']?.toString() == args.selectedId.toString())
          .toList();
  return computeElevationSpotsFlat(flattenProfiles(source));
}

/// Concatenates every profile in [profiles] into one spot series, then
/// LTTB-downsamples it. Selecting a single activity is the caller's job — see
/// [computeElevationSpots] for why that has to happen before flattening. Pure and top-level so it can run via [compute] on a
/// background isolate — a long trip's *full* elevation payload (every
/// activity's profile concatenated, with no activity selected) can be tens of
/// thousands of points, and doing that concatenation + downsampling
/// synchronously on the UI isolate is the same "big computation on the UI
/// isolate" mistake that caused the day-carousel ANR (see map_panel.dart's
/// buildDayIndex doc comment). A single selected activity's profile is
/// bounded and cheap, so that path stays synchronous — see
/// _ElevationChartState._compute. Exposed (not `_`-prefixed) only for testing
/// this computation directly; every real caller is in this file.
@visibleForTesting
({List<FlSpot> spots, double minY, double maxY}) computeElevationSpotsFlat(
  FlatProfiles profiles,
) {
  final spots = <FlSpot>[];
  double offsetKm = 0;
  for (final a in profiles) {
    // No `continue` on an empty buffer: an activity whose samples were all
    // malformed contributed no spots but still advanced the offset before
    // this was flattened, and the offsets must not shift.
    final pts = a.points;
    for (int i = 0; i < pts.length; i += 2) {
      spots.add(FlSpot(pts[i] + offsetKm, pts[i + 1]));
    }
    if (a.elevTotalKm > 0) offsetKm += a.elevTotalKm;
  }

  double minY = 0, maxY = 0;
  if (spots.isNotEmpty) {
    // Compute min/max over full data before downsampling — LTTB may not
    // select the global peak or valley, but the y-axis must contain them.
    minY = spots.first.y;
    maxY = spots.first.y;
    for (final s in spots) {
      if (s.y < minY) minY = s.y;
      if (s.y > maxY) maxY = s.y;
    }
  }
  final downsampled =
      spots.length > _kMaxChartPoints ? _lttb(spots, _kMaxChartPoints) : spots;
  return (spots: downsampled, minY: minY, maxY: maxY);
}

/// Largest-Triangle-Three-Buckets downsampling.  O(n) — selects [threshold]
/// points from [data] that best preserve the visual shape of the series.
List<FlSpot> _lttb(List<FlSpot> data, int threshold) {
  final n = data.length;
  assert(n > threshold);
  final out = <FlSpot>[data.first];
  int a = 0;
  final every = (n - 2) / (threshold - 2);
  for (int i = 0; i < threshold - 2; i++) {
    // Centroid of the next bucket — used as the "future" anchor.
    final nS = ((i + 1) * every + 1).floor();
    final nE = ((i + 2) * every + 1).floor().clamp(0, n);
    double avgX = 0, avgY = 0;
    for (int j = nS; j < nE; j++) { avgX += data[j].x; avgY += data[j].y; }
    final cnt = nE - nS;
    avgX /= cnt; avgY /= cnt;
    // Current bucket — pick the point that forms the largest triangle
    // with the previously selected point (a) and the next-bucket centroid.
    final cS = (i * every + 1).floor();
    final cE = ((i + 1) * every + 1).floor().clamp(0, n);
    final ax = data[a].x, ay = data[a].y;
    double maxArea = -1; int best = cS;
    for (int j = cS; j < cE; j++) {
      final area = ((ax - avgX) * (data[j].y - ay)
                  - (ax - data[j].x) * (avgY - ay)).abs();
      if (area > maxArea) { maxArea = area; best = j; }
    }
    out.add(data[best]);
    a = best;
  }
  out.add(data.last);
  return out;
}

class ElevationChart extends StatefulWidget {
  final List<Map<String, dynamic>> activities;
  final dynamic selectedActivityId;

  /// Called with the map position under the chart cursor, or null when the
  /// user lifts / exits. Drives the elevation cursor marker on the map.
  final void Function(GeoPoint?)? onCursorChanged;

  /// Driven by map taps — shows a vertical line at this distance (km).
  final ValueNotifier<double?>? mapCursorNotifier;

  /// Pre-built distance-indexed track (cumulative km → LatLng).
  /// Built by ProjectNotifier from GeoJSON so Flutter never needs to decode
  /// the polyline.  Pass fullTrack when no activity is selected, or the
  /// per-activity track (0-based distances) when one is selected.
  /// Ignored when [elevation] is given.
  final List<(double, GeoPoint)> track;

  /// A trip's elevation facet (#294). When given, the chart takes its track
  /// from it — the full track, or [selectedActivityId]'s own — and rebuilds
  /// whenever the notifier rebuilds the tracks, so the cursor never maps
  /// through a track the notifier has since replaced. Null for a chart of a
  /// track that is not a trip's (the track editor's, an export's).
  final ElevationFacet? elevation;

  /// Color of the chart line and fill. Defaults to black when null.
  final Color? color;

  /// When false, the line is hidden but the filled area below still renders.
  final bool showLine;

  const ElevationChart({
    super.key,
    required this.activities,
    this.track = const [],
    this.elevation,
    this.selectedActivityId,
    this.onCursorChanged,
    this.mapCursorNotifier,
    this.color,
    this.showLine = true,
  });

  @override
  State<ElevationChart> createState() => _ElevationChartState();
}

class _ElevationChartState extends State<ElevationChart> {
  List<FlSpot> _spots = const [];
  double _minY = 0;
  double _maxY = 0;
  // Bumped on every _compute call; guards a stale async result (from a
  // superseded call — e.g. two rapid activity de-selections) from
  // overwriting a newer one.
  int _computeGen = 0;

  @override
  void initState() {
    super.initState();
    _compute(widget.activities, widget.selectedActivityId);
    widget.mapCursorNotifier?.addListener(_onMapCursor);
    widget.elevation?.addListener(_onElevation);
  }

  @override
  void didUpdateWidget(ElevationChart oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.mapCursorNotifier != widget.mapCursorNotifier) {
      oldWidget.mapCursorNotifier?.removeListener(_onMapCursor);
      widget.mapCursorNotifier?.addListener(_onMapCursor);
    }
    // A notifier swap (an account change) brings a new facet; the old one may
    // already be disposed, which removeListener allows.
    if (!identical(oldWidget.elevation, widget.elevation)) {
      oldWidget.elevation?.removeListener(_onElevation);
      widget.elevation?.addListener(_onElevation);
    }
    if (!identical(oldWidget.activities, widget.activities) ||
        oldWidget.selectedActivityId?.toString() !=
            widget.selectedActivityId?.toString()) {
      _compute(widget.activities, widget.selectedActivityId);
    }
  }

  @override
  void dispose() {
    widget.mapCursorNotifier?.removeListener(_onMapCursor);
    widget.elevation?.removeListener(_onElevation);
    super.dispose();
  }

  void _onMapCursor() => setState(() {});

  void _onElevation() => setState(() {});

  /// The track the cursor maps through: [ElevationChart.elevation]'s current
  /// one when given, else [ElevationChart.track].
  List<(double, GeoPoint)> get _track {
    final e = widget.elevation;
    if (e == null) return widget.track;
    final id = widget.selectedActivityId;
    return id != null
        ? e.perActivityTracks[id.toString()] ?? e.fullTrack
        : e.fullTrack;
  }

  static Widget _elevLeftTitle(double value, TitleMeta meta) =>
      Text('${value.toInt()} m', style: const TextStyle(fontSize: 9));

  static Widget _elevBottomTitle(double value, TitleMeta meta) {
    // Skip the first and last tick to avoid clipping at chart edges.
    if (value == meta.min || value == meta.max) return const SizedBox.shrink();
    return Text('${value.toStringAsFixed(0)} km',
        style: const TextStyle(fontSize: 9));
  }

  double get _bottomInterval {
    if (_spots.isEmpty) return 50;
    final total = _spots.last.x;
    if (total <= 30)   return 5;
    if (total <= 100)  return 10;
    if (total <= 250)  return 25;
    if (total <= 600)  return 50;
    if (total <= 1200) return 100;
    if (total <= 3000) return 200;
    if (total <= 6000) return 500;
    return 1000;
  }

  void _compute(List<Map<String, dynamic>> activities, dynamic selectedId) {
    final args = (activities: activities, selectedId: selectedId);
    // Bumped unconditionally, before the branch below — not just on the
    // compute() path. An inline call must be able to supersede a hop still in
    // flight, or the older full-trip series lands after the user has selected
    // an activity and overwrites it, leaving the chart showing the whole trip
    // while `widget.track` is the per-activity track (so the cursor mapping
    // disagrees too). This is the same bug, and the same fix, as
    // ProjectNotifier._buildFullTrack's _buildFullTrackGen.
    final gen = ++_computeGen;
    // A single *selected* activity's profile is bounded — and the common case
    // (map/panel activity click) shouldn't pay an isolate-hop latency or flash
    // "No elevation data" for a frame. Only the unfiltered full-trip aggregate
    // can get big enough to need moving off the UI isolate.
    //
    // Web takes this path too, whatever the size: `compute` has no isolate to
    // hop to there and runs its callback inline, so the hop buys nothing and
    // flattening first would only add a second full pass over every sample on
    // the main thread.
    if (selectedId != null ||
        kIsWeb ||
        _totalProfilePoints(activities) <= _kInlineComputeThreshold) {
      // No setState here: called synchronously from initState (too early —
      // Flutter forbids setState there) and from didUpdateWidget (already
      // part of the build Flutter is about to run for this widget) — both
      // read these fields via the build() that follows, same as before this
      // split existed.
      final result = computeElevationSpots(args);
      _spots = result.spots;
      _minY = result.minY;
      _maxY = result.maxY;
      return;
    }
    // Flatten *before* the hop, not inside it: compute() copies its argument,
    // so handing it the activity maps serialised a million-odd small list
    // objects on the UI isolate — see [FlatProfiles]. `selectedId` is null on
    // this branch, so there is nothing to filter out first.
    final flat = perfSpans
        .blocking('elevation_flatten', () => flattenProfiles(activities));
    compute(computeElevationSpotsFlat, flat).then((result) {
      if (!mounted || gen != _computeGen) return;
      setState(() {
        _spots = result.spots;
        _minY = result.minY;
        _maxY = result.maxY;
      });
    });
  }

  void _onTouch(FlTouchEvent event, LineTouchResponse? response) {
    // Do NOT clear on FlPointerExitEvent — the cursor should persist at the
    // last hovered/clicked position so the user can inspect it after moving
    // the mouse off the chart.
    final spots = response?.lineBarSpots;
    if (spots == null || spots.isEmpty) return;
    final pos = latLonAtDistance(_track, spots.first.x);
    if (pos != null) widget.onCursorChanged?.call(pos);
  }

  @override
  Widget build(BuildContext context) {
    // The other uninstrumented widget on the map screen: 180 days of
    // elevation data, rebuilt alongside the map (#276).
    return perfSpans.blocking('elevation_chart_build', () {
    if (_spots.isEmpty) {
      return const SizedBox(
        height: 160,
        child: Center(child: Text('No elevation data')),
      );
    }

    final yPad = ((_maxY - _minY) * 0.1).clamp(10.0, double.infinity);

    return SizedBox(
      height: 160,
      child: LineChart(
        LineChartData(
          minY: _minY - yPad,
          maxY: _maxY + yPad,
          gridData: FlGridData(
            show: true,
            drawHorizontalLine: true,
            horizontalInterval: 100,
            drawVerticalLine: false,
            getDrawingHorizontalLine: (value) {
              return FlLine(
                color: Colors.grey.withValues(alpha: 0.3),
                strokeWidth: 1,
                dashArray: [2, 2],
              );
            },
          ),
          borderData: FlBorderData(show: false),
          titlesData: FlTitlesData(
            leftTitles: AxisTitles(
              axisNameWidget: RotatedBox(
                quarterTurns: 0,
                child: Text(
                  'Elevation (m)',
                  style: Theme.of(context).textTheme.labelSmall,
                ),
              ),
              axisNameSize: 14,
              sideTitles: SideTitles(
                showTitles: true,
                reservedSize: 40,
                getTitlesWidget: _elevLeftTitle,
              ),
            ),
            bottomTitles: AxisTitles(
              sideTitles: SideTitles(
                showTitles: true,
                reservedSize: 22,
                interval: _bottomInterval,
                getTitlesWidget: _elevBottomTitle,
              ),
            ),
            topTitles: const AxisTitles(
                sideTitles: SideTitles(showTitles: false)),
            rightTitles: const AxisTitles(
                sideTitles: SideTitles(showTitles: false)),
          ),
          extraLinesData: () {
            final d = widget.mapCursorNotifier?.value;
            if (d == null) return null;
            return ExtraLinesData(verticalLines: [
              VerticalLine(
                x: d,
                color: widget.color ?? Colors.black,
                strokeWidth: 1.5,
                dashArray: [4, 4],
              ),
            ]);
          }(),
          lineTouchData: LineTouchData(
            touchCallback: _onTouch,
            handleBuiltInTouches: true,
            touchTooltipData: LineTouchTooltipData(
              getTooltipColor: (_) => Colors.transparent,
            ),
          ),
          lineBarsData: [
            LineChartBarData(
              spots: _spots,
              isCurved: true,
              color: widget.showLine
                  ? (widget.color ?? Colors.black)
                  : const Color(0x01000000), // alpha=1: invisible but touchable
              barWidth: 2,
              dotData: const FlDotData(show: false),
              belowBarData: BarAreaData(
                show: true,
                color: (widget.color ?? Colors.black).withValues(alpha: 0.2),
              ),
            ),
          ],
        ),
      ),
    );
    });
  }
}

/// Drop-in replacement for [ElevationChart] shown while elevation data is
/// loading in the background.  Must match ElevationChart's preferred height
/// so the layout does not jump when the real chart replaces it.
class ElevationLoadingPlaceholder extends StatelessWidget {
  const ElevationLoadingPlaceholder({super.key});

  @override
  Widget build(BuildContext context) => const SizedBox(
    height: 160,
    child: Center(
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          SizedBox(
            width: 16,
            height: 16,
            child: CircularProgressIndicator(strokeWidth: 2),
          ),
          SizedBox(width: 10),
          Text('Loading elevation data…'),
        ],
      ),
    ),
  );
}
