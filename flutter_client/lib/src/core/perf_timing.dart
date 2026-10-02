library;

import 'dart:async';

import 'package:flutter/scheduler.dart' show FrameTiming;
import 'package:flutter/widgets.dart';

import 'process_memory_stub.dart'
    if (dart.library.io) 'process_memory_native.dart';

/// Dev-only frame-timing recorder. Compiled in only when built with
/// `--dart-define=PERF_TIMING=true`; in every normal/production build the const
/// is false, [PerfTiming.start] is a no-op, and the tree-shaker drops the rest.
///
/// Why this over DevTools/the performance overlay: it prints exact UI-thread
/// (build) and raster-thread (GPU) frame durations as percentiles every couple
/// of seconds, so a scroll burst is trivially isolated from idle — no GUI
/// scrubbing. The 60 fps budget is 16.7 ms; a frame is "janky" when either
/// thread blows it. build-bound jank ⇒ per-row build/layout cost; raster-bound
/// jank ⇒ paint/compositing/renderer cost. See the activity-panel diagnosis.
const bool kPerfTiming = bool.fromEnvironment('PERF_TIMING');

/// Dev diagnostic: when true, AppScreen renders a flat placeholder instead of
/// the map. Lets a scroll measurement isolate whether the raster cost is the
/// map or the activity list. False (and dropped) in every normal build.
const bool kPerfNoMap = bool.fromEnvironment('PERF_NO_MAP');

/// 60 fps frame budget in milliseconds.
const double kFrameBudgetMs = 1000.0 / 60.0; // 16.67

/// Rough heap cost of the big per-trip structures, as a human-readable label.
///
/// Deliberately an *estimate*, and labelled as one in the report: Dart exposes
/// no way to size an object graph, and the alternative — reading a 1.5 GB RSS
/// and guessing which of several candidates it is — is what several rounds of
/// issue #276 already wasted. Knowing whether our own structures account for
/// 200 MB or 1 GB decides where to look next, and that does not need
/// precision.
///
/// Per-entry costs on the 64-bit VM, deliberately conservative:
/// * a `(double, GeoPoint)` track entry: the outer record, the inner record
///   and three boxed doubles — call it 64 B.
/// * a geo coordinate: `jsonDecode` yields a 2-element `List<dynamic>` of
///   boxed doubles, so a list header plus two pointers plus two boxes — call
///   it 88 B. The memoised `LatLng` per coordinate adds ~32 B on top.
String perfEstimateStructBytes({
  required int fullTrackPoints,
  required int perActivityTrackPoints,
  required int geoCoords,
}) {
  const trackEntry = 64;
  const coordEntry = 88 + 32;
  final bytes = (fullTrackPoints + perActivityTrackPoints) * trackEntry +
      geoCoords * coordEntry;
  return '~${(bytes / (1024 * 1024)).toStringAsFixed(0)} MB (estimate)';
}

/// A one-line, bounded description of a thrown object, for the failure report.
/// `toString()` is used deliberately rather than type-switching: ApiException
/// already prints its status code, and this file must not import the API
/// layer to find that out.
String perfDescribeError(Object e) {
  final text = e.toString().replaceAll(RegExp(r'\s+'), ' ').trim();
  return text.length <= 120 ? text : '${text.substring(0, 117)}...';
}

/// Nearest-rank percentile of an already-ascending-sorted list, in ms.
/// Returns 0 for an empty list. Pure + testable.
double perfPercentile(List<double> sortedAsc, int pct) {
  if (sortedAsc.isEmpty) return 0;
  final idx = ((pct / 100.0) * (sortedAsc.length - 1)).round();
  return sortedAsc[idx.clamp(0, sortedAsc.length - 1)];
}

/// One-line summary of a window of frame timings. Pure + testable so the
/// reporting format is covered without needing a real frame pipeline.
String perfSummaryLine(List<double> buildMs, List<double> rasterMs) {
  final n = buildMs.length;
  if (n == 0) return '[perf] (no frames)';
  var janky = 0;
  for (var i = 0; i < n; i++) {
    if (buildMs[i] > kFrameBudgetMs ||
        (i < rasterMs.length && rasterMs[i] > kFrameBudgetMs)) {
      janky++;
    }
  }
  final b = [...buildMs]..sort();
  final r = [...rasterMs]..sort();
  String f(double ms) => ms.toStringAsFixed(1);
  return '[perf] frames=$n  '
      'build p50=${f(perfPercentile(b, 50))} p90=${f(perfPercentile(b, 90))} '
      'p99=${f(perfPercentile(b, 99))} max=${f(b.last)}ms  |  '
      'raster p50=${f(perfPercentile(r, 50))} p90=${f(perfPercentile(r, 90))} '
      'p99=${f(perfPercentile(r, 99))} max=${f(r.last)}ms  |  '
      'janky(>${kFrameBudgetMs.toStringAsFixed(1)}ms)=$janky/$n';
}

