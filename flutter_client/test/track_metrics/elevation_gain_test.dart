// Pins the Dart elevation-gain pipeline and the dropout-sentinel rule to the
// vectors produced by the Python (issue #366): src/models/track_edit.py and
// alembic/versions/c4a9e1f70b38_repair_elevation_dropout_sentinel.py.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/track_metrics/elevation_gain.dart';

import 'vectors.dart';

void main() {
  group('constants', () {
    test('every Python constant is ported with the same value', () {
      final ported = elevationGainConstants;
      final expected = vectorConstants();
      expect(ported.keys.toSet(), expected.keys.toSet());
      for (final name in expected.keys) {
        final want = expected[name];
        final got = ported[name];
        if (want is Map) {
          // JSON object keys are strings; the Dart map is keyed by int.
          expectSame({for (final e in (got as Map).entries) '${e.key}': e.value},
              want, name);
        } else {
          expectSame(got, want, name);
        }
      }
    });
  });

  group('run_stride', () {
    checkSection('run_stride', (i) => runStride(doubles(i['elevations'])));
  });

  group('noise_estimate', () {
    checkSection('noise_estimate', (i) {
      final r = noiseEstimate(doubles(i['elevations']));
      return {'sigma': r.sigma, 'stride': r.stride};
    });
  });

  group('smooth_elevations', () {
    checkSection('smooth_elevations', (i) {
      final r = smoothElevations(
        doubles(i['elevations']),
        i['distances_km'] == null ? null : doubles(i['distances_km']),
        (i['sigma'] as num).toDouble(),
        i['stride'] as int,
      );
      return {'smoothed': r.smoothed, 'window': r.window};
    });
  });

  group('noise_threshold', () {
    checkSection(
      'noise_threshold',
      (i) => noiseThreshold((i['sigma'] as num).toDouble(), i['stride'] as int,
          (i['window'] as num).toDouble()),
    );
  });

  group('elevation_gain', () {
    checkSection(
      'elevation_gain',
      (i) => elevationGain(doubles(i['elevations']),
          i['distances_km'] == null ? null : doubles(i['distances_km'])),
    );
  });

  group('sentinel_mask', () {
    checkSection('sentinel_mask',
        (i) => sentinelMask(doubles(i['elevations']), doubles(i['distances_km'])));
  });
}
