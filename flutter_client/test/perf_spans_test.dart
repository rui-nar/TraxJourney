import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/perf_timing.dart';

/// Issue #291. The span recorder is the measurement seam the whole map-load
/// plan (docs/PERF_MAP_LOAD.md) is verified against, so its aggregation and
/// its over-budget verdict are pinned here — a harness that quietly reports
/// the wrong number is worse than no harness.
void main() {
  setUp(() {
    perfSpans
      ..resetSession()
      ..enabled = true;
  });

  tearDown(() {
    perfSpans
      ..resetSession()
      ..enabled = true;  // the library default — see PerfSpans.enabled
  });

  group('PerfSpans recording', () {
    test('disabled recorder still runs the body but records nothing', () {
      perfSpans.enabled = false;
      var ran = false;
      perfSpans.blocking('decode_geo', () => ran = true);
      expect(ran, isTrue);
      expect(perfSpans.blockingSpans, isEmpty);
    });

    test('blocking() returns the body result and records one sample', () {
      final result = perfSpans.blocking('decode_geo', () => 42);
      expect(result, 42);
      expect(perfSpans.blockingSpans['decode_geo'], hasLength(1));
    });

    test('repeated calls accumulate samples under the same name', () {
      for (var i = 0; i < 3; i++) {
        perfSpans.blocking('apply_geo', () {});
      }
      expect(perfSpans.blockingSpans['apply_geo'], hasLength(3));
    });

    test('a throwing body still records its stall, then rethrows', () {
      expect(
        () => perfSpans.blocking('decode_details', () => throw StateError('x')),
        throwsStateError,
      );
      expect(perfSpans.blockingSpans['decode_details'], hasLength(1));
    });

    test('stage() records into the wall-clock bucket, not the blocking one',
        () async {
      await perfSpans.stage('fetch_geo', () async {});
      expect(perfSpans.stageSpans['fetch_geo'], hasLength(1));
      // The distinction is the whole point: an async stage that spent its time
      // on a worker isolate costs zero frames and must never be mistaken for
      // UI-isolate stall.
      expect(perfSpans.blockingSpans, isEmpty);
    });

    test('reset clears both buckets', () async {
      perfSpans.blocking('a', () {});
      await perfSpans.stage('b', () async {});
      perfSpans.reset();
      expect(perfSpans.blockingSpans, isEmpty);
      expect(perfSpans.stageSpans, isEmpty);
    });

    test('snapshots are unmodifiable so a caller cannot corrupt the record',
        () {
      perfSpans.blocking('a', () {});
      expect(() => perfSpans.blockingSpans['a']!.add(1.0), throwsUnsupportedError);
    });
  });

  group('perfOverBudgetSpans', () {
    test('no spans over budget yields an empty list', () {
      expect(perfOverBudgetSpans(const {'a': [1.0, 2.0]}), isEmpty);
    });

    test('a span is over budget on its WORST sample, not its average', () {
      // 40ms once among fast samples is a dropped frame the user saw; an
      // average would hide it.
      expect(
        perfOverBudgetSpans(const {'decode_geo': [1.0, 1.0, 1.0, 40.0]}),
        ['decode_geo'],
      );
    });

    test('names are reported sorted so the assertion message is stable', () {
      expect(
        perfOverBudgetSpans(const {
          'z_span': [40.0],
          'a_span': [40.0],
        }),
        ['a_span', 'z_span'],
      );
    });

    test('the budget is overridable for a deliberately looser assertion', () {
      expect(perfOverBudgetSpans(const {'a': [20.0]}), ['a']);
      expect(perfOverBudgetSpans(const {'a': [20.0]}, budgetMs: 50), isEmpty);
    });

    test('exactly at budget is not over budget', () {
      expect(perfOverBudgetSpans({'a': [kFrameBudgetMs]}), isEmpty);
    });
  });

  group('perfLoadReport', () {
    // This is what a user reads off Settings -> Performance and pastes into an
    // issue, so its shape is pinned rather than incidental.
    test('carries both buckets and an explicit all-clear', () {
      final r = perfLoadReport(const {'style_markers': [0.7]},
          const {'decode_geo': [1200.0]});
      expect(r, contains('blocking (UI isolate)'));
      expect(r, contains('style_markers'));
      expect(r, contains('stages (wall clock)'));
      expect(r, contains('decode_geo'));
      // A slow *stage* is not jank: it ran on a worker. Saying so explicitly
      // is the point, otherwise a 1.2 s decode_geo reads as a problem.
      expect(r, contains('no UI-isolate span over'));
    });

    test('payload notes are rendered, sorted, when present', () {
      // A duration alone cannot tell a big payload on a slow link from a slow
      // server; the size is what disambiguates them.
      final r = perfLoadReport(const {}, const {'fetch_geo': [5263.0]},
          const {'geo': '11.4 MB', 'details': '3.2 MB'});
      expect(r, contains('payloads'));
      expect(r.indexOf('details'), lessThan(r.indexOf('geo  11.4 MB')));
      expect(r, contains('11.4 MB'));
    });

    test('no payloads section when nothing was noted', () {
      expect(perfLoadReport(const {}, const {'a': [1.0]}), isNot(contains('payloads')));
    });

    test('note() is a no-op while recording is disabled', () {
      perfSpans.enabled = false;
      perfSpans.note('geo', '1 MB');
      expect(perfSpans.notes, isEmpty);
    });

    test('names the over-budget spans when there are any', () {
      final r = perfLoadReport(const {'build_specs': [480.0]}, const {});
      expect(r, contains('OVER BUDGET'));
      expect(r, contains('build_specs'));
    });
  });

  group('stage failures', () {
    // A span timed in a finally is written whether the body returned or threw,
    // so duration alone cannot tell a failing fetch from a slow one. On issue
    // #276 that ambiguity was the whole problem: fetch_geo showed 5.1 s twice
    // with no decode after it, and the report could not say why.
    test('a failed stage is recorded as a failure, and still rethrows',
        () async {
      await expectLater(
        perfSpans.stage('fetch_geo', () async => throw StateError('boom')),
        throwsStateError,
      );
      expect(perfSpans.failures['fetch_geo'], hasLength(1));
      expect(perfSpans.failures['fetch_geo']!.single, contains('boom'));
      // The duration is still recorded — a failure has a duration too.
      expect(perfSpans.stageSpans['fetch_geo'], hasLength(1));
    });

    test('a successful stage records no failure', () async {
      await perfSpans.stage('fetch_geo', () async => 1);
      expect(perfSpans.failures, isEmpty);
    });

    test('repeated failures accumulate, newest last', () async {
      for (final msg in ['first', 'second']) {
        try {
          await perfSpans.stage('fetch_details', () async => throw StateError(msg));
        } on StateError {
          // expected
        }
      }
      expect(perfSpans.failures['fetch_details'], hasLength(2));
      expect(perfSpans.failures['fetch_details']!.last, contains('second'));
    });

    test('reset clears failures', () async {
      try {
        await perfSpans.stage('x', () async => throw StateError('e'));
      } on StateError {
        // expected
      }
      perfSpans.reset();
      expect(perfSpans.failures, isEmpty);
    });
  });

  group('perfDescribeError', () {
    test('collapses whitespace to one line', () {
      expect(perfDescribeError('a\n  b'), 'a b');
    });

    test('truncates something very long', () {
      final d = perfDescribeError('x' * 500);
      expect(d.length, 120);
      expect(d, endsWith('...'));
    });
  });

  group('failure reporting', () {
    test('failures are called out, with the last error', () {
      final r = perfFullReport(const {}, const {'fetch_geo': [5081.0, 38.0]},
          const {}, failures: const {'fetch_geo': ['ApiException(504)', 'ApiException(504)']});
      expect(r, contains('FAILURES'));
      expect(r, contains('fetch_geo'));
      expect(r, contains('x2'));
      expect(r, contains('504'));
    });

    test('no failures section when everything succeeded', () {
      expect(perfFullReport(const {}, const {'fetch_geo': [10.0]}, const {}),
          isNot(contains('FAILURES')));
    });
  });

  group('gesture-scoped blocking', () {
    test('work outside a gesture is not attributed to one', () {
      perfSpans.blocking('activity_panel_build', () {});
      expect(perfSpans.blockingSpans['activity_panel_build'], hasLength(1));
      expect(perfSpans.gestureBlockingSpans, isEmpty);
    });

    test('work during a gesture lands in both buckets', () {
      perfSpans.beginGesture();
      perfSpans.blocking('activity_panel_build', () {});
      perfSpans.endGesture();
      perfSpans.blocking('activity_panel_build', () {});
      expect(perfSpans.blockingSpans['activity_panel_build'], hasLength(2));
      expect(perfSpans.gestureBlockingSpans['activity_panel_build'],
          hasLength(1),
          reason: 'session totals cannot say which work happens while panning');
    });

    test('a load-scoped reset keeps the gesture bucket', () {
      perfSpans.beginGesture();
      perfSpans.blocking('x', () {});
      perfSpans.reset();
      expect(perfSpans.gestureBlockingSpans['x'], hasLength(1));
    });
  });

  group('session context', () {
    test('loads are counted across resets', () {
      perfSpans.recordLoad();
      perfSpans.reset();
      perfSpans.recordLoad();
      expect(perfSpans.session.loads, 2,
          reason: 'an unexpected reload is only visible if someone counts it');
    });

    test('the last background refresh is named and timestamped', () {
      perfSpans.recordBackgroundRefresh('degraded_route_check');
      final v = perfSpans.session.lastBackgroundRefresh!;
      expect(v, startsWith('degraded_route_check at '));
      expect(v, matches(RegExp(r'\d{2}:\d{2}:\d{2}$')));
    });

    test('the most recent refresh wins', () {
      perfSpans.recordBackgroundRefresh('photo_poll');
      perfSpans.recordBackgroundRefresh('viewport_url_sync');
      expect(perfSpans.session.lastBackgroundRefresh, contains('viewport_url_sync'));
    });

    test('session context is rendered in the report', () {
      final r = perfFullReport(const {}, const {}, const {},
          loads: 3, lastBackgroundRefresh: 'viewport_url_sync at 10:11:12');
      expect(r, contains('3 load(s)'));
      expect(r, contains('viewport_url_sync'));
    });
  });

  group('frame capture window', () {
    test('frames are still accepted just after a gesture ends', () {
      // FrameTiming is delivered in a batch AFTER the frames complete, so a
      // short pan's samples arrive once the gesture is already over. Filtering
      // on "in gesture" alone dropped exactly those, which is why reports read
      // "1 gesture, (no frames)" while the user was demonstrably panning.
      perfSpans.beginGesture();
      perfSpans.endGesture();
      expect(perfSpans.gestureFrames.gestures, 1);
    });
  });

  group('perfEstimateStructBytes', () {
    // Removing the 33 MB details payload took peak RSS from ~1.9 GB to
    // ~1.5 GB, so it was a large contributor but not the bulk. This estimate
    // exists to say whether our own structures account for the remainder or
    // whether it is engine/GPU/native-image memory RSS lumps in — a question
    // several rounds of guessing failed to answer.
    test('scales with every input', () {
      String est(int t, int p, int g) => perfEstimateStructBytes(
          fullTrackPoints: t, perActivityTrackPoints: p, geoCoords: g);
      expect(est(0, 0, 0), contains('0 MB'));
      // Half a million track points and as many coordinates is the shape of a
      // 180-day trip; the answer must land in hundreds of MB, not single MB.
      final big = est(500000, 500000, 500000);
      final mb = int.parse(RegExp(r'(\d+) MB').firstMatch(big)!.group(1)!);
      expect(mb, greaterThan(100));
      expect(mb, lessThan(1000));
    });

    test('is labelled an estimate, because it is one', () {
      expect(
          perfEstimateStructBytes(
              fullTrackPoints: 1, perActivityTrackPoints: 1, geoCoords: 1),
          contains('estimate'));
    });

    test('doubling the points roughly doubles the answer', () {
      int mb(String s) => int.parse(RegExp(r'(\d+) MB').firstMatch(s)!.group(1)!);
      final one = mb(perfEstimateStructBytes(
          fullTrackPoints: 100000, perActivityTrackPoints: 0, geoCoords: 0));
      final two = mb(perfEstimateStructBytes(
          fullTrackPoints: 200000, perActivityTrackPoints: 0, geoCoords: 0));
      expect(two, closeTo(one * 2, 2));
    });
  });

  group('freeze diagnostics', () {
    // The three numbers that decide whether the remaining freeze on #276 is
    // GC: was the watchdog even running, how many freeze-length frames
    // happened, and what did process memory look like at the worst one.
    test('report names the worst frame and the freeze count', () {
      final r = perfFullReport(const {}, const {}, const {},
          diagnostics: (
            stallTicks: 400,
            freezeFrames: 3,
            worstFrameMs: 2656.3,
            rssMaxBytes: 720 * 1024 * 1024,
            rssAtWorstFrameBytes: 710 * 1024 * 1024,
            worstFrameContext: const <String>{},
          ));
      expect(r, contains('worst build 2656ms'));
      expect(r, contains('3 frame(s) over 500ms'));
      expect(r, contains('watchdog ticks: 400'));
      expect(r, contains('peak 720 MB'));
      expect(r, contains('710 MB at the worst frame'));
    });

    test('a watchdog that never ticked says so', () {
      // "No stall recorded" only means anything if the watchdog ran; zero
      // ticks means the instrument is broken, not that nothing stalled.
      final r = perfFullReport(const {}, const {}, const {},
          diagnostics: (
            stallTicks: 0,
            freezeFrames: 0,
            worstFrameMs: 0,
            rssMaxBytes: 0,
            rssAtWorstFrameBytes: 0,
            worstFrameContext: const <String>{},
          ));
      expect(r, contains('watchdog ticks: 0'));
    });

    test('process memory is omitted where the platform cannot answer it', () {
      final r = perfFullReport(const {}, const {}, const {},
          diagnostics: (
            stallTicks: 10,
            freezeFrames: 0,
            worstFrameMs: 12.0,
            rssMaxBytes: 0,
            rssAtWorstFrameBytes: 0,
            worstFrameContext: const <String>{'build_specs'},
          ));
      expect(r, isNot(contains('process memory')));
    });

    test('an empty worst-frame context is stated, not omitted', () {
      // The decisive line for #276: "the worst frame ran none of our
      // instrumented work" is a finding, and a blank would read as missing
      // data rather than as the answer.
      final r = perfFullReport(const {}, const {}, const {},
          diagnostics: (
            stallTicks: 100,
            freezeFrames: 1,
            worstFrameMs: 2640.0,
            rssMaxBytes: 0,
            rssAtWorstFrameBytes: 0,
            worstFrameContext: const <String>{},
          ));
      expect(r, contains('worst frame ran: (no instrumented work)'));
    });

    test('a worst-frame context names its spans, sorted', () {
      final r = perfFullReport(const {}, const {}, const {},
          diagnostics: (
            stallTicks: 100,
            freezeFrames: 1,
            worstFrameMs: 2640.0,
            rssMaxBytes: 0,
            rssAtWorstFrameBytes: 0,
            worstFrameContext: const <String>{'style_markers', 'build_specs'},
          ));
      expect(r, contains('worst frame ran: build_specs, style_markers'));
    });

    test('no diagnostics section when none were passed', () {
      expect(perfFullReport(const {}, const {'a': [1.0]}, const {}),
          isNot(contains('watchdog ticks')));
    });
  });

  group('stall reporting', () {
    // The number that corresponds to a freeze. Frames that never happen leave
    // no timing behind, so build/raster percentiles stay survivable through an
    // ANR; a late timer tick measures the block directly.
    test('a stall is reported, split by whether the user was panning', () {
      final r = perfFullReport(const {}, const {}, const {},
          worstStallMs: 5200, worstGestureStallMs: 5200);
      expect(r, contains('worst event-loop stall'));
      expect(r, contains('5200ms overall'));
      expect(r, contains('5200ms while panning'));
    });

    test('no stall line when nothing stalled', () {
      expect(perfFullReport(const {}, const {'a': [1.0]}, const {}),
          isNot(contains('event-loop stall')));
    });

    test('gesture-scoped blocking is reported under the gesture section', () {
      final r = perfFullReport(const {}, const {}, const {},
          gestures: 3,
          gestureBuild: const [5.0],
          gestureRaster: const [5.0],
          gestureBlocking: const {'elevation_chart_build': [40.0]});
      expect(r, contains('blocking DURING gestures'));
      expect(r, contains('elevation_chart_build'));
    });
  });

  group('gesture frames', () {
    test('nothing is recorded until a gesture begins', () {
      expect(perfSpans.gestureFrames.gestures, 0);
      expect(perfSpans.gestureFrames.build, isEmpty);
    });

    test('beginGesture counts a gesture; repeats do not double-count', () {
      perfSpans.beginGesture();
      perfSpans.beginGesture(); // a second camera event in the same drag
      expect(perfSpans.gestureFrames.gestures, 1);
      perfSpans.endGesture();
      perfSpans.beginGesture();
      expect(perfSpans.gestureFrames.gestures, 2);
    });

    test('a disabled recorder counts nothing', () {
      perfSpans.enabled = false;
      perfSpans.beginGesture();
      expect(perfSpans.gestureFrames.gestures, 0);
    });

    test('a load-scoped reset does NOT erase gesture history', () {
      // reset() runs at the start of every load. Clearing this here meant a
      // reload erased the record of the freeze it had just caused — in
      // exactly the scenario worth diagnosing (issue #276).
      perfSpans.beginGesture();
      perfSpans.reset();
      expect(perfSpans.gestureFrames.gestures, 1);
    });

    test('resetSession does clear it', () {
      perfSpans.beginGesture();
      perfSpans.resetSession();
      expect(perfSpans.gestureFrames.gestures, 0);
    });
  });

  group('perfFullReport', () {
    test('omits the gesture section when nothing panned', () {
      expect(perfFullReport(const {}, const {'a': [1.0]}, const {}),
          isNot(contains('map gestures')));
    });

    test('reports gesture frames when there are any', () {
      // A pan that drops frames must be visible even when every load span is
      // comfortably under budget — that combination is exactly what issue
      // #276 turned out to be.
      final r = perfFullReport(
        const {'build_specs': [12.0]},
        const {},
        const {},
        gestures: 2,
        gestureBuild: const [4.0, 90.0, 5.0],
        gestureRaster: const [3.0, 6.0, 4.0],
      );
      expect(r, contains('map gestures: 2'));
      expect(r, contains('frames=3'));
      expect(r, contains('janky'));
      expect(r, isNot(contains('OVER BUDGET')),
          reason: 'the load was fine; the gesture was not — they must not be '
              'conflated');
    });
  });

  group('perfSpanReport', () {
    test('empty spans report explicitly', () {
      expect(perfSpanReport('blocking', const {}), contains('none'));
    });

    test('reports count, total and worst per span', () {
      final line = perfSpanReport('blocking', const {
        'decode_geo': [10.0, 30.0],
      });
      expect(line, contains('decode_geo'));
      expect(line, contains('n=2'));
      expect(line, contains('total=40.0ms'));
      expect(line, contains('worst=30.0ms'));
    });

    test('worst span is listed first', () {
      final report = perfSpanReport('blocking', const {
        'cheap': [1.0],
        'expensive': [500.0],
        'middling': [50.0],
      });
      final lines = report.split('\n');
      expect(lines[1], contains('expensive'));
      expect(lines[2], contains('middling'));
      expect(lines[3], contains('cheap'));
    });
  });

  // Issue #276. The report a human reads is assembled by the Settings panel,
// and it had drifted from the one `report()` builds: it omitted `frameSpans`,
// `gestureBlocking` and both stall maxima, because they are optional named
// parameters whose defaults are silently harmless.
//
// The cost was that the map's own layout and paint cost — the whole point of
// the instrumentation added for the viewport work — was collected on every
// frame and never shown, so a device run came back unable to answer the one
// question it was taken to answer. A missing instrument reads exactly like an
// instrument that measured nothing.
//
// Both readers now go through PerfSpans.buildReport, and these pin that.

  group('buildReport carries every instrument', () {
    setUp(() {
      perfSpans.resetSession();
      perfSpans.reset();
      perfSpans.enabled = true;
    });

    test('the map layout and paint line is present', () {
      perfSpans.blocking('something', () {});
      perfSpans.recordFrameSpan('map_paint', 12.5, 1);
      perfSpans.recordFrameSpan('map_lines_paint', 4.0, 1);
      final report = perfSpans.buildReport();
      expect(report, contains('map_paint'),
          reason: 'collected on every frame and never shown is the bug');
      expect(report, contains('map_lines_paint'));
    });

    test('work recorded during a gesture is present', () {
      perfSpans.beginGesture();
      perfSpans.blocking('during_pan', () {});
      perfSpans.endGesture();
      expect(perfSpans.buildReport(), contains('during_pan'));
    });

    test('an empty frame-span set does not print an empty section', () {
      perfSpans.blocking('something', () {});
      expect(perfSpans.buildReport(), isNot(contains('map_paint')));
    });
  });
}
