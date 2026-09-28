/// Client-side GeoJSON builder (issue #29).
///
/// Mirrors api/geo.py's `_build_full_geo_features` and
/// src/project/repo_core.py's `_compute_low_res_geo`. Once a user has E2EE
/// enabled, the server can no longer decode an encrypted activity's geometry
/// to build the map's GeoJSON, so the client builds it itself from the
/// already-decrypted `activities`/`items` ProjectNotifier holds (see
/// `ProjectNotifier._revealActivities`). Produces the same GeoJSON Feature
/// shape the server endpoints do, so map_panel.dart doesn't need to change how
/// it *consumes* the data — only where it comes from.
library;

import 'dart:convert';

import '../map/great_circle.dart';
import '../map/polyline_decoder.dart';
import '../crypto/encrypted_display.dart';

Map<String, dynamic> _linestring(
  List<List<double>> coords,
  Map<String, dynamic> properties,
) =>
    {
      'type': 'Feature',
      'geometry': {'type': 'LineString', 'coordinates': coords},
      'properties': properties,
    };

List<List<double>> _decodeActivityPolyline(String polyline) => [
      for (final p in decodePolyline(polyline)) [p.lon, p.lat],
    ];

/// `a['start_latlng']`/`a['end_latlng']` are `[lat, lon]` once revealed.
List<double>? _asLatLng(dynamic v) {
  if (v is! List || v.length < 2) return null;
  final lat = (v[0] as num?)?.toDouble();
  final lon = (v[1] as num?)?.toDouble();
  if (lat == null || lon == null) return null;
  return [lat, lon];
}

/// Coordinates for one connecting-segment item, matching the branch order in
/// api/geo.py's `_build_full_geo_features`: a stored rail/ferry/bus polyline
/// wins when present, else a 50-point great-circle arc between the endpoints.
List<List<double>> _segmentCoords(Map segment) {
  final routeMode = segment['route_mode'] as String? ?? 'great_circle';
  final routePolyline = segment['route_polyline'] as String?;
  if ((routeMode == 'rail' || routeMode == 'ferry' || routeMode == 'bus') &&
      routePolyline != null && routePolyline.isNotEmpty) {
    final decoded = jsonDecode(routePolyline);
    if (decoded is! List) return const [];
    return [
      for (final pair in decoded)
        if (pair is List && pair.length >= 2)
          [(pair[0] as num).toDouble(), (pair[1] as num).toDouble()],
    ];
  }
  final start = segment['start'] as Map?;
  final end = segment['end'] as Map?;
  final startLat = (start?['lat'] as num?)?.toDouble();
  final startLon = (start?['lon'] as num?)?.toDouble();
  final endLat = (end?['lat'] as num?)?.toDouble();
  final endLon = (end?['lon'] as num?)?.toDouble();
  if (startLat == null || startLon == null || endLat == null || endLon == null) {
    return const [];
  }
  final pts = greatCirclePoints(startLat, startLon, endLat, endLon, nPoints: 50);
  return [for (final p in pts) [p.lon, p.lat]];
}

Map<String, Map<String, dynamic>> activitiesById(
  List<Map<String, dynamic>> activities,
) =>
    {
      for (final a in activities)
        if (a['id'] != null) a['id'].toString(): a,
    };

/// Full-resolution GeoJSON FeatureCollection, built entirely client-side.
/// Mirrors `_build_full_geo_features`'s expanded-coordinates shape (there's no
/// over-the-wire payload to shrink here, so there's no encoded-polyline mode).
Map<String, dynamic> buildFullGeo(
  List<Map<String, dynamic>> items,
  Map<String, Map<String, dynamic>> activitiesById,
) {
  final features = <Map<String, dynamic>>[];
  for (final item in items) {
    if (item['item_type'] == 'activity') {
      final a = activitiesById[item['activity_id']?.toString()];
      if (a == null) continue;
      // The wire shape nests the polyline under "map" (to_strava_dict()'s
      // {"map": {"summary_polyline": ...}}), matching activity_editor_page.dart.
      final polyline = (a['map'] as Map?)?['summary_polyline'] as String?;
      List<List<double>>? coords;
      if (polyline != null && polyline.isNotEmpty) {
        coords = _decodeActivityPolyline(polyline);
      } else {
        final start = _asLatLng(a['start_latlng']);
        final end = _asLatLng(a['end_latlng']);
        if (start != null && end != null) {
          coords = [
            [start[1], start[0]],
            [end[1], end[0]],
          ];
        }
      }
      if (coords == null || coords.length < 2) continue;
      features.add(_linestring(coords, {
        'type': 'activity',
        'activity_id': a['id'],
        'name': shownText(a['name'] as String?),
        'sport_type': a['type'],
      }));
    } else if (item['item_type'] == 'segment') {
      final seg = item['segment'] as Map?;
      if (seg == null) continue;
      final coords = _segmentCoords(seg);
      if (coords.length < 2) continue;
      features.add(_linestring(coords, {
        'type': 'segment',
        'segment_id': seg['id'],
        'segment_type': seg['segment_type'],
        'label': seg['label'],
        'route_mode': seg['route_mode'],
      }));
    }
  }
  return {'type': 'FeatureCollection', 'features': features};
}

/// Low-res GeoJSON FeatureCollection (straight lines per activity), built
/// client-side. Mirrors `_compute_low_res_geo`: no polyline decoding for
/// activities, so it's cheap enough to rebuild on every load.
Map<String, dynamic> buildLowResGeo(
  List<Map<String, dynamic>> items,
  Map<String, Map<String, dynamic>> activitiesById,
) {
  final features = <Map<String, dynamic>>[];
  for (final item in items) {
    if (item['item_type'] == 'activity') {
      final a = activitiesById[item['activity_id']?.toString()];
      if (a == null) continue;
      final start = _asLatLng(a['start_latlng']);
      final end = _asLatLng(a['end_latlng']);
      if (start == null || end == null) continue;
      features.add(_linestring([
        [start[1], start[0]],
        [end[1], end[0]],
      ], {
        'type': 'activity',
        'activity_id': a['id'],
        'name': shownText(a['name'] as String?),
        'sport_type': a['type'],
      }));
    } else if (item['item_type'] == 'segment') {
      final seg = item['segment'] as Map?;
      if (seg == null) continue;
      final coords = _segmentCoords(seg);
      if (coords.length < 2) continue;
      features.add(_linestring(coords, {
        'type': 'segment',
        'segment_id': seg['id'],
        'segment_type': seg['segment_type'],
        'label': seg['label'],
        'route_mode': seg['route_mode'],
        'route_degraded': seg['route_degraded'],
        'route_status': seg['route_status'],
      }));
    }
  }
  return {'type': 'FeatureCollection', 'features': features};
}
