/// Dart port of the elevation-gain pipeline in src/models/track_edit.py
/// (`elevation_gain` and its helpers) and of `_sentinel_mask` in
/// alembic/versions/c4a9e1f70b38_repair_elevation_dropout_sentinel.py.
///
/// Python is the source of truth. The constants keep their Python names, and
/// tests/test_track_metrics_parity.py parses them out of this file, so each is
/// a `const` literal. See flutter_client/test/fixtures/track_metrics_vectors.json
/// for the vectors every function here is pinned by.
// ignore_for_file: constant_identifier_names
library;

import 'dart:math' as math;

/// Mirrors the constants of the same name in src/models/track_edit.py; see
/// there for what each is for.
const double ELEV_SMOOTH_SPAN_M = 60.0;
const double ELEV_SMOOTH_MAX_SPAN_M = 240.0;
const double ELEV_SMOOTH_TARGET_SIGMA_M = 1.0;
const double ELEV_NOISE_SIGMA_MULTIPLE = 4.5;
const double ELEV_GAIN_THRESHOLD_MIN_M = 1.0;
const double ELEV_GAIN_THRESHOLD_MAX_M = 20.0;
const _NOISE_DIFFERENCE_ORDERS = <int>[3, 4, 5];
const int _NOISE_MIN_DIFFERENCES = 8;
const int _NOISE_MAX_RUN_STRIDE = 16;
const int _NOISE_MEDIAN_SAMPLE_CAP = 20000;

/// Mirrors the constants of the same name in the migration c4a9e1f70b38.
const double _SENTINEL_MIN_GRADE = 1.0;
const double _SENTINEL_MIN_STEP_M = 5.0;

/// `math.comb(2k, k)` as a double (exact for the small orders used here).
double _centralBinomial(int k) {
  var c = 1.0;
  for (var i = 1; i <= k; i++) {
    c = c * (k + i) / i;
  }
  return c;
}

/// Mirrors `_NOISE_DIFFERENCE_SCALE`: `1.4826 / sqrt(C(2k, k))` per order.
final Map<int, double> _noiseDifferenceScale = {
  for (final k in _NOISE_DIFFERENCE_ORDERS)
    k: 1.4826 / math.sqrt(_centralBinomial(k)),
};

/// Every constant above under its Python name, so tests can pin them to the
/// vectors' `constants` section (the private ones are not importable).
Map<String, Object> get elevationGainConstants => {
      'ELEV_SMOOTH_SPAN_M': ELEV_SMOOTH_SPAN_M,
      'ELEV_SMOOTH_MAX_SPAN_M': ELEV_SMOOTH_MAX_SPAN_M,
      'ELEV_SMOOTH_TARGET_SIGMA_M': ELEV_SMOOTH_TARGET_SIGMA_M,
      'ELEV_NOISE_SIGMA_MULTIPLE': ELEV_NOISE_SIGMA_MULTIPLE,
      'ELEV_GAIN_THRESHOLD_MIN_M': ELEV_GAIN_THRESHOLD_MIN_M,
      'ELEV_GAIN_THRESHOLD_MAX_M': ELEV_GAIN_THRESHOLD_MAX_M,
      '_NOISE_DIFFERENCE_ORDERS': _NOISE_DIFFERENCE_ORDERS,
      '_NOISE_DIFFERENCE_SCALE': _noiseDifferenceScale,
      '_NOISE_MIN_DIFFERENCES': _NOISE_MIN_DIFFERENCES,
      '_NOISE_MAX_RUN_STRIDE': _NOISE_MAX_RUN_STRIDE,
      '_NOISE_MEDIAN_SAMPLE_CAP': _NOISE_MEDIAN_SAMPLE_CAP,
      '_SENTINEL_MIN_GRADE': _SENTINEL_MIN_GRADE,
      '_SENTINEL_MIN_STEP_M': _SENTINEL_MIN_STEP_M,
    };

/// Mirrors `_run_stride`: samples per distinct altitude reading, as a median
/// run length, capped at `_NOISE_MAX_RUN_STRIDE`.
int runStride(List<double> elevations) {
  final counts = List<int>.filled(_NOISE_MAX_RUN_STRIDE + 1, 0);
  var runs = 0;
  var length = 1;
  for (var i = 1; i < elevations.length; i++) {
    if (elevations[i - 1] == elevations[i]) {
      length += 1;
    } else {
      counts[length < _NOISE_MAX_RUN_STRIDE ? length : _NOISE_MAX_RUN_STRIDE] +=
          1;
      runs += 1;
      length = 1;
    }
  }
  counts[length < _NOISE_MAX_RUN_STRIDE ? length : _NOISE_MAX_RUN_STRIDE] += 1;
  runs += 1;

  var seen = 0;
  for (var stride = 1; stride <= _NOISE_MAX_RUN_STRIDE; stride++) {
    seen += counts[stride];
    if (seen * 2 > runs) return stride;
  }
  return _NOISE_MAX_RUN_STRIDE;
}

