/// Dedicated full-screen editor for a single activity's track geometry (#31).
///
/// Presents a map (full polyline always drawn; draggable vertex handles only
/// materialised for the portion currently in view, per the perf requirement)
/// above a synced elevation chart, plus a tool bar for Trim / Add / Remove /
/// Split and Save / Reset-to-Strava actions. All edit logic lives in
/// [TrackEditorController] + [TrackEditModel]; the map/gesture chrome lives in
/// [TrackMapEditor] (shared with the segment track editor, issue #150); this
/// widget supplies the activity-specific menu actions and persists through
/// [ProjectNotifier].
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:latlong2/latlong.dart';

import '../api/client.dart';
import '../core/design_tokens.dart';
import '../map/geo_point.dart';
import 'elevation_chart.dart';
import 'project_notifier.dart';
import 'track_edit_model.dart';
import 'track_editor_controller.dart';
import 'track_map_editor.dart';
import '../crypto/encrypted_display.dart';

/// Build the [TrackEditModel] for [activity] from its stored polyline +
/// elevation profile pairs (`[[distKm, elevM], …]`).
TrackEditModel modelForActivity(Map<String, dynamic> activity) {
  final poly = (activity['map'] as Map?)?['summary_polyline'] as String?;
  final rawEp = activity['elevation_profile'];
  List<List<double>>? pairs;
  if (rawEp is List) {
    pairs = [
      for (final p in rawEp)
        if (p is List && p.length >= 2)
          [(p[0] as num).toDouble(), (p[1] as num).toDouble()],
    ];
    if (pairs.isEmpty) pairs = null;
  }
  return TrackEditModel.fromEncoded(poly, pairs);
}

/// What the editor's AppBar folds into its overflow menu on a phone.
enum _EditorMenuAction { reset }

class ActivityEditorPage extends StatefulWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic> activity;

  /// Set for an activity whose stored track is encrypted: [activity] is then
  /// the `GET …/track` response as stored, and this its decrypted track. Edits
  /// and splits are measured and encrypted on this device and sent to the
  /// encrypted routes (E2EE remnants decision 7).
  final EncryptedTrackEdit? encrypted;

  const ActivityEditorPage({
    super.key,
    required this.notifier,
    required this.activity,
    this.encrypted,
  });

  @override
  State<ActivityEditorPage> createState() => _ActivityEditorPageState();
}

class _ActivityEditorPageState extends State<ActivityEditorPage> {
  late final TrackEditorController _c;
  final ValueNotifier<double?> _chartCursor = ValueNotifier(null);

  bool _saving = false;

  int get _activityId => (widget.activity['id'] as num).toInt();
  bool get _isEdited => widget.activity['is_edited'] == true;

  /// The project's lock_version as of when this activity was fetched for
  /// editing (see GET .../track) — sent back on save/split so the server can
  /// reject a stale write with 409 if the project changed elsewhere since
  /// (issue #31). Null on an older cached payload that predates the field,
  /// in which case the save/split proceeds unconditionally.
  int? get _lockVersion => (widget.activity['lock_version'] as num?)?.toInt();

  /// Local activities (split tails, added transport) carry a synthetic negative
  /// id and never came from Strava, so there is no Strava original to reset to —
  /// reset only undoes edits made since the piece was created (issue #131).
  bool get _isLocal => _activityId < 0;

  String get _resetLabel => _isLocal ? 'Reset track' : 'Reset to Strava';

  /// True when this track came out of a GPX file rather than from Strava.
  ///
  /// Worth saying HERE in particular: this is the screen where the geometry is
  /// changed, and what "Reset" restores depends on where the track came from.
  /// The activity panel has shown a badge since GPX import shipped, but the
  /// editor gave no hint at all.
  bool get _isGpxImport => widget.activity['source'] == 'gpx';

  /// How many pieces resetting this activity would destroy.
  ///
  /// Reset restores the track this activity held before its last edit, which on
  /// anything that has been split spans the pieces cut out of it — so the server
  /// deletes them rather than leave them duplicating the restored track. That is
  /// the whole family on a root (issue #141) and just its own children on a piece
  /// that was itself split again (issue #143), which is exactly the transitive
  /// walk of split_parent_id below. An activity with no pieces under it returns
  /// 0 and resets silently, undoing only its own edits (issue #131).
  int get _splitPiecesRemovedByReset {
    final acts = widget.notifier.itemsFacet.activities;
    final removed = <int>{};
    var frontier = {_activityId};
    while (frontier.isNotEmpty) {
      final children = <int>{};
      for (final a in acts) {
        final id = (a['id'] as num?)?.toInt();
        final parent = (a['split_parent_id'] as num?)?.toInt();
        if (id != null &&
            parent != null &&
            frontier.contains(parent) &&
            id != _activityId &&
            removed.add(id)) {
          children.add(id);
        }
      }
      frontier = children;
    }
    return removed.length;
  }

