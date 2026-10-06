/// Read-only project view for users accessing a shared link (no auth required).
library;

import 'dart:async' show StreamSubscription, Timer;

import 'package:cryptography_plus/cryptography_plus.dart';
import 'package:flutter/gestures.dart' show TapGestureRecognizer;
import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart' show MapEvent;
import 'package:flutter_map_animations/flutter_map_animations.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../api/client.dart' show api;
import '../auth/auth_notifier.dart';
import '../core/perf_timing.dart';
import '../core/project_ref.dart';
import '../projects/basemaps.dart';
import '../projects/geo_viewport.dart';
import '../projects/heavy_decode.dart' as heavy;
import '../projects/elevation_chart.dart' show ElevationChart, ElevationLoadingPlaceholder;
import '../projects/facets/project_facet_providers.dart';
import '../projects/map_panel.dart';
import '../projects/memory_detail_modal.dart';
import '../projects/project_notifier.dart';
import '../projects/project_service.dart';
import '../projects/project_stats_screen.dart';
import 'anonymous_id.dart';
import 'share_fragment_key.dart';
import '../crypto/encrypted_display.dart';

// ── Shared service — calls /api/share/{token}, appends ?aid= when provided ───

class _SharedProjectService extends ProjectService {
  final String token;
  final String? anonymousId;
  String ownerName = '';

  _SharedProjectService(this.token, {this.anonymousId});

  String get _aidParam =>
      anonymousId != null ? '?aid=${Uri.encodeComponent(anonymousId!)}' : '';

