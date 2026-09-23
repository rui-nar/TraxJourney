/// Shared map + gesture chrome for a [TrackEditorController]: the tile map
/// with the current polyline, draggable vertex handles (materialised only for
/// the portion currently in view, and capped in number — see
/// [visibleVertexIndices]), the add-mode
/// toolbar, and each vertex's long-press/right-click context menu.
///
/// Extracted out of the original activity track editor (issue #31) so a
/// second editor — the segment/ferry-route editor (issue #150) — can share
/// the exact same map and gesture behaviour rather than re-implement it.
/// Callers own the [TrackEditorController] and the surrounding
/// Scaffold/AppBar/Save/Reset chrome; only the map body lives here.
library;

import 'dart:async' show Timer;

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';

import '../core/brand.dart';
import '../core/design_tokens.dart';
import 'basemaps.dart';
import 'track_edit_model.dart';
import 'track_editor_controller.dart';

/// One entry in a track point's long-press/right-click context menu.
///
/// [onSelected] receives the menu's own [BuildContext] plus the point
/// [index] it was opened on; callers that need to show a confirmation dialog
/// can ignore the passed context and use their own State's context instead —
/// both resolve to the same Overlay/Navigator.
class PointMenuAction {
  final String label;
  final IconData icon;
  final bool enabled;
  final Future<void> Function(BuildContext context, int index) onSelected;
  final Color? color;

  /// Renders a [PopupMenuDivider] immediately above this entry — used to set
  /// a destructive action (e.g. "Delete point") apart from the rest.
  final bool dividerBefore;

  const PointMenuAction({
    required this.label,
    required this.icon,
    required this.enabled,
    required this.onSelected,
    this.color,
    this.dividerBefore = false,
  });
}

/// Upper bound on the number of draggable vertex handles materialised at
/// once, however many of the track's vertices are in view.
///
/// Viewport filtering alone does not bound anything: zoom out to fit a whole
/// resolved rail route and every one of its vertices is "in view". Overpass
/// stores rail geometry at full OSM-node resolution, so that is routinely
/// thousands of points — 9,700 measured for a single Hamburg→Offenburg
/// route — and each one becomes a Marker wrapping a Listener, a
/// GestureDetector and a [_VertexHandle], built in one frame.
///
/// 500 is not a round guess: a handle is 22x22 logical px, so 500 of them
/// cover ~242,000 px² — about 80% of a 390x780 phone viewport. Past that
/// they physically tile the screen and no individual vertex can be aimed at
/// any more, so the cap never withholds a handle that was usable in the
/// first place. It is also ~19x below the measured worst case.
const kMaxVertexHandles = 500;

/// Indices of the vertices in [points] that get a draggable handle: those
/// inside [bounds] (all of them when [bounds] is null, i.e. before the map
/// has reported a camera), thinned to at most [maxHandles] evenly-spaced
/// entries, always keeping the first and last of the visible run.
///
/// Thinning is *even*, not shape-aware — deliberately unlike map_panel's
/// [decimatePolylinePoints], which is LTTB because it optimises how a line
/// *looks*. Handles are an interaction surface, and even spacing is what
/// makes the thinning recoverable: every vertex dropped here is reachable by
/// zooming in, because zooming shrinks the visible run until it fits under
/// [maxHandles] and every remaining vertex gets its handle back. This
/// editor sets no `MapOptions.maxZoom` and its raster tiles simply upscale
/// past `maxNativeZoom`, so there is always a zoom level tight enough.
///
/// Pure and side-effect free: this only decides what to *show*. The
/// controller's point list — and so the persisted polyline — is never
/// touched by viewing, panning or zooming.
@visibleForTesting
List<int> visibleVertexIndices(
  List<EditPoint> points,
  LatLngBounds? bounds, {
  int maxHandles = kMaxVertexHandles,
}) {
  final visible = <int>[];
  for (var i = 0; i < points.length; i++) {
    if (bounds == null ||
        bounds.contains(LatLng(points[i].lat, points[i].lng))) {
      visible.add(i);
    }
  }
  if (visible.length <= maxHandles) return visible;
  // step > 1 here (visible.length > maxHandles), so the floor()ed picks are
  // strictly increasing and the result holds exactly maxHandles indices.
  final step = (visible.length - 1) / (maxHandles - 1);
  return [for (var k = 0; k < maxHandles; k++) visible[(k * step).round()]];
}

