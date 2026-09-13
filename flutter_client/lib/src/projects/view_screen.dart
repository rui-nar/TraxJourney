/// View-mode project screen — map + elevation chart, no editing controls.
///
/// Accessible to authenticated project owners via `/view?project=name`.
/// Shows the same map as manage mode but without the activity panel,
/// and with the Esri World Imagery (satellite) basemap.
library;

import 'dart:async' show Timer, StreamSubscription;

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart' show MapEvent;
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:go_router/go_router.dart';
import 'package:latlong2/latlong.dart';
import 'package:provider/provider.dart';

import '../auth/auth_notifier.dart';
import '../core/brand.dart';
import '../core/current_location.dart' show currentDeviceLatLng;
import '../core/last_opened_project.dart';
import '../core/project_ref.dart';
import '../core/stale_shared_ref.dart';
import 'activity_panel.dart';
import 'basemaps.dart';
import 'geo_viewport.dart';
import 'day_carousel.dart';
import 'elevation_chart.dart';
import 'map_panel.dart';
import 'people_screen.dart';
import 'project_add_fab.dart';
import 'project_notifier.dart';
import 'project_service.dart';
import 'sync_import_dialog.dart';
import 'sync_import_notifier.dart';
import 'viewport_sync.dart';

// ── Service — calls /api/projects/{name}/meta first, elevation in background

/// View mode uses the base service unchanged.
///
/// It used to add a `fetchFullDetails` for its own Phase 2 elevation fetch.
/// That Phase 2 is gone (see [ViewProjectNotifier.loadView]) — the inherited
/// background upgrade fetches the compact /elevation endpoint instead of the
/// 33 MB details payload, so there is nothing left for a subclass to add.
class _ViewProjectService extends ProjectService {}

// ── Notifier — progressive two-phase load ────────────────────────────────────

class ViewProjectNotifier extends ProjectNotifier {
  bool _disposed = false;

  ViewProjectNotifier._internal(_ViewProjectService super.svc);

  factory ViewProjectNotifier() {
    final svc = _ViewProjectService();
    return ViewProjectNotifier._internal(svc);
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }

  Future<void> loadView(ProjectRef ref) async {
    isMetaLoaded = false;
    isElevationLoaded = false;
    isGeoLoaded = false;

    // Phase 1: load() calls _viewSvc.getDetailsMeta() (inherited, unoverridden
    // — a view-mode ref addresses a real project, so the base implementation
    // already hits the right /meta endpoint) which returns the lightweight
    // response in ~1 s.  isLoading goes false after that.
    //
    // Captured synchronously, *before* awaiting: load()'s body runs its
    // synchronous prefix — including bumping the load-supersession token —
    // the instant it's called, before it suspends on its own first await. So
    // this is guaranteed to be *this* loadView() call's own token. Reading
    // currentLoadToken only after `await load(ref)` returned instead (this
    // fix's first cut) could pick up a second concurrent loadView() call's
    // token if that call's own load() had already bumped it by the time this
    // one's await resolved — silently un-superseding a call that should have
    // been rejected (issue #283 review finding).
    final loadFuture = load(ref);
    final token = currentLoadToken;
    await loadFuture;
    if (_disposed || currentLoadKey != ref || currentLoadToken != token) return;
    isMetaLoaded = true;
    // The /meta response now carries a downsampled (low-res) elevation profile,
    // so the chart can render immediately instead of waiting for the full
    // profile; Phase 2 below upgrades it in place.
    isElevationLoaded = true;
    notifyListeners(); // project name, map, activity panel + elevation chart visible

    // There is deliberately no Phase 2 here any more.
    //
    // It used to fetch the full details payload for elevation — on a 180-day
    // trip that is 33 MB and ~14 s (issue #295). The inherited load() already
    // upgrades elevation in its own background phase, and since #295 it does
    // so from the compact /elevation endpoint: 2.8 MB and ~57 ms to decode on
    // the same trip. Keeping this block meant view mode fetched elevation
    // *twice*, once cheaply and once expensively, and the expensive one was
    // still the largest single thing a view-mode load did.
    //
    // isElevationLoaded is already true above so the chart renders from the
    // low-res profile /meta carries; the inherited upgrade replaces it in
    // place, notifying through the same camera-idle gate.
  }
}