  /// Returns the lightweight /meta response (~363 KB) — this is what
  /// load()'s Phase 1 actually calls (via the base class's getDetailsMeta()).
  ///
  /// This override was missing before this fix: the inherited
  /// ProjectService.getDetailsMeta() builds /api/projects/{token}/meta — an
  /// authenticated, owner-scoped endpoint that doesn't recognise a share
  /// token as a project name — so every shared-project load's Phase 1 hit
  /// the wrong endpoint and failed outright.
  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef _) async {
    final meta = await _getJson('decode_meta', '/api/share/$token/meta$_aidParam');
    ownerName = (meta['owner_name'] as String?) ?? '';
    return meta;
  }

  /// Fetch + off-UI-isolate decode, matching what [ProjectService] does for
  /// the owner-side endpoints (issue #292). The shared screen loads the same
  /// payloads — a ~363 KB /meta and a ~3 MB full details — so decoding them
  /// inline stalled the UI isolate here exactly as it did there.
  Future<Map<String, dynamic>> _getJson(String span, String path,
      {Duration? timeout}) async {
    final bytes = await perfSpans.stage(
        'fetch_${span.replaceFirst('decode_', '')}',
        () => timeout == null
            ? api.getBytes(path)
            : api.getBytes(path, timeout: timeout));
    perfSpans.note(span.replaceFirst('decode_', ''), perfSizeLabel(bytes.length));
    return perfSpans.stage(span, () => heavy.decodeJsonMapOffIsolate(bytes));
  }

  /// Fetches the full ~3 MB response (elevation_profile included) — the
  /// getDetails() contract's actual meaning (see ProjectService.getDetails).
  /// Called directly by SharedProjectNotifier.loadShared() as Phase 2, after
  /// load() has returned, so meta gets exclusive bandwidth before this
  /// request fires.
  Future<Map<String, dynamic>> fetchFullDetails() async {
    final m = await _getJson('decode_details', '/api/share/$token$_aidParam',
        timeout: const Duration(minutes: 2));
    ownerName = (m['owner_name'] as String?) ?? '';
    return m;
  }

  /// Base-class code (e.g. ProjectNotifier._loadElevationData, fired in the
  /// background by every load()) calls getDetails() expecting the real full
  /// payload — this used to be overridden to return the /meta response
  /// instead, so any such caller silently got meta-shaped data. Routing it
  /// through fetchFullDetails() keeps a single implementation and makes
  /// getDetails() mean what the base contract says everywhere.
  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef _, {bool bypassCache = false}) =>
      fetchFullDetails();

  /// The base class's two full-res methods differ only in what they do to
  /// `projectDataCache`, and this service caches nothing either way, so both
  /// land on the one share endpoint below.
  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) =>
      fetchFullGeoUncached(ref);

  /// Routed through [heavy.decodeGeoOffIsolate], not the plain JSON decode,
  /// so a shared viewer gets the same seeded coordinate/arc-midpoint caches an
  /// owner does (issue #294) instead of paying the cold derivation on the UI
  /// isolate. The share endpoint returns expanded `coordinates` rather than
  /// encoded polylines, and the expansion pass is a documented no-op on those,
  /// so sharing the owner-side path costs nothing here.
  ///
  /// Overriding [fetchFullGeoUncached] specifically is what keeps a shared
  /// viewer's image export (issue #317, via the share card of issue #15) off
  /// the owner-scoped `/api/geo/project`, which 401s for them.
  @override
  Future<Map<String, dynamic>> fetchFullGeoUncached(ProjectRef _) =>
      () async {
        final bytes = await perfSpans.stage(
            'fetch_geo',
            () => api.getBytes('/api/share/$token/geo$_aidParam',
                timeout: const Duration(seconds: 90)));
        perfSpans.note('geo', perfSizeLabel(bytes.length));
        return perfSpans.stage(
            'decode_geo', () => heavy.decodeGeoOffIsolate(bytes));
      }();

  /// The share-scoped zoom-simplified endpoint (issue #321). A public link
  /// used to be the one path that still shipped the full-resolution geometry
  /// the zoom level of detail exists to avoid — 4.5 MB and ~180 MB of heap on
  /// the trip it was measured against, on the device least likely to have room
  /// for it.
  ///
  /// [fetchSimplifiedGeo] rather than getSimplifiedGeo, so the base class's
  /// in-flight dedup still applies: a mode toggle mid-load and a concurrent
  /// zoom refetch then share one request instead of racing two.
  ///
  /// No `aid` — it is the visitor id, and /meta already records the visit.
  @override
  Future<Map<String, dynamic>> fetchSimplifiedGeo(
      ProjectRef _, double zoom, GeoBox? bbox) async {
    final box = bbox == null ? '' : '&bbox=${bbox.param}';
    final bytes = await perfSpans.geoLodFetch(() => api.getBytesWithHeaders(
        '/api/share/$token/geo/simplified?zoom=$zoom$box',
        timeout: const Duration(seconds: 90)));
    perfSpans.note('geo_lod', perfSizeLabel(bytes.length));
    // Same off-isolate hop the owner path uses, so the geometry caches are
    // seeded and the map's first build after the swap does no O(points) work.
    return perfSpans.stage(
        'decode_geo_lod', () => heavy.decodeGeoOffIsolate(bytes));
  }

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef _) =>
      _getJson('decode_low_res_geo', '/api/share/$token/geo/low-res');

  @override
  Future<Map<String, dynamic>> getStats(ProjectRef _, {List<String> tags = const []}) async {
    final query = tags.isEmpty
        ? ''
        : '?${tags.map((t) => 'tags=${Uri.encodeComponent(t)}').join('&')}';
    final data = await api.get('/api/share/$token/stats$query');
    return data as Map<String, dynamic>;
  }
}

// ── Shared notifier ───────────────────────────────────────────────────────────

class SharedProjectNotifier extends ProjectNotifier {
  final String token;
  final _SharedProjectService _sharedSvc;
  String get ownerName => _sharedSvc.ownerName;
  @override
  ProjectService get service => _sharedSvc;
  bool _disposed = false;

  /// Per-share content key from the URL fragment (issue #28), or null if
  /// absent — read once at construction time via [readShareKeyFromUrlFragment].
  @override
  final SecretKey? shareContentKey;

  @override
  bool get loadOwnerExtras => false;