  /// Test-only access to the editor controller so widget tests can drive edits
  /// without simulating map-tile gestures.
  @visibleForTesting
  TrackEditorController get editorControllerForTest => _c;

  @override
  void initState() {
    super.initState();
    final encrypted = widget.encrypted;
    _c = TrackEditorController(encrypted == null
        ? modelForActivity(widget.activity)
        : TrackEditModel.aligned(encrypted.polyline, encrypted.profile));
    _c.addListener(_onChange);
  }

  @override
  void dispose() {
    _c.removeListener(_onChange);
    _c.dispose();
    _chartCursor.dispose();
    super.dispose();
  }

  void _onChange() => setState(() {});

  // ── Per-point context menu ───────────────────────────────────────────────

  List<PointMenuAction> _actionsForPoint(int index) {
    final last = _c.points.length - 1;
    return [
      PointMenuAction(
        label: 'Trim: keep from here',
        icon: Icons.first_page,
        enabled: index > 0,
        onSelected: (ctx, i) async => _c.trimFrom(i),
      ),
      PointMenuAction(
        label: 'Trim: keep up to here',
        icon: Icons.last_page,
        enabled: index < last,
        onSelected: (ctx, i) async => _c.trimTo(i),
      ),
      PointMenuAction(
        label: 'Split here',
        icon: Icons.call_split,
        // Not while another write is in flight: it could not go out beside it.
        enabled: !_saving && _c.canSplitAt(index),
        onSelected: (ctx, i) => _confirmSplit(i),
      ),
      PointMenuAction(
        label: 'Cut & add transport',
        icon: Icons.alt_route,
        enabled: !_saving && _c.canCutForTransport(index),
        onSelected: (ctx, i) => _confirmCutForTransport(i),
      ),
      PointMenuAction(
        label: 'Delete point',
        icon: Icons.delete_outline,
        enabled: _c.points.length > 2,
        color: kAccent,
        dividerBefore: true,
        onSelected: (ctx, i) async => _c.removeSelected(i),
      ),
    ];
  }

  // ── Actions ────────────────────────────────────────────────────────────────

  /// True for the 409 the server returns when this activity's project changed
  /// elsewhere since the editor loaded it (issue #31) — TrackEditRequest /
  /// SplitRequest.lock_version mismatched. Also any other 409 on a save or
  /// split: the activity was encrypted, or decrypted, since the editor opened,
  /// which reopening it is equally the way to see.
  bool _isStaleVersionConflict(Object e) => e is ApiException && e.statusCode == 409;

  /// The `detail.code` of a 409, e.g. `nothing_to_restore`; null otherwise.
  static String? _conflictCode(Object e) {
    if (e is! ApiException || e.statusCode != 409) return null;
    try {
      final detail = (jsonDecode(e.body) as Map)['detail'];
      return detail is Map ? detail['code'] as String? : null;
    } catch (_) {
      return null;
    }
  }

  /// Close this editor with [result], whatever has been opened over it since
  /// the write started.
  ///
  /// A save, reset or split can land while a menu or dialog is up — Save and ⋮
  /// tapped in the same frame both go through, and the map stays live while
  /// Save spins. A bare pop would close that instead and hand it the editor's
  /// result, which it cannot take; the save's catch then reported a save that
  /// had succeeded as failed and left the editor open (#407 review). [route]
  /// is the editor's own, captured before the await. If it is already leaving
  /// the navigator there is nothing to close, and popping anyway would take
  /// the screen under it with it.
  void _closeEditor(
      NavigatorState navigator, ModalRoute<Object?>? route, Object? result) {
    if (route == null || !route.isActive) return;
    navigator.popUntil((r) => r == route);
    navigator.pop(result);
  }