// ── Entry point ───────────────────────────────────────────────────────────────

class ViewScreen extends StatelessWidget {
  final String projectName;

  /// Owning user's id for a project shared with the caller (issue #106);
  /// null for one of the caller's own projects.
  final int? ownerId;

  /// Camera position carried over from edit mode via the mode toggle, so
  /// switching modes doesn't reset the map viewport to fit-all-bounds.
  final double? initialLat;
  final double? initialLng;
  final double? initialZoom;

  const ViewScreen({
    super.key,
    required this.projectName,
    this.ownerId,
    this.initialLat,
    this.initialLng,
    this.initialZoom,
  });

  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider(
      create: (_) => ViewProjectNotifier(),
      child: _ViewBody(
        projectName: projectName,
        ownerId: ownerId,
        initialLat: initialLat,
        initialLng: initialLng,
        initialZoom: initialZoom,
      ),
    );
  }
}

// ── Screen body ───────────────────────────────────────────────────────────────

class _ViewBody extends StatefulWidget {
  final String projectName;
  final int? ownerId;
  final double? initialLat;
  final double? initialLng;
  final double? initialZoom;
  const _ViewBody({
    required this.projectName,
    this.ownerId,
    this.initialLat,
    this.initialLng,
    this.initialZoom,
  });

  /// Addressing for [projectName]/[ownerId] — issue #106.
  ProjectRef get projectRef => ProjectRef(name: projectName, ownerId: ownerId);

  @override
  State<_ViewBody> createState() => _ViewBodyState();
}

class _ViewBodyState extends State<_ViewBody> with TickerProviderStateMixin {
  late final AnimatedMapController _mapController =
      AnimatedMapController(vsync: this, duration: const Duration(milliseconds: 500));
  bool _autoZoom = false;

  // Captured (not looked up via context) so dispose() can stop it: when this
  // widget is torn down as part of a larger subtree/route unmount, its
  // element is already deactivated by the time dispose() runs, and
  // context.read() on a deactivated element throws ("Looking up a
  // deactivated widget's ancestor is unsafe") — issue #207 CI failure.
  ViewProjectNotifier? _degradedRouteWatchNotifier;

  // Highlighted point set when the user taps an encounter's place icon
  // (issue #72); cleared on the next unrelated map tap/selection.
  LatLng? _focusedLatLng;

  /// Zooms the map in on (lat, lon) and drops a highlighted pin there.
  void _focusLocation(double lat, double lon) {
    final target = LatLng(lat, lon);
    final currentZoom = _mapController.mapController.camera.zoom;
    setState(() => _focusedLatLng = target);
    _mapController.centerOnPoint(
      target,
      zoom: currentZoom < 15 ? 15 : currentZoom,
    );
  }

  void _clearFocusedLocation() {
    if (_focusedLatLng != null) setState(() => _focusedLatLng = null);
  }

  // Debounced camera → URL sync (issue #76 follow-up) — mirrors app_screen.dart.
  StreamSubscription<MapEvent>? _mapEventSub;
  Timer? _viewportSyncTimer;
  // Tells ProjectNotifier the camera is moving so its background full-res geo
  // upgrade can hold off on a rebuild until panning actually pauses (see
  // ProjectNotifier.setMapCameraActive).
  Timer? _cameraIdleTimer;

