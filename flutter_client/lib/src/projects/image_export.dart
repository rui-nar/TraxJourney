library;

import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';

import '../core/brand.dart';
import 'basemaps.dart';
import 'elevation_chart.dart';
import 'map_panel.dart' show buildDayBreakpointMarkers, dayStartActivityIds;
import 'project_notifier.dart';

const _kExportMapWidth    = 2400.0;
const _kExportMapHeight   = 1600.0;
const _kExportChartHeight = 200.0;

class ImageExportOptions {
  final bool includeChart;
  final bool includeTitle;
  const ImageExportOptions({
    required this.includeChart,
    required this.includeTitle,
  });
}

class ImageExportDialog extends StatefulWidget {
  final String projectName;
  final void Function(ImageExportOptions) onExport;
  const ImageExportDialog({super.key, required this.projectName, required this.onExport});
  @override
  State<ImageExportDialog> createState() => _ImageExportDialogState();
}

class _ImageExportDialogState extends State<ImageExportDialog> {
  bool _includeChart = true;
  bool _includeTitle = false;

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Export image'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Canvas: 2400 × 1600 px',
              style: Theme.of(context).textTheme.bodySmall),
          const SizedBox(height: 12),
          CheckboxListTile(
            title: const Text('Include elevation chart'),
            value: _includeChart,
            onChanged: (v) => setState(() => _includeChart = v!),
            contentPadding: EdgeInsets.zero,
            dense: true,
          ),
          CheckboxListTile(
            title: const Text('Include project title'),
            value: _includeTitle,
            onChanged: (v) => setState(() => _includeTitle = v!),
            contentPadding: EdgeInsets.zero,
            dense: true,
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: () {
            final opts = ImageExportOptions(
              includeChart: _includeChart,
              includeTitle: _includeTitle,
            );
            Navigator.of(context).pop();
            widget.onExport(opts);
          },
          child: const Text('Export PNG'),
        ),
      ],
    );
  }
}

bool uint8ListEquals(Uint8List a, Uint8List b) {
  if (a.length != b.length) return false;
  for (var i = 0; i < a.length; i++) {
    if (a[i] != b[i]) return false;
  }
  return true;
}

/// Returns true if the thumbnail is a near-uniform (blank/grey) image —
/// i.e., tiles haven't started rendering yet.
bool isThumbnailBlank(Uint8List bytes) {
  if (bytes.length < 16) return true;
  final ref = bytes[0];
  // Sample R channel every 4 bytes (RGBA). Any pixel differing by >12
  // from the first means real content has appeared.
  for (var i = 0; i < bytes.length; i += 4) {
    if ((bytes[i] - ref).abs() > 12) return false;
  }
  return true;
}

