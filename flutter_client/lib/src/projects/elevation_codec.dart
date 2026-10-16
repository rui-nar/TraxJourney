/// Decoder for the compact elevation profiles served by
/// `GET /api/projects/{name}/elevation` (issue #295).
///
/// Mirrors `src/project/elevation_codec.py`: zig-zag varint deltas in the
/// polyline alphabet, distance quantised to 1 m and elevation to 0.1 m. The
/// two must stay in lockstep, so the quantisation constants are named here
/// rather than inlined.
///
/// This exists so the client can stop fetching the full project details
/// payload purely to obtain elevation — 33 MB on a 180-day trip, ~9 s to
/// fetch and ~5 s to decode, and retained in memory afterwards. See
/// docs/plans/PERF_MAP_LOAD.md.
library;

const double _kDistScale = 1000.0; // km -> m
const double _kElevScale = 10.0; // m  -> dm

/// Decodes one activity's profile into `[[distance_km, elevation_m], ...]`.
///
/// Truncated or malformed input yields whatever decoded cleanly rather than
/// throwing: a profile is telemetry, and a bad tail must not cost an activity
/// its chart. Mirrors the server decoder's behaviour exactly.
List<List<double>> decodeElevationProfile(String encoded) {
  final out = <List<double>>[];
  var index = 0;
  var d = 0;
  var e = 0;

  // Web-safe, for the reason polyline_decoder.dart documents at length:
  // compiled to JavaScript, bitwise ops run as 32-bit and `~` returns the
  // *unsigned* complement, so `~(r >> 1)` turns every negative delta into
  // ~4.29e9. This codec's deltas go negative on any descent, so the classic
  // formulation would have made elevation charts nonsense on web while every
  // VM test passed. Multiplication for the shift, addition for the OR (the
  // 5-bit groups never overlap) and integer division for the zig-zag stay
  // exact and identical on the VM, dart2js and DDC.
  int? readDelta() {
    var shift = 0, r = 0, b = 0;
    do {
      if (index >= encoded.length) return null; // truncated
      b = encoded.codeUnitAt(index++) - 63;
      r += (b & 0x1f) * (1 << shift);
      shift += 5;
    } while (b >= 0x20);
    return (r & 1) != 0 ? -((r + 1) ~/ 2) : (r ~/ 2);
  }

  while (index < encoded.length) {
    final dd = readDelta();
    if (dd == null) return out;
    final de = readDelta();
    if (de == null) return out;
    d += dd;
    e += de;
    out.add([d / _kDistScale, e / _kElevScale]);
  }
  return out;
}

/// Decodes a whole `{activityId: encoded}` map, as the endpoint returns it.
///
/// Top-level and pure so it can run under `compute` — the point of the
/// endpoint is to keep this work off the UI isolate, and doing the decode
/// there would give most of that back.
Map<String, List<List<double>>> decodeElevationProfiles(
    Map<String, dynamic> profiles) {
  final out = <String, List<List<double>>>{};
  for (final entry in profiles.entries) {
    final value = entry.value;
    if (value is! String || value.isEmpty) continue;
    final decoded = decodeElevationProfile(value);
    if (decoded.isNotEmpty) out[entry.key] = decoded;
  }
  return out;
}