class PerfTiming {
  PerfTiming._();
  static final PerfTiming instance = PerfTiming._();

  final List<double> _build = [];
  final List<double> _raster = [];
  Timer? _timer;
  bool _started = false;

  /// Begin recording. No-op unless built with PERF_TIMING=true.
  void start() {
    if (_started || !kPerfTiming) return;
    _started = true;
    WidgetsBinding.instance.addTimingsCallback(_onTimings);
    _timer = Timer.periodic(const Duration(seconds: 2), (_) => _report());
    debugPrint('[perf] frame-timing recorder ON '
        '(budget ${kFrameBudgetMs.toStringAsFixed(1)}ms/frame). '
        'Scroll the activity panel — a summary prints every 2s; '
        'idle windows are skipped.');
  }

  void _onTimings(List<FrameTiming> timings) {
    for (final t in timings) {
      _build.add(t.buildDuration.inMicroseconds / 1000.0);
      _raster.add(t.rasterDuration.inMicroseconds / 1000.0);
    }
  }

  void _report() {
    if (_build.isEmpty) return; // skip idle windows
    debugPrint(perfSummaryLine(_build, _raster));
    _build.clear();
    _raster.clear();
  }

  /// Stop recording (rarely needed — it's meant to run for the whole session).
  void stop() {
    _timer?.cancel();
    _timer = null;
    if (_started) {
      WidgetsBinding.instance.removeTimingsCallback(_onTimings);
      _started = false;
    }
  }
}

// ── Named phase spans (issue #291) ───────────────────────────────────────────
//
// Frame percentiles above answer "was that window janky"; they cannot answer
// "which pipeline stage caused it". Every phase of the map-load plan
// (docs/PERF_MAP_LOAD.md) needs a before/after number attached to a specific
// stage, so the load path is instrumented with named spans instead.
//
// Two kinds, deliberately distinct — conflating them is what makes most
// "perf timing" harnesses lie:
//
//   * BLOCKING spans ([PerfSpans.blocking]) wrap *synchronous* work and
//     measure UI-isolate stall. These are the jank numbers, and the only ones
//     a frame-budget assertion may be made against.
//   * STAGE spans ([PerfSpans.stage]) wrap async work and measure wall clock,
//     which includes awaits and isolate hops. A 3 s `decode_geo` stage that
//     ran entirely on a worker isolate costs zero frames — useful for
//     time-to-interactive, meaningless for jank.

/// One line per recorded span: count, total, p50, p90, worst. The worst alone
/// is not what a user mostly waits for: one cold request among twenty warm
/// ones dominates it (issue #401). Pure + testable, mirroring
/// [perfSummaryLine]'s split so the numbers read off a dev run are trustworthy.
/// [label] distinguishes the two buckets in the printed report.
String perfSpanReport(String label, Map<String, List<double>> spans) {
  if (spans.isEmpty) return '[perf] $label (none)';
  // Worst-first: the line that matters is always at the top.
  final names = spans.keys.toList()
    ..sort((a, b) => _spanMax(spans[b]!).compareTo(_spanMax(spans[a]!)));
  final buf = StringBuffer('[perf] $label');
  for (final name in names) {
    final samples = spans[name]!;
    if (samples.isEmpty) continue;
    var total = 0.0;
    for (final s in samples) {
      total += s;
    }
    final sorted = [...samples]..sort();
    buf.write('\n  $name  n=${samples.length}  '
        'total=${total.toStringAsFixed(1)}ms  '
        'p50=${perfPercentile(sorted, 50).toStringAsFixed(1)}ms  '
        'p90=${perfPercentile(sorted, 90).toStringAsFixed(1)}ms  '
        'worst=${_spanMax(samples).toStringAsFixed(1)}ms');
  }
  return buf.toString();
}

