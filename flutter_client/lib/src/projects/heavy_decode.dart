/// Off-UI-isolate decoding for the project payloads big enough to drop frames
/// (issue #292).
///
/// The ANR work up to #290 moved every *derived* computation off the UI
/// isolate — `buildFullTrackResult`, `hitTestMapTap`, `computeElevationSpots`,
/// `decimatePolylinePoints`. Those are the cheap half. The `jsonDecode` that
/// produces their input ran inline in `ApiClient._handle` for every endpoint,
/// including the ~12 MB details payload and the full-res geo, with no
/// threshold guard at all — the largest unguarded main-thread block left in
/// the app. See docs/plans/PERF_MAP_LOAD.md.
///
/// The functions here are top-level and take plain byte/JSON input so they can
/// run under [compute]. Note there is deliberately no `dart:isolate` import:
/// `TransferableTypedData` would move the request bytes to the worker without
/// a copy, but it does not exist on web, and a memcpy of the response body is
/// a rounding error next to parsing it.
library;

import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:latlong2/latlong.dart';

import '../map/polyline_decoder.dart';
import 'map_geometry_memo.dart';
import 'polyline_decimation.dart';

/// Below this many bytes, decoding inline beats hopping isolates.
///
/// Spawning a worker costs ~1–3 ms; at the ~50–100 MB/s `jsonDecode` manages
/// on a mid-range phone, a payload this size parses in about the same time.
/// Above it, the parse is what blows the frame budget and the hop pays for
/// itself. Mirrors the established threshold pattern in this codebase —
/// `kInlineFullTrackThreshold`, `_kInlineHitTestThreshold`,
/// `_kInlineComputeThreshold`.
const int kInlineDecodeThresholdBytes = 256 * 1024;

/// Decodes UTF-8 JSON [bytes] into a Map. Pure; the [compute] entrypoint for
/// every payload that needs no further transformation.
Map<String, dynamic> decodeJsonMapBytes(Uint8List bytes) =>
    jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;

/// Decodes a full-res geo payload **and** expands its encoded activity
/// polylines, in a single pass.
///
/// Fusing the two is the point: they used to run as two separate inline steps
/// (`jsonDecode` in `ApiClient._handle`, then `expandEncodedActivities` in
/// `ProjectService.getGeo`), so a large trip paid for a full JSON parse and a
/// Google-polyline decode over 300k+ points back to back on the UI isolate.
Map<String, dynamic> decodeGeoBytes(Uint8List bytes) =>
    expandEncodedActivities(decodeJsonMapBytes(bytes));

/// Decodes [bytes] as a JSON map, on a background isolate when the payload is
/// big enough to be worth one.
Future<Map<String, dynamic>> decodeJsonMapOffIsolate(Uint8List bytes) =>
    bytes.length <= kInlineDecodeThresholdBytes
        ? Future.value(decodeJsonMapBytes(bytes))
        : compute(decodeJsonMapBytes, bytes);

/// [decodeGeoBytes], plus the per-feature coordinate->LatLng conversion the
/// map would otherwise do on its first build after the swap.
///
/// Both halves are produced together so they ride back on one hop, and the
/// coords lists and their converted points arrive as a single object graph —
/// which is what lets [seedCoordsLatLng] key the cache on coords identity
/// across the isolate boundary. Top-level and pure so it can run under
/// [compute]. Parallel to `geo['features']`, one entry per feature, empty
/// where a feature has no usable geometry.
({
  Map<String, dynamic> geo,
  List<List<LatLng>> points,
  List<LatLng?> midpoints,
  List<List<LatLng>?> decimated,
  List<Float64List?> flat,
}) decodeGeoWithPoints(Uint8List bytes) {
  final geo = decodeGeoBytes(bytes);
  final features = geo['features'];
  final points = <List<LatLng>>[];
  final midpoints = <LatLng?>[];
  // The track builder's input, flattened here rather than at the isolate
  // boundary — issue #337. Null for every feature the track is not built from,
  // so no buffer is produced (or retained) for a segment or an id-less
  // feature; the skip decision is [trackFeatureCoords]', shared with
  // `flattenGeoCoords`, which is the only reader.
  final flat = <Float64List?>[];
  if (features is List) {
    for (final f in features) {
      final coords = f is Map ? (f['geometry'] as Map? ?? {})['coordinates'] : null;
      points.add(coords is List ? coordsToLatLng(coords) : const []);
      midpoints.add(coords is List ? arcMidpoint(coords) : null);
      final t = trackFeatureCoords(f);
      flat.add(t == null ? null : flatCoords(t.coords));
    }
  }

  // Decimation, over exactly the features the map will draw — the >= 2 point
  // filter mirrors _buildPolylineSpecs, so the aggregate budget is shared out
  // the same way it would have been on the UI isolate.
  final drawIndexes = <int>[];
  final lines = <List<(double, double)>>[];
  for (var i = 0; i < points.length; i++) {
    if (points[i].length < 2) continue;
    drawIndexes.add(i);
    lines.add([for (final p in points[i]) (p.latitude, p.longitude)]);
  }
  final reduced =
      decimatePolylinePoints((lines: lines, budget: kMaxTotalPolylinePoints));
  final decimated = List<List<LatLng>?>.filled(points.length, null);
  for (var k = 0; k < drawIndexes.length; k++) {
    decimated[drawIndexes[k]] = identical(reduced[k], lines[k])
        ? points[drawIndexes[k]] // under budget: unchanged, reuse the list
        : [for (final (lat, lon) in reduced[k]) LatLng(lat, lon)];
  }

  return (
    geo: geo,
    points: points,
    midpoints: midpoints,
    decimated: decimated,
    flat: flat,
  );
}

