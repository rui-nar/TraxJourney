/// Main app screen — map + activity panel for an open project.
// ignore_for_file: deprecated_member_use
library;

import 'dart:async' show TimeoutException, Timer, StreamSubscription;
import 'dart:typed_data' show Uint8List;
import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart' show LatLngBounds, MapEvent;
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:go_router/go_router.dart';
import 'package:latlong2/latlong.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'basemaps.dart';
import 'geo_viewport.dart';
import 'download_stub.dart' if (dart.library.html) 'download_web.dart';
import 'elevation_chart.dart';
import 'gpx_import_dialog.dart';
import '../api/client.dart' show ApiException;
import '../auth/auth_notifier.dart';
import '../core/brand.dart';
import '../core/current_location.dart' show currentDeviceLatLng;
import '../core/design_tokens.dart' show kWarning, kWarningDark;
import '../core/last_opened_project.dart';
import '../core/perf_timing.dart' show kPerfNoMap, perfSpans;
import '../core/project_ref.dart';
import '../core/stale_shared_ref.dart';
import 'project_notifier.dart';
import 'activity_panel.dart';
import 'panel_resize.dart';
import 'people_screen.dart';
import 'map_panel.dart';
import 'project_add_fab.dart';
import 'project_file.dart';
import 'image_export.dart';
import 'image_download.dart';
import 'poster_config_dialog.dart';
import 'poster_job_notifier.dart';
import 'poster_status_card.dart';
import 'poster_title_dialog.dart';
import 'social_share_dialog.dart';
import 'sync_import_notifier.dart';
import 'sync_import_dialog.dart';
import 'viewport_sync.dart';

// ── AppScreen ─────────────────────────────────────────────────────────────────

class AppScreen extends StatefulWidget {
  final String projectName;

  /// Owning user's id for a project shared with the caller (issue #106);
  /// null for one of the caller's own projects.
  final int? ownerId;

  /// Camera position carried over from view mode via the mode toggle, so
  /// switching modes doesn't reset the map viewport to fit-all-bounds.
  final double? initialLat;
  final double? initialLng;
  final double? initialZoom;

  const AppScreen({
    super.key,
    required this.projectName,
    this.ownerId,
    this.initialLat,
    this.initialLng,
    this.initialZoom,
  });

  /// Addressing for [projectName]/[ownerId], threaded down to the notifier
  /// and used to build any project-scoped URL this screen needs directly.
  ProjectRef get projectRef => ProjectRef(name: projectName, ownerId: ownerId);

  @override
  State<AppScreen> createState() => _AppScreenState();
}

class _AppScreenState extends State<AppScreen> with TickerProviderStateMixin {
  late final AnimatedMapController _mapController =
      AnimatedMapController(vsync: this, duration: const Duration(milliseconds: 500));
  final GlobalKey<ManageMapPanelState> _mapPanelKey = GlobalKey();

  // Captured (not looked up via context) so dispose() can stop it: when this
  // widget is torn down as part of a larger subtree/route unmount, its
  // element is already deactivated by the time dispose() runs, and
  // context.read() on a deactivated element throws ("Looking up a
  // deactivated widget's ancestor is unsafe") — issue #207 CI failure.
  ProjectNotifier? _degradedRouteWatchNotifier;
  // Survives ManageMapPanelState recreation — prevents re-fitting after user pans.
  // Seeded true when a camera position was carried over from view mode.
  // Seeded in initState, NOT lazily: the camera→URL sync writes lat/lng onto
  // our own route, so a `late` initialiser read after that point would mistake
  // a self-written param for "the user arrived with an explicit viewport" and
  // skip the fit (the view-mode regression). The answer is about arrival.
  late final ValueNotifier<bool> _mapFitted;
  final ScrollController _activityScrollController = ScrollController();
  // Separate controller for the narrow-layout overlay panel: wide and narrow
  // are mutually-exclusive LayoutBuilder branches, and sharing one controller
  // across them risks "attached to multiple scroll views" during a resize.
  final ScrollController _mobileActivityScrollController = ScrollController();
  bool _panelOpen = false;
  void _togglePanel() => setState(() => _panelOpen = !_panelOpen);
  bool _autoZoom = false;
  bool _isExporting = false;

  // Poster generation flow (issue #14, unit F) — frame-picker overlay toggle.
  bool _framePickerActive = false;

  // Poster status card (issue #14, unit G) — small ambient overlay tracking
  // the poster job's real server-side state; see poster_status_card.dart.
  // Owned here (not via Provider) so its lifetime matches this screen's, the
  // same reasoning as _mapController/_mapFitted above.
  final PosterStatusNotifier _posterStatusNotifier = PosterStatusNotifier();

  // Highlighted point set when the user taps an encounter's place icon
  // (issue #72); cleared on the next unrelated map tap/selection.
  LatLng? _focusedLatLng;