class TrackMapEditor extends StatefulWidget {
  final TrackEditorController controller;

  /// Menu entries offered for the vertex at [index]; recomputed each time a
  /// point's menu is opened so `enabled` reflects the current point list.
  final List<PointMenuAction> Function(int index) actionsForPoint;

  /// Hint text shown in the toolbar when Add mode is off — names which
  /// actions the per-point menu offers, since that set differs by caller.
  final String pointMenuHint;

  const TrackMapEditor({
    super.key,
    required this.controller,
    required this.actionsForPoint,
    required this.pointMenuHint,
  });

  @override
  State<TrackMapEditor> createState() => _TrackMapEditorState();
}

class _TrackMapEditorState extends State<TrackMapEditor> {
  final MapController _map = MapController();
  // Anchors screen→LatLng conversions for vertex drags to the map's own box.
  final GlobalKey _mapKey = GlobalKey();

  // When on, tapping the map inserts a point; point-specific edits are always
  // available via a per-point long-press / right-click menu.
  bool _addMode = false;
  // Current visible map bounds; drives which vertex handles are materialised.
  LatLngBounds? _bounds;
  // Debounces onPositionChanged → _bounds updates: flutter_map fires that
  // callback continuously (once per frame) during a drag, and each update
  // forces a rebuild that rescans every point in the track
  // (_visibleVertexIndices) and reconstructs a handle Marker for each one
  // visible — on a thousands-point track, doing that every single pan frame
  // is enough to ANR. Committing _bounds only once panning pauses keeps that
  // work off the drag's hot path; the full polyline (not gated on _bounds)
  // still tracks the finger immediately, so the pan itself never looks stalled.
  Timer? _boundsDebounce;
  // Live drag state: the vertex being dragged and its provisional position, so
  // the handle and its adjacent polyline segments follow the finger before the
  // move is committed on drop via [TrackEditorController.moveVertex]. The drag is
  // driven by raw pointer events (a [Listener]) rather than a pan gesture so it
  // wins over flutter_map's own scale/drag recognizer; the map is locked for the
  // duration so it can't pan underneath the moving vertex.
  int? _dragIndex;
  LatLng? _dragLatLng;
  int? _dragPointer;
  Offset? _dragStartGlobal;
  bool _dragging = false;
  // True while a pointer is down on a vertex handle: disables map interaction so
  // grabbing a vertex never pans/zooms the map.
  bool _mapLocked = false;

