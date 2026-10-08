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
/// Copyright fleaflet), with one change: one visible copy keeps [Marker.key]
/// — the one that held it last frame while it stays in view — so a keyed
/// marker keeps its Element (and the memory thumbnail state under it) across
/// rebuilds, pans and the antimeridian, and every other copy on screen at the
/// same time is keyed by that key plus its world index.
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
  // Screen x, last frame, of the copy that held each marker key's plain key.
  // Kept as a position, not a world index: the camera renormalises its centre
  // across the antimeridian, which renumbers every copy at once, while the
  // copy the user is looking at barely moves on screen.
  Map<Key, double> _plainKeyX = {};

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
    final previousPlainKeyX = _plainKeyX;
    final plainKeyX = _plainKeyX = <Key, double>{};

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
            // positive east.
            bool inView(int copy) {
              final shiftedX = px + copy * worldWidth;
              return pixelBounds.overlaps(Rect.fromPoints(
                Offset(shiftedX + left, py - bottom),
                Offset(shiftedX - right, py + top),
              ));
            }

            final copies = <int>[if (inView(0)) 0];
            // The main copy being culled says nothing about the others.
            if (worldWidth != 0) {
              for (var copy = -1; inView(copy); copy--) {
                copies.add(copy);
              }
              for (var copy = 1; inView(copy); copy++) {
                copies.add(copy);
              }
            }
            if (copies.isEmpty) continue;

            double screenX(int copy) =>
                px + copy * worldWidth - pixelOrigin.dx;

            // One copy keeps the plain key, so the Element (and thumbnail
            // state) under it survives: the copy that held it last frame,
            // found as the one nearest where it was on screen, else the main
            // copy, else the first in view. Keying by world index instead
            // remounted the copy being watched whenever its index changed —
            // across the antimeridian, or when another copy slid into view.
            // Only copies on screen alongside it are told apart by index.
            final key = m.key;
            var keyed = copies.first;
            final lastX = key == null ? null : previousPlainKeyX[key];
            if (lastX != null) {
              for (final copy in copies) {
                if ((screenX(copy) - lastX).abs() <
                    (screenX(keyed) - lastX).abs()) {
                  keyed = copy;
                }
              }
            }
            if (key != null) plainKeyX[key] = screenX(keyed);

            for (final copy in copies) {
              final local = Offset(screenX(copy), py - pixelOrigin.dy);
              yield Positioned(
                key: key == null || copy == keyed ? key : ValueKey((key, copy)),
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
          }
        }()
            .toList(),
      ),
    );
  }
}