  // Debounced camera → URL sync (issue #76 follow-up) so a forced reload (the
  // black-screen JS backstop) or a normal browser refresh restores the
  // viewport the user was actually looking at, not just the position carried
  // over from the last mode-toggle switch.
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
    context.read<ProjectNotifier>()
      ..setMapCameraActive(true)
      // Zoom level of detail (issue #295) and its viewport box (#324): the
      // notifier fetches geometry matched to what is on screen, so it has to
      // know what is on screen — both how much detail, and which region.
      ..setMapZoom(camera.zoom,
          viewport: viewportBox(
              visible.west, visible.south, visible.east, visible.north));
    _cameraIdleTimer?.cancel();
    _cameraIdleTimer = Timer(const Duration(milliseconds: 250), () {
      if (mounted) context.read<ProjectNotifier>().setMapCameraActive(false);
    });
    _viewportSyncTimer?.cancel();
    _viewportSyncTimer = Timer(const Duration(milliseconds: 700), () {
      // Guards against a missing GoRouter ancestor (e.g. this widget under
      // test in isolation) — context.replace() would otherwise throw.
      if (!mounted || GoRouter.maybeOf(context) == null) return;
      // Recorded because this fires *while the user is panning* and performs a
      // route change; if a remount/reload is what lands heavy work mid-gesture
      // (issue #276), this is the line that starts it.
      perfSpans.recordBackgroundRefresh('viewport_url_sync');
      final cam = _mapController.mapController.camera;
      context.replace(viewportSyncPath(
        basePath: '/app',
        projectName: widget.projectName,
        ownerId: widget.ownerId,
        lat: cam.center.latitude,
        lng: cam.center.longitude,
        zoom: cam.zoom,
      ));
    });
  }

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

  // Width of the wide-layout activity panel; drag the divider to resize.
  static const String _kPanelWidthPref = 'activity_panel_width';
  double _panelWidth = 280;

  void _onPanelDrag(double dx, double available) {
    setState(() => _panelWidth = clampPanelWidth(
          current: _panelWidth,
          dx: dx,
          available: available,
        ));
  }

  Future<void> _savePanelWidth() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setDouble(_kPanelWidthPref, _panelWidth);
  }

  @override
  void initState() {
    super.initState();
    _mapFitted = ValueNotifier(widget.initialLat != null);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final notifier = context.read<ProjectNotifier>();
      // The URL-derived ref carries no role — resolve it against the signed-in
      // user so capability-gated UI (ProjectNotifier.canEditContent etc.,
      // issue #109) is correct for shared projects (issue #106).
      final projectRef = widget.projectRef
          .resolveRoleFor(context.read<AuthNotifier>().user?.id);

      void afterLoad() {
        if (!mounted) return;
        if (notifier.error != null) {
          // Stale shared-project ref (owner renamed the trip) — issue #111.
          if (notifier.loadErrorStatus == 404 && !projectRef.isOwn) {
            recoverFromStaleSharedRef(context,
                staleRef: projectRef, routePath: '/app');
          }
          return;
        }
        saveLastOpenedProject(
            context.read<AuthNotifier>().user?.id, notifier.ref ?? projectRef);
        // Issue #207: tied to this screen's lifecycle, not load()'s — this
        // notifier is also reused ambiently by screens (e.g. ProjectStatsScreen)
        // that never render the banner and must not carry this timer around.
        _degradedRouteWatchNotifier = notifier;
        notifier.startDegradedRouteWatch(notifier.ref ?? projectRef);
      }

      // This (singleton, manage-mode) notifier may already hold this exact
      // project — e.g. toggling back from view mode a moment later — in
      // which case a fresh network round trip is pure redundancy. Only skip
      // it when the held data is actually good: a matching ref that finished
      // loading without error. A mismatched ref, a still-in-flight load, or
      // one that failed all still need a real load() — mirrors (with the
      // extra loading/error guards) ProjectStatsScreen's identical singleton
      // reuse check (see project_stats_screen.dart).
      if (notifier.ref == projectRef &&
          !notifier.isLoading &&
          notifier.error == null) {
        afterLoad();
      } else {
        // Seed the zoom before loading, so the first geometry fetch is for
        // the level about to be shown rather than a hard-coded default that
        // a second fetch would immediately correct (issue #295).
        if (widget.initialZoom != null) notifier.setMapZoom(widget.initialZoom!);
        notifier.load(projectRef).then((_) => afterLoad());
      }
    });
    SharedPreferences.getInstance().then((prefs) {
      final saved = prefs.getDouble(_kPanelWidthPref);
      if (saved != null && mounted) {
        setState(() => _panelWidth =
            saved.clamp(kMinPanelWidth, kMaxPanelWidth).toDouble());
      }
    });
    _mapEventSub =
        _mapController.mapController.mapEventStream.listen(_onMapEvent);
    // Resumes tracking a poster job started before the user navigated away
    // and back within this session (issue #14 rule 5) — a one-shot check,
    // not a poll loop of its own; see PosterStatusNotifier.resume.
    _posterStatusNotifier.resume(widget.projectRef);
  }

  @override
  void dispose() {
    _mapEventSub?.cancel();
    _viewportSyncTimer?.cancel();
    _cameraIdleTimer?.cancel();
    _mapController.dispose();
    _mapFitted.dispose();
    _activityScrollController.dispose();
    _mobileActivityScrollController.dispose();
    _posterStatusNotifier.dispose();
    _degradedRouteWatchNotifier?.stopDegradedRouteWatch();
    super.dispose();
  }


  /// Builds a sub-route location for [path] carrying `project` (and `owner`
  /// when this is a shared project — issue #106) so a reload/deep-link into
  /// e.g. `/stats` or `/project-settings` still resolves the right project.
  String _route(String path) => widget.projectRef
      .withOwner('$path?project=${Uri.encodeComponent(widget.projectName)}');

  Future<void> _downloadFile(String apiPath, String fallbackFilename) async {
    setState(() => _isExporting = true);
    try {
      final res = await context
          .read<ProjectNotifier>()
          .fetchExportBytes(apiPath);
      String filename = fallbackFilename;
      final cd = res.headers['content-disposition'] ?? '';
      final match = RegExp(r'filename="([^"]+)"').firstMatch(cd);
      if (match != null) filename = match.group(1)!;

      final mimeType =
          res.headers['content-type'] ?? 'application/octet-stream';
      triggerBrowserDownload(res.bodyBytes, mimeType, filename);

      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$filename downloaded')));
      }
    } on Exception catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(
              'Export failed: ${e.toString().replaceFirst('Exception: ', '')}')));
    } finally {
      if (mounted) setState(() => _isExporting = false);
    }
  }

  Future<void> _exportOptions() async {
    final name = widget.projectName;
    final ref = widget.projectRef;

    final notifier = context.read<ProjectNotifier>();
    final hasMemoryPhotos = notifier.items.any(
      (i) =>
          i['item_type'] == 'memory' &&
          ((i['memory']?['photos'] as List?)?.isNotEmpty ?? false),
    );

    if (!mounted) return;
    final choice = await showDialog<String>(
      context: context,
      builder: (ctx) => SimpleDialog(
        title: const Text('Export project'),
        children: [
          SimpleDialogOption(
            onPressed: () => Navigator.of(ctx).pop('gpx'),
            child: const ListTile(
              leading: Icon(Icons.map_outlined),
              title: Text('GPX file'),
              subtitle: Text('Memories as waypoints, no photos'),
            ),
          ),
          SimpleDialogOption(
            onPressed: () => Navigator.of(ctx).pop('project'),
            child: const ListTile(
              leading: Icon(Icons.article_outlined),
              title: Text('.$kProjectFileExtension file'),
              subtitle: Text('Full project data, no photo files'),
            ),
          ),
          if (hasMemoryPhotos)
            SimpleDialogOption(
              onPressed: () => Navigator.of(ctx).pop('zip'),
              child: const ListTile(
                leading: Icon(Icons.archive_outlined),
                title: Text('ZIP archive'),
                subtitle: Text('.$kProjectFileExtension + all memory photos'),
              ),
            ),
          SimpleDialogOption(
            onPressed: () => Navigator.of(ctx).pop('image'),
            child: const ListTile(
              leading: Icon(Icons.photo_outlined),
              title: Text('Export image (PNG)'),
              subtitle: Text('Map + elevation chart as a high-quality image'),
            ),
          ),
          SimpleDialogOption(
            onPressed: () => Navigator.of(ctx).pop('poster'),
            child: const ListTile(
              leading: Icon(Icons.map),
              title: Text('Generate poster…'),
              subtitle: Text('High-resolution A0 poster (PNG/PDF)'),
            ),
          ),
          SimpleDialogOption(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const ListTile(
              leading: Icon(Icons.close),
              title: Text('Cancel'),
            ),
          ),
        ],
      ),
    );

    if (choice == null || !mounted) return;
    if (choice == 'gpx') {
      await _downloadFile(ref.path('/export'), '$name.gpx');
    } else if (choice == 'project') {
      await _downloadFile(ref.path(kProjectFileExportRoute),
          '$name.$kProjectFileExtension');
    } else if (choice == 'zip') {
      await _downloadFile(ref.path('/export-zip'), '$name.zip');
    } else if (choice == 'image') {
      await _exportImage();
    } else if (choice == 'poster') {
      setState(() => _framePickerActive = true);
    }
  }

  Future<void> _exportImage() async {
    if (!mounted) return;
    if (_isExporting) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Export already in progress')));
      return;
    }
    await showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => ImageExportDialog(
        projectName: widget.projectName,
        onExport: _startExport,
      ),
    );
  }

  Future<void> _startExport(ImageExportOptions opts) async {
    if (!mounted) return;
    setState(() => _isExporting = true);
    final messenger = ScaffoldMessenger.of(context);
    try {
      final png = await performOffscreenExport(
        context: context,
        notifier: context.read<ProjectNotifier>(),
        projectName: widget.projectName,
        opts: opts,
      );
      if (png != null) {
        downloadPng(png, '${widget.projectName}.png');
        messenger.showSnackBar(const SnackBar(
            content: Text('Export complete'),
            duration: Duration(seconds: 3)));
      }
    } finally {
      if (mounted) setState(() => _isExporting = false);
    }
  }

  // ── Poster generation flow (issue #14, unit F) ────────────────────────────
  // Frame picker (region + orientation) -> config dialog (which sections to
  // include) -> title dialog (position/text/size of the title card) ->
  // preview -> job kicked off, user notified by email when ready.

  void _cancelFramePicker() {
    if (!mounted) return;
    setState(() => _framePickerActive = false);
  }

  Future<void> _onFrameConfirmed(
      LatLngBounds bounds, String orientation, String paperSize) async {
    if (!mounted) return;
    setState(() => _framePickerActive = false);
    await showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (_) => PosterConfigDialog(
        onConfirm: (opts) =>
            _showPosterTitleDialog(bounds, orientation, paperSize, opts),
      ),
    );
  }

  Future<void> _showPosterTitleDialog(LatLngBounds bounds, String orientation,
      String paperSize, PosterConfigOptions opts) async {
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (_) => PosterTitleDialog(
        orientation: orientation,
        initialTitle: widget.projectName,
        onConfirm: (titleOpts) => _showPosterPreview(
            bounds, orientation, paperSize, opts, titleOpts),
      ),
    );
  }

  List<Map<String, dynamic>> _posterMemoriesPayload() {
    final notifier = context.read<ProjectNotifier>();
    return [
      for (final item in notifier.items)
        if (item['item_type'] == 'memory' && item['memory'] is Map)
          posterMemoryJson((item['memory'] as Map).cast<String, dynamic>()),
    ];
  }

  Future<void> _showPosterPreview(
      LatLngBounds bounds,
      String orientation,
      String paperSize,
      PosterConfigOptions opts,
      PosterTitleOptions titleOpts) async {
    if (!mounted) return;
    final memories = _posterMemoriesPayload();
    await showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (_) => _PosterPreviewDialog(
        projectRef: widget.projectRef,
        bounds: bounds,
        orientation: orientation,
        paperSize: paperSize,
        opts: opts,
        titleOpts: titleOpts,
        memories: memories,
        onGenerate: () => _startPosterJob(
            bounds, orientation, paperSize, opts, titleOpts, memories),
      ),
    );
  }

  /// Kicks off the poster job and hands control straight back to the user
  /// (issue #14 feedback): a blocking, undismissable dialog polling for up to
  /// 120s used to report a false "timed out" failure on real A0 renders that
  /// legitimately run longer, even though the server kept working. Now the
  /// client only needs to confirm the job was *created* — the render happens
  /// server-side and the user is emailed a download link when it's ready.
  Future<void> _startPosterJob(
      LatLngBounds bounds,
      String orientation,
      String paperSize,
      PosterConfigOptions opts,
      PosterTitleOptions titleOpts,
      List<Map<String, dynamic>> memories) async {
    if (!mounted) return;
    final messenger = ScaffoldMessenger.of(context);
    try {
      final jobId = await createPosterJob(
        ref: widget.projectRef,
        bounds: posterBoundsFromLatLngBounds(bounds),
        orientation: orientation,
        paperSize: paperSize,
        config: opts.toJson(),
        memories: memories,
        titlePosition: {'x': titleOpts.positionX, 'y': titleOpts.positionY},
        titleText: titleOpts.titleText,
        titleScale: titleOpts.titleScale,
      );
      // Ambient status card (issue #14, unit G) picks up from here — the
      // SnackBar below is a one-off confirmation, the card is what actually
      // reflects real server state (generating/done/failed) afterwards.
      _posterStatusNotifier.start(ref: widget.projectRef, jobId: jobId);
      messenger.showSnackBar(const SnackBar(
          content: Text("Generating your poster — we'll email you a "
              "download link when it's ready.")));
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(
          content: Text('Could not start poster generation: ${e.body}')));
    } catch (e) {
      messenger.showSnackBar(SnackBar(
          content: Text(
              'Could not start poster generation: ${e.toString().replaceFirst('Exception: ', '')}')));
    }
  }

  void _openSyncDialog(BuildContext context) {
    final notifier = context.read<ProjectNotifier>();
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
      notifier.load(projectRef);
    });
  }

  Future<void> _openGpxImportDialog(BuildContext context) async {
    final notifier = context.read<ProjectNotifier>();
    final messenger = ScaffoldMessenger.of(context);
    final imported = await showDialog<GpxImportResult>(
      context: context,
      useRootNavigator: true,
      builder: (_) => GpxImportDialog(
        projectRef: widget.projectRef,
        // So the date picker opens on the trip rather than on today, and a date
        // outside it is flagged before it silently extends the trip.
        tripStart: notifier.tripStart,
        tripEnd: notifier.tripEnd,
      ),
    );
    if (imported == null || !mounted) return;

    await notifier.load(widget.projectRef);
    if (!mounted) return;
    // An import is never a one-way door: View goes to what was just added,
    // Undo deletes it. Both are cheap here because a GPX activity is local.
    // A SnackBar carries one action, and this needs two — so Undo sits in the
    // content beside the message and View stays the primary.
    messenger.showSnackBar(SnackBar(
      duration: const Duration(seconds: 6),
      content: Row(
        children: [
          Expanded(child: Text('Imported "${imported.name}"')),
          TextButton(
            onPressed: () async {
              messenger.hideCurrentSnackBar();
              // The snackbar outlives the screen, and the delete goes through
              // whichever trip the notifier is pointed at when it is tapped.
              // The server now refuses a cross-trip delete outright (#405), so
              // this guard is no longer what keeps the other trip's activity
              // safe — it keeps the six-second window from turning Undo into
              // "Could not undo the import." for someone who only changed
              // trips, and whose import is still sitting where they left it.
              if (notifier.ref != widget.projectRef) return;
              try {
                await notifier.deleteLocalActivity(imported.activityId);
              } catch (_) {
                if (!mounted) return;
                messenger.showSnackBar(const SnackBar(
                    content: Text('Could not undo the import.')));
              }
            },
            child: const Text('Undo'),
          ),
        ],
      ),
      action: SnackBarAction(
        label: 'View',
        onPressed: () => notifier.selectActivity(imported.activityId),
      ),
    ));
  }

  void _showFilterSheet(BuildContext context, ProjectNotifier notifier,
      {required bool readOnly}) {
    showModalBottomSheet<void>(
      context: context,
      useRootNavigator: true,
      isScrollControlled: true,
      builder: (_) => FilterSheet(notifier: notifier, readOnly: readOnly),
    );
  }

  void _showShareDialog() {
    // Repurposed: the top-bar share button now opens the social-share composer.
    // Read-only link management lives in the project settings "Share" section.
    showSocialShareDialog(context, context.read<ProjectNotifier>());
  }

  @override
  Widget build(BuildContext context) {
    // Only rebuild AppScreen (AppBar + LayoutBuilder) when the title changes.
    // ActivityPanel and MapPanel subscribe to the notifier themselves via Consumer,
    // so they still react to every notifyListeners() without pulling the AppBar
    // through an unnecessary rebuild on every selectActivity() call.
    final title = context.select<ProjectNotifier, String>(
      (n) => n.projectName ?? widget.projectName,
    );
    final isLoading = context.select<ProjectNotifier, bool>((n) => n.isLoading);

    final isNarrow = MediaQuery.sizeOf(context).width < 720;

    return Scaffold(
      floatingActionButton:
          buildProjectAddFab(context, context.read<ProjectNotifier>()),
      appBar: AppBar(
        automaticallyImplyLeading: false,
        title: Text(
          title.isEmpty ? kAppName : title,
          overflow: TextOverflow.ellipsis,
        ),
        actions: [
          // Hamburger — narrow only
          if (isNarrow)
            IconButton(
              icon: AnimatedSwitcher(
                duration: const Duration(milliseconds: 200),
                child: Icon(
                  _panelOpen ? Icons.menu_open : Icons.menu,
                  key: ValueKey(_panelOpen),
                ),
              ),
              onPressed: _togglePanel,
            ),

          // View mode toggle — always
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
              selected: const {false},
              onSelectionChanged: (s) {
                // Same guard as view_screen.dart's toggle (issue #219): the
                // toggle must win even if the map hasn't attached yet.
                double? lat, lng, zoom;
                try {
                  final cam = _mapController.mapController.camera;
                  lat = cam.center.latitude;
                  lng = cam.center.longitude;
                  zoom = cam.zoom;
                } catch (_) {}
                context.go(widget.projectRef.withOwner(viewportSyncPath(
                  basePath: '/view',
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
                builder: (_) =>
                    PeopleScreen(notifier: context.read<ProjectNotifier>()),
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

          // Filter — always visible
          Consumer<ProjectNotifier>(
            builder: (_, n, __) {
              final active = n.hasActiveFilter;
              return IconButton(
                icon: Badge(
                  isLabelVisible: active,
                  label: Text('${n.activeFilterCount}'),
                  child: Icon(
                    Icons.tune,
                    color: active ? Theme.of(context).colorScheme.primary : null,
                  ),
                ),
                tooltip: 'Filter',
                onPressed: !n.hasFilterableContent
                    ? null
                    : () => _showFilterSheet(context, n, readOnly: false),
              );
            },
          ),

          // Auto-zoom — always visible
          IconButton(
            icon: Icon(
              Icons.fit_screen,
              color: _autoZoom ? Theme.of(context).colorScheme.primary : null,
            ),
            tooltip: _autoZoom ? 'Auto-zoom on (tap to disable)' : 'Auto-zoom to selection',
            onPressed: () => setState(() => _autoZoom = !_autoZoom),
          ),

          if (isNarrow) ...[
            // ── Narrow: stats + strava visible; rest in overflow (#94) ──
            IconButton(
              icon: const Icon(Icons.bar_chart_outlined),
              tooltip: 'Statistics',
              onPressed: isLoading ? null : () => context.push(
                _route('/stats'),
              ),
            ),
            IconButton(
              icon: const Icon(Icons.playlist_add),
              tooltip: 'Import activities from Strava',
              onPressed: () => context.push(
                  _route('/strava-import')),
            ),
            PopupMenuButton<int>(
              icon: const Icon(Icons.more_vert),
              tooltip: 'More options',
              onSelected: (v) {
                switch (v) {
                  case 0: if (!_isExporting) _exportOptions();
                  case 1: context.push(
                      _route('/polarsteps-import'));
                  case 2: _openGpxImportDialog(context);
                  case 3: _showShareDialog();
                  case 4: context.push(
                    _route('/project-settings'),
                  );
                  case 5: context.push('/settings');
                  case 6: context.go('/projects');
                }
              },
              itemBuilder: (_) => [
                PopupMenuItem(
                  value: 0,
                  enabled: !_isExporting,
                  child: ListTile(
                    leading: _isExporting
                        ? const SizedBox(width: 24, height: 24,
                            child: CircularProgressIndicator(strokeWidth: 2))
                        : const Icon(Icons.download),
                    title: const Text('Export project'),
                    enabled: !_isExporting,
                  ),
                ),
                const PopupMenuItem(
                  value: 1,
                  child: ListTile(
                    leading: Icon(Icons.explore_outlined),
                    title: Text('Import steps from Polarsteps'),
                  ),
                ),
                const PopupMenuItem(
                  value: 2,
                  child: ListTile(
                    leading: Icon(Icons.upload_file),
                    title: Text('Import GPX file'),
                  ),
                ),
                const PopupMenuItem(
                  value: 3,
                  child: ListTile(
                    leading: Icon(Icons.share_outlined),
                    title: Text('Share'),
                  ),
                ),
                PopupMenuItem(
                  value: 4,
                  enabled: !isLoading,
                  child: ListTile(
                    leading: const Icon(Icons.tune),
                    title: const Text('Project settings'),
                    enabled: !isLoading,
                  ),
                ),
                const PopupMenuItem(
                  value: 5,
                  child: ListTile(
                    leading: Icon(Icons.settings_outlined),
                    title: Text('Settings'),
                  ),
                ),
                const PopupMenuItem(
                  value: 6,
                  child: ListTile(
                    leading: Icon(Icons.arrow_back),
                    title: Text('Back to projects'),
                  ),
                ),
              ],
            ),
          ] else ...[
            // ── Wide: all icons in original order ─────────────────────────
            IconButton(
              icon: _isExporting
                  ? const SizedBox(width: 20, height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.download),
              tooltip: 'Export project',
              onPressed: _isExporting ? null : _exportOptions,
            ),
            IconButton(
              icon: const Icon(Icons.playlist_add),
              tooltip: 'Import activities from Strava',
              onPressed: () => context.push(
                  _route('/strava-import')),
            ),
            IconButton(
              icon: const Icon(Icons.explore_outlined),
              tooltip: 'Import steps from Polarsteps',
              onPressed: () => context.push(
                  _route('/polarsteps-import')),
            ),
            IconButton(
              icon: const Icon(Icons.upload_file),
              tooltip: 'Import GPX file',
              onPressed: () => _openGpxImportDialog(context),
            ),
            IconButton(
              icon: const Icon(Icons.share_outlined),
              tooltip: 'Share project',
              onPressed: _showShareDialog,
            ),
            IconButton(
              icon: const Icon(Icons.bar_chart_outlined),
              tooltip: 'Statistics',
              onPressed: isLoading ? null : () => context.push(
                _route('/stats'),
              ),
            ),
            IconButton(
              icon: const Icon(Icons.tune),
              tooltip: 'Project settings',
              onPressed: isLoading ? null : () => context.push(
                _route('/project-settings'),
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
        ],
      ),
      body: Column(
        children: [
          // ── Auto-sync banner ───────────────────────────────────────────
          Selector<ProjectNotifier, bool>(
            selector: (_, n) => n.pendingSync != null,
            builder: (context, hasSync, __) {
              if (!hasSync) return const SizedBox.shrink();
              final n = context.read<ProjectNotifier>();
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
                    onPressed: () => context.read<ProjectNotifier>().markSynced(),
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
          Selector<ProjectNotifier, bool>(
            selector: (_, n) => n.offlineFromCache,
            builder: (context, offline, __) {
              if (!offline) return const SizedBox.shrink();
              final n = context.read<ProjectNotifier>();
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
                      if (ref != null) n.load(ref);
                    },
                    child: const Text('Retry'),
                  ),
                ],
              );
            },
          ),
          // ── Degraded-route upgrade banner (issue #207) ───────────────────
          Selector<ProjectNotifier, bool>(
            selector: (_, n) => n.degradedRouteUpgradeAvailable,
            builder: (context, hasUpgrade, __) {
              if (!hasUpgrade) return const SizedBox.shrink();
              final n = context.read<ProjectNotifier>();
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
          Expanded(child: LayoutBuilder(
        builder: (context, constraints) {
          // Auto-close the panel when switching to wide layout.
          if (constraints.maxWidth >= 720 && _panelOpen) {
            WidgetsBinding.instance.addPostFrameCallback((_) {
              if (mounted && _panelOpen) setState(() => _panelOpen = false);
            });
          }
          if (constraints.maxWidth >= 720) {
            // ── Wide layout: side-by-side ────────────────────────────────
            return Row(
              children: [
                SizedBox(
                  width: _panelWidth,
                  child: Consumer<ProjectNotifier>(
                    builder: (_, n, __) => ActivityPanel(
                      notifier: n,
                      mapController: _mapController,
                      scrollController: _activityScrollController,
                      onLocationTap: _focusLocation,
                    ),
                  ),
                ),
                _PanelResizeHandle(
                  onDrag: (dx) => _onPanelDrag(dx, constraints.maxWidth),
                  onDragEnd: _savePanelWidth,
                ),
                Expanded(
                  child: Stack(
                    children: [
                      // RepaintBoundary isolates the map's raster from the
                      // activity panel's scroll. Without it, scrolling the
                      // sibling list re-rasterizes the (expensive) map every
                      // frame on web/CanvasKit — measured ~85ms/frame raster.
                      RepaintBoundary(child: kPerfNoMap
                          ? const ColoredBox(color: Color(0xFF334155))
                          : Consumer<ProjectNotifier>(
                        builder: (_, n, __) => ManageMapPanel(
                          key: _mapPanelKey,
                          notifier: n,
                          mapController: _mapController,
                          autoZoom: _autoZoom,
                          basemapUrl: kActiveManageBasemapUrl,
                          basemapSubdomains: kActiveManageSubdomains,
                          fittedNotifier: _mapFitted,
                          basemapStyleUri: kActiveManageStyleUri,
                          initialLat: widget.initialLat,
                          initialLng: widget.initialLng,
                          initialZoom: widget.initialZoom,
                          focusedLatLng: _focusedLatLng,
                          onLocationTap: _focusLocation,
                          onClearFocusedLocation: _clearFocusedLocation,
                          hereLatLng: _hereLatLng,
                          locatingHere: _locatingHere,
                          onLocateMe: _locateMe,
                        ),
                      )),
                      Positioned(
                        bottom: 0, left: 0, right: 0,
                        child: Builder(builder: (ctx) => Container(
                          color: Theme.of(ctx).colorScheme.surface.withOpacity(0.5),
                          child: Selector<ProjectNotifier,
                              (List<Map<String, dynamic>>, Object?, String?, Set<String>)>(
                            selector: (_, n) => (
                              n.activities,
                              n.selectedActivityId as Object?,
                              n.selectedDay,
                              n.selectedDays,
                            ),
                            shouldRebuild: (a, b) =>
                                !identical(a.$1, b.$1) ||
                                a.$2?.toString() != b.$2?.toString() ||
                                a.$3 != b.$3 ||
                                !ManageMapPanelState.setEquals(a.$4, b.$4),
                            builder: (ctx, tuple, __) {
                              final n = ctx.read<ProjectNotifier>();
                              final allActivities = tuple.$1;
                              final selActId = tuple.$2;
                              final selDay = tuple.$3;
                              final selDays = tuple.$4;
                              final effectiveDays = selDays.isNotEmpty
                                  ? selDays
                                  : (selDay != null ? {selDay} : <String>{});
                              final activities = effectiveDays.isEmpty
                                  ? allActivities
                                  : allActivities.where((a) =>
                                      effectiveDays.contains(
                                        (a['start_date_local'] as String? ?? '')
                                            .split('T').first)).toList();
                              return RepaintBoundary(child: ElevationChart(
                                activities: activities,
                                selectedActivityId: selActId,
                                onCursorChanged: (pos) =>
                                    n.elevationCursorNotifier.value = pos,
                                mapCursorNotifier: n.mapCursorDistNotifier,
                                track: selActId != null
                                    ? n.perActivityTracks[selActId.toString()] ?? n.fullTrack
                                    : n.fullTrack,
                                color: n.effectiveElevationChartColor,
                                showLine: n.elevationChartShowLine,
                              ));
                            },
                          ),
                        )),
                      ),
                      if (_framePickerActive)
                        FramePickerOverlay(
                          mapController: _mapController,
                          onNext: _onFrameConfirmed,
                          onCancel: _cancelFramePicker,
                        ),
                      // Poster status card (issue #14, unit G) — fixed offset
                      // from the top-right, below where SelectionStatsOverlay
                      // renders inside ManageMapPanel's own Stack (that
                      // widget's runtime position isn't visible from here).
                      Positioned(
                        top: 90,
                        right: 12,
                        child: PosterStatusCard(notifier: _posterStatusNotifier),
                      ),
                    ],
                  ),
                ),
              ],
            );
          } else {
            // ── Narrow layout: full-screen map + slide-in activity panel ──
            final mapHeight = constraints.maxHeight;
            return Stack(
              children: [
                // Base: full-height map (RepaintBoundary — see wide layout).
                RepaintBoundary(child: kPerfNoMap
                    ? const ColoredBox(color: Color(0xFF334155))
                    : Consumer<ProjectNotifier>(
                  builder: (_, n, __) => ManageMapPanel(
                    key: _mapPanelKey,
                    notifier: n,
                    mapController: _mapController,
                    autoZoom: _autoZoom,
                    basemapUrl: kActiveManageBasemapUrl,
                    basemapSubdomains: kActiveManageSubdomains,
                    fittedNotifier: _mapFitted,
                    basemapStyleUri: kActiveManageStyleUri,
                    initialLat: widget.initialLat,
                    initialLng: widget.initialLng,
                    initialZoom: widget.initialZoom,
                    focusedLatLng: _focusedLatLng,
                    onLocationTap: _focusLocation,
                    onClearFocusedLocation: _clearFocusedLocation,
                    hereLatLng: _hereLatLng,
                    locatingHere: _locatingHere,
                    onLocateMe: _locateMe,
                  ),
                )),

                // Elevation chart overlaid at bottom
                Positioned(
                  bottom: 0, left: 0, right: 0,
                  child: Builder(builder: (ctx) => Container(
                    color: Theme.of(ctx).colorScheme.surface.withOpacity(0.42),
                    child: Selector<ProjectNotifier,
                        (List<Map<String, dynamic>>, Object?, String?)>(
                      selector: (_, n) => (
                        n.activities,
                        n.selectedActivityId as Object?,
                        n.selectedDay,
                      ),
                      shouldRebuild: (a, b) =>
                          !identical(a.$1, b.$1) ||
                          a.$2?.toString() != b.$2?.toString() ||
                          a.$3 != b.$3,
                      builder: (ctx, tuple, __) {
                        final n = ctx.read<ProjectNotifier>();
                        final allActivities = tuple.$1;
                        final selActId = tuple.$2;
                        final selDay = tuple.$3;
                        final activities = selDay != null
                            ? allActivities.where((a) =>
                                (a['start_date_local'] as String? ?? '')
                                    .split('T').first == selDay).toList()
                            : allActivities;
                        return RepaintBoundary(child: ElevationChart(
                          activities: activities,
                          selectedActivityId: selActId,
                          onCursorChanged: (pos) =>
                              n.elevationCursorNotifier.value = pos,
                          mapCursorNotifier: n.mapCursorDistNotifier,
                          track: selActId != null
                              ? n.perActivityTracks[selActId.toString()] ?? n.fullTrack
                              : n.fullTrack,
                          color: n.effectiveElevationChartColor,
                        ));
                      },
                    ),
                  )),
                ),

                // Overlay: activity panel slides in from the left
                AnimatedSlide(
                  offset: _panelOpen ? Offset.zero : const Offset(-1.0, 0),
                  duration: const Duration(milliseconds: 280),
                  curve: Curves.easeInOut,
                  child: Align(
                    alignment: Alignment.topLeft,
                    child: Consumer<ProjectNotifier>(
                      builder: (_, n, __) => MobileActivityPanelOverlay(
                        notifier: n,
                        mapController: _mapController,
                        height: mapHeight,
                        scrollController: _mobileActivityScrollController,
                        isVisible: _panelOpen,
                        onLocationTap: _focusLocation,
                      ),
                    ),
                  ),
                ),

                if (_framePickerActive)
                  FramePickerOverlay(
                    mapController: _mapController,
                    onNext: _onFrameConfirmed,
                    onCancel: _cancelFramePicker,
                  ),

                // Poster status card (issue #14, unit G) — see the wide-layout
                // Stack above for why this fixed offset was chosen.
                Positioned(
                  top: 90,
                  right: 12,
                  child: PosterStatusCard(notifier: _posterStatusNotifier),
                ),

              ],
            );
          }
        },
      )),
        ],
      ),
    );
  }
}

/// Fast, low-resolution layout preview shown between the config dialog and
/// actually generating the poster (issue #14) — fetches
/// `POST .../poster/preview` (no basemap imagery, just pins/cards/legend at
/// a small size) so the user can sanity-check card placement/overlap before
/// committing to the slower full-resolution job.
class _PosterPreviewDialog extends StatefulWidget {
  final ProjectRef projectRef;
  final LatLngBounds bounds;
  final String orientation;
  final String paperSize;
  final PosterConfigOptions opts;
  final PosterTitleOptions titleOpts;
  final List<Map<String, dynamic>> memories;
  final VoidCallback onGenerate;

  const _PosterPreviewDialog({
    required this.projectRef,
    required this.bounds,
    required this.orientation,
    required this.paperSize,
    required this.opts,
    required this.titleOpts,
    required this.memories,
    required this.onGenerate,
  });

  @override
  State<_PosterPreviewDialog> createState() => _PosterPreviewDialogState();
}

class _PosterPreviewDialogState extends State<_PosterPreviewDialog> {
  Uint8List? _bytes;
  String? _warning;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _error = null;
      _bytes = null;
      _warning = null;
    });
    try {
      final preview = await fetchPosterPreview(
        ref: widget.projectRef,
        bounds: posterBoundsFromLatLngBounds(widget.bounds),
        orientation: widget.orientation,
        paperSize: widget.paperSize,
        config: widget.opts.toJson(),
        memories: widget.memories,
        titlePosition: {
          'x': widget.titleOpts.positionX,
          'y': widget.titleOpts.positionY,
        },
        titleText: widget.titleOpts.titleText,
        titleScale: widget.titleOpts.titleScale,
      );
      if (!mounted) return;
      setState(() {
        _bytes = preview.bytes;
        _warning = preview.hasWarning ? preview.warning : null;
      });
    } on TimeoutException {
      if (!mounted) return;
      // A raw "TimeoutException after 0:00:20.000000: ..." told the user
      // nothing about *what* had happened — not whether the server was
      // still working, hung, or had genuinely failed (issue #14 feedback).
      // It hadn't: the server itself now gives up well inside this timeout
      // and reports why (see the warning-banner path above), so reaching
      // this catch means the connection itself is the problem, not the
      // render — state that plainly instead of dumping the exception.
      setState(() => _error =
          'The preview took too long to reach the server. Check your '
          'connection and try again.');
    } catch (e) {
      if (!mounted) return;
      // Surface the real reason: "Could not load the preview" gave the user
      // nothing to act on (issue #14 feedback).
      setState(() => _error = '$e');
    }
  }

  void _generate() {
    Navigator.of(context).pop();
    widget.onGenerate();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Poster layout preview'),
      content: SizedBox(
        width: 420,
        child: _error != null
            ? Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'The preview could not be rendered.',
                    style: TextStyle(color: Theme.of(context).colorScheme.error),
                  ),
                  const SizedBox(height: 6),
                  Text(_error!, style: Theme.of(context).textTheme.bodySmall),
                  const SizedBox(height: 10),
                  OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(minimumSize: const Size(0, 36)),
                    onPressed: _load,
                    icon: const Icon(Icons.refresh, size: 18),
                    label: const Text('Try again'),
                  ),
                ],
              )
            : _bytes == null
                ? const SizedBox(
                    height: 200,
                    child: Center(child: CircularProgressIndicator()),
                  )
                : Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Image.memory(_bytes!, fit: BoxFit.contain),
                      const SizedBox(height: 8),
                      if (_warning != null) ...[
                        _PosterWarningBanner(message: _warning!, onRetry: _load),
                        const SizedBox(height: 8),
                      ],
                      Text(
                        'Low-resolution preview — the poster prints at full size '
                        'and print resolution.',
                        style: Theme.of(context).textTheme.bodySmall,
                        textAlign: TextAlign.center,
                      ),
                    ],
                  ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Back'),
        ),
        FilledButton(
          style: FilledButton.styleFrom(minimumSize: const Size(0, 36)),
          onPressed: (_bytes != null || _error != null) ? _generate : null,
          child: const Text('Generate poster'),
        ),
      ],
    );
  }
}

