/// A [MarkerLayer] whose world copies never share a key.
///
/// Zoomed out until the world is narrower than the viewport, flutter_map 8's
/// MarkerLayer draws one Positioned per visible world copy of each marker and
/// gives every copy the marker's own `key`. Our markers are keyed, so that
/// key appeared twice among the Stack's children. Debug builds assert;
/// release builds lost track of one copy, which was never removed and stayed
/// painted where it was, leaving frozen "ghost trips" behind after zooming
/// back in.
///
/// This is flutter_map 8.3.1's MarkerLayer placement (BSD-3-Clause,
/// Copyright fleaflet), with one change: the copy in the main world keeps
/// [Marker.key], so a keyed marker keeps its Element (and the memory
/// thumbnail state under it) across rebuilds, and every other copy is keyed by
/// that key plus its world index.
library;

import 'package:flutter/widgets.dart';
import 'package:flutter_map/flutter_map.dart';

class WorldCopyMarkerLayer extends StatefulWidget {
  const WorldCopyMarkerLayer({super.key, required this.markers});

  final List<Marker> markers;

  @override
  State<WorldCopyMarkerLayer> createState() => _WorldCopyMarkerLayerState();
}

class _WorldCopyMarkerLayerState extends State<WorldCopyMarkerLayer> {
  // Projected (zoom-independent) marker points, in markers order. Projection
  // depends only on the CRS, so a camera move costs a linear transform per
  // marker rather than a re-projection.
  List<Offset>? _projectedPoints;
  Crs? _projectionCrs;

  @override
  void didUpdateWidget(WorldCopyMarkerLayer oldWidget) {
    super.didUpdateWidget(oldWidget);
    _projectedPoints = null;
  }

  List<Offset> _projectPoints(Crs crs) => List<Offset>.generate(
        widget.markers.length,
        (i) {
          final point = widget.markers[i].point;
          if (!(point.latitude.isFinite && point.longitude.isFinite)) {
            throw RangeError('All markers must have finite `point`s');
          }
          return crs.projection.project(point);
        },
        growable: false,
      );

  @override
  Widget build(BuildContext context) {
    final map = MapCamera.of(context);
    final crs = map.crs;
    if (_projectedPoints == null || _projectionCrs != crs) {
      _projectionCrs = crs;
      _projectedPoints = _projectPoints(crs);
    }
    final projectedPoints = _projectedPoints!;
    final worldWidth = map.getWorldWidthAtZoom();
    final zoomScale = crs.scale(map.zoom);
    final pixelBounds = map.pixelBounds;
    final pixelOrigin = map.pixelOrigin;
    final markers = widget.markers;

    return MobileLayerTransformer(
      child: Stack(
        children: () sync* {
          for (var i = 0; i < markers.length; i++) {
            final m = markers[i];
            final alignment = m.alignment ?? Alignment.center;
            final left = 0.5 * m.width * (alignment.x + 1);
            final top = 0.5 * m.height * (alignment.y + 1);
            final right = m.width - left;
            final bottom = m.height - top;

            final projected = projectedPoints[i];
            final (px, py) =
                crs.transform(projected.dx, projected.dy, zoomScale);

            // [copy] is the world index: 0 is the main world, negative west,
            // positive east. Null when the copy is out of view.
            Positioned? positioned(int copy) {
              final shiftedX = px + copy * worldWidth;
              if (!pixelBounds.overlaps(Rect.fromPoints(
                Offset(shiftedX + left, py - bottom),
                Offset(shiftedX - right, py + top),
              ))) {
                return null;
              }
              final local = Offset(shiftedX, py) - pixelOrigin;
              final key = m.key;
              return Positioned(
                key: key == null || copy == 0 ? key : ValueKey((key, copy)),
                width: m.width,
                height: m.height,
                left: local.dx - right,
                top: local.dy - bottom,
                child: (m.rotate ?? false)
                    ? Transform.rotate(
                        angle: -map.rotationRad,
                        alignment: alignment * -1,
                        child: m.child,
                      )
                    : m.child,
              );
            }

            final main = positioned(0);
            if (main != null) yield main;
            // The main copy being culled says nothing about the others.
            if (worldWidth == 0) continue;
            for (var copy = -1;; copy--) {
              final p = positioned(copy);
              if (p == null) break;
              yield p;
            }
            for (var copy = 1;; copy++) {
              final p = positioned(copy);
              if (p == null) break;
              yield p;
            }
          }
        }()
            .toList(),
      ),
    );
  }
}
