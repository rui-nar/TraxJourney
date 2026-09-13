import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/perf_timing.dart';

/// The frame-timing recorder itself needs a real frame pipeline, but its pure
/// reporting helpers (percentile + summary line) are unit-testable so the
/// numbers we read off during a scroll measurement are trustworthy.
void main() {
  group('perfPercentile', () {
    test('empty list yields 0', () {
      expect(perfPercentile(const [], 50), 0);
    });

    test('nearest-rank percentile over a sorted list', () {
      final s = [for (var i = 1; i <= 100; i++) i.toDouble()]; // 1..100
      expect(perfPercentile(s, 50), 51); // round(0.5 * 99) = 50 → element[50]=51
      expect(perfPercentile(s, 90), 90); // round(0.9 * 99) = 89 → element[89]=90
      expect(perfPercentile(s, 99), 99); // round(0.99 * 99) = 98 → element[98]=99
      expect(perfPercentile(s, 100), 100);
    });

    test('single element returns that element for any percentile', () {
      expect(perfPercentile(const [7.0], 50), 7.0);
      expect(perfPercentile(const [7.0], 99), 7.0);
    });
  });

  group('perfSummaryLine', () {
    test('no frames is reported explicitly', () {
      expect(perfSummaryLine(const [], const []), contains('no frames'));
    });

    test('counts frames over budget on either thread as janky', () {
      // 3 smooth frames + 1 build-bound (20ms) + 1 raster-bound (33ms).
      final build = [5.0, 6.0, 7.0, 20.0, 8.0];
      final raster = [4.0, 5.0, 6.0, 7.0, 33.0];
      final line = perfSummaryLine(build, raster);
      expect(line, contains('frames=5'));
      expect(line, contains('janky'));
      expect(line, contains('=2/5')); // two frames blew the 16.7ms budget
    });

    test('all-smooth window reports zero janky', () {
      final line = perfSummaryLine(const [5, 6, 7], const [4, 5, 6]);
      expect(line, contains('=0/3'));
    });
  });
}