/// [decodeGeoBytes] on a background isolate when the payload is big enough,
/// with the map's coordinate cache pre-warmed from the same hop.
///
/// Seeding matters because both derivations are O(total points) and happen
/// exactly once per geo swap: measured on a 100-activity / 500k-point trip at
/// ~83 ms of UI-isolate stall for the conversion and ~45 ms for the arc
/// midpoints (and more again on a mid-range phone), against ~0.1 ms once
/// warm. Since #293 collapsed the staged reveal into a single swap, these
/// were the O(points) costs left on the UI isolate in the geo path.
///
/// **Native only.** On Flutter web [compute] runs its callback inline on the
/// main thread, so there the conversion and the midpoint pass still happen on
/// the UI thread — front-loaded into the decode rather than removed from it.
/// Web's answer is to stop shipping payloads this size at all (Phase 4 of
/// docs/plans/PERF_MAP_LOAD.md), not more [compute] calls.
///
/// The trip's *render* geometry deliberately stays `List<LatLng>`, which the
/// original plan proposed replacing with `Float64List` buffers: flutter_map's
/// `Polyline` takes `List<LatLng>`, so typed buffers would have to be
/// materialised back into exactly this list on the render path — moving the
/// cost rather than removing it. See docs/plans/PERF_MAP_LOAD.md.
///
/// The *track builder's* geometry is a different consumer with the opposite
/// need — it crosses an isolate boundary, where one Dart list per point is
/// the expensive shape — so its `Float64List` per activity is produced here
/// too (issue #337) and seeded alongside the rest. It rides back on the same
/// zero-copy hop, so it costs the UI isolate nothing; before, the UI isolate
/// walked every coordinate to build it, measured at 22.6 ms.
Future<Map<String, dynamic>> decodeGeoOffIsolate(Uint8List bytes) async {
  final decoded = bytes.length <= kInlineDecodeThresholdBytes
      ? decodeGeoWithPoints(bytes)
      : await compute(decodeGeoWithPoints, bytes);
  final features = decoded.geo['features'];
  if (features is List) {
    for (var i = 0; i < features.length && i < decoded.points.length; i++) {
      final f = features[i];
      final coords = f is Map ? (f['geometry'] as Map? ?? {})['coordinates'] : null;
      if (coords is! List) continue;
      seedCoordsLatLng(coords, decoded.points[i]);
      final mid = i < decoded.midpoints.length ? decoded.midpoints[i] : null;
      if (mid != null) seedArcMidpoint(coords, mid);
      final dec = i < decoded.decimated.length ? decoded.decimated[i] : null;
      if (dec != null) seedDecimatedLatLng(coords, dec);
      final fl = i < decoded.flat.length ? decoded.flat[i] : null;
      if (fl != null) seedFlatCoords(coords, fl);
    }
  }
  return decoded.geo;
}

/// Expand any activity feature carrying a Google-encoded `polyline` property
/// into a standard GeoJSON `coordinates` array (`[[lon, lat], …]`). No-op for
/// features that already have coordinates (segments, straight-line fallbacks,
/// and the share endpoint's expanded responses).
///
/// Lives here rather than on `ProjectService` so it is a top-level function
/// reachable from an isolate entrypoint; `ProjectService.expandEncodedActivities`
/// still delegates to it, keeping that call site's contract unchanged.
Map<String, dynamic> expandEncodedActivities(Map<String, dynamic> geo) {
  final features = geo['features'];
  if (features is! List) return geo;
  for (final f in features) {
    if (f is! Map) continue;
    final props = f['properties'];
    if (props is! Map) continue;
    final enc = props['polyline'];
    if (enc is! String || enc.isEmpty) continue;
    final geom = f['geometry'];
    if (geom is! Map) continue;
    final existing = geom['coordinates'];
    if (existing is List && existing.isNotEmpty) continue; // already expanded
    // Validate every decoded point: on the web the polyline decode can yield
    // out-of-range values, and a single bad latitude crashes flutter_map's
    // bounds assertion. Drop non-finite / out-of-range points rather than
    // ever handing them to the map.
    final coords = <List<double>>[];
    for (final p in decodePolyline(enc)) {
      if (p.lat.isFinite && p.lon.isFinite &&
          p.lat >= -90 && p.lat <= 90 && p.lon >= -180 && p.lon <= 180) {
        coords.add([p.lon, p.lat]);
      }
    }
    if (coords.length >= 2) geom['coordinates'] = coords;
  }
  return geo;
}