double _spanMax(List<double> samples) {
  var max = 0.0;
  for (final s in samples) {
    if (s > max) max = s;
  }
  return max;
}

/// Names of every blocking span that blew [kFrameBudgetMs]. Pure + testable —
/// this is the assertion the load-pipeline regression tests are written
/// against, so it must not depend on a frame pipeline or a wall clock.
List<String> perfOverBudgetSpans(Map<String, List<double>> blockingSpans,
    {double budgetMs = kFrameBudgetMs}) {
  final over = <String>[];
  for (final entry in blockingSpans.entries) {
    if (_spanMax(entry.value) > budgetMs) over.add(entry.key);
  }
  over.sort();
  return over;
}

/// Records named load-pipeline spans. See the block comment above for why
/// blocking and stage timings are kept in separate buckets.
///
/// Unlike [PerfTiming], this is not `kPerfTiming`-gated at the const level:
/// [enabled] is a mutable field so tests can turn it on. The production cost
/// is one field read and a branch per *stage* — these wrap multi-millisecond
/// fetches and decodes, never per-point loops — which is well below the noise
/// floor of the work they measure.
class PerfSpans {
  PerfSpans._();
  static final PerfSpans instance = PerfSpans._();

  /// Recording is on in every build, not just `--dart-define=PERF_TIMING`
  /// ones. Each span wraps a multi-millisecond fetch, decode or build, so a
  /// Stopwatch and a map insert sit far below the noise floor of what they
  /// measure — and [lastReport] is surfaced in-app (Settings -> Performance)
  /// precisely so a real device can be diagnosed without a debugger attached,
  /// which a dart-define-gated debugPrint cannot do.
  bool enabled = true;

  final Map<String, List<double>> _blocking = {};
  final Map<String, List<double>> _stage = {};
  final Map<String, String> _notes = {};
  final Map<String, List<String>> _failures = {};

  // ── Frames during map gestures ─────────────────────────────────────────
  // Every span above measures *loading*. The ANR on issue #276 happens while
  // panning, and nothing measured that window — three rounds of fixes were
  // aimed at load-time work on the strength of load-time numbers. These
  // capture the gesture itself: a pan that drops frames shows up here even
  // when every load span is comfortably under budget.
  bool _framesHooked = false;
  bool _inGesture = false;
  final Map<String, List<double>> _gestureBlocking = {};

  // ── Event-loop stall watchdog ──────────────────────────────────────────
  // Frame timings only exist for frames that COMPLETE. A main thread blocked
  // for five seconds emits no FrameTiming callbacks at all, so an ANR shows
  // up as missing samples rather than as a slow frame — which is why the
  // build/raster percentiles above kept looking survivable while issue #276
  // was still freezing the app. A periodic timer measuring its own lateness
  // detects the stall directly: if the loop is blocked, the tick arrives
  // late by however long it was blocked.
  Timer? _stallTimer;
  DateTime? _lastTick;
  double _maxStallMs = 0;
  double _maxGestureStallMs = 0;
  static const _kStallInterval = Duration(milliseconds: 250);

  /// When the last gesture ended, so frames that complete during a gesture but
  /// whose FrameTiming is *delivered* just after it still count. The callback
  /// is batched and arrives late, so filtering purely on [_inGesture] dropped
  /// exactly the samples a short pan produces — which is why reports kept
  /// saying "1 gesture, (no frames)" while the user was demonstrably panning.
  DateTime? _gestureEndedAt;
  static const _kFrameGrace = Duration(seconds: 1);

  int _loads = 0;
  String? _lastBackgroundRefresh;

  // How many times the watchdog has actually ticked. Reported because the
  // watchdog recording *zero* stalls is only meaningful if it was running at
  // all — and on issue #276 it reported nothing through a 2656 ms frame,
  // which is either a broken instrument or evidence that the block does not
  // delay Dart timers the way a plain busy loop would.
  int _stallTicks = 0;

  // Process RSS: the available proxy for the GC hypothesis. Dart exposes no
  // public GC-pause API outside the VM service, but a major collection is
  // attributed to whichever frame it interrupts — exactly the shape of a
  // multi-second frame containing no instrumented work.
  int _rssMaxBytes = 0;
  int _rssAtWorstFrameBytes = 0;
  double _worstFrameMs = 0;
  int _freezeFrames = 0;