/// Mirrors `_noise_estimate`: per-sample sensor noise in metres, plus the run
/// stride it was read at. The smallest estimate over difference orders 3 to 5;
/// 0.0 for a series too short to have `_NOISE_MIN_DIFFERENCES` differences.
({double sigma, int stride}) noiseEstimate(List<double> elevations) {
  final stride = runStride(elevations);
  var differences = elevations;
  double? estimate;
  final lastOrder = _NOISE_DIFFERENCE_ORDERS.last;
  for (var order = 1; order <= lastOrder; order++) {
    final next = <double>[];
    for (var i = 0; i + stride < differences.length; i++) {
      next.add(differences[i + stride] - differences[i]);
    }
    differences = next;
    if (differences.length < _NOISE_MIN_DIFFERENCES) break;
    if (!_NOISE_DIFFERENCE_ORDERS.contains(order)) continue;
    final step = 1 + differences.length ~/ _NOISE_MEDIAN_SAMPLE_CAP;
    final magnitudes = <double>[
      for (var i = 0; i < differences.length; i += step) differences[i].abs(),
    ]..sort();
    final middle = magnitudes[magnitudes.length ~/ 2];
    final sigma = _noiseDifferenceScale[order]! * middle;
    estimate = estimate == null ? sigma : math.min(estimate, sigma);
  }
  return (sigma: estimate ?? 0.0, stride: stride);
}

/// Mirrors `_smooth_elevations`: a centred moving average over travel, widened
/// to suit the measured noise. Returns the smoothed series and the mean number
/// of samples its windows held. Without usable [distancesKm] the series is
/// returned untouched with a window of 1.
({List<double> smoothed, double window}) smoothElevations(
  List<double> elevations,
  List<double>? distancesKm,
  double sigma,
  int stride,
) {
  final n = elevations.length;
  if (n < 3 || distancesKm == null || distancesKm.isEmpty || distancesKm.length != n) {
    return (smoothed: List<double>.of(elevations), window: 1.0);
  }

  final gaps = <double>[
    for (var i = 1; i < n; i++) distancesKm[i] - distancesKm[i - 1],
  ]..sort();
  final medianGapM = gaps[gaps.length ~/ 2] * 1000.0;
  final ratio = sigma / ELEV_SMOOTH_TARGET_SIGMA_M;
  final wanted = stride * (ratio * ratio) * medianGapM;
  final span =
      math.max(ELEV_SMOOTH_SPAN_M, math.min(ELEV_SMOOTH_MAX_SPAN_M, wanted));
  final halfKm = (span / 2.0) / 1000.0;

  // Prefix sums so each window costs one subtraction.
  final prefix = <double>[0.0];
  for (final value in elevations) {
    prefix.add(prefix.last + value);
  }

  final out = <double>[];
  var totalWidth = 0;
  var lo = 0, hi = 0;
  for (var i = 0; i < n; i++) {
    while (distancesKm[i] - distancesKm[lo] > halfKm) {
      lo += 1;
    }
    while (hi + 1 < n && distancesKm[hi + 1] - distancesKm[i] <= halfKm) {
      hi += 1;
    }
    final width = hi + 1 - lo;
    totalWidth += width;
    out.add((prefix[hi + 1] - prefix[lo]) / width);
  }
  return (smoothed: out, window: totalWidth / n);
}

/// Mirrors `_noise_threshold`: the hysteresis band, sized from the noise
/// smoothing did not remove, clamped to
/// [ELEV_GAIN_THRESHOLD_MIN_M, ELEV_GAIN_THRESHOLD_MAX_M].
double noiseThreshold(double sigma, int stride, double window) {
  final residual = sigma / math.sqrt(math.max(1.0, window / stride));
  return math.min(
    ELEV_GAIN_THRESHOLD_MAX_M,
    math.max(
        ELEV_GAIN_THRESHOLD_MIN_M, ELEV_NOISE_SIGMA_MULTIPLE * residual),
  );
}

/// Mirrors `elevation_gain`: total ascent in metres over an ordered elevation
/// series. [distancesKm] is the cumulative distance of each sample.
double elevationGain(List<double> elevations, [List<double>? distancesKm]) {
  if (elevations.length < 2) return 0.0;
  final noise = noiseEstimate(elevations);
  final smoothing =
      smoothElevations(elevations, distancesKm, noise.sigma, noise.stride);
  final smoothed = smoothing.smoothed;
  final threshold = noiseThreshold(noise.sigma, noise.stride, smoothing.window);
  var gain = 0.0;
  var ref = smoothed[0];
  for (var i = 1; i < smoothed.length; i++) {
    final value = smoothed[i];
    if (value - ref >= threshold) {
      gain += value - ref;
      ref = value;
    } else if (ref - value >= threshold) {
      ref = value;
    }
  }
  return gain;
}

/// Mirrors `_sentinel_mask` of migration c4a9e1f70b38: true where an exact 0.0
/// is the pre-#374 "no reading" sentinel rather than a real reading. Each run of
/// zeros is judged by the grade of the step to its neighbour on each side that
/// exists; a run is a dropout when the largest grade is strictly above
/// `_SENTINEL_MIN_GRADE`. Every sample is false when the lists differ in length.
List<bool> sentinelMask(List<double> elevations, List<double> distancesKm) {
  final n = elevations.length;
  final mask = List<bool>.filled(n, false);
  if (distancesKm.length != n) return mask;
  var i = 0;
  while (i < n) {
    if (elevations[i] != 0.0) {
      i += 1;
      continue;
    }
    var runEnd = i;
    while (runEnd < n && elevations[runEnd] == 0.0) {
      runEnd += 1;
    }
    final grades = <double>[];
    for (final (idx, edge) in [(i - 1, i), (runEnd, runEnd - 1)]) {
      if (idx < 0 || idx >= n) continue;
      final step = elevations[idx].abs();
      if (step < _SENTINEL_MIN_STEP_M) continue;
      final runM = (distancesKm[idx] - distancesKm[edge]).abs() * 1000.0;
      grades.add(runM > 0 ? step / runM : double.infinity);
    }
    if (grades.isNotEmpty && grades.reduce(math.max) > _SENTINEL_MIN_GRADE) {
      for (var j = i; j < runEnd; j++) {
        mask[j] = true;
      }
    }
    i = runEnd;
  }
  return mask;
}