  void _onMapEvent(MapEvent event) {
    if (!shouldSyncViewport(event)) return;
    final camera = _mapController.mapController.camera;
    final visible = camera.visibleBounds;
    context.read<ViewProjectNotifier>()
      ..setMapCameraActive(true)
      // Zoom level of detail (issue #295) and its viewport box (#324): the
      // notifier fetches geometry matched to what is on screen, so it has to
      // know what is on screen — both how much detail, and which region.
      ..setMapZoom(camera.zoom,
          viewport: viewportBox(
              visible.west, visible.south, visible.east, visible.north));
    _cameraIdleTimer?.cancel();
    _cameraIdleTimer = Timer(const Duration(milliseconds: 250), () {
      if (mounted) context.read<ViewProjectNotifier>().setMapCameraActive(false);
    });
    _viewportSyncTimer?.cancel();
    _viewportSyncTimer = Timer(const Duration(milliseconds: 700), () {
      // Guards against a missing GoRouter ancestor (e.g. this widget under
      // test in isolation) — context.replace() would otherwise throw.
      if (!mounted || GoRouter.maybeOf(context) == null) return;
      final cam = _mapController.mapController.camera;
      context.replace(viewportSyncPath(
        basePath: '/view',
        projectName: widget.projectName,
        ownerId: widget.ownerId,
        lat: cam.center.latitude,
        lng: cam.center.longitude,
        zoom: cam.zoom,
      ));
    });
  }

  // Locate-me pin (issue #88); replaced (not accumulated) on each tap.
  LatLng? _hereLatLng;
  bool _locatingHere = false;

  /// Fetches the device's current location and pans the map to it at the
  /// CURRENT zoom (iso-zoom — unlike [_focusLocation], no zoom floor).
  /// Silent-fail on denied/unavailable/timed-out location, matching this
  /// app's established convention (see `current_location.dart`).
  Future<void> _locateMe() async {
    setState(() => _locatingHere = true);
    final here = await currentDeviceLatLng();
    if (!mounted) return;
    setState(() {
      _locatingHere = false;
      if (here != null) _hereLatLng = here;
    });
    if (here != null) {
      final currentZoom = _mapController.mapController.camera.zoom;
      _mapController.centerOnPoint(here, zoom: currentZoom);
    }
  }