  // What instrumented work ran between one frame's timings and the next.
  //
  // A 2640 ms frame with no span over 16.7 ms says the time went somewhere
  // none of this app's spans wrap — but not whether that frame was doing
  // anything of ours at all. If the worst frame's context is empty, the cost
  // is entirely inside the framework's own layout and paint (issue #276);
  // if it names a span, that span is running alongside something expensive.
  final Set<String> _sinceLastFrame = <String>{};
  Set<String> _worstFrameContext = <String>{};

  // ── Layout/paint of a named widget subtree (issue #276) ────────────────
  //
  // The largest measurement gap this investigation had: nothing measured
  // flutter_map's own layout and paint. `FrameTiming.buildDuration` covers
  // build + layout + paint as one number, and every [blocking] span in this
  // app wraps a *build* callback we wrote — so a 2437 ms frame was traced to
  // work outside every span, and the report could only say "(no instrumented
  // work)". [recordFrameSpan] closes that: `PerfSubtree` times
  // `performLayout` and `paint` of a subtree and reports here.
  //
  // Aggregated PER FRAME, not per call: paint can run several times in one
  // frame (a repaint boundary flushing more than one layer), and a list of
  // per-call samples would answer the wrong question and grow at 60 Hz. Only
  // (count, total, worst) is kept per name, so this is O(names) forever
  // rather than O(frames) — the same unbounded-growth mistake the payload
  // cache and the thumbnail cache both had to be fixed for.
  final Map<String, ({int n, double total, double worst})> _frameSpans = {};
  final Map<String, double> _framePending = {};
  Object? _framePendingId;

  /// Frames whose build phase exceeded this are counted separately: a handful
  /// of these back to back is what an ANR is made of, and they vanish in a p99.
  static const double _kFreezeFrameMs = 500;

  /// Counts a project load. Session-scoped: a report saying "3 loads" is how
  /// an unexpected reload — the thing most likely to land heavy work in the
  /// middle of a gesture — becomes visible at all.
  void recordLoad() {
    if (enabled) _loads++;
  }

  /// Names the most recent background refresh, so a report can distinguish a
  /// degraded-route check from photo polling from a viewport URL sync without
  /// anyone having to guess which timer fired.
  void recordBackgroundRefresh(String what) {
    if (!enabled) return;
    final t = DateTime.now().toIso8601String();
    _lastBackgroundRefresh = '$what at ${t.substring(11, 19)}';
  }
  int _gestures = 0;
  final List<double> _gestureBuild = [];
  final List<double> _gestureRaster = [];

  /// Bounded so a long session cannot grow this without limit; the worst
  /// frame is what matters, and trimming the oldest keeps it.
  static const _kMaxGestureFrames = 4000;

  /// Called when the map camera starts moving — see
  /// `ProjectNotifier.setMapCameraActive`, which drives both.
  void beginGesture() {
    if (!enabled) return;
    _hookFrames();
    if (_inGesture) return;
    _inGesture = true;
    _gestures++;
  }

  void endGesture() {
    if (!_inGesture) return;
    _inGesture = false;
    _gestureEndedAt = DateTime.now();
  }

  void _hookFrames() {
    if (_framesHooked) return;
    try {
      WidgetsBinding.instance.addTimingsCallback(_onGestureFrames);
      _framesHooked = true;
    } catch (_) {
      // No binding (a plain unit test) — frame capture simply stays off.
    }
  }

  bool get _withinGestureWindow {
    if (_inGesture) return true;
    final ended = _gestureEndedAt;
    return ended != null && DateTime.now().difference(ended) < _kFrameGrace;
  }

  void _onGestureFrames(List<FrameTiming> timings) {
    for (final t in timings) {
      final buildMs = t.buildDuration.inMicroseconds / 1000.0;
      // Freeze accounting is deliberately NOT gesture-scoped: a 2.6 s frame
      // matters whether or not the camera happened to be moving.
      if (buildMs > _kFreezeFrameMs) _freezeFrames++;
      if (buildMs > _worstFrameMs) {
        _worstFrameMs = buildMs;
        _rssAtWorstFrameBytes = currentRssBytes() ?? 0;
        _worstFrameContext = Set<String>.from(_sinceLastFrame);
      }
    }
    _sinceLastFrame.clear();
    if (!_withinGestureWindow) return;
    for (final t in timings) {
      _gestureBuild.add(t.buildDuration.inMicroseconds / 1000.0);
      _gestureRaster.add(t.rasterDuration.inMicroseconds / 1000.0);
    }
    if (_gestureBuild.length > _kMaxGestureFrames) {
      _gestureBuild.removeRange(0, _gestureBuild.length - _kMaxGestureFrames);
      _gestureRaster.removeRange(0, _gestureRaster.length - _kMaxGestureFrames);
    }
  }