/// Renders an offscreen map + optional elevation chart, polls until tiles
/// settle, then returns the composited PNG bytes (or null if rendering could
/// not complete). The byte-capture path is platform-agnostic; callers that
/// want a browser download pass the result to `downloadPng` (web-only).
///
/// [boundsOverride] fits the camera to a specific region (e.g. one day's
/// route); when null the whole trip's bounds are used.
///
/// [geoOverride] supplies the geometry to draw, for a caller that has already
/// fetched it — the share card reads the same full-resolution geometry to
/// work out the day's bounds, and passing it in is what keeps that one fetch
/// from becoming two.
Future<Uint8List?> performOffscreenExport({
  required BuildContext context,
  required ProjectNotifier notifier,
  required String projectName,
  required ImageExportOptions opts,
  LatLngBounds? boundsOverride,
  Map<String, dynamic>? geoOverride,
}) async {
  // Not `notifier.geo`: that is simplified to the zoom the map is showing
  // (issue #295), and an export fits its own camera — a day-scoped one much
  // tighter than the whole trip — so it rendered visibly angular (issue
  // #317). The extra request is affordable on an operation the user asked
  // for and that already waits seconds for tiles.
  final geo = geoOverride ?? await notifier.fullResGeoForExport();
  if (geo == null) return null;
  if (!context.mounted) return null;

  final allPoints = <LatLng>[];
  final polylines = <Polyline>[];
  final features = geo['features'];
  if (features is List) {
    for (final feature in features) {
      if (feature is! Map) continue;
      final geometry = feature['geometry'] as Map? ?? {};
      final coords = geometry['coordinates'];
      if (coords is! List) continue;
      final points = <LatLng>[];
      for (final c in coords) {
        if (c is List && c.length >= 2) {
          points.add(LatLng((c[1] as num).toDouble(), (c[0] as num).toDouble()));
        }
      }
      if (points.isEmpty) continue;
      allPoints.addAll(points);
      final props = feature['properties'] as Map? ?? {};
      final isSegment = props['type'] == 'segment';
      polylines.add(Polyline(
        points: points,
        color: isSegment ? const Color(0xFF888888) : Colors.black,
        strokeWidth: isSegment ? 2.0 : 2.5,
      ));
    }
  }

  // Day breakpoint nodes (#19) — ring colour matches the exported track line
  // (black) so they read as joints on it.
  final dayMarkers = buildDayBreakpointMarkers(
    geo,
    dayStartActivityIds(
        notifier.items, {for (final a in notifier.activities) a['id']: a}),
    Colors.black,
  );

  final exportKey  = GlobalKey();
  final exportCtrl = MapController();

  // Render at the actual screen width so the widget is within the browser
  // viewport — Flutter Web culls off-screen widgets and tiles never load.
  // Scale down from _kExportMapWidth proportionally, then use a matching
  // pixelRatio in toImage() to get back to the target 2400×1600 resolution.
  final screenSize = MediaQuery.of(context).size;
  final renderW = screenSize.width.clamp(600.0, _kExportMapWidth);
  final renderScale = renderW / _kExportMapWidth;
  final renderMapH   = _kExportMapHeight   * renderScale;
  final renderChartH = _kExportChartHeight * renderScale;
  final totalRenderH = renderMapH + (opts.includeChart ? renderChartH : 0);
  final captureRatio = _kExportMapWidth / renderW;

  final overlay = OverlayEntry(builder: (_) {
    // Widget renders at renderW×totalRenderH at position 0,0 — fully within
    // the viewport so Flutter paints it and the tile provider fetches tiles.
    // toImage(pixelRatio: captureRatio) upscales the capture to the target
    // 2400×1600 resolution.
    return Positioned(
      left: 0,
      top: 0,
      child: IgnorePointer(
        child: SizedBox(
          width: renderW,
          height: totalRenderH,
          child: RepaintBoundary(
            key: exportKey,
            child: Container(
              color: Colors.white,
              child: Column(
                children: [
                  Expanded(
                    child: FlutterMap(
                      mapController: exportCtrl,
                      options: MapOptions(
                        initialCameraFit: (boundsOverride != null ||
                                allPoints.isNotEmpty)
                            ? CameraFit.bounds(
                                bounds: boundsOverride ??
                                    LatLngBounds.fromPoints(allPoints),
                                padding: const EdgeInsets.all(48))
                            : null,
                        interactionOptions: const InteractionOptions(
                            flags: InteractiveFlag.none),
                      ),
                      children: [
                        // Esri satellite — no auth token required, reliably
                        // loadable via NetworkTileProvider in offscreen context.
                        // The main map may use Mapbox vector tiles but those
                        // don't capture correctly via toImage().
                        TileLayer(
                          urlTemplate: kViewBasemapUrl,
                          userAgentPackageName: kAppPackageId,
                          tileProvider: NetworkTileProvider(),
                          maxNativeZoom: 19,
                        ),
                        TileLayer(
                          urlTemplate: kViewLabelsUrl,
                          userAgentPackageName: kAppPackageId,
                          tileProvider: NetworkTileProvider(),
                          maxNativeZoom: 19,
                        ),
                        if (polylines.isNotEmpty)
                          PolylineLayer(
                              polylines: polylines,
                              simplificationTolerance: 0),
                        if (dayMarkers.isNotEmpty)
                          MarkerLayer(markers: dayMarkers),
                      ],
                    ),
                  ),
                  if (opts.includeChart)
                    SizedBox(
                      height: renderChartH,
                      child: ElevationChart(
                        activities: notifier.activities,
                        selectedActivityId: null,
                        track: notifier.fullTrack,
                        color: notifier.effectiveElevationChartColor,
                      ),
                    ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  });

  Overlay.of(context).insert(overlay);

  final messenger = ScaffoldMessenger.of(context);
  messenger.showSnackBar(const SnackBar(
    content: Row(children: [
      SizedBox(width: 16, height: 16,
          child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white)),
      SizedBox(width: 12),
      Text('Preparing map export…'),
    ]),
    duration: Duration(seconds: 60),
  ));

  // Wait for tiles to settle: poll low-res thumbnails until two consecutive
  // captures are pixel-identical (or a 12 s deadline is reached).
  await Future.delayed(const Duration(seconds: 1));
  {
    Uint8List? prevBytes;
    for (var i = 0; i < 18; i++) {
      final b = exportKey.currentContext?.findRenderObject()
          as RenderRepaintBoundary?;
      if (b == null) break;
      // Use pixelRatio 0.05 → ~120×80 px thumbnail — fast to capture & compare.
      final probe = await b.toImage(pixelRatio: 0.05);
      final bd = await probe.toByteData();
      probe.dispose();
      if (bd == null) break;
      final bytes = bd.buffer.asUint8List();
      if (prevBytes != null &&
          uint8ListEquals(prevBytes, bytes) &&
          !isThumbnailBlank(bytes)) { break; }
      prevBytes = bytes;
      await Future.delayed(const Duration(milliseconds: 600));
    }
  }

  try {
    final boundary = exportKey.currentContext?.findRenderObject()
        as RenderRepaintBoundary?;
    if (boundary == null) return null;

    ui.Image image = await boundary.toImage(pixelRatio: captureRatio);

    // Composite onto white background + optional title overlay.
    {
      final recorder = ui.PictureRecorder();
      final canvas = Canvas(recorder);
      canvas.drawRect(
        Rect.fromLTWH(0, 0, image.width.toDouble(), image.height.toDouble()),
        Paint()..color = const Color(0xFFFFFFFF),
      );
      canvas.drawImage(image, Offset.zero, Paint());
      if (opts.includeTitle) {
        final tp = TextPainter(
          text: TextSpan(
            text: projectName,
            style: const TextStyle(
              color: Colors.black,
              fontSize: 18,
              fontWeight: FontWeight.bold,
            ),
          ),
          textDirection: TextDirection.ltr,
        )..layout(maxWidth: image.width.toDouble() - 32);
        tp.paint(canvas, const Offset(16, 16));
      }
      final picture = recorder.endRecording();
      image = await picture.toImage(image.width, image.height);
    }

    final byteData = await image.toByteData(format: ui.ImageByteFormat.png);
    if (byteData == null) return null;
    return byteData.buffer.asUint8List();
  } finally {
    messenger.hideCurrentSnackBar();
    overlay.remove();
    exportCtrl.dispose();
  }
}