  /// A share link is addressed by token, and after /meta its ref is only the
  /// trip's name — indistinguishable from the viewer's own trip of that name.
  /// Persisting here overwrote that trip's saved filter and selection (#409).
  @override
  bool get persistsUiState => false;

  @override
  String photoThumbUrl(String memId, String uuid) =>
      '$apiBaseUrl/api/share/$token/photos/$memId/$uuid/thumb';

  @override
  String photoFullUrl(String memId, String uuid) =>
      '$apiBaseUrl/api/share/$token/photos/$memId/$uuid';

  @override
  Map<String, String> get photoAuthHeaders => const {};

  SharedProjectNotifier._internal(this.token, _SharedProjectService svc, this.shareContentKey)
      : _sharedSvc = svc,
        super(svc);

  factory SharedProjectNotifier(String token, {String? anonymousId}) {
    final svc = _SharedProjectService(token, anonymousId: anonymousId);
    return SharedProjectNotifier._internal(token, svc, readShareKeyFromUrlFragment());
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }

  Future<void> loadShared() async {
    isMetaLoaded = false;
    isElevationLoaded = false;
    isGeoLoaded = false;

    // Phase 1: load() calls _sharedSvc.getDetailsMeta() which returns the
    // lightweight /meta response in ~1 s.  isLoading goes false after that.
    //
    // loadToken captured synchronously, *before* awaiting: load()'s body runs
    // its synchronous prefix — including bumping the load-supersession token
    // — the instant it's called, before it suspends on its own first await.
    // So this is guaranteed to be *this* loadShared() call's own token.
    // Reading currentLoadToken only after `await load(tokenRef)` returned
    // instead (this fix's first cut) could pick up a second concurrent
    // loadShared() call's token if that call's own load() had already
    // bumped it by the time this one's await resolved — silently
    // un-superseding a call that should have been rejected (issue #283
    // review finding).
    final tokenRef = ProjectRef(name: token);
    final loadFuture = load(tokenRef);
    final loadToken = currentLoadToken;
    await loadFuture;
    if (_disposed || currentLoadKey != tokenRef || currentLoadToken != loadToken) return;
    isMetaLoaded = true;
    // The /meta response now carries a downsampled (low-res) elevation profile,
    // so the chart can render immediately; Phase 2 below upgrades it in place.
    isElevationLoaded = true;
    notifyListeners(); // project name, activity list, memories, elevation chart visible

    // Phase 2 (background): fetch the full ~3 MB response for elevation data.
    // Fired here — after load() has returned — so meta had exclusive bandwidth.
    try {
      final fullDetails = await _sharedSvc.fetchFullDetails();
      if (_disposed || currentLoadKey != tokenRef || currentLoadToken != loadToken) return;
      final rawActs = fullDetails['activities'];
      if (rawActs is List) {
        // applyFullActivities notifies internally (gated on camera-idle) —
        // isElevationLoaded was already set true above, so no second notify
        // is needed here just to surface it.
        await applyFullActivities(rawActs.cast<Map<String, dynamic>>(),
            ref: tokenRef, token: loadToken);
      }
    } catch (_) {
      // Non-fatal — elevation placeholder stays visible. Still notify (like
      // ProjectNotifier._loadFullGeoProgressively's own catch block) so a
      // mutation applyFullActivities may have already made (the activities
      // merge, before its own _buildFullTrack() threw) doesn't stay
      // unbroadcast — issue #283 bug #4.
      if (!_disposed && currentLoadKey == tokenRef && currentLoadToken == loadToken) {
        notifyListeners();
      }
    }
  }
}

// ── Screen ────────────────────────────────────────────────────────────────────

class SharedProjectScreen extends StatefulWidget {
  final String token;

  /// When set, the memory with this stable public_id is opened automatically
  /// once the project has loaded (deep link `/share/<token>?memory=<id>`).
  final String? initialMemoryPublicId;

  const SharedProjectScreen({
    super.key,
    required this.token,
    this.initialMemoryPublicId,
  });