  /// Frames captured while the map camera was moving.
  ({int gestures, List<double> build, List<double> raster}) get gestureFrames =>
      (
        gestures: _gestures,
        build: List.unmodifiable(_gestureBuild),
        raster: List.unmodifiable(_gestureRaster),
      );

  /// Session-level context for the report: how many loads have run and which
  /// background refresh fired most recently.
  ({int loads, String? lastBackgroundRefresh}) get session =>
      (loads: _loads, lastBackgroundRefresh: _lastBackgroundRefresh);

  /// Everything needed to judge the "is it GC?" question: whether the watchdog
  /// ran at all, how many freeze-length frames occurred, and what process
  /// memory looked like overall and at the worst frame.
  ({
    int stallTicks,
    int freezeFrames,
    double worstFrameMs,
    int rssMaxBytes,
    int rssAtWorstFrameBytes,
    Set<String> worstFrameContext,
  }) get diagnostics => (
        stallTicks: _stallTicks,
        freezeFrames: _freezeFrames,
        worstFrameMs: _worstFrameMs,
        rssMaxBytes: _rssMaxBytes,
        rssAtWorstFrameBytes: _rssAtWorstFrameBytes,
        worstFrameContext: Set<String>.unmodifiable(_worstFrameContext),
      );

  /// UI-isolate stall of a synchronous [body], recorded under [name].
  T blocking<T>(String name, T Function() body) {
    if (!enabled) return body();
    final sw = Stopwatch()..start();
    try {
      return body();
    } finally {
      final ms = sw.elapsedMicroseconds / 1000.0;
      (_blocking[name] ??= []).add(ms);
      _sinceLastFrame.add(name);
      // Same measurement, scoped to gestures: session totals cannot say which
      // work happens while the user is actually panning.
      if (_inGesture) (_gestureBlocking[name] ??= []).add(ms);
    }
  }

  /// Adds [ms] of layout or paint time for [name], observed during the frame
  /// identified by [frameId]. Samples accumulate until a different [frameId]
  /// arrives, so one frame contributes exactly one sample per name however
  /// many times the framework called into the subtree.
  ///
  /// [frameId] is `SchedulerBinding.currentFrameTimeStamp` in production; a
  /// test passes whatever it likes, which is the point — the aggregation is
  /// testable without a frame pipeline.
  void recordFrameSpan(String name, double ms, Object frameId) {
    if (!enabled) return;
    if (_framePendingId != frameId) {
      flushFrameSpans();
      _framePendingId = frameId;
    }
    _framePending[name] = (_framePending[name] ?? 0) + ms;
  }

  /// Folds the frame currently accumulating into the totals. Called when a new
  /// frame starts and before anything reads [frameSpans], so the last frame of
  /// a session is never silently dropped.
  void flushFrameSpans() {
    if (_framePending.isEmpty) return;
    for (final e in _framePending.entries) {
      final prev = _frameSpans[e.key];
      _frameSpans[e.key] = (
        n: (prev?.n ?? 0) + 1,
        total: (prev?.total ?? 0) + e.value,
        worst: (prev != null && prev.worst > e.value) ? prev.worst : e.value,
      );
      // So the worst-frame context can name framework layout/paint instead of
      // reporting "(no instrumented work)".
      _sinceLastFrame.add(e.key);
    }
    _framePending.clear();
  }

  /// Per-name layout/paint cost, one sample per frame. Flushes first.
  Map<String, ({int n, double total, double worst})> get frameSpans {
    flushFrameSpans();
    return Map.unmodifiable(_frameSpans);
  }

