import 'dart:async';

import 'package:flutter/material.dart';

import '../map/great_circle.dart';
import 'location_picker_dialog.dart';
import 'project_notifier.dart';
import 'segment_track_editor_page.dart';
import '../crypto/encrypted_display.dart';

class SegmentDialog extends StatefulWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic>? editSegment;       // non-null = edit mode
  final int? insertAfterIndex;                   // create mode: where to insert
  final dynamic preselectedStartActivityId;      // pre-fill start from selected activity

  const SegmentDialog({
    super.key,
    required this.notifier,
    this.editSegment,
    this.insertAfterIndex,
    this.preselectedStartActivityId,
  });

  @override
  State<SegmentDialog> createState() => _SegmentDialogState();
}

class _SegmentDialogState extends State<SegmentDialog> {
  final _formKey = GlobalKey<FormState>();
  late String _segmentType;
  late TextEditingController _labelCtrl;
  late TextEditingController _startLatCtrl;
  late TextEditingController _startLonCtrl;
  late TextEditingController _endLatCtrl;
  late TextEditingController _endLonCtrl;
  DateTime? _date;
  bool _saving = false;
  // Pre-submit validation error, shown inline inside the dialog. Using the root
  // ScaffoldMessenger here would render the SnackBar behind the modal, where the
  // user never sees it (issue #20).
  String? _formError;
  Timer? _previewDebounce;

  dynamic _startActivityId;
  dynamic _endActivityId;
  late List<Map<String, dynamic>> _activityList;

  // Rail-track fields (train segments only)
  String _routeMode = 'great_circle';
  late TextEditingController _trainNumberCtrl;

  static const _months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  static String _fmtDate(DateTime d) => '${_months[d.month - 1]} ${d.day}, ${d.year}';
  static String _toIso(DateTime d) =>
      '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';

  @override
  void initState() {
    super.initState();
    final seg = widget.editSegment;
    _segmentType = (seg?['segment_type'] as String?) ?? 'flight';
    _labelCtrl = TextEditingController(text: seg?['label'] as String? ?? '');
    _startLatCtrl = TextEditingController(
        text: _fmt(seg?['start']?['lat']));
    _startLonCtrl = TextEditingController(
        text: _fmt(seg?['start']?['lon']));
    _endLatCtrl = TextEditingController(
        text: _fmt(seg?['end']?['lat']));
    _endLonCtrl = TextEditingController(
        text: _fmt(seg?['end']?['lon']));

    // Date: edit mode reads from segment, create mode auto-infers
    final dateStr = seg?['date'] as String?;
    if (dateStr != null) {
      _date = DateTime.tryParse(dateStr);
    }

    // Rail-track fields
    _routeMode     = (seg?['route_mode'] as String?) ?? 'great_circle';
    _trainNumberCtrl = TextEditingController(
        text: seg?['train_number'] as String? ?? '');
    _activityList = List<Map<String, dynamic>>.from(widget.notifier.activities)
      ..sort((a, b) {
        final da = (a['start_date_local'] as String?) ?? '';
        final db = (b['start_date_local'] as String?) ?? '';
        return db.compareTo(da); // most recent first
      });

    // Auto-populate from adjacent activities if creating new segment
    if (seg == null) _autoPopulate();

    // Update arc preview whenever any coordinate field changes.
    // Debounced so greatCirclePoints() is not called on every keystroke.
    _startLatCtrl.addListener(_schedulePreviewUpdate);
    _startLonCtrl.addListener(_schedulePreviewUpdate);
    _endLatCtrl.addListener(_schedulePreviewUpdate);
    _endLonCtrl.addListener(_schedulePreviewUpdate);
    // Defer so the ValueNotifier change doesn't fire during the first build.
    WidgetsBinding.instance.addPostFrameCallback((_) => _updatePreview());
  }

  void _schedulePreviewUpdate() {
    _previewDebounce?.cancel();
    _previewDebounce = Timer(
      const Duration(milliseconds: 250),
      _updatePreview,
    );
  }

  void _updatePreview() {
    if (!widget.notifier.isAlive) return; // see dispose()
    final lat1 = double.tryParse(_startLatCtrl.text.trim());
    final lon1 = double.tryParse(_startLonCtrl.text.trim());
    final lat2 = double.tryParse(_endLatCtrl.text.trim());
    final lon2 = double.tryParse(_endLonCtrl.text.trim());
    if (lat1 != null && lon1 != null && lat2 != null && lon2 != null) {
      widget.notifier.previewArcNotifier.value = greatCirclePoints(lat1, lon1, lat2, lon2);
    } else {
      widget.notifier.previewArcNotifier.value = null;
    }
  }

