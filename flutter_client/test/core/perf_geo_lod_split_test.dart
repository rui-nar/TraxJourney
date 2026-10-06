// Per-fetch timing of the simplified geometry (issue #401): the server's
// Server-Timing total, the wait around it, and the payload-size distribution.

import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/perf_timing.dart';

Future<({Uint8List bytes, Map<String, String> headers})> _res(
        int n, Map<String, String> headers,
        {Duration delay = Duration.zero}) =>
    Future.delayed(delay, () => (bytes: Uint8List(n), headers: headers));

void main() {
  setUp(() => PerfSpans.instance
    ..enabled = true
    ..resetSession());

  group('perfServerTimingTotalMs', () {
    test('reads the total of a build', () {
      expect(
          perfServerTimingTotalMs(
              'load;dur=1.5, build;dur=20.0, gzip;dur=3.2, total;dur=24.9'),
          24.9);
    });

    test('reads the total of a cache hit', () {
      expect(perfServerTimingTotalMs('cache;desc=hit, total;dur=0.8'), 0.8);
    });

    test('is null when missing or malformed', () {
      expect(perfServerTimingTotalMs(null), isNull);
      expect(perfServerTimingTotalMs(''), isNull);
      expect(perfServerTimingTotalMs('load;dur=1.0'), isNull);
      expect(perfServerTimingTotalMs('total'), isNull);
      expect(perfServerTimingTotalMs('total;dur=abc'), isNull);
      expect(perfServerTimingTotalMs('total;dur=-3'), isNull);
      expect(perfServerTimingTotalMs(';;,,total;dur='), isNull);
    });
  });

  group('PerfSpans.geoLodFetch', () {
    test('records server, wait and the fetch span', () async {
      final bytes = await perfSpans.geoLodFetch(() => _res(
          10, {'server-timing': 'total;dur=5.0'},
          delay: const Duration(milliseconds: 40)));
      expect(bytes.length, 10);
      final s = perfSpans.stageSpans;
      expect(s['geo_lod_server'], [5.0]);
      expect(s['geo_lod_wait']!.single, greaterThan(0));
      expect(s['fetch_geo_lod']!.length, 1);
    });

    test('wait is never negative', () async {
      await perfSpans.geoLodFetch(
          () => _res(1, {'server-timing': 'total;dur=999999'}));
      expect(perfSpans.stageSpans['geo_lod_wait'], [0.0]);
    });

    test('no header: no server sample, no error, bytes still recorded',
        () async {
      await perfSpans.geoLodFetch(() => _res(7, {}));
      final s = perfSpans.stageSpans;
      expect(s.containsKey('geo_lod_server'), isFalse);
      expect(s.containsKey('geo_lod_wait'), isFalse);
      expect(s['fetch_geo_lod']!.length, 1);
      expect(perfSpans.geoLodBytes, [7.0]);
      expect(perfSpans.failures, isEmpty);
    });

    test('bytes are recorded per fetch and HITs are counted', () async {
      await perfSpans.geoLodFetch(() => _res(1024, {'x-cache': 'MISS'}));
      await perfSpans.geoLodFetch(() => _res(2048, {'x-cache': 'HIT'}));
      await perfSpans.geoLodFetch(() => _res(4096, {'x-cache': 'HIT'}));
      expect(perfSpans.geoLodBytes, [1024.0, 2048.0, 4096.0]);
      expect(perfSpans.geoLodHits, 2);
    });

    test('a failing fetch is recorded as a failure and rethrown', () async {
      await expectLater(
          perfSpans.geoLodFetch(() => throw StateError('boom')),
          throwsStateError);
      expect(perfSpans.failures['fetch_geo_lod'], isNotNull);
      expect(perfSpans.geoLodBytes, isEmpty);
    });

    test('the report shows both spans and the payload line', () async {
      for (var i = 1; i <= 3; i++) {
        await perfSpans.geoLodFetch(() => _res(
            i * 1024, {'server-timing': 'total;dur=$i.0', 'x-cache': 'HIT'}));
      }
      final text = perfSpans.buildReport();
      final server =
          text.split('\n').firstWhere((l) => l.contains('geo_lod_server'));
      final wait =
          text.split('\n').firstWhere((l) => l.contains('geo_lod_wait'));
      for (final l in [server, wait]) {
        expect(l, contains('p50='));
        expect(l, contains('p90='));
      }
      expect(text, contains('geo_lod payload  n=3  p50=2.0KB  p90=3.0KB'));
      expect(text, contains('hits=3'));
    });

    test('reset clears the per-load samples', () async {
      await perfSpans.geoLodFetch(() => _res(5, {'x-cache': 'HIT'}));
      perfSpans.reset();
      expect(perfSpans.geoLodBytes, isEmpty);
      expect(perfSpans.geoLodHits, 0);
    });
  });
}
