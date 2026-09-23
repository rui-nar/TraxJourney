// Regression tests for the main-map pan ANR: with no selection (the default
// view right after a trip opens), map_panel.dart draws every activity's full
// track simultaneously, and flutter_map's PolylineLayer re-walks every point
// of every polyline overlapping the viewport on every single camera frame —
// confirmed via flutter_map 8.3.1's own source, and confirmed as the actual
// cause via an Android ANR trace ("Waited 5001ms for MotionEvent" with the
// app's main thread pegged near 100% CPU throughout a pan on a large trip).
// decimatePolylinePoints caps the AGGREGATE point count handed to
// PolylineLayer regardless of trip size, which flutter_map's own per-line,
// zoom-adaptive simplificationTolerance has no way to do on its own.

import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/map_panel.dart';

List<(double, double)> _line(int n, {double lonStart = 0}) =>
    [for (var i = 0; i < n; i++) (i.toDouble(), lonStart + i.toDouble())];

void main() {
  group('totalPolylinePoints', () {
    test('sums points across every line', () {
      expect(totalPolylinePoints([_line(3), _line(5)]), 8);
    });

    test('empty input is zero', () {
      expect(totalPolylinePoints([]), 0);
    });
  });

  group('decimatePolylinePoints', () {
    test('returns the lines unchanged when already under budget', () {
      final lines = [_line(10), _line(20)];
      final result = decimatePolylinePoints((lines: lines, budget: 100));
      expect(result, same(lines));
    });

    test('empty input returns empty output', () {
      expect(decimatePolylinePoints((lines: [], budget: 100)), isEmpty);
    });

    test('caps the aggregate at roughly the budget when over it', () {
      final lines = [_line(5000), _line(5000), _line(5000)];
      final result = decimatePolylinePoints((lines: lines, budget: 3000));
      final total = totalPolylinePoints(result);
      expect(total, lessThanOrEqualTo(3000 + result.length));
      expect(total, greaterThan(2000));
    });

    test('every output line keeps its exact first and last point', () {
      final lines = [_line(2000, lonStart: 0), _line(500, lonStart: 1000)];
      final result = decimatePolylinePoints((lines: lines, budget: 300));
      for (var i = 0; i < lines.length; i++) {
        expect(result[i].first, lines[i].first);
        expect(result[i].last, lines[i].last);
      }
    });

    test('a longer line gets a larger share of the budget than a short one',
        () {
      final lines = [_line(9000, lonStart: 0), _line(1000, lonStart: 10000)];
      final result = decimatePolylinePoints((lines: lines, budget: 1000));
      expect(result[0].length, greaterThan(result[1].length));
    });

    test('lines of 2 points or fewer are never further reduced', () {
      final lines = [
        [(0.0, 0.0), (1.0, 1.0)],
        _line(9000),
      ];
      final result = decimatePolylinePoints((lines: lines, budget: 500));
      expect(result[0], lines[0]);
    });
  });

  // ── The budget as a safety valve, not a resolution policy (issue #276) ──
  //
  // At 6000 this constant bound *below* the server's own pixel-accurate
  // output: a 219-activity trip's simplified payload was 13,273 coordinates
  // and the map drew 6,029 of them, so the track on screen was about twice as
  // coarse as the line the server had already built and paid to transfer.
  group('kMaxTotalPolylinePoints', () {
    test('does not bind on the measured pixel-accurate whole-trip payload', () {
      // 219 activities, 13,273 coordinates: the real numbers from the device
      // report that prompted this. Nothing may be discarded.
      final lines = [for (var i = 0; i < 219; i++) _line(61, lonStart: i * 1000.0)];
      final total = totalPolylinePoints(lines);
      expect(total, greaterThan(13000));
      expect(total, lessThan(kMaxTotalPolylinePoints),
          reason: 'the server decides resolution now; this must not undo it');
      final result =
          decimatePolylinePoints((lines: lines, budget: kMaxTotalPolylinePoints));
      expect(identical(result, lines), isTrue,
          reason: 'under budget, the input is passed straight through');
    });

    test('still catches the full-resolution fallback payload', () {
      // getGeo() — offline, an older server, or a client-built E2EE trip —
      // hands the renderer 1,465,345 points on that same trip.
      final lines = [for (var i = 0; i < 219; i++) _line(6700, lonStart: i * 1000.0)];
      expect(totalPolylinePoints(lines), greaterThan(1400000));
      final result =
          decimatePolylinePoints((lines: lines, budget: kMaxTotalPolylinePoints));
      expect(totalPolylinePoints(result),
          lessThanOrEqualTo(kMaxTotalPolylinePoints + lines.length));
    });
  });
}
