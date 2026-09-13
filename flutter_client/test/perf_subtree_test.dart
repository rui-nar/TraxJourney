// Issue #276, unit 1: measuring the framework's own layout and paint.
//
// The blind spot this closes: `FrameTiming.buildDuration` covers build,
// layout AND paint as one number, and every `perfSpans.blocking()` span in
// this app wraps a build callback we wrote. A 2437 ms frame was traced to
// work outside every span, and the report could only say "worst frame ran:
// (no instrumented work)". Without this, raising the client's polyline point
// budget would be a guess with no way to check it.

import 'package:flutter/rendering.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/perf_subtree.dart';
import 'package:traxjourney_client/src/core/perf_timing.dart';

void main() {
  setUp(() => perfSpans
    ..resetSession()
    ..enabled = true);
  tearDown(() => perfSpans
    ..resetSession()
    ..enabled = true);

  group('per-frame aggregation', () {
    test('several calls in one frame become ONE sample, summed', () {
      // paint() can run more than once per frame (a repaint boundary
      // flushing more than one layer). Counting each call separately would
      // answer "how many paints" when the question is "what did this frame
      // cost".
      perfSpans
        ..recordFrameSpan('map_paint', 3.0, 'frame-1')
        ..recordFrameSpan('map_paint', 4.0, 'frame-1')
        ..recordFrameSpan('map_paint', 5.0, 'frame-1');

      final s = perfSpans.frameSpans['map_paint']!;
      expect(s.n, 1);
      expect(s.total, 12.0);
      expect(s.worst, 12.0);
    });

    test('a new frame id closes the previous frame', () {
      perfSpans
        ..recordFrameSpan('map_paint', 3.0, 'frame-1')
        ..recordFrameSpan('map_paint', 4.0, 'frame-1')
        ..recordFrameSpan('map_paint', 20.0, 'frame-2');

      final s = perfSpans.frameSpans['map_paint']!;
      expect(s.n, 2);
      expect(s.total, 27.0);
      expect(s.worst, 20.0, reason: 'the worst FRAME, not the worst call');
    });

    test('names are kept apart', () {
      perfSpans
        ..recordFrameSpan('map_layout', 1.0, 'f')
        ..recordFrameSpan('map_paint', 2.0, 'f');
      expect(perfSpans.frameSpans['map_layout']!.total, 1.0);
      expect(perfSpans.frameSpans['map_paint']!.total, 2.0);
    });

    test('storage is O(names), not O(frames)', () {
      // The payload cache and the thumbnail cache were both fixed for
      // unbounded growth in this same investigation; a per-call sample list
      // recorded at 60 Hz would be the third instance.
      for (var i = 0; i < 5000; i++) {
        perfSpans.recordFrameSpan('map_paint', 1.0, 'frame-$i');
      }
      expect(perfSpans.frameSpans, hasLength(1));
      expect(perfSpans.frameSpans['map_paint']!.n, 5000);
    });

    test('the frame still accumulating is flushed before it is read', () {
      perfSpans.recordFrameSpan('map_paint', 7.0, 'only-frame');
      expect(perfSpans.frameSpans['map_paint']!.n, 1,
          reason: 'the last frame of a session must not be dropped');
    });

    test('a disabled recorder records nothing', () {
      perfSpans.enabled = false;
      perfSpans.recordFrameSpan('map_paint', 9.0, 'f');
      expect(perfSpans.frameSpans, isEmpty);
    });

    test('frame spans survive reset() and clear on resetSession()', () {
      // reset() runs at the start of every load. Clearing map layout/paint
      // there would erase the evidence of the very rebuild worth diagnosing —
      // the same reasoning that already keeps gesture frames across a load.
      perfSpans.recordFrameSpan('map_paint', 5.0, 'f');
      perfSpans.reset();
      expect(perfSpans.frameSpans, isNotEmpty);
      perfSpans.resetSession();
      expect(perfSpans.frameSpans, isEmpty);
    });

    test('the worst frame context names framework layout/paint', () {
      // The decisive line of the last device report was "worst frame ran:
      // (no instrumented work)". It can now say map_paint instead.
      perfSpans.recordFrameSpan('map_paint', 5.0, 'f');
      perfSpans.flushFrameSpans();
      expect(perfSpans.blockingSpans, isEmpty,
          reason: 'frame spans must not pollute the UI-isolate stall bucket, '
              'whose samples are a per-call list');
    });
  });

  group('the report', () {
    test('prints points drawn next to the milliseconds they cost', () {
      final line = perfMapFrameLine(
        {'map_paint': (n: 100, total: 250.0, worst: 41.0)},
        {'rendered_points': '13273', 'markers': '586'},
      );
      expect(line, contains('13273'));
      expect(line, contains('586'));
      expect(line, contains('worst=41.0ms'));
      expect(line, contains('avg=2.50ms'));
    });

    test('worst-first, so the line that matters is at the top', () {
      final line = perfMapFrameLine({
        'map_lines_paint': (n: 1, total: 2.0, worst: 2.0),
        'map_markers_layout': (n: 1, total: 90.0, worst: 90.0),
      }, const {});
      expect(line.indexOf('map_markers_layout'),
          lessThan(line.indexOf('map_lines_paint')));
    });

    test('nothing recorded prints nothing', () {
      expect(perfMapFrameLine(const {}, const {}), isEmpty);
    });

    test('the full report carries the map frame line', () {
      final text = perfFullReport(
        const {},
        {
          'fetch_geo_lod': [10.0]
        },
        {'rendered_points': '6029'},
        frameSpans: {'map_paint': (n: 10, total: 100.0, worst: 30.0)},
      );
      expect(text, contains('map frame cost'));
      expect(text, contains('6029'));
    });
  });

  group('RenderPerfSubtree', () {
    testWidgets('records layout and paint of the subtree it wraps',
        (tester) async {
      await tester.pumpWidget(const Directionality(
        textDirection: TextDirection.ltr,
        child: PerfSubtree(
          name: 'map',
          child: SizedBox(width: 40, height: 40, child: ColoredBox(color: Color(0xFF000000))),
        ),
      ));
      expect(perfSpans.frameSpans.keys, containsAll(['map_layout', 'map_paint']));
    });

    testWidgets('is layout-transparent', (tester) async {
      // "Do not change any rendering behaviour" — the child must occupy
      // exactly the box it would have without the wrapper.
      await tester.pumpWidget(const Directionality(
        textDirection: TextDirection.ltr,
        child: Center(
          child: PerfSubtree(
            name: 'map',
            child: SizedBox(width: 123, height: 45, child: Placeholder()),
          ),
        ),
      ));
      expect(tester.getSize(find.byType(PerfSubtree)), const Size(123, 45));
      expect(tester.getSize(find.byType(Placeholder)), const Size(123, 45));
      expect(tester.getTopLeft(find.byType(Placeholder)),
          tester.getTopLeft(find.byType(PerfSubtree)));
    });

    testWidgets('a disabled recorder still lays out and paints', (tester) async {
      perfSpans.enabled = false;
      await tester.pumpWidget(const Directionality(
        textDirection: TextDirection.ltr,
        child: Center(
          child: PerfSubtree(
            name: 'map',
            child: SizedBox(width: 10, height: 10, child: Placeholder()),
          ),
        ),
      ));
      expect(tester.getSize(find.byType(Placeholder)), const Size(10, 10));
      expect(perfSpans.frameSpans, isEmpty);
    });

    testWidgets('renaming updates the render object in place', (tester) async {
      Future<void> pump(String name) => tester.pumpWidget(Directionality(
            textDirection: TextDirection.ltr,
            child: PerfSubtree(
              name: name,
              child: const SizedBox(width: 10, height: 10),
            ),
          ));
      await pump('a');
      await pump('b');
      final ro = tester.renderObject<RenderPerfSubtree>(find.byType(PerfSubtree));
      expect(ro.spanName, 'b');
      expect(ro, isA<RenderProxyBox>());
    });
  });
}