  @override
  State<SharedProjectScreen> createState() => _SharedProjectScreenState();
}

class _SharedProjectScreenState extends State<SharedProjectScreen> {
  SharedProjectNotifier? _notifier;

  @override
  void initState() {
    super.initState();
    _init();
  }

  Future<void> _init() async {
    final anonId = await getOrCreateAnonId();
    if (!mounted) return;
    final notifier = SharedProjectNotifier(widget.token, anonymousId: anonId)
      ..loadShared();
    setState(() => _notifier = notifier);
  }

  @override
  void dispose() {
    _notifier?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_notifier == null) {
      return const Scaffold(
        body: Center(child: CircularProgressIndicator()),
      );
    }
    return ChangeNotifierProvider.value(
      value: _notifier!,
      child: ProjectFacetProviders<SharedProjectNotifier>(
        child: _SharedProjectView(
          token: widget.token,
          initialMemoryPublicId: widget.initialMemoryPublicId,
        ),
      ),
    );
  }
}

class _SharedProjectView extends StatefulWidget {
  final String token;
  final String? initialMemoryPublicId;
  const _SharedProjectView({required this.token, this.initialMemoryPublicId});

  @override
  State<_SharedProjectView> createState() => _SharedProjectViewState();
}

class _SharedProjectViewState extends State<_SharedProjectView>
    with TickerProviderStateMixin {
  late final AnimatedMapController _mapController =
      AnimatedMapController(vsync: this, duration: const Duration(milliseconds: 500));

  bool _deepLinkHandled = false;

  // Tells ProjectNotifier the camera is moving so its background full-res
  // geo/elevation upgrades can hold off on a rebuild until panning actually
  // pauses (see ProjectNotifier.setMapCameraActive). Mirrors app_screen.dart /
  // view_screen.dart's _onMapEvent — this screen has no route params to sync,
  // so it only needs the camera-activity signal, not the URL-sync half of
  // that pattern. Missing here before this fix meant shared/public trip
  // viewers got none of that protection (issue #276 follow-up).
  StreamSubscription<MapEvent>? _mapEventSub;
  Timer? _cameraIdleTimer;

  void _onMapEvent(MapEvent event) {
    final camera = _mapController.mapController.camera;
    context.read<SharedProjectNotifier>()
      ..setMapCameraActive(true)
      // Zoom level of detail (issue #295) and its viewport box (#324): the
      // notifier fetches geometry matched to what is on screen, so it has to
      // know what is on screen. Shared viewers have no simplified endpoint
      // (issue #321), so this currently arms nothing here — passing it anyway
      // keeps the three map screens' camera wiring identical, which is how
      // this screen came to be missing setMapCameraActive in the first place.
      ..setMapZoom(camera.zoom,
          viewport: viewportBox(camera.visibleBounds.west,
              camera.visibleBounds.south, camera.visibleBounds.east,
              camera.visibleBounds.north));
    _cameraIdleTimer?.cancel();
    _cameraIdleTimer = Timer(const Duration(milliseconds: 250), () {
      if (mounted) context.read<SharedProjectNotifier>().setMapCameraActive(false);
    });
  }

  @override
  void initState() {
    super.initState();
    _mapEventSub =
        _mapController.mapController.mapEventStream.listen(_onMapEvent);
  }

  @override
  void dispose() {
    _mapEventSub?.cancel();
    _cameraIdleTimer?.cancel();
    _mapController.dispose();
    super.dispose();
  }

  /// Opens the deep-linked memory once the project has loaded. Matches on the
  /// stable public_id; if not found (e.g. the memory was removed), it silently
  /// leaves the reader at the trip root.
  void _maybeOpenDeepLinkedMemory(ProjectNotifier pn) {
    if (_deepLinkHandled || widget.initialMemoryPublicId == null) return;
    if (!pn.isMetaLoaded) return;
    _deepLinkHandled = true;

    final match = pn.items.firstWhere(
      (i) =>
          i['item_type'] == 'memory' &&
          (i['memory'] as Map?)?['public_id'] == widget.initialMemoryPublicId,
      orElse: () => const <String, dynamic>{},
    );
    final mem = match['memory'];
    if (mem is! Map) return;

    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      showMemoryDetail(
        context,
        pn,
        mem.cast<String, dynamic>(),
        readOnly: true,
        shareToken: widget.token,
        shareContentKey: pn is SharedProjectNotifier ? pn.shareContentKey : null,
      );
    });
  }

  @override
  Widget build(BuildContext context) {
    final notifier = context.watch<SharedProjectNotifier>();
    final pn = notifier as ProjectNotifier;
    _maybeOpenDeepLinkedMemory(pn);
    final theme = Theme.of(context);
    final authUser = context.watch<AuthNotifier>().user;
    final isAnonymous = authUser == null;

    return Scaffold(
      appBar: AppBar(
        title: Text(
          notifier.projectName?.isNotEmpty == true
              ? notifier.ownerName.isNotEmpty
                  ? '${notifier.projectName} — shared by ${notifier.ownerName}'
                  : notifier.projectName!
              : 'Shared project',
        ),
        actions: [
          if (notifier.isMetaLoaded)
            IconButton(
              icon: const Icon(Icons.bar_chart_outlined),
              tooltip: 'Statistics',
              onPressed: () => Navigator.push(
                context,
                MaterialPageRoute(
                  builder: (_) => ProjectStatsScreen(
                    projectName: notifier.projectName ?? '',
                    availableTags: notifier.availableTags,
                    sleepingOptionGroups: notifier.sleepingOptionGroups,
                    service: notifier.service,
                  ),
                ),
              ),
            ),
        ],
      ),
      body: Column(
        children: [
          const ViewOnlyBanner(),
          if (isAnonymous) AnonBanner(token: widget.token),
          Expanded(
            child: !notifier.isMetaLoaded
                ? (notifier.error != null
                    ? Center(
                        child: Padding(
                          padding: const EdgeInsets.all(24),
                          child: Text(
                            notifier.error!,
                            style: TextStyle(color: theme.colorScheme.error),
                            textAlign: TextAlign.center,
                          ),
                        ),
                      )
                    : const Center(child: CircularProgressIndicator()))
                : LayoutBuilder(
                    builder: (context, constraints) {
                      final wide = constraints.maxWidth >= 720;
                      final mapPanel = MapPanel(
                        notifier: pn,
                        mapController: _mapController,
                        basemapUrl: kActiveViewBasemapUrl,
                        labelsUrl: kActiveViewLabelsOverlayUrl,
                        basemapStyleUri: kActiveViewStyleUri,
                      );
                      final activityList = _ReadOnlyActivityList(notifier: pn);
                      final selectedId = notifier.selectedActivityId;
                      final elevChart = notifier.isElevationLoaded
                          ? ElevationChart(
                              activities: notifier.activities,
                              selectedActivityId: selectedId,
                              track: selectedId == null
                                  ? notifier.fullTrack
                                  : notifier.perActivityTracks[
                                          selectedId.toString()] ??
                                      notifier.fullTrack,
                              onCursorChanged: (pos) =>
                                  notifier.elevationCursorNotifier.value = pos,
                              mapCursorNotifier: notifier.mapCursorDistNotifier,
                              color: pn.effectiveElevationChartColor,
                            )
                          : const ElevationLoadingPlaceholder();

                      if (wide) {
                        return Row(
                          children: [
                            SizedBox(width: 260, child: activityList),
                            Expanded(
                              child: Column(
                                children: [
                                  Expanded(child: mapPanel),
                                  elevChart,
                                ],
                              ),
                            ),
                          ],
                        );
                      } else {
                        return Column(
                          children: [
                            Expanded(child: mapPanel),
                            SizedBox(
                              height: constraints.maxHeight * 0.4,
                              child: Column(
                                children: [
                                  Expanded(child: activityList),
                                  elevChart,
                                ],
                              ),
                            ),
                          ],
                        );
                      }
                    },
                  ),
          ),
        ],
      ),
    );
  }
}