  void _openSyncDialog(BuildContext context) {
    final notifier = context.read<ViewProjectNotifier>();
    final pending = notifier.pendingSync;
    if (pending == null) return;
    // Captured before the dialog's async gap; see initState for why the
    // URL-derived ref's role is resolved against the signed-in user.
    final projectRef = widget.projectRef
        .resolveRoleFor(context.read<AuthNotifier>().user?.id);
    showDialog<void>(
      context: context,
      useRootNavigator: true,
      builder: (_) => ChangeNotifierProvider(
        create: (_) => SyncImportNotifier(
          stravaActivities: pending.strava,
          psSteps: pending.polarsteps,
        ),
        child: SyncImportDialog(projectRef: widget.projectRef),
      ),
    ).then((_) {
      if (!mounted) return;
      notifier.markSynced();
      if (widget.initialZoom != null) notifier.setMapZoom(widget.initialZoom!);
      notifier.loadView(projectRef);
    });
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final notifier = context.read<ViewProjectNotifier>();
      // Resolve the URL-derived ref's role against the signed-in user so
      // editor gating is correct for shared projects (issue #106).
      final projectRef = widget.projectRef
          .resolveRoleFor(context.read<AuthNotifier>().user?.id);
      notifier.loadView(projectRef).then((_) {
        if (!mounted) return;
        if (notifier.error != null) {
          // Stale shared-project ref (owner renamed the trip) — issue #111.
          if (notifier.loadErrorStatus == 404 && !projectRef.isOwn) {
            recoverFromStaleSharedRef(context,
                staleRef: projectRef, routePath: '/view');
          }
          return;
        }
        saveLastOpenedProject(
            context.read<AuthNotifier>().user?.id, notifier.ref ?? projectRef);
        // Issue #207: tied to this screen's lifecycle, not loadView()'s —
        // see the equivalent call in app_screen.dart for why.
        _degradedRouteWatchNotifier = notifier;
        notifier.startDegradedRouteWatch(notifier.ref ?? projectRef);
      });
    });
    _mapEventSub =
        _mapController.mapController.mapEventStream.listen(_onMapEvent);
  }

  @override
  void dispose() {
    _mapEventSub?.cancel();
    _viewportSyncTimer?.cancel();
    _cameraIdleTimer?.cancel();
    _mapController.dispose();
    _degradedRouteWatchNotifier?.stopDegradedRouteWatch();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final title = context.select<ViewProjectNotifier, String>(
      (n) => n.projectName ?? widget.projectName,
    );
    final isLoading = context.select<ViewProjectNotifier, bool>((n) => n.isLoading);

    return Scaffold(
      floatingActionButton:
          buildProjectAddFab(context, context.read<ViewProjectNotifier>()),
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => context.go('/projects'),
        ),
        title: Text(
          title.isEmpty ? kAppName : title,
          overflow: TextOverflow.ellipsis,
        ),
        actions: [
          // Mode toggle — manage / view (active)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 4),
            child: SegmentedButton<bool>(
              segments: const [
                ButtonSegment(
                  value: false,
                  icon: Icon(Icons.edit_outlined),
                  tooltip: 'Manage mode',
                ),
                ButtonSegment(
                  value: true,
                  icon: Icon(Icons.visibility_outlined),
                  tooltip: 'View mode',
                ),
              ],
              selected: const {true},
              onSelectionChanged: (s) {
                // The toggle must win even if the map hasn't attached yet
                // (e.g. the user taps it before the meta load finishes and
                // mounts the map) — issue #219. Camera access throws in that
                // case; fall back to switching modes without a saved
                // viewport rather than silently dropping the tap.
                double? lat, lng, zoom;
                try {
                  final cam = _mapController.mapController.camera;
                  lat = cam.center.latitude;
                  lng = cam.center.longitude;
                  zoom = cam.zoom;
                } catch (_) {}
                context.go(widget.projectRef.withOwner(viewportSyncPath(
                  basePath: '/app',
                  projectName: widget.projectName,
                  lat: lat,
                  lng: lng,
                  zoom: zoom,
                )));
              },
              style: const ButtonStyle(
                  visualDensity: VisualDensity.compact,
                  tapTargetSize: MaterialTapTargetSize.shrinkWrap),
            ),
          ),

          // People directory (#40)
          IconButton(
            tooltip: 'Encounters',
            icon: const Icon(Icons.groups_outlined),
            onPressed: () async {
              final result = await Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => PeopleScreen(
                    notifier: context.read<ViewProjectNotifier>()),
              ));
              if (!mounted) return;
              // A location tapped inside PeopleScreen (issue #72) — focus it
              // on this screen's map.
              if (result is Map) {
                final lat = (result['lat'] as num?)?.toDouble();
                final lon = (result['lon'] as num?)?.toDouble();
                if (lat != null && lon != null) _focusLocation(lat, lon);
              }
            },
          ),

          Consumer<ViewProjectNotifier>(
            builder: (_, n, __) {
              final active = n.tagFilter.isNotEmpty;
              return IconButton(
                icon: Badge(
                  isLabelVisible: active,
                  label: Text('${n.tagFilter.length}'),
                  child: Icon(
                    Icons.label_outline,
                    color: active ? Theme.of(context).colorScheme.primary : null,
                  ),
                ),
                tooltip: 'Filter by tag',
                onPressed: n.availableTags.isEmpty
                    ? null
                    : () => showModalBottomSheet<void>(
                          context: context,
                          useRootNavigator: true,
                          builder: (_) => TagFilterSheet(
                              notifier: n, readOnly: true),
                        ),
              );
            },
          ),
          IconButton(
            icon: Icon(
              Icons.fit_screen,
              color: _autoZoom ? Theme.of(context).colorScheme.primary : null,
            ),
            tooltip: _autoZoom ? 'Auto-zoom on (tap to disable)' : 'Auto-zoom to selection',
            onPressed: () => setState(() => _autoZoom = !_autoZoom),
          ),
          IconButton(
            icon: const Icon(Icons.bar_chart_outlined),
            tooltip: 'Statistics',
            // The /stats route no longer reads GoRouterState.extra (issue #76
            // follow-up) — ProjectStatsScreen now sources tags/groups from the
            // ambient (manage-mode) ProjectNotifier singleton, loading this
            // project into it first if needed. That happens unconditionally
            // here since view mode's ViewProjectNotifier is a separate
            // instance the singleton doesn't share.
            onPressed: isLoading ? null : () => context.push(
              widget.projectRef.withOwner(
                  '/stats?project=${Uri.encodeComponent(widget.projectName)}'),
            ),
          ),
          IconButton(
            icon: const Icon(Icons.settings_outlined),
            tooltip: 'Settings',
            onPressed: () => context.push('/settings'),
          ),
          IconButton(
            icon: const Icon(Icons.arrow_back),
            tooltip: 'Back to projects',
            onPressed: () => context.go('/projects'),
          ),
          const SizedBox(width: 4),
        ],
      ),
      body: Column(
        children: [
          // ── Auto-sync banner ─────────────────────────────────────────────
          Selector<ViewProjectNotifier, bool>(
            selector: (_, n) => n.pendingSync != null,
            builder: (context, hasSync, __) {
              if (!hasSync) return const SizedBox.shrink();
              final n = context.read<ViewProjectNotifier>();
              final strava = n.pendingSync!.strava.length;
              final ps = n.pendingSync!.polarsteps.length;
              final parts = [
                if (strava > 0) '$strava Strava ${strava == 1 ? 'activity' : 'activities'}',
                if (ps > 0) '$ps Polarsteps ${ps == 1 ? 'step' : 'steps'}',
              ];
              return MaterialBanner(
                padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
                content: Text('New: ${parts.join(' and ')}'),
                leading: const Icon(Icons.sync, size: 20),
                actions: [
                  TextButton(
                    onPressed: () => context.read<ViewProjectNotifier>().markSynced(),
                    child: const Text('Later'),
                  ),
                  TextButton(
                    onPressed: () => _openSyncDialog(context),
                    child: const Text('Import'),
                  ),
                ],
              );
            },
          ),
          // ── Offline / stale-cache banner ──────────────────────────────────
          Selector<ViewProjectNotifier, bool>(
            selector: (_, n) => n.offlineFromCache,
            builder: (context, offline, __) {
              if (!offline) return const SizedBox.shrink();
              final n = context.read<ViewProjectNotifier>();
              return MaterialBanner(
                padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
                content: const Text(
                  "Showing the last saved version of this trip — couldn't "
                  'reach the server.',
                ),
                leading: const Icon(Icons.cloud_off, size: 20),
                actions: [
                  TextButton(
                    onPressed: n.dismissOfflineBanner,
                    child: const Text('Dismiss'),
                  ),
                  TextButton(
                    onPressed: () {
                      final ref = n.ref;
                      if (ref != null) n.loadView(ref);
                    },
                    child: const Text('Retry'),
                  ),
                ],
              );
            },
          ),
          // ── Degraded-route upgrade banner (issue #207) ───────────────────
          Selector<ViewProjectNotifier, bool>(
            selector: (_, n) => n.degradedRouteUpgradeAvailable,
            builder: (context, hasUpgrade, __) {
              if (!hasUpgrade) return const SizedBox.shrink();
              final n = context.read<ViewProjectNotifier>();
              return MaterialBanner(
                padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
                content: const Text(
                  'A route that was only an approximate straight line has '
                  'been resolved with real track data.',
                ),
                leading: const Icon(Icons.route, size: 20),
                actions: [
                  TextButton(
                    onPressed: n.dismissDegradedRouteUpgrade,
                    child: const Text('Later'),
                  ),
                  TextButton(
                    onPressed: n.reloadForDegradedUpgrade,
                    child: const Text('Reload'),
                  ),
                ],
              );
            },
          ),
          Expanded(
            child: Consumer<ViewProjectNotifier>(
              builder: (context, notifier, _) {
                if (!notifier.isMetaLoaded && notifier.geo == null) {
                  if (notifier.error != null) {
                    return Center(
                      child: Padding(
                        padding: const EdgeInsets.all(24),
                        child: Text(
                          notifier.error!,
                          style: TextStyle(
                              color: Theme.of(context).colorScheme.error),
                          textAlign: TextAlign.center,
                        ),
                      ),
                    );
                  }
                  return const Center(child: CircularProgressIndicator());
                }
                return _ViewLayout(
                  notifier: notifier,
                  mapController: _mapController,
                  autoZoom: _autoZoom,
                  initialLat: widget.initialLat,
                  initialLng: widget.initialLng,
                  initialZoom: widget.initialZoom,
                  focusedLatLng: _focusedLatLng,
                  onLocationTap: _focusLocation,
                  onClearFocusedLocation: _clearFocusedLocation,
                  hereLatLng: _hereLatLng,
                  locatingHere: _locatingHere,
                  onLocateMe: _locateMe,
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

// ── Layout: map + elevation chart ─────────────────────────────────────────────

class _ViewLayout extends StatelessWidget {
  final ViewProjectNotifier notifier;
  final AnimatedMapController mapController;
  final bool autoZoom;
  final double? initialLat;
  final double? initialLng;
  final double? initialZoom;
  final LatLng? focusedLatLng;
  final void Function(double lat, double lon)? onLocationTap;
  final VoidCallback? onClearFocusedLocation;
  final LatLng? hereLatLng;
  final bool locatingHere;
  final VoidCallback? onLocateMe;

  const _ViewLayout({
    required this.notifier,
    required this.mapController,
    required this.autoZoom,
    this.initialLat,
    this.initialLng,
    this.initialZoom,
    this.focusedLatLng,
    this.onLocationTap,
    this.onClearFocusedLocation,
    this.hereLatLng,
    this.locatingHere = false,
    this.onLocateMe,
  });

  @override
  Widget build(BuildContext context) {
    final selActId = notifier.selectedActivityId;
    final elevChart = notifier.isElevationLoaded
        ? ElevationChart(
            activities: notifier.activities,
            selectedActivityId: selActId,
            onCursorChanged: (pos) =>
                notifier.elevationCursorNotifier.value = pos,
            mapCursorNotifier: notifier.mapCursorDistNotifier,
            track: selActId != null
                ? notifier.perActivityTracks[selActId.toString()] ??
                    notifier.fullTrack
                : notifier.fullTrack,
            color: notifier.effectiveElevationChartColor,
          )
        : const ElevationLoadingPlaceholder();

    final mapPanel = MapPanel(
      notifier: notifier,
      mapController: mapController,
      basemapUrl: kActiveViewBasemapUrl,
      labelsUrl: kActiveViewLabelsOverlayUrl,
      basemapStyleUri: kActiveViewStyleUri,
      autoZoom: autoZoom,
      initialLat: initialLat,
      initialLng: initialLng,
      initialZoom: initialZoom,
      // Owner-only — this screen is authenticated, unlike shared_project_screen.dart
      // which reuses MapPanel for public/unauthenticated links (issue #71).
      showEncounters: true,
      focusedLatLng: focusedLatLng,
      onLocationTap: onLocationTap,
      onClearFocusedLocation: onClearFocusedLocation,
      // Owner-only, same reasoning as showEncounters above (issue #88) —
      // shared_project_screen.dart's public MapPanel never sets this.
      showLocateMe: true,
      hereLatLng: hereLatLng,
      locatingHere: locatingHere,
      onLocateMe: onLocateMe,
    );

    return Column(
      children: [
        Expanded(
          child: Stack(
            fit: StackFit.expand,
            children: [
              mapPanel,
              // Inset top/bottom so the carousel clears MapPanel's own
              // top-right selection-stats badge and bottom-right locate-me
              // button (both live inside MapPanel's Stack at the same
              // right:12 edge) instead of overlapping them.
              Positioned(
                top: 90,
                bottom: 80,
                right: 0,
                child: DayCarousel(notifier: notifier),
              ),
            ],
          ),
        ),
        elevChart,
      ],
    );
  }
}