  /// Tell the user their edit was rejected because the activity changed
  /// elsewhere, and close the editor: it's holding a now-stale copy, and
  /// reopening it (activity_panel.dart always re-fetches on open) is the only
  /// way to see the latest version before trying again. Mirrors
  /// ProjectNotifier's segment-conflict handling (_resyncOnConflict in
  /// project_segment_crud_mixin.dart).
  void _handleStaleVersionConflict(
    ScaffoldMessengerState messenger, NavigatorState navigator,
    ModalRoute<Object?>? route,
  ) {
    messenger.showSnackBar(const SnackBar(
      content: Text('This activity changed elsewhere. Close and reopen the '
          'editor to see the latest version, then try again.'),
    ));
    _closeEditor(navigator, route, false);
  }

  Future<void> _save() async {
    if (!_c.canSave || _saving) return;
    setState(() => _saving = true);
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    final route = ModalRoute.of(context);
    try {
      final encrypted = widget.encrypted;
      if (encrypted == null) {
        await widget.notifier.saveActivityTrack(
            _activityId, _c.toSavePayload(), lockVersion: _lockVersion);
      } else {
        await widget.notifier
            .saveEncryptedActivityTrack(_activityId, encrypted, _c.points);
      }
      if (!mounted) return;
      _closeEditor(navigator, route, true);
    } catch (e) {
      if (!mounted) return;
      setState(() => _saving = false);
      if (_isStaleVersionConflict(e)) {
        _handleStaleVersionConflict(messenger, navigator, route);
        return;
      }
      messenger.showSnackBar(SnackBar(content: Text('Save failed: $e')));
    }
  }