  /// Starts the event-loop stall watchdog. Idempotent; call once at startup.
  void start() {
    if (!enabled || _stallTimer != null) return;
    // Hook frames here, not only on the first gesture: a freeze-length frame
    // counts whether or not the camera was moving, and on issue #276 one
    // arrived with no gesture in progress at all.
    _hookFrames();
    _lastTick = DateTime.now();
    _stallTimer = Timer.periodic(_kStallInterval, (_) {
      _stallTicks++;
      final rss = currentRssBytes();
      if (rss != null && rss > _rssMaxBytes) _rssMaxBytes = rss;
      final now = DateTime.now();
      final late = now.difference(_lastTick!).inMilliseconds -
          _kStallInterval.inMilliseconds;
      _lastTick = now;
      if (late <= 0) return;
      final ms = late.toDouble();
      if (ms > _maxStallMs) _maxStallMs = ms;
      if (_inGesture && ms > _maxGestureStallMs) _maxGestureStallMs = ms;
    });
  }

  /// Longest observed event-loop stall, overall and while the map camera was
  /// moving, in milliseconds.
  ({double worst, double worstDuringGesture}) get stalls =>
      (worst: _maxStallMs, worstDuringGesture: _maxGestureStallMs);

  /// UI-isolate stalls recorded while the map camera was moving.
  Map<String, List<double>> get gestureBlockingSpans => {
        for (final e in _gestureBlocking.entries)
          e.key: List.unmodifiable(e.value)
      };

  /// Wall-clock duration of an async [body], recorded under [name]. Includes
  /// awaits and isolate hops, so this is NOT a jank measurement.
  /// Wall-clock duration of an async [body], recorded under [name]. Includes
  /// awaits and isolate hops, so this is NOT a jank measurement.
  ///
  /// A failure is recorded too, and separately. The duration alone is
  /// actively misleading without it: a span timed in a `finally` is written
  /// whether the body returned or threw, so a failing fetch and a slow
  /// successful one are indistinguishable. On issue #276 that is exactly what
  /// happened — `fetch_geo` showed 5.1 s twice with no decode after it, and
  /// the report gave no way to tell a slow server from a failing one.
  Future<T> stage<T>(String name, Future<T> Function() body) async {
    if (!enabled) return body();
    final sw = Stopwatch()..start();
    try {
      return await body();
    } on Object catch (e) {
      (_failures[name] ??= []).add(perfDescribeError(e));
      rethrow;
    } finally {
      (_stage[name] ??= []).add(sw.elapsedMicroseconds / 1000.0);
    }
  }

  /// Snapshot of recorded UI-isolate stalls, keyed by span name.
  Map<String, List<double>> get blockingSpans =>
      {for (final e in _blocking.entries) e.key: List.unmodifiable(e.value)};

  /// Snapshot of recorded wall-clock stage durations, keyed by span name.
  Map<String, List<double>> get stageSpans =>
      {for (final e in _stage.entries) e.key: List.unmodifiable(e.value)};

  /// Records a non-timing fact about this load — a payload size, a cache
  /// verdict. A duration alone cannot distinguish a big payload on a slow
  /// link from a slow server, and those have entirely different fixes.
  void note(String name, String value) {
    if (!enabled) return;
    _notes[name] = value;
  }

  Map<String, String> get notes => Map.unmodifiable(_notes);

  /// Failures per stage, newest last.
  Map<String, List<String>> get failures =>
      {for (final e in _failures.entries) e.key: List.unmodifiable(e.value)};

  /// Clears the per-LOAD record: spans, stages, notes, failures.
  ///
  /// Deliberately leaves gesture frames, gesture-scoped spans, stall maxima
  /// and the load counter alone — those are session-level facts. reset() runs
  /// at the start of every load, so clearing them meant a reload erased the
  /// evidence of the very freeze it had just caused, in exactly the scenario
  /// worth diagnosing (issue #276). Use [resetSession] to clear everything.
  void reset() {
    _blocking.clear();
    _stage.clear();
    _notes.clear();
    _failures.clear();
  }