  String _fmt(dynamic v) {
    if (v == null) return '';
    final d = (v as num).toDouble();
    return d == 0.0 ? '' : d.toStringAsFixed(6);
  }

  void _autoPopulate() {
    final items = widget.notifier.items;
    final activities = widget.notifier.activities;
    final insertAfter = widget.insertAfterIndex;

    Map<String, dynamic>? actById(dynamic id) {
      if (id == null) return null;
      try {
        return activities.firstWhere((a) => a['id'] == id);
      } catch (_) {
        return null;
      }
    }

    // If a selected activity was passed, use it as start and auto-advance end.
    final preselected = widget.preselectedStartActivityId;
    if (preselected != null) {
      final a = actById(preselected);
      if (a != null) {
        _startActivityId = preselected;
        final endLl = a['end_latlng'];
        if (endLl is List && endLl.length >= 2) {
          _startLatCtrl.text = (endLl[0] as num).toStringAsFixed(6);
          _startLonCtrl.text = (endLl[1] as num).toStringAsFixed(6);
        }
        final ds = a['start_date_local'] as String?;
        if (ds != null) _date = DateTime.tryParse(ds);
        // Auto-advance end: list sorted descending, so next-in-time = idx - 1.
        final idx = _activityList.indexWhere((x) => x['id'] == preselected);
        if (idx > 0) {
          final next = _activityList[idx - 1];
          _endActivityId = next['id'];
          final nll = next['start_latlng'];
          if (nll is List && nll.length >= 2) {
            _endLatCtrl.text = (nll[0] as num).toStringAsFixed(6);
            _endLonCtrl.text = (nll[1] as num).toStringAsFixed(6);
          }
        }
        return;
      }
      // a == null: activity not in list — fall through to insertAfterIndex logic.
    }

    if (insertAfter == null || items.isEmpty) return;

    // Predecessor: item at insertAfter
    if (insertAfter >= 0 && insertAfter < items.length) {
      final prev = items[insertAfter];
      if (prev['item_type'] == 'activity') {
        final a = actById(prev['activity_id']);
        final endLl = a?['end_latlng'];
        if (endLl is List && endLl.length >= 2) {
          _startLatCtrl.text = (endLl[0] as num).toStringAsFixed(6);
          _startLonCtrl.text = (endLl[1] as num).toStringAsFixed(6);
          _startActivityId = prev['activity_id'];
        }
      } else if (prev['item_type'] == 'segment') {
        final end = prev['segment']?['end'] as Map?;
        if (end != null) {
          _startLatCtrl.text = (end['lat'] as num).toStringAsFixed(6);
          _startLonCtrl.text = (end['lon'] as num).toStringAsFixed(6);
        }
      }
    }

    // Successor: item after insertAfter
    final nextIdx = insertAfter + 1;
    if (nextIdx < items.length) {
      final next = items[nextIdx];
      if (next['item_type'] == 'activity') {
        final a = actById(next['activity_id']);
        final startLl = a?['start_latlng'];
        if (startLl is List && startLl.length >= 2) {
          _endLatCtrl.text = (startLl[0] as num).toStringAsFixed(6);
          _endLonCtrl.text = (startLl[1] as num).toStringAsFixed(6);
          _endActivityId = next['activity_id'];
        }
      } else if (next['item_type'] == 'segment') {
        final start = next['segment']?['start'] as Map?;
        if (start != null) {
          _endLatCtrl.text = (start['lat'] as num).toStringAsFixed(6);
          _endLonCtrl.text = (start['lon'] as num).toStringAsFixed(6);
        }
      }
    }

  }

