/// Full-screen dialog that lets the user tap a point on the map.
///
/// Returns a [LatLonResult] record on confirm, or null on cancel.
library;

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';

import '../core/brand.dart';
import '../map/geo_point.dart';
import '../map/great_circle.dart';

typedef LatLonResult = ({double lat, double lon});

class LocationPickerDialog extends StatefulWidget {
  /// Label shown in the title bar, e.g. "Pick start location".
  final String title;

  /// Pre-selected position (centers the map and places a marker).
  final double? initialLat;
  final double? initialLon;

  /// Project GeoJSON — rendered as context polylines so the user can see
  /// the existing route while picking a new point.
  final Map<String, dynamic>? geo;

  /// When set, the picker updates the live segment arc preview on the main map.
  final ValueNotifier<List<GeoPoint>?>? previewArcNotifier;

  /// Coordinates of the *other* endpoint — used to draw the preview arc.
  final double? otherLat;
  final double? otherLon;

  /// True when this picker is for the start point; false for the end point.
  final bool isStart;

  const LocationPickerDialog({
    super.key,
    required this.title,
    this.initialLat,
    this.initialLon,
    this.geo,
    this.previewArcNotifier,
    this.otherLat,
    this.otherLon,
    this.isStart = true,
  });

  @override
  State<LocationPickerDialog> createState() => _LocationPickerDialogState();
}

class _LocationPickerDialogState extends State<LocationPickerDialog> {
  LatLng? _picked;
  late final NetworkTileProvider _tileProvider;
  late final MapController _mapController;

  @override
  void initState() {
    super.initState();
    _tileProvider = NetworkTileProvider();
    _mapController = MapController();
    if (widget.initialLat != null && widget.initialLon != null) {
      _picked = LatLng(widget.initialLat!, widget.initialLon!);
    }
  }

  @override
  void dispose() {
    _mapController.dispose();
    super.dispose();
  }

  void _onTap(LatLng latlng) {
    setState(() => _picked = latlng);
    _mapController.move(latlng, _mapController.camera.zoom);
    // Drive live arc preview on the main map if both endpoints are known
    final arcNotifier = widget.previewArcNotifier;
    final oLat = widget.otherLat;
    final oLon = widget.otherLon;
    if (arcNotifier != null && oLat != null && oLon != null) {
      final lat1 = widget.isStart ? latlng.latitude  : oLat;
      final lon1 = widget.isStart ? latlng.longitude : oLon;
      final lat2 = widget.isStart ? oLat  : latlng.latitude;
      final lon2 = widget.isStart ? oLon  : latlng.longitude;
      arcNotifier.value = greatCirclePoints(lat1, lon1, lat2, lon2);
    }
  }

  // ── Map initialisation ──────────────────────────────────────────────────────

  LatLng get _center {
    if (_picked != null) return _picked!;
    // Default: zoom out to world view
    return const LatLng(20, 0);
  }

  double get _zoom => _picked != null ? 10.0 : 2.0;

  // ── Polylines from project GeoJSON ─────────────────────────────────────────

  List<Polyline> _buildPolylines() {
    final geo = widget.geo;
    if (geo == null) return [];
    final features = geo['features'];
    if (features is! List) return [];

    final lines = <Polyline>[];
    for (final f in features) {
      if (f is! Map) continue;
      final coords = f['geometry']?['coordinates'];
      if (coords is! List) continue;
      final pts = <LatLng>[];
      for (final c in coords) {
        if (c is List && c.length >= 2) {
          pts.add(LatLng((c[1] as num).toDouble(), (c[0] as num).toDouble()));
        }
      }
      if (pts.isEmpty) continue;
      final isSegment = f['properties']?['type'] == 'segment';
      lines.add(Polyline(
        points: pts,
        color: isSegment
            ? const Color(0x88888888)
            : Colors.black.withValues(alpha: 0.5),
        strokeWidth: isSegment ? 1.5 : 2.5,
      ));
    }
    return lines;
  }

  // ── UI ──────────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final polylines = _buildPolylines();

    return Dialog(
      insetPadding: const EdgeInsets.all(16),
      child: ConstrainedBox(
        constraints: const BoxConstraints(
            minWidth: 320, maxWidth: 700, maxHeight: 540),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            // ── Header ─────────────────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 16, 8, 4),
              child: Row(
                children: [
                  const Icon(Icons.location_on, size: 20),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(widget.title,
                        style: theme.textTheme.titleMedium),
                  ),
                  IconButton(
                    icon: const Icon(Icons.close),
                    onPressed: () => Navigator.of(context).pop(null),
                  ),
                ],
              ),
            ),

            // ── Instruction ─────────────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 20),
              child: Text(
                'Tap anywhere on the map to place a pin.',
                style: theme.textTheme.bodySmall?.copyWith(
                    color: theme.colorScheme.onSurface.withValues(alpha: 0.6)),
              ),
            ),
            const SizedBox(height: 8),

            // ── Map ──────────────────────────────────────────────────────────
            Expanded(
              child: ClipRect(
                child: FlutterMap(
                  mapController: _mapController,
                  options: MapOptions(
                    initialCenter: _center,
                    initialZoom: _zoom,
                    onTap: (_, latlng) => _onTap(latlng),
                    interactionOptions: const InteractionOptions(
                      flags: InteractiveFlag.all & ~InteractiveFlag.rotate,
                    ),
                  ),
                  children: [
                    TileLayer(
                      urlTemplate:
                          'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                      userAgentPackageName: kAppPackageId,
                      tileProvider: _tileProvider,
                      maxNativeZoom: 19,
                    ),
                    if (polylines.isNotEmpty)
                      PolylineLayer(polylines: polylines),
                    if (_picked != null)
                      MarkerLayer(
                        markers: [
                          Marker(
                            point: _picked!,
                            width: 36,
                            height: 36,
                            alignment: Alignment.topCenter,
                            child: const Icon(
                              Icons.location_pin,
                              color: Colors.red,
                              size: 36,
                            ),
                          ),
                        ],
                      ),
                  ],
                ),
              ),
            ),

            // ── Footer ───────────────────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
              child: Row(
                children: [
                  // Coordinate readout
                  Expanded(
                    child: _picked == null
                        ? Text('No point selected',
                            style: theme.textTheme.bodySmall)
                        : Text(
                            '${_picked!.latitude.toStringAsFixed(6)}, '
                            '${_picked!.longitude.toStringAsFixed(6)}',
                            style: theme.textTheme.bodySmall?.copyWith(
                                fontFamily: 'monospace'),
                          ),
                  ),
                  TextButton(
                    onPressed: () => Navigator.of(context).pop(null),
                    child: const Text('Cancel'),
                  ),
                  const SizedBox(width: 8),
                  ElevatedButton.icon(
                    style: ElevatedButton.styleFrom(
                        minimumSize: const Size(96, 44)),
                    icon: const Icon(Icons.check, size: 16),
                    label: const Text('Confirm'),
                    onPressed: _picked == null
                        ? null
                        : () => Navigator.of(context).pop<LatLonResult>((
                              lat: _picked!.latitude,
                              lon: _picked!.longitude,
                            )),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