  // Distance (logical px) a pointer must travel before a press becomes a drag,
  // so a stationary long-press still opens the context menu instead of moving.
  static const double _dragSlop = 8;

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_onChange);
  }

  @override
  void didUpdateWidget(covariant TrackMapEditor oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.controller != widget.controller) {
      oldWidget.controller.removeListener(_onChange);
      widget.controller.addListener(_onChange);
    }
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onChange);
    _boundsDebounce?.cancel();
    super.dispose();
  }

  void _onChange() => setState(() {});

  // ── Map interactions ────────────────────────────────────────────────────

  /// Index of the segment (its start vertex) nearest to [tap], for inserts.
  int _nearestSegment(LatLng tap) {
    final pts = widget.controller.points;
    if (pts.length < 2) return -1;
    int best = 0;
    double bestD = double.infinity;
    for (var i = 0; i < pts.length - 1; i++) {
      final mLat = (pts[i].lat + pts[i + 1].lat) / 2;
      final mLng = (pts[i].lng + pts[i + 1].lng) / 2;
      final dLat = mLat - tap.latitude;
      final dLng = mLng - tap.longitude;
      final d = dLat * dLat + dLng * dLng;
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    }
    return best;
  }

  void _onMapTap(LatLng tap) {
    if (!_addMode) return;
    final seg = _nearestSegment(tap);
    if (seg >= 0) {
      widget.controller.addPointAfter(seg, EditPoint(tap.latitude, tap.longitude));
    }
  }

  // ── Vertex drag (issue #36) ──────────────────────────────────────────────

  /// Convert a drag's global position to a map [LatLng] using the map camera.
  /// Coordinates are taken relative to the map's own render box so the marker's
  /// tiny handle box does not skew the unprojection.
  LatLng? _globalToLatLng(Offset globalPos) {
    final box = _mapKey.currentContext?.findRenderObject() as RenderBox?;
    if (box == null) return null;
    return _map.camera.offsetToCrs(box.globalToLocal(globalPos));
  }

  void _onVertexPointerDown(int index, PointerDownEvent e) {
    _dragPointer = e.pointer;
    _dragStartGlobal = e.position;
    _dragIndex = index;
    _dragging = false;
    // Lock the map immediately so it cannot pan out from under a grabbed vertex.
    setState(() => _mapLocked = true);
  }

  void _onVertexPointerMove(PointerMoveEvent e) {
    if (e.pointer != _dragPointer || _dragIndex == null) return;
    if (!_dragging) {
      if ((e.position - _dragStartGlobal!).distance < _dragSlop) return;
      _dragging = true; // crossed the slop → this is a drag, not a long-press
    }
    final ll = _globalToLatLng(e.position);
    if (ll == null) return;
    setState(() => _dragLatLng = ll);
  }

  void _onVertexPointerUp(PointerEvent e) {
    if (e.pointer != _dragPointer) return;
    final index = _dragIndex;
    final ll = _dragLatLng;
    final moved = _dragging;
    _dragPointer = null;
    _dragStartGlobal = null;
    _dragging = false;
    setState(() {
      _mapLocked = false;
      _dragIndex = null;
      _dragLatLng = null;
    });
    if (moved && index != null && ll != null) {
      widget.controller.moveVertex(index, ll.latitude, ll.longitude);
    }
  }

  /// Per-point context menu (long-press on touch, right-click on web/desktop),
  /// built from [TrackMapEditor.actionsForPoint].
  Future<void> _showPointMenu(int index, Offset globalPos) async {
    final overlay =
        Overlay.of(context).context.findRenderObject() as RenderBox;
    final actions = widget.actionsForPoint(index);
    final items = <PopupMenuEntry<int>>[];
    for (var i = 0; i < actions.length; i++) {
      if (actions[i].dividerBefore) items.add(const PopupMenuDivider());
      items.add(_menuItem(i, actions[i]));
    }
    final selected = await showMenu<int>(
      context: context,
      position: RelativeRect.fromLTRB(
        globalPos.dx,
        globalPos.dy,
        overlay.size.width - globalPos.dx,
        overlay.size.height - globalPos.dy,
      ),
      items: items,
    );
    if (selected == null || !mounted) return;
    await actions[selected].onSelected(context, index);
  }

  PopupMenuItem<int> _menuItem(int value, PointMenuAction action) =>
      PopupMenuItem<int>(
        value: value,
        enabled: action.enabled,
        child: Row(
          children: [
            Icon(action.icon, size: 18, color: action.color),
            const SizedBox(width: 12),
            Flexible(
              child: Text(
                action.label,
                style: TextStyle(color: action.color),
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ],
        ),
      );

  // ── Handle materialisation (perf) ────────────────────────────────────────

  List<Marker> _buildHandleMarkers() {
    final pts = widget.controller.points;
    final markers = <Marker>[];
    for (final i in visibleVertexIndices(pts, _bounds)) {
      final point = (i == _dragIndex && _dragLatLng != null)
          ? _dragLatLng!
          : LatLng(pts[i].lat, pts[i].lng);
      markers.add(Marker(
        point: point,
        width: 22,
        height: 22,
        child: Listener(
          key: ValueKey('vertex_$i'),
          onPointerDown: (e) => _onVertexPointerDown(i, e),
          onPointerMove: _onVertexPointerMove,
          onPointerUp: _onVertexPointerUp,
          onPointerCancel: _onVertexPointerUp,
          child: GestureDetector(
            behavior: HitTestBehavior.opaque,
            onLongPressStart: (d) => _showPointMenu(i, d.globalPosition),
            onSecondaryTapDown: (d) => _showPointMenu(i, d.globalPosition),
            child: const _VertexHandle(),
          ),
        ),
      ));
    }
    return markers;
  }

  @override
  Widget build(BuildContext context) {
    final pts = widget.controller.points;
    // Live-preview the dragged vertex so its adjacent segments follow the finger.
    final polyline = [
      for (var i = 0; i < pts.length; i++)
        (i == _dragIndex && _dragLatLng != null)
            ? _dragLatLng!
            : LatLng(pts[i].lat, pts[i].lng),
    ];
    final center = polyline.isNotEmpty ? polyline.first : const LatLng(0, 0);

    return Column(
      children: [
        _ToolBar(
          addMode: _addMode,
          hint: widget.pointMenuHint,
          onAddModeChanged: (v) => setState(() => _addMode = v),
        ),
        Expanded(
          child: Stack(
            children: [
              FlutterMap(
                key: _mapKey,
                mapController: _map,
                options: MapOptions(
                  initialCenter: center,
                  initialZoom: 12,
                  interactionOptions: InteractionOptions(
                    // Locked while a vertex is being dragged so the map can't
                    // pan/zoom out from under the grabbed handle.
                    flags: _mapLocked
                        ? InteractiveFlag.none
                        : InteractiveFlag.all & ~InteractiveFlag.rotate,
                  ),
                  onTap: (_, latlng) => _onMapTap(latlng),
                  onPositionChanged: (camera, _) {
                    _boundsDebounce?.cancel();
                    _boundsDebounce = Timer(const Duration(milliseconds: 120), () {
                      if (mounted) setState(() => _bounds = camera.visibleBounds);
                    });
                  },
                ),
                children: [
                  TileLayer(
                    urlTemplate: kActiveManageBasemapUrl,
                    subdomains: kActiveManageSubdomains,
                    userAgentPackageName: kAppPackageId,
                    maxNativeZoom: 20,
                  ),
                  if (polyline.length >= 2)
                    PolylineLayer(
                      polylines: [
                        Polyline(
                          points: polyline,
                          color: kAccent,
                          strokeWidth: 3,
                        ),
                      ],
                      simplificationTolerance: 0.5,
                    ),
                  MarkerLayer(markers: _buildHandleMarkers()),
                ],
              ),
            ],
          ),
        ),
      ],
    );
  }
}

// ── Tool bar ─────────────────────────────────────────────────────────────────

class _ToolBar extends StatelessWidget {
  final bool addMode;
  final String hint;
  final ValueChanged<bool> onAddModeChanged;
  const _ToolBar({
    required this.addMode,
    required this.hint,
    required this.onAddModeChanged,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Material(
      color: theme.colorScheme.surfaceContainerHighest,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
        child: Row(
          children: [
            FilterChip(
              avatar: const Icon(Icons.add_location_alt_outlined, size: 18),
              label: const Text('Add points'),
              selected: addMode,
              showCheckmark: false,
              selectedColor: kAccentSoft,
              onSelected: onAddModeChanged,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                addMode ? 'Tap the map to insert a point' : hint,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _VertexHandle extends StatelessWidget {
  const _VertexHandle();

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Container(
        width: 14,
        height: 14,
        decoration: BoxDecoration(
          color: Colors.white,
          shape: BoxShape.circle,
          border: Border.all(color: kAccent, width: 2),
        ),
      ),
    );
  }
}

// ── Save button (metallic, per design system) ────────────────────────────────
//
// Shared by every track editor's AppBar so Save always looks and behaves the
// same regardless of what's being edited (activity vs. segment route).

class TrackEditorSaveButton extends StatelessWidget {
  final bool enabled;
  final bool saving;
  final VoidCallback onPressed;

  const TrackEditorSaveButton({
    super.key,
    required this.enabled,
    required this.saving,
    required this.onPressed,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final fg = enabled ? Colors.white : cs.onSurfaceVariant;
    return DecoratedBox(
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(8),
        gradient: enabled ? metallicBlue(Theme.of(context).brightness) : null,
        color: enabled ? null : cs.surfaceContainerHighest,
      ),
      child: TextButton.icon(
        onPressed: enabled ? onPressed : null,
        icon: saving
            ? SizedBox(
                width: 16,
                height: 16,
                child: CircularProgressIndicator(strokeWidth: 2, color: fg),
              )
            : Icon(Icons.check, size: 17, color: fg),
        label: Text('Save',
            style: TextStyle(color: fg, fontWeight: FontWeight.w600)),
        style: TextButton.styleFrom(
          minimumSize: const Size(0, 36),
          padding: const EdgeInsets.symmetric(horizontal: 16),
        ),
      ),
    );
  }
}