  /// Clears everything, including session-level state. For tests.
  @visibleForTesting
  void resetSession() {
    reset();
    _gestureBlocking.clear();
    _gestureBuild.clear();
    _gestureRaster.clear();
    _gestures = 0;
    _maxStallMs = 0;
    _maxGestureStallMs = 0;
    _loads = 0;
    _lastBackgroundRefresh = null;
    _stallTicks = 0;
    _rssMaxBytes = 0;
    _rssAtWorstFrameBytes = 0;
    _worstFrameMs = 0;
    _freezeFrames = 0;
    _sinceLastFrame.clear();
    _worstFrameContext = <String>{};
    // Session-level like the gesture frames above, and for the same reason:
    // map layout/paint is what a *pan* costs, and reset() runs at the start
    // of every load — clearing it there would erase the evidence of the very
    // rebuild worth diagnosing.
    _frameSpans.clear();
    _framePending.clear();
    _framePendingId = null;
    // Self-heals on the next camera event either way, but leaving it set
    // would make the next beginGesture() a no-op.
    _inGesture = false;
    _gestureEndedAt = null;
  }

  /// The whole report for the state this holds right now.
  ///
  /// The single source of truth for *every* reader. It exists because there
  /// were two call sites assembling this argument list by hand — [report] and
  /// the Settings screen — and the one a human actually reads had silently
  /// fallen behind: it omitted `frameSpans` (so the map's own layout and paint
  /// cost, the entire point of the instrumentation added for issue #276, never
  /// appeared), `gestureBlocking`, and both stall maxima. Nothing warned,
  /// because they are optional named parameters with harmless defaults. A
  /// missing instrument reads exactly like an instrument that measured
  /// nothing, which is the worst possible failure mode for a diagnostic.
  String buildReport() {
    flushFrameSpans();
    return perfFullReport(_blocking, _stage, _notes,
        frameSpans: _frameSpans,
        failures: _failures,
        loads: _loads,
        lastBackgroundRefresh: _lastBackgroundRefresh,
        diagnostics: diagnostics,
        gestureBlocking: _gestureBlocking,
        worstStallMs: _maxStallMs,
        worstGestureStallMs: _maxGestureStallMs,
        gestures: _gestures,
        gestureBuild: _gestureBuild,
        gestureRaster: _gestureRaster);
  }

  /// The most recently completed load's report, or null if none has finished
  /// in this session. A [ValueNotifier] so a Settings screen left open across
  /// a load updates itself.
  final ValueNotifier<String?> lastReport = ValueNotifier<String?>(null);

  /// Snapshots both buckets into [lastReport] (and prints them on a
  /// PERF_TIMING build). Called once per load, after the background phases
  /// finish; a no-op when nothing was recorded.
  void report() {
    if (_blocking.isEmpty && _stage.isEmpty) return;
    final text = buildReport();
    lastReport.value = text;
    if (kPerfTiming) debugPrint(text);
  }
}

/// The map's own layout and paint cost, next to what it was drawing.
///
/// These two facts only mean something together. "map_paint worst 41 ms" is
/// unreadable on its own; "41 ms to paint 13,273 points across 586 markers"
/// is a number a human can divide. Issue #276 spent three rounds unable to
/// attribute a 2.4 s frame precisely because `rendered_points` and the frame
/// cost were never printed against each other.
///
/// Pure + testable: [frameSpans] is [PerfSpans.frameSpans], [notes] is
/// [PerfSpans.notes] (for `rendered_points` / `markers`).
String perfMapFrameLine(
  Map<String, ({int n, double total, double worst})> frameSpans,
  Map<String, String> notes,
) {
  if (frameSpans.isEmpty) return '';
  final drawn = notes['rendered_points'] ?? '?';
  final markers = notes['markers'] ?? '?';
  final buf = StringBuffer(
      '[perf] map frame cost (layout/paint, one sample per frame) '
      '— drawing $drawn points, $markers markers');
  final names = frameSpans.keys.toList()
    ..sort((a, b) => frameSpans[b]!.worst.compareTo(frameSpans[a]!.worst));
  for (final name in names) {
    final st = frameSpans[name]!;
    buf.write('\n  $name  frames=${st.n}  '
        'total=${st.total.toStringAsFixed(1)}ms  '
        'worst=${st.worst.toStringAsFixed(1)}ms  '
        'avg=${(st.total / st.n).toStringAsFixed(2)}ms');
  }
  return buf.toString();
}

/// The full report for one load: UI-isolate stalls, wall-clock stages, and an
/// explicit over-budget verdict. Pure + testable — this is what a user reads
/// off Settings -> Performance and pastes into an issue, so its shape is
/// pinned rather than incidental.
String perfLoadReport(Map<String, List<double>> blocking,
        Map<String, List<double>> stage,
        [Map<String, String> notes = const {}]) =>
    perfFullReport(blocking, stage, notes);