  @override
  void dispose() {
    _previewDebounce?.cancel();
    // Defer so we don't fire the ValueNotifier while the framework tree is
    // locked during unmount (finalizeTree / lockState).
    final notifier = widget.notifier;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      // Unless the notifier went first: the provider disposes it when the
      // account changes, and a disposed ValueNotifier asserts (issue #418).
      if (notifier.isAlive) notifier.previewArcNotifier.value = null;
    });
    _labelCtrl.dispose();
    _startLatCtrl.dispose();
    _startLonCtrl.dispose();
    _endLatCtrl.dispose();
    _endLonCtrl.dispose();
    _trainNumberCtrl.dispose();
    super.dispose();
  }

  Future<void> _pickLocation({
    required String title,
    required TextEditingController latCtrl,
    required TextEditingController lonCtrl,
    required bool isStart,
  }) async {
    // Determine the "other" endpoint so the picker can show a live arc preview
    final otherLatCtrl = isStart ? _endLatCtrl   : _startLatCtrl;
    final otherLonCtrl = isStart ? _endLonCtrl   : _startLonCtrl;
    final result = await showDialog<LatLonResult>(
      context: context,
      useRootNavigator: true,
      builder: (_) => LocationPickerDialog(
        title: title,
        initialLat: double.tryParse(latCtrl.text.trim()),
        initialLon: double.tryParse(lonCtrl.text.trim()),
        geo: widget.notifier.geoFacet.geo,
        previewArcNotifier: widget.notifier.previewArcNotifier,
        otherLat: double.tryParse(otherLatCtrl.text.trim()),
        otherLon: double.tryParse(otherLonCtrl.text.trim()),
        isStart: isStart,
      ),
    );
    if (result != null && mounted) {
      setState(() {
        latCtrl.text = result.lat.toStringAsFixed(6);
        lonCtrl.text = result.lon.toStringAsFixed(6);
      });
    }
  }

  /// Close this dialog and push the full-screen track editor (issue #150) for
  /// the segment being edited — the same close-then-push hand-off
  /// [ActivityEditorPage]'s "Cut & add transport" flow uses. Unsaved label/
  /// date changes in this form are discarded; only the route geometry is
  /// edited on the next screen.
  void _openTrackEditor(BuildContext context) {
    final navigator = Navigator.of(context, rootNavigator: true);
    final seg = widget.editSegment!;
    navigator.pop();
    navigator.push(MaterialPageRoute(
      builder: (_) =>
          SegmentTrackEditorPage(notifier: widget.notifier, segment: seg),
    ));
  }

  /// Whether the current form selection would trigger an auto-resolve on save
  /// (i.e. "Follow route" is selected for a type that supports one).
  bool get _needsResolve =>
      (_segmentType == 'train' && _routeMode == 'rail')  ||
      (_segmentType == 'boat'  && _routeMode == 'ferry') ||
      (_segmentType == 'bus'   && _routeMode == 'bus');

  Future<void> _save() async {
    final startLat = double.tryParse(_startLatCtrl.text.trim());
    final startLon = double.tryParse(_startLonCtrl.text.trim());
    final endLat   = double.tryParse(_endLatCtrl.text.trim());
    final endLon   = double.tryParse(_endLonCtrl.text.trim());
    if (_date == null) {
      setState(() => _formError = 'Set a date for the segment');
      return;
    }
    if (startLat == null || startLon == null ||
        endLat   == null || endLon   == null) {
      setState(() => _formError = 'Set a start and end location');
      return;
    }

    // Re-resolving a manually edited route (issue #150) discards the edit —
    // confirm BEFORE the saving spinner starts, not after: showing this on
    // top of an already-spinning Save button leaves that indeterminate
    // progress indicator animating for as long as the confirm dialog is
    // open, which (as well as reading oddly — nothing is saving yet) never
    // lets a widget test's pumpAndSettle() converge.
    final wasManuallyEdited = widget.editSegment?['route_edited'] == true;
    if (_needsResolve && wasManuallyEdited) {
      final discard = await showDialog<bool>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: const Text('Discard manual route edits?'),
          content: const Text(
            'This route was manually edited. Resolving it automatically will '
            'discard those edits and replace them with the auto-calculated route.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.of(ctx).pop(true),
              child: const Text('Discard & re-resolve'),
            ),
          ],
        ),
      );
      if (discard != true || !mounted) return;
    }

    setState(() {
      _formError = null;
      _saving = true;
    });
    try {
      await _saveBody(startLat, startLon, endLat, endLon, force: wasManuallyEdited);
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        _formError = e.toString().replaceFirst('Exception: ', '');
      });
    }
  }

  Future<void> _saveBody(
    double startLat, double startLon,
    double endLat, double endLon, {
    required bool force,
  }) async {
    final dateStr = _toIso(_date!);

    // Default label: "Day N - Type M" when user leaves the field empty.
    String label = _labelCtrl.text.trim();
    if (label.isEmpty) {
      // Resolve trip start — explicit setting first, then earliest activity date
      // (mirrors the activity panel's _buildDisplayList fallback logic).
      String? tripStartStr = widget.notifier.tripStart;
      if (tripStartStr == null) {
        for (final a in widget.notifier.activities) {
          final ds = (a['start_date_local'] as String?)?.split('T').first;
          if (ds != null && (tripStartStr == null || ds.compareTo(tripStartStr) < 0)) {
            tripStartStr = ds;
          }
        }
      }
      int dayNum = 1;
      if (tripStartStr != null) {
        final ts = DateTime.parse(tripStartStr);
        final tripStartDate = DateTime.utc(ts.year, ts.month, ts.day);
        final segDate = DateTime.utc(_date!.year, _date!.month, _date!.day);
        dayNum = segDate.difference(tripStartDate).inDays + 1;
      }
      final currentSegId = widget.editSegment?['id'] as String?;
      final siblingsOnDay = widget.notifier.items.where((item) {
        if (item['item_type'] != 'segment') return false;
        final seg = item['segment'] as Map?;
        if (seg?['date'] != dateStr) return false;
        if (currentSegId != null && seg?['id'] == currentSegId) return false;
        return true;
      }).length;
      final typeLabel = _segmentType[0].toUpperCase() + _segmentType.substring(1);
      label = 'Day $dayNum - $typeLabel ${siblingsOnDay + 1}';
    }

    final trainNum = _trainNumberCtrl.text.trim().isEmpty
        ? null
        : _trainNumberCtrl.text.trim();
    final needsResolve = _needsResolve;

    // If a start activity is selected, insert the segment immediately after it
    // so it sits between the start and end activities in the panel.
    int? insertAfterIndex = widget.insertAfterIndex;
    if (_startActivityId != null) {
      final notifierItems = widget.notifier.items;
      for (int i = 0; i < notifierItems.length; i++) {
        if (notifierItems[i]['item_type'] == 'activity' &&
            notifierItems[i]['activity_id'] == _startActivityId) {
          insertAfterIndex = i;
          break;
        }
      }
    }

    String resolveSegId = '';
    if (widget.editSegment != null) {
      await widget.notifier.updateSegment(
        widget.editSegment!['id'] as String,
        segmentType: _segmentType,
        label: label,
        startLat: startLat,
        startLon: startLon,
        endLat: endLat,
        endLon: endLon,
        date: dateStr,
        trainNumber: trainNum,
        routeMode: {'train', 'boat', 'bus'}.contains(_segmentType) ? _routeMode : null,
      );
      resolveSegId = widget.editSegment!['id'] as String;
    } else {
      resolveSegId = await widget.notifier.addSegment(
        segmentType: _segmentType,
        label: label,
        startLat: startLat,
        startLon: startLon,
        endLat: endLat,
        endLon: endLon,
        insertAfterIndex: insertAfterIndex,
        date: dateStr,
        trainNumber: trainNum,
      );
    }

    if (!mounted) return;

    if (needsResolve && resolveSegId.isNotEmpty) {
      final messenger = ScaffoldMessenger.of(context);
      final notifier  = widget.notifier;
      final segId     = resolveSegId;
      final routeMode = const {'train': 'rail', 'boat': 'ferry', 'bus': 'bus'}[_segmentType]!;
      final resolveMsg = routeMode == 'ferry'
          ? 'Calculating ferry route — this may take up to a minute…'
          : routeMode == 'bus'
              ? 'Calculating bus route — this may take up to a minute…'
              : 'Calculating rail route — this may take up to a minute…';
      messenger.showSnackBar(SnackBar(
        content: Text(resolveMsg),
        duration: const Duration(seconds: 10),
      ));
      Navigator.of(context).pop();
      if (widget.editSegment == null) notifier.selectSegment(segId);
      unawaited(_resolveAsync(
        notifier, segId, routeMode, trainNum, dateStr, messenger,
        force: force,
      ));
    } else {
      Navigator.of(context).pop();
      if (widget.editSegment == null && resolveSegId.isNotEmpty) {
        widget.notifier.selectSegment(resolveSegId);
      }
    }
  }

  static Future<void> _resolveAsync(
    ProjectNotifier notifier,
    String segId,
    String routeMode,
    String? trainNumber,
    String? date,
    ScaffoldMessengerState messenger, {
    bool force = false,
  }) async {
    try {
      final result = await notifier.resolveTrainRoute(
        segId,
        routeMode: routeMode,
        trainNumber: routeMode == 'rail' ? trainNumber : null,
        date: date,
        force: force,
      );
      final status = result['route_status'] as String? ?? 'resolved';
      if (status == 'cancelled') return; // navigated away / deleted — stay silent
      if (!notifier.isAlive) return;     // page gone — don't touch the messenger
      final stopCount = result['stop_count'] as int? ?? 0;
      final degraded = result['degraded'] == true;
      final hafasFailed = result['hafas_failed'] == true;
      messenger.showSnackBar(SnackBar(
        content: Text(resolveOutcomeMessage(
          degraded: degraded,
          hafasFailed: hafasFailed,
          routeMode: routeMode,
          stopCount: stopCount,
          routeError: result['route_error'] as String?,
        )),
        duration: Duration(seconds: degraded || hafasFailed ? 6 : 5),
      ));
    } catch (e) {
      if (!notifier.isAlive) return;
      messenger.showSnackBar(SnackBar(
        content: Text('Route unavailable: ${_resolveErrMsg(e)}'),
        duration: const Duration(seconds: 6),
      ));
    }
  }

  /// Converts a resolve-route exception into a short human-readable string.
  /// Strips nginx HTML error pages that would otherwise flood the snackbar.
  static String _resolveErrMsg(Object e) {
    final s = e.toString();
    final detail = RegExp(r'"detail"\s*:\s*"([^"]+)"').firstMatch(s)?.group(1);
    if (detail != null) return detail;
    final codeMatch = RegExp(r'ApiException\((\d+)\)').firstMatch(s);
    if (codeMatch != null) {
      final code = codeMatch.group(1)!;
      final body = s.substring(codeMatch.end).replaceFirst(RegExp(r'^:\s*'), '');
      if (body.trimLeft().startsWith('<')) {
        if (code == '504') return 'Server timeout — route calculation took too long, please try again';
        if (code == '502' || code == '503') return 'Server unreachable ($code)';
        return 'Server error ($code)';
      }
      return body;
    }
    return s.replaceFirst('Exception: ', '');
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isEdit = widget.editSegment != null;
    return AlertDialog(
      title: Text(isEdit ? 'Edit Segment' : 'Add Segment'),
      content: SizedBox(
        width: 420,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Flexible(
              child: SingleChildScrollView(
                child: Form(
          key: _formKey,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Segment type
              const Text('Type'),
              const SizedBox(height: 6),
              SizedBox(
                width: double.infinity,
                child: SegmentedButton<String>(
                  segments: const [
                    ButtonSegment(value: 'flight', icon: Icon(Icons.flight), label: Text('Flight')),
                    ButtonSegment(value: 'train',  icon: Icon(Icons.train),  label: Text('Train')),
                    ButtonSegment(value: 'bus',    icon: Icon(Icons.directions_bus), label: Text('Bus')),
                    ButtonSegment(value: 'boat',   icon: Icon(Icons.directions_boat), label: Text('Boat')),
                  ],
                  selected: {_segmentType},
                  onSelectionChanged: (s) => setState(() {
                    final prev = _segmentType;
                    _segmentType = s.first;
                    const modeForType = {'train': 'rail', 'boat': 'ferry', 'bus': 'bus'};
                    // Leaving a mode-aware type → reset to great circle.
                    if (modeForType[prev] != null &&
                        _routeMode == modeForType[prev] &&
                        s.first != prev) {
                      _routeMode = 'great_circle';
                    }
                    // Entering a mode-aware type → auto-enable its route mode.
                    final newMode = modeForType[_segmentType];
                    if (newMode != null && _routeMode == 'great_circle') {
                      _routeMode = newMode;
                    }
                  }),
                  multiSelectionEnabled: false,
                ),
              ),
              const SizedBox(height: 16),
              // Label
              TextFormField(
                controller: _labelCtrl,
                decoration: const InputDecoration(
                  labelText: 'Label (optional)',
                  hintText: 'e.g. Basel → Paris',
                ),
              ),
              const SizedBox(height: 12),
              // Date
              InkWell(
                borderRadius: BorderRadius.circular(8),
                onTap: () async {
                  final picked = await showDatePicker(
                    context: context,
                    initialDate: _date ?? DateTime.now(),
                    firstDate: DateTime(2000),
                    lastDate: DateTime(2100),
                  );
                  if (picked != null) {
                    setState(() {
                      _date = picked;
                      _formError = null;
                    });
                  }
                },
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Row(
                    children: [
                      Icon(Icons.calendar_today, size: 18,
                          color: theme.colorScheme.onSurfaceVariant),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Text(
                          _date == null ? 'No date set' : _fmtDate(_date!),
                          style: theme.textTheme.bodyMedium?.copyWith(
                            color: _date == null
                                ? theme.colorScheme.onSurfaceVariant
                                : null,
                          ),
                        ),
                      ),
                      if (_date != null)
                        IconButton(
                          icon: const Icon(Icons.close, size: 16),
                          visualDensity: VisualDensity.compact,
                          onPressed: () => setState(() => _date = null),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 4),
              // ── Start ────────────────────────────────────────────────
              _EndpointRow(
                label: 'Start',
                activities: _activityList,
                selectedId: _startActivityId,
                hint: 'From activity (end point)',
                latCtrl: _startLatCtrl,
                lonCtrl: _startLonCtrl,
                onActivityChanged: (a) => setState(() {
                  _startActivityId = a?['id'];
                  if (a == null) return;
                  final ll = a['end_latlng'];
                  if (ll is List && ll.length >= 2) {
                    _startLatCtrl.text = (ll[0] as num).toStringAsFixed(6);
                    _startLonCtrl.text = (ll[1] as num).toStringAsFixed(6);
                  }
                  // Set date from the start activity's date.
                  final ds = a['start_date_local'] as String?;
                  if (ds != null) _date = DateTime.tryParse(ds);
                  // Auto-advance end to the chronologically next activity.
                  // List is sorted descending, so next-in-time = idx - 1.
                  final idx = _activityList.indexWhere((x) => x['id'] == a['id']);
                  if (idx > 0) {
                    final next = _activityList[idx - 1];
                    _endActivityId = next['id'];
                    final nll = next['start_latlng'];
                    if (nll is List && nll.length >= 2) {
                      _endLatCtrl.text = (nll[0] as num).toStringAsFixed(6);
                      _endLonCtrl.text = (nll[1] as num).toStringAsFixed(6);
                    }
                  }
                }),
                onPickMap: () => _pickLocation(
                  title: 'Pick start location',
                  latCtrl: _startLatCtrl,
                  lonCtrl: _startLonCtrl,
                  isStart: true,
                ),
              ),
              const SizedBox(height: 12),
              // ── End ──────────────────────────────────────────────────────
              _EndpointRow(
                label: 'End',
                activities: _activityList,
                selectedId: _endActivityId,
                hint: 'From activity (start point)',
                latCtrl: _endLatCtrl,
                lonCtrl: _endLonCtrl,
                onActivityChanged: (a) => setState(() {
                  _endActivityId = a?['id'];
                  if (a == null) return;
                  final ll = a['start_latlng'];
                  if (ll is List && ll.length >= 2) {
                    _endLatCtrl.text = (ll[0] as num).toStringAsFixed(6);
                    _endLonCtrl.text = (ll[1] as num).toStringAsFixed(6);
                  }
                }),
                onPickMap: () => _pickLocation(
                  title: 'Pick end location',
                  latCtrl: _endLatCtrl,
                  lonCtrl: _endLonCtrl,
                  isStart: false,
                ),
              ),

              // ── Rail track section (train only) ──────────────────────
              if (_segmentType == 'train') ...[
                const SizedBox(height: 16),
                const Divider(),
                const SizedBox(height: 8),
                Text('Rail track', style: Theme.of(context).textTheme.labelMedium),
                const SizedBox(height: 10),
                SizedBox(
                  width: double.infinity,
                  child: SegmentedButton<String>(
                    segments: const [
                      ButtonSegment(
                        value: 'great_circle',
                        icon: Icon(Icons.show_chart, size: 16),
                        label: Text('Great circle'),
                      ),
                      ButtonSegment(
                        value: 'rail',
                        icon: Icon(Icons.train, size: 16),
                        label: Text('Follow rail lines'),
                      ),
                    ],
                    selected: {_routeMode},
                    onSelectionChanged: (s) =>
                        setState(() => _routeMode = s.first),
                    multiSelectionEnabled: false,
                  ),
                ),
                if (_routeMode == 'rail') ...[
                  const SizedBox(height: 12),
                  TextFormField(
                    controller: _trainNumberCtrl,
                    decoration: const InputDecoration(
                      labelText: 'Train number (optional)',
                      hintText: 'e.g. ICE 596',
                      isDense: true,
                    ),
                  ),
                ],
              ],

              // ── Ferry route section (boat only) ───────────────────────
              if (_segmentType == 'boat') ...[
                const SizedBox(height: 16),
                const Divider(),
                const SizedBox(height: 8),
                Text('Ferry route', style: Theme.of(context).textTheme.labelMedium),
                const SizedBox(height: 10),
                SizedBox(
                  width: double.infinity,
                  child: SegmentedButton<String>(
                    segments: const [
                      ButtonSegment(
                        value: 'great_circle',
                        icon: Icon(Icons.show_chart, size: 16),
                        label: Text('Great circle'),
                      ),
                      ButtonSegment(
                        value: 'ferry',
                        icon: Icon(Icons.directions_boat, size: 16),
                        label: Text('Follow ferry route'),
                      ),
                    ],
                    selected: {_routeMode == 'ferry' ? 'ferry' : 'great_circle'},
                    onSelectionChanged: (s) => setState(() => _routeMode = s.first),
                    multiSelectionEnabled: false,
                  ),
                ),
              ],

              // ── Bus route section (bus only) ──────────────────────────
              if (_segmentType == 'bus') ...[
                const SizedBox(height: 16),
                const Divider(),
                const SizedBox(height: 8),
                Text('Bus route', style: Theme.of(context).textTheme.labelMedium),
                const SizedBox(height: 10),
                SizedBox(
                  width: double.infinity,
                  child: SegmentedButton<String>(
                    segments: const [
                      ButtonSegment(
                        value: 'great_circle',
                        icon: Icon(Icons.show_chart, size: 16),
                        label: Text('Great circle'),
                      ),
                      ButtonSegment(
                        value: 'bus',
                        icon: Icon(Icons.directions_bus, size: 16),
                        label: Text('Follow bus route'),
                      ),
                    ],
                    selected: {_routeMode == 'bus' ? 'bus' : 'great_circle'},
                    onSelectionChanged: (s) => setState(() => _routeMode = s.first),
                    multiSelectionEnabled: false,
                  ),
                ),
              ],

              // ── Manual track editing (issue #150) ─────────────────────
              // Only for an existing segment (nothing to attach a track to
              // before it's created) whose type supports a real route.
              if (isEdit &&
                  (_segmentType == 'train' ||
                      _segmentType == 'boat' ||
                      _segmentType == 'bus')) ...[
                const SizedBox(height: 12),
                SizedBox(
                  width: double.infinity,
                  child: OutlinedButton.icon(
                    icon: const Icon(Icons.edit_road, size: 18),
                    label: const Text('Edit track manually'),
                    onPressed: () => _openTrackEditor(context),
                  ),
                ),
              ],
            ],
          ),
                ),
              ),
            ),
            if (_formError != null) ...[
              const SizedBox(height: 12),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                decoration: BoxDecoration(
                  color: theme.colorScheme.errorContainer,
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Row(
                  children: [
                    Icon(Icons.error_outline, size: 18,
                        color: theme.colorScheme.onErrorContainer),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        _formError!,
                        style: theme.textTheme.bodySmall?.copyWith(
                            color: theme.colorScheme.onErrorContainer),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ],
        ),
      ),
      actionsPadding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
      actions: [
        Row(
          children: [
            Expanded(
              child: FilledButton.tonal(
                onPressed: () => Navigator.of(context).pop(),
                child: const Text('Cancel'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: ElevatedButton(
                onPressed: _saving ? null : _save,
                child: _saving
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('Save'),
              ),
            ),
          ],
        ),
      ],
    );
  }
}

/// The label shown in the *closed* dropdown field when no activity is
/// linked. A custom map pin (lat/lon set with no linked activity) reads as
/// "Custom" rather than the "— clear —" label, which would otherwise wrongly
/// imply nothing is selected.
String endpointClearOptionLabel({required bool hasCoords}) =>
    hasCoords ? 'Custom' : '— clear —';

/// The snackbar message for a resolve that came back `resolved`.
///
/// Rail can "resolve" to a straight endpoint chord when OSM has no usable
/// track ([degraded]), and the selected train's own schedule lookup can fail
/// so the geometry is a generic two-point route ([hafasFailed]). Both happen
/// together whenever the train provider is down (issue #277) — reporting only
/// the approximate route there hides the real cause, so the train failure is
/// named first and both are said in one message. [routeError] is the
/// provider's own reason, when the server kept one.
String resolveOutcomeMessage({
  required bool degraded,
  required bool hafasFailed,
  required String routeMode,
  required int stopCount,
  String? routeError,
}) {
  if (hafasFailed) {
    final reason =
        (routeError == null || routeError.isEmpty) ? '' : ' ($routeError)';
    return degraded
        ? 'The selected train could not be looked up$reason — approximate '
            'route shown, no detailed track found for this segment'
        : 'Route resolved, but the selected train could not be looked up'
            '$reason — showing a generic route instead';
  }
  if (degraded) {
    return 'Approximate route shown — no detailed track found for this segment';
  }
  return switch (routeMode) {
    'ferry' => 'Ferry route resolved',
    'bus'   => 'Bus route resolved',
    _       => 'Rail route resolved · $stopCount stops',
  };
}

// ── Endpoint row (activity picker + map picker, no raw coord fields) ──────────

class _EndpointRow extends StatelessWidget {
  final String label;
  final List<Map<String, dynamic>> activities;
  final dynamic selectedId;
  final String hint;
  final TextEditingController latCtrl;
  final TextEditingController lonCtrl;
  final void Function(Map<String, dynamic>?) onActivityChanged;
  final VoidCallback onPickMap;

  const _EndpointRow({
    required this.label,
    required this.activities,
    required this.selectedId,
    required this.hint,
    required this.latCtrl,
    required this.lonCtrl,
    required this.onActivityChanged,
    required this.onPickMap,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    Map<String, dynamic>? selectedActivity;
    if (selectedId != null) {
      try {
        selectedActivity =
            activities.firstWhere((a) => a['id'] == selectedId);
      } catch (_) {}
    }

    final hasCoords =
        latCtrl.text.isNotEmpty && lonCtrl.text.isNotEmpty;

    String? statusText;
    if (selectedActivity != null) {
      statusText = shownText(selectedActivity['name'] as String?) ?? 'Activity';
    } else if (hasCoords) {
      final lat = double.tryParse(latCtrl.text);
      final lon = double.tryParse(lonCtrl.text);
      if (lat != null && lon != null) {
        statusText =
            '${lat.toStringAsFixed(3)}°, ${lon.toStringAsFixed(3)}°';
      }
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(children: [
          Text(label,
              style: theme.textTheme.labelMedium
                  ?.copyWith(fontWeight: FontWeight.w600)),
          const Spacer(),
          TextButton.icon(
            icon: const Icon(Icons.location_on_outlined, size: 16),
            label: const Text('Pick on map'),
            style:
                TextButton.styleFrom(visualDensity: VisualDensity.compact),
            onPressed: onPickMap,
          ),
        ]),
        if (activities.isNotEmpty) ...[
          const SizedBox(height: 4),
          DropdownButtonFormField<dynamic>(
            initialValue: selectedId,
            isExpanded: true,
            decoration: InputDecoration(
              isDense: true,
              hintText: hint,
              contentPadding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            ),
            items: [
              const DropdownMenuItem<dynamic>(
                value: null,
                child: Text('— clear —',
                    style: TextStyle(fontStyle: FontStyle.italic)),
              ),
              ...activities.map((a) => DropdownMenuItem<dynamic>(
                    value: a['id'],
                    child: Text(
                      shownText(a['name'] as String?) ?? 'Activity',
                      overflow: TextOverflow.ellipsis,
                    ),
                  )),
            ],
            // Menu options always offer "— clear —"; but if a custom
            // map pin is set (no linked activity), the closed field should
            // read "Custom" rather than implying nothing is selected.
            selectedItemBuilder: (context) => [
              Text(endpointClearOptionLabel(hasCoords: hasCoords),
                  style: const TextStyle(fontStyle: FontStyle.italic)),
              ...activities.map((a) => Text(
                    shownText(a['name'] as String?) ?? 'Activity',
                    overflow: TextOverflow.ellipsis,
                  )),
            ],
            onChanged: (id) {
              if (id == null) {
                onActivityChanged(null);
              } else {
                try {
                  onActivityChanged(
                      activities.firstWhere((a) => a['id'] == id));
                } catch (_) {
                  onActivityChanged(null);
                }
              }
            },
          ),
        ],
        const SizedBox(height: 4),
        Text(
          statusText ?? 'No location set',
          style: theme.textTheme.bodySmall?.copyWith(
            color: statusText != null
                ? theme.colorScheme.primary
                : theme.colorScheme.onSurfaceVariant
                    .withValues(alpha: 0.6),
            fontStyle:
                statusText == null ? FontStyle.italic : FontStyle.normal,
          ),
        ),
      ],
    );
  }
}