// ── View-only visitor banner ──────────────────────────────────────────────────

class ViewOnlyBanner extends StatelessWidget {
  const ViewOnlyBanner({super.key});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      color: theme.colorScheme.secondaryContainer,
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.lock_outlined,
              size: 14, color: theme.colorScheme.onSecondaryContainer),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              'View only — you are viewing a shared project',
              style: theme.textTheme.bodySmall?.copyWith(
                color: theme.colorScheme.onSecondaryContainer,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ── Anonymous visitor banner ──────────────────────────────────────────────────

class AnonBanner extends StatefulWidget {
  final String token;
  const AnonBanner({super.key, required this.token});

  @override
  State<AnonBanner> createState() => _AnonBannerState();
}

class _AnonBannerState extends State<AnonBanner> {
  late final TapGestureRecognizer _loginRecognizer;

  @override
  void initState() {
    super.initState();
    _loginRecognizer = TapGestureRecognizer()
      ..onTap = () => context.go('/login?return_to=/share/${widget.token}');
  }

  @override
  void dispose() {
    _loginRecognizer.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final textStyle = theme.textTheme.bodySmall?.copyWith(
      color: theme.colorScheme.onTertiaryContainer,
    );
    return Material(
      color: theme.colorScheme.tertiaryContainer,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.person_outline,
                size: 18, color: theme.colorScheme.onTertiaryContainer),
            const SizedBox(width: 10),
            Expanded(
              child: Text.rich(
                TextSpan(
                  style: textStyle,
                  children: [
                    const TextSpan(text: 'Browsing as a guest — '),
                    TextSpan(
                      text: 'login/register',
                      style: textStyle?.copyWith(
                        decoration: TextDecoration.underline,
                      ),
                      recognizer: _loginRecognizer,
                    ),
                    const TextSpan(text: ' to enable the full experience'),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Read-only activity list ───────────────────────────────────────────────────

class _ReadOnlyActivityList extends StatelessWidget {
  final ProjectNotifier notifier;
  const _ReadOnlyActivityList({required this.notifier});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final activities = notifier.activities;

    if (activities.isEmpty) {
      return Center(
        child: Text('No activities', style: theme.textTheme.bodySmall),
      );
    }

    return ListView.builder(
      padding: const EdgeInsets.symmetric(vertical: 8),
      itemCount: activities.length,
      itemBuilder: (context, i) {
        final act = activities[i];
        final id = act['id'];
        final name = shownText(act['name'] as String?) ?? 'Activity';
        final type = act['type'] as String? ?? '';
        final distM = (act['distance'] as num? ?? 0).toDouble();
        final distKm = (distM / 1000).toStringAsFixed(1);
        final isSelected =
            notifier.selectedActivityId?.toString() == id?.toString();

        return ListTile(
          dense: true,
          selected: isSelected,
          selectedTileColor:
              theme.colorScheme.primaryContainer.withValues(alpha: 0.4),
          leading: Icon(
            _sportIcon(type),
            size: 18,
            color: isSelected ? theme.colorScheme.primary : null,
          ),
          title: Text(name,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: theme.textTheme.bodyMedium),
          subtitle: Text('$distKm km', style: theme.textTheme.bodySmall),
          onTap: () => notifier.selectActivity(id),
        );
      },
    );
  }

  IconData _sportIcon(String type) {
    return switch (type.toLowerCase()) {
      'ride' || 'virtualride' || 'ebikeride' => Icons.directions_bike,
      'run' || 'virtualrun' => Icons.directions_run,
      'hike' || 'walk' => Icons.hiking,
      'swim' => Icons.pool,
      'alpineski' || 'nordicski' || 'snowboard' => Icons.downhill_skiing,
      _ => Icons.route,
    };
  }
}