/// [perfLoadReport] plus the map-gesture frame summary. A separate entry
/// point so the three-argument form, and its tests, keep working.
String perfFullReport(
  Map<String, List<double>> blocking,
  Map<String, List<double>> stage,
  Map<String, String> notes, {
  Map<String, ({int n, double total, double worst})> frameSpans = const {},
  Map<String, List<String>> failures = const {},
  Map<String, List<double>> gestureBlocking = const {},
  double worstStallMs = 0,
  double worstGestureStallMs = 0,
  int loads = 0,
  String? lastBackgroundRefresh,
  ({
    int stallTicks,
    int freezeFrames,
    double worstFrameMs,
    int rssMaxBytes,
    int rssAtWorstFrameBytes,
    Set<String> worstFrameContext,
  })? diagnostics,
  int gestures = 0,
  List<double> gestureBuild = const [],
  List<double> gestureRaster = const [],
}) {
  final buf = StringBuffer()
    ..writeln(perfSpanReport('blocking (UI isolate)', blocking))
    ..writeln(perfSpanReport('stages (wall clock)', stage));
  if (gestures > 0) {
    buf
      ..writeln('[perf] map gestures: $gestures')
      ..writeln('  ${perfSummaryLine(gestureBuild, gestureRaster)}');
    if (gestureBlocking.isNotEmpty) {
      buf.writeln(perfSpanReport('blocking DURING gestures', gestureBlocking));
    }
  }
  if (frameSpans.isNotEmpty) {
    buf.writeln(perfMapFrameLine(frameSpans, notes));
  }
  if (diagnostics != null) {
    final d = diagnostics;
    String mb(int bytes) => (bytes / (1024 * 1024)).toStringAsFixed(0);
    buf.writeln('[perf] frames: worst build ${d.worstFrameMs.toStringAsFixed(0)}ms,'
        ' ${d.freezeFrames} frame(s) over 500ms');
    // A watchdog that reported no stall is only informative if it was ticking.
    buf.writeln('[perf] watchdog ticks: ${d.stallTicks}');
    // The decisive line for issue #276: an empty context means the worst
    // frame ran none of our instrumented work, so the cost is framework
    // layout/paint — markers and polylines — not anything this app times.
    buf.writeln('[perf] worst frame ran: '
        '${d.worstFrameContext.isEmpty ? "(no instrumented work)" : (d.worstFrameContext.toList()..sort()).join(", ")}');
    if (d.rssMaxBytes > 0) {
      buf.writeln('[perf] process memory: peak ${mb(d.rssMaxBytes)} MB,'
          ' ${mb(d.rssAtWorstFrameBytes)} MB at the worst frame');
    }
  }
  if (loads > 0 || lastBackgroundRefresh != null) {
    buf.writeln('[perf] session: $loads load(s)'
        '${lastBackgroundRefresh == null ? '' : ', last background refresh: $lastBackgroundRefresh'}');
  }
  if (worstStallMs > 0) {
    // The number that actually corresponds to a freeze: frames that never
    // happened leave no timing behind, but a late timer tick measures the
    // block directly.
    buf.writeln('[perf] worst event-loop stall: '
        '${worstStallMs.toStringAsFixed(0)}ms overall, '
        '${worstGestureStallMs.toStringAsFixed(0)}ms while panning');
  }
  if (failures.isNotEmpty) {
    buf.writeln('[perf] FAILURES');
    final names = failures.keys.toList()..sort();
    for (final n in names) {
      final errs = failures[n]!;
      buf.writeln('  $n  x${errs.length}  ${errs.last}');
    }
  }
  if (notes.isNotEmpty) {
    buf.writeln('[perf] payloads');
    final names = notes.keys.toList()..sort();
    for (final n in names) {
      buf.writeln('  $n  ${notes[n]}');
    }
  }
  final over = perfOverBudgetSpans(blocking);
  buf.write(over.isEmpty
      ? '[perf] no UI-isolate span over '
          '${kFrameBudgetMs.toStringAsFixed(1)}ms'
      : '[perf] OVER BUDGET (>${kFrameBudgetMs.toStringAsFixed(1)}ms on the '
          'UI isolate): ${over.join(', ')}');
  return buf.toString();
}

/// Shorthand for the instrumentation call sites.
PerfSpans get perfSpans => PerfSpans.instance;