  /// Warn before a reset that also removes the pieces this activity was split
  /// into — restoring the full track destroys them and any edits they carry, so
  /// that has to be a choice rather than a surprise (issue #141).
  Future<bool> _confirmResetRemovesPieces(int pieces) async {
    // Worded to hold for a root (restoring the whole Strava track, undoing the
    // split outright) and for a piece that was itself split again (#143).
    final one = pieces == 1;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(_isLocal ? 'Reset track' : 'Reset to Strava'),
        content: Text(
          'This restores the track this activity had before it was split.\n\n'
          '${one ? 'The piece' : 'The $pieces pieces'} cut out of it will be '
          'removed from the trip, along with any track edits made to '
          '${one ? 'it' : 'them'}. This cannot be undone.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Reset'),
          ),
        ],
      ),
    );
    return ok == true;
  }

  Future<void> _reset() async {
    // Disabling Reset only takes effect on the next rebuild; a tap in the same
    // frame as Save still arrives here, and must not send a second write.
    if (_saving) return;
    final pieces = _splitPiecesRemovedByReset;
    if (pieces > 0 && !await _confirmResetRemovesPieces(pieces)) return;
    if (!mounted) return;
    setState(() => _saving = true);
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    final route = ModalRoute.of(context);
    try {
      final lockVersion = _lockVersion;
      if (lockVersion == null) {
        await widget.notifier.resetActivityTrack(_activityId);
      } else {
        await widget.notifier
            .resetActivityTrackIfUnchanged(_activityId, lockVersion);
      }
      if (!mounted) return;
      _closeEditor(navigator, route, true);
    } catch (e) {
      if (!mounted) return;
      setState(() => _saving = false);
      switch (_conflictCode(e)) {
        case 'stale_write':
          _handleStaleVersionConflict(messenger, navigator, route);
        case 'nothing_to_restore':
          messenger.showSnackBar(const SnackBar(
            content: Text("This activity's original track was not kept, so it "
                "can't be reset."),
          ));
        default:
          messenger.showSnackBar(SnackBar(content: Text('Reset failed: $e')));
      }
    }
  }

  /// The phone AppBar's overflow menu, opened and awaited by the page itself.
  ///
  /// Not a PopupMenuButton: the button leaves the bar when the window widens
  /// past 720 px, and a PopupMenuButton that has gone drops whatever is picked
  /// in the menu it left open, so Reset became a dead tap (#407 review). This
  /// State outlives the button, and looks again for a write that started while
  /// the menu was up — Save tapped in the same frame as ⋮ still opens it.
  Future<void> _openMoreOptions(BuildContext buttonContext) async {
    final button = buttonContext.findRenderObject()! as RenderBox;
    final overlay = Navigator.of(buttonContext).overlay!.context
        .findRenderObject()! as RenderBox;
    final action = await showMenu<_EditorMenuAction>(
      context: context,
      // Over the button, as PopupMenuButton places it by default.
      position: RelativeRect.fromRect(
        Rect.fromPoints(
          button.localToGlobal(Offset.zero, ancestor: overlay),
          button.localToGlobal(button.size.bottomRight(Offset.zero),
              ancestor: overlay),
        ),
        Offset.zero & overlay.size,
      ),
      items: [
        PopupMenuItem(
          value: _EditorMenuAction.reset,
          child: ListTile(
            leading: const Icon(Icons.restore),
            title: Text(_resetLabel),
            contentPadding: EdgeInsets.zero,
          ),
        ),
      ],
    );
    if (!mounted || action == null || _saving) return;
    switch (action) {
      case _EditorMenuAction.reset:
        _reset();
    }
  }

  /// Note appended to the Split / Cut confirmations when the editor holds
  /// unsaved changes: those points are what gets cut (#127), so say so rather
  /// than let the user assume the stored track is being split.
  String get _pendingEditsNote => _c.isDirty
      ? '\n\nYour unsaved point edits will be applied as part of this.'
      : '';

  Future<void> _confirmSplit(int index) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Split activity'),
        content: Text(
          'Split into two activities at point ${index + 1}? '
          'The second half becomes a new local activity.$_pendingEditsNote',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Split'),
          ),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    // Backstop for the entry disabled in _actionsForPoint: a write that got
    // going while this was being confirmed has to land first, and the user
    // confirmed this, so say why nothing happens rather than drop it.
    if (_saving) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('Wait for the save to finish, then try again.')));
      return;
    }
    setState(() => _saving = true);
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    final route = ModalRoute.of(context);
    try {
      await _split(index, dropBoundary: false);
      if (!mounted) return;
      _closeEditor(navigator, route, true);
    } catch (e) {
      if (!mounted) return;
      setState(() => _saving = false);
      if (_isStaleVersionConflict(e)) {
        _handleStaleVersionConflict(messenger, navigator, route);
        return;
      }
      messenger.showSnackBar(SnackBar(content: Text('Split failed: $e')));
    }
  }

  /// Split at [index] of the editor's points, on the device for an encrypted
  /// track, by the server otherwise.
  Future<void> _split(int index, {required bool dropBoundary}) {
    final encrypted = widget.encrypted;
    if (encrypted != null) {
      return widget.notifier.splitEncryptedActivity(
          _activityId, encrypted, _c.points, index,
          dropBoundary: dropBoundary);
    }
    return widget.notifier.splitActivity(_activityId, index,
        dropBoundary: dropBoundary,
        payload: _c.toSavePayload(),
        lockVersion: _lockVersion);
  }

  /// Cut the track at [index] and drop the shared boundary point from the new
  /// tail activity, leaving a gap for a transportation segment to bridge (#104).
  /// Pops with a request for the caller to open the Add Transportation dialog,
  /// pre-filled from this activity's (now-shorter) end point.
  Future<void> _confirmCutForTransport(int index) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Cut for transportation'),
        content: Text(
          'Cut the track at point ${index + 1}? The portion after this point '
          'becomes a new local activity, and you\'ll be prompted to add a '
          'transportation segment to bridge the gap.$_pendingEditsNote',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Cut'),
          ),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    // Backstop for the entry disabled in _actionsForPoint: a write that got
    // going while this was being confirmed has to land first, and the user
    // confirmed this, so say why nothing happens rather than drop it.
    if (_saving) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('Wait for the save to finish, then try again.')));
      return;
    }
    setState(() => _saving = true);
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    final route = ModalRoute.of(context);
    try {
      await _split(index, dropBoundary: true);
      if (!mounted) return;
      _closeEditor(navigator, route, {'openSegmentFor': _activityId});
    } catch (e) {
      if (!mounted) return;
      setState(() => _saving = false);
      if (_isStaleVersionConflict(e)) {
        _handleStaleVersionConflict(messenger, navigator, route);
        return;
      }
      messenger.showSnackBar(SnackBar(content: Text('Cut failed: $e')));
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final pts = _c.points;
    // The app's phone/wide breakpoint (app_screen, day_carousel, the add FAB).
    // The editor is a full-screen route, so the screen's width is the AppBar's.
    final compact = MediaQuery.sizeOf(context).width < 720;

    return Scaffold(
      appBar: AppBar(
        title: LayoutBuilder(builder: (context, constraints) {
          final name = widget.encrypted?.name ??
              shownText(widget.activity['name'] as String?) ??
              'Activity';
          final title = Text(
            // On a phone "Edit — " took 49 of the title's 120 px at 360 px
            // (measured in Roboto) to say what Save, the point handles and the
            // hint below already say; the name is what tells one track from
            // another (#407). A screen reader still hears the whole title.
            compact ? name : 'Edit — $name',
            semanticsLabel: 'Edit — $name',
            style: theme.textTheme.titleMedium,
            overflow: TextOverflow.ellipsis,
          );
          // The AppBar gives the title whatever the actions leave it, and on a
          // phone with large text that is sometimes less than the badge's own
          // 24 px. A Row cannot hand space back, so below the point where the
          // name would get any at all the badge stands down instead of
          // overflowing into the actions.
          if (!_isGpxImport || constraints.maxWidth < 48) return title;
          return Row(
            // The AppBar centres the title on iOS when centerTitle is left to
            // the platform; a max-width Row would silently left-align it.
            mainAxisSize: MainAxisSize.min,
            children: [
              Flexible(child: title),
              const SizedBox(width: 8),
              Tooltip(
                message: 'Imported from a GPX file',
                child: Semantics(
                  label: 'Imported from a GPX file',
                  child: Icon(Icons.route,
                      key: const ValueKey('gpx_editor_badge'),
                      size: 16,
                      color: theme.colorScheme.onSurfaceVariant),
                ),
              ),
            ],
          );
        }),
        actions: [
          if (_isEdited && !compact)
            TextButton.icon(
              onPressed: _saving ? null : _reset,
              icon: const Icon(Icons.restore, size: 18),
              label: Text(_resetLabel),
              style: TextButton.styleFrom(
                foregroundColor: theme.colorScheme.onSurfaceVariant,
              ),
            ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 8),
            child: TrackEditorSaveButton(
              enabled: _c.canSave && !_saving,
              saving: _saving,
              onPressed: _save,
            ),
          ),
          // On a phone the labelled Reset button took nearly all of the title
          // slot (#407): measured in Roboto with a back button, the title got
          // 0 px at 320, 18 at 360 and 70 at 412, and "Edit — " alone is 49.
          // There Reset moves behind the overflow menu rather than shrinking
          // to a bare icon: it throws the edits away and closes the editor,
          // without asking unless pieces go with it, so the tap that commits
          // to it should land on its label and not on an unlabelled glyph
          // sitting next to Save.
          if (_isEdited && compact)
            Builder(
              builder: (buttonContext) => IconButton(
                icon: const Icon(Icons.more_vert),
                tooltip: 'More options',
                onPressed:
                    _saving ? null : () => _openMoreOptions(buttonContext),
              ),
            ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: TrackMapEditor(
              controller: _c,
              actionsForPoint: _actionsForPoint,
              pointMenuHint:
                  'Long-press or right-click a point to trim, split or delete',
            ),
          ),
          // Elevation chart synced to the current point list.
          Container(
            padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
            color: theme.colorScheme.surface,
            child: ElevationChart(
              activities: [_chartActivity(pts)],
              track: _chartTrack(pts),
              mapCursorNotifier: _chartCursor,
              color: kAccent,
            ),
          ),
        ],
      ),
    );
  }

  /// Wrap the current points as a pseudo-activity for [ElevationChart], which
  /// reads `elevation_profile` as `[[distKm, elevM], …]`.
  Map<String, dynamic> _chartActivity(List<EditPoint> pts) {
    final profile = <List<double>>[];
    double cum = 0;
    for (var i = 0; i < pts.length; i++) {
      if (i > 0) {
        cum += _haversineKm(pts[i - 1], pts[i]);
      }
      profile.add([cum, pts[i].elev ?? 0]);
    }
    return {
      'id': _activityId,
      'elevation_profile': profile,
    };
  }

  List<(double, GeoPoint)> _chartTrack(List<EditPoint> pts) {
    final track = <(double, GeoPoint)>[];
    double cum = 0;
    for (var i = 0; i < pts.length; i++) {
      if (i > 0) cum += _haversineKm(pts[i - 1], pts[i]);
      track.add((cum, (lat: pts[i].lat, lon: pts[i].lng)));
    }
    return track;
  }

  static double _haversineKm(EditPoint a, EditPoint b) {
    const distance = Distance();
    return distance.as(
          LengthUnit.Meter,
          LatLng(a.lat, a.lng),
          LatLng(b.lat, b.lng),
        ) /
        1000.0;
  }
}