/// Inline notice shown over a poster preview that rendered everything except
/// the map imagery (issue #14 feedback).
///
/// Without this the user sees a plain grey map and has no way to tell a
/// misconfigured `MAPBOX_TOKEN` from an intentional design — which is exactly
/// how the "the map is still grey" report came about.
class _PosterWarningBanner extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;

  const _PosterWarningBanner({required this.message, required this.onRetry});

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final amber = Theme.of(context).brightness == Brightness.dark
        ? kWarningDark
        : kWarning;
    return Container(
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: amber.withValues(alpha: 0.10),
        border: Border.all(color: amber.withValues(alpha: 0.45)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.warning_amber_rounded, size: 20, color: amber),
          const SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Map imagery unavailable — the preview below shows the layout '
                  'on a blank background. Generating the poster will fail until '
                  'this is fixed.',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
                const SizedBox(height: 4),
                Text(
                  message,
                  style: Theme.of(context)
                      .textTheme
                      .bodySmall
                      ?.copyWith(color: scheme.onSurfaceVariant),
                ),
              ],
            ),
          ),
          TextButton(onPressed: onRetry, child: const Text('Retry')),
        ],
      ),
    );
  }
}

/// Thin draggable divider between the activity panel and the map. Drag left/right
/// to resize the panel; shows a resize cursor on web/desktop.
class _PanelResizeHandle extends StatelessWidget {
  final ValueChanged<double> onDrag;
  final VoidCallback onDragEnd;
  const _PanelResizeHandle({required this.onDrag, required this.onDragEnd});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return MouseRegion(
      cursor: SystemMouseCursors.resizeColumn,
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onHorizontalDragUpdate: (d) => onDrag(d.delta.dx),
        onHorizontalDragEnd: (_) => onDragEnd(),
        child: SizedBox(
          width: 8,
          child: Center(
            child: Container(
              width: 2,
              color: cs.outlineVariant,
            ),
          ),
        ),
      ),
    );
  }
}

// ── Image export ──────────────────────────────────────────────────────────────

