/// Import an activity from a GPX file: pick, review, confirm (issue #260).
///
/// The first version of this dialog asked for a date, a start time, an end time
/// and an activity type up front, then uploaded and only then said whether the
/// file was acceptable at all. A user filled in four fields for a ride they did
/// three weeks ago and learned afterwards that the file had two tracks in it.
///
/// So the file speaks first. Picking it inspects it — a dry run that writes
/// nothing — and everything the file knows arrives already filled in: its name,
/// its type, when it happened, how far it went, how much it climbed, drawn as a
/// thumbnail so "wrong file" is obvious before it becomes an activity someone
/// has to delete. The user confirms rather than transcribes, and every field is
/// still editable, because the file is not always right.
///
/// Unlike the Strava and Polarsteps imports there is no OAuth round-trip, so
/// this stays a `showDialog` modal rather than a routed screen.
library;

import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../api/client.dart';
import '../core/design_tokens.dart';
import '../core/picked_file_bytes.dart';
import '../core/project_ref.dart';
import '../map/geo_point.dart';
import 'gpx_import_model.dart';

/// (value, label) pairs for the activity-type selector — the concrete lowercase
/// type strings this app recognises, one per icon/colour bucket, plus the
/// catch-all. "Other" is what a kayak or a ski tour honestly is here; without it
/// they could only be imported as mislabelled hikes.
const _kActivityTypes = [
  ('run', 'Run'),
  ('ride', 'Ride'),
  ('hike', 'Hike'),
  ('walk', 'Walk'),
  ('Workout', 'Other'),
];

IconData _typeIcon(String type) => switch (type) {
      'run' => Icons.directions_run,
      'ride' => Icons.directions_bike,
      'walk' => Icons.directions_walk,
      'hike' => Icons.hiking,
      _ => Icons.map_outlined,
    };

/// What the dialog hands back on success, so the caller can offer to open the
/// new activity or undo it rather than only reloading the trip.
///
/// One track gives one activity. "Import all" gives [activityIds] (possibly
/// none, when every track was already in the trip) and counts what it
/// [skipped] as duplicates, so the caller can say both.
class GpxImportResult {
  GpxImportResult({required int activityId, required this.name})
      : activityIds = [activityId],
        skipped = 0;

  const GpxImportResult.all({
    required this.activityIds,
    required this.skipped,
    this.name = '',
  });

  final List<int> activityIds;
  final int skipped;
  final String name;

  /// The first (for a single import, the only) activity created.
  int get activityId => activityIds.first;
}

class GpxImportDialog extends StatefulWidget {
  const GpxImportDialog({
    super.key,
    required this.projectRef,
    this.httpClient,
    this.tripStart,
    this.tripEnd,
    this.initialFile,
  });

  final ProjectRef projectRef;

  /// A file already in hand (one opened from another app, issue #368): the
  /// dialog skips the pick step and inspects it straight away.
  final ({String name, Uint8List bytes})? initialFile;

  /// Injectable so tests can supply one backed by a MockClient — mirrors
  /// ApiClient's own constructor-injection pattern. `http.MultipartRequest`'s
  /// own `.send()` always spins up a fresh, un-mockable client, so this dialog
  /// routes through [httpClient] explicitly instead.
  final http.Client? httpClient;

  /// The trip's own dates, as `YYYY-MM-DD`. Used to aim the date picker at the
  /// trip rather than at today, and to warn when a chosen date falls outside it
  /// — which silently extends the trip, and costs a billing day.
  final String? tripStart;
  final String? tripEnd;

  @override
  State<GpxImportDialog> createState() => _GpxImportDialogState();
}

enum _Stage { pick, reading, choose, review, submitting }

class _GpxImportDialogState extends State<GpxImportDialog> {
  late final http.Client _client = widget.httpClient ?? http.Client();

  _Stage _stage = _Stage.pick;
  Uint8List? _fileBytes;
  String? _fileName;
  GpxInspection? _inspection;
  GpxCandidate? _chosen;

  final _nameController = TextEditingController();
  String? _activityType;
  DateTime? _date;
  TimeOfDay? _startTime;
  TimeOfDay? _endTime;

  /// True while the form still holds exactly what the file said, so the import
  /// can omit the times and let the server read them from the file — where they
  /// carry seconds, which `HH:MM` would throw away. Echoing the truncated
  /// minute back is what gave one file two fingerprints and let a second copy
  /// slip past the duplicate check.
  bool _timesUntouched = true;

  /// True from the moment "Import all" is tapped until its answer arrives:
  /// the stage is `submitting`, but there is no single track under review.
  bool _importingAll = false;

  List<String>? _serverErrors;
  String? _genericError;

  static const _months = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
  ];

  static String _fmtDate(DateTime d) =>
      '${_months[d.month - 1]} ${d.day}, ${d.year}';
  static String _toIso(DateTime d) =>
      '${d.year}-${d.month.toString().padLeft(2, '0')}-'
      '${d.day.toString().padLeft(2, '0')}';
  static String _fmtTime(TimeOfDay t) =>
      '${t.hour.toString().padLeft(2, '0')}:'
      '${t.minute.toString().padLeft(2, '0')}';

  static String _fmtDuration(int seconds) {
    final hours = seconds ~/ 3600;
    final minutes = (seconds % 3600) ~/ 60;
    return hours > 0
        ? '${hours}h${minutes.toString().padLeft(2, '0')}'
        : '${minutes}m';
  }

  @override
  void initState() {
    super.initState();
    final initial = widget.initialFile;
    if (initial != null) {
      _fileBytes = initial.bytes;
      _fileName = initial.name;
      _stage = _Stage.reading;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _inspect();
      });
    }
  }

  @override
  void dispose() {
    _nameController.dispose();
    super.dispose();
  }

  DateTime? get _tripStart => DateTime.tryParse(widget.tripStart ?? '');
  DateTime? get _tripEnd => DateTime.tryParse(widget.tripEnd ?? '');

  /// True when the chosen date sits outside the trip. Not an error — importing
  /// a ride from the day before a trip is legitimate — but worth saying,
  /// because it stretches the trip and the user may simply have mistyped.
  bool get _outsideTrip {
    final date = _date;
    final start = _tripStart;
    final end = _tripEnd;
    if (date == null || start == null || end == null) return false;
    final day = DateTime(date.year, date.month, date.day);
    return day.isBefore(DateTime(start.year, start.month, start.day)) ||
        day.isAfter(DateTime(end.year, end.month, end.day));
  }

  Future<void> _pickFile() async {
    // FileType.any, not FileType.custom: custom greys out files whose UTI iOS
    // does not recognise, and .gpx arriving from Mail or iCloud commonly is
    // one. The server is the only thing that can really judge a GPX anyway.
    final picked = await FilePicker.pickFile(type: FileType.any);
    if (picked == null) return;
    final bytes = await picked.readAsBytesOrNull();
    if (bytes == null) return;
    setState(() {
      _fileBytes = bytes;
      _fileName = picked.name;
      _serverErrors = null;
      _genericError = null;
    });
    await _inspect();
  }

  Future<void> _inspect() async {
    setState(() => _stage = _Stage.reading);
    final response = await _send('/activities/gpx/inspect', const {});
    if (response == null || !mounted) return;

    if (response.statusCode != 200) {
      setState(() {
        _stage = _Stage.pick;
        _applyErrorBody(response);
      });
      return;
    }

    final inspection = GpxInspection.fromJson(
        jsonDecode(response.body) as Map<String, dynamic>);
    if (inspection.candidates.isEmpty) {
      setState(() {
        _stage = _Stage.pick;
        _serverErrors = inspection.errors.isEmpty
            ? const ['The file was rejected.']
            : inspection.errors;
      });
      return;
    }

    final importable =
        inspection.candidates.where((c) => c.isImportable).toList();
    if (importable.isEmpty) {
      // Every track in the file is unusable. Opening the review step on one
      // of them showed a form that could never be submitted and never said
      // why; the reasons belong where the file was picked.
      setState(() {
        _stage = _Stage.pick;
        _serverErrors = [
          for (final candidate in inspection.candidates) ...candidate.errors,
        ];
      });
      return;
    }

    // A connecting segment an export wrote is not something to pick between:
    // a file of one activity and its connections opens that activity.
    final tracks = inspection.importAllCandidates;
    setState(() {
      _inspection = inspection;
      if (tracks.length > 1 || (tracks.isEmpty && importable.length > 1)) {
        _stage = _Stage.choose;
      } else {
        _choose(tracks.isNotEmpty ? tracks.first : importable.first);
      }
    });
  }

  /// Move to the review step with every field prefilled from the file.
  void _choose(GpxCandidate candidate) {
    _chosen = candidate;
    _stage = _Stage.review;
    // Every field is derived below, so nothing may survive from a candidate
    // reviewed before this one — including a rejection the server gave for
    // it, which would otherwise sit under a different track's form.
    _serverErrors = null;
    _genericError = null;
    _nameController.text = candidate.name ?? _inspection?.suggestedName ?? '';
    _activityType = candidate.suggestedType;
    // The wall clock where the track was recorded (issue #365), read as the
    // clock face it is. A server that sends none leaves the UTC instants, as
    // before.
    final started = candidate.startLocal ?? candidate.startedAt;
    if (started != null) {
      _date = started;
      _startTime = TimeOfDay.fromDateTime(started);
      final ended = candidate.endLocal ?? candidate.endedAt;
      _endTime = ended != null ? TimeOfDay.fromDateTime(ended) : null;
      _timesUntouched = true;
    } else {
      // A planned route has no clock, so these have to be asked for. Leaving
      // them empty and marked required is the honest version of that.
      _date = null;
      _startTime = null;
      _endTime = null;
      _timesUntouched = false;
    }
  }

  bool get _timesComplete =>
      _date != null && _startTime != null && _endTime != null;

  /// True when the end time lands before the start, which means the
  /// activity ran past midnight. Said out loud rather than left to be
  /// discovered, since the date field shows only the starting day.
  bool get _crossesMidnight {
    final start = _startTime;
    final end = _endTime;
    if (start == null || end == null) return false;
    return end.hour * 60 + end.minute < start.hour * 60 + start.minute;
  }

  /// True unless the two times are the same instant.
  ///
  /// An end BEFORE the start is not an error: a night ride leaving at 23:30
  /// and returning at 00:30 is the ordinary shape of a night ride, and
  /// comparing minutes-past-midnight refused it outright — including when
  /// the times came from the file and the user had touched nothing. The
  /// server rolls such an end onto the next day; only a zero-length
  /// activity is genuinely wrong.
  bool get _timeOrderValid {
    final start = _startTime;
    final end = _endTime;
    if (start == null || end == null) return true;
    return end != start;
  }

  bool get _canSubmit =>
      (_stage == _Stage.review) &&
      (_chosen?.isImportable ?? false) &&
      _timesComplete &&
      _timeOrderValid &&
      _activityType != null &&
      _nameController.text.trim().isNotEmpty;

  Future<http.Response?> _send(String path, Map<String, String> fields) async {
    final bytes = _fileBytes;
    if (bytes == null) return null;
    try {
      final token = api.tokenForUpload;
      final request = http.MultipartRequest(
          'POST', Uri.parse('${api.baseUrl}${widget.projectRef.path(path)}'))
        ..fields.addAll(fields)
        ..files.add(http.MultipartFile.fromBytes('file', bytes,
            filename: _fileName ?? 'track.gpx'));
      if (token != null) request.headers['Authorization'] = 'Bearer $token';
      final streamed = await _client.send(request);
      return await http.Response.fromStream(streamed);
    } catch (e) {
      if (mounted) {
        setState(() {
          // Back to a stage the user can act from. Staying in `reading` or
          // `submitting` left a spinner turning with Cancel disabled and no
          // way out of the dialog at all.
          _stage = _importingAll
              ? _Stage.choose
              : _chosen == null ? _Stage.pick : _Stage.review;
          _importingAll = false;
          _genericError = 'Could not reach the server: '
              '${e.toString().replaceFirst('Exception: ', '')}';
        });
      }
      return null;
    }
  }

  void _applyErrorBody(http.Response response) {
    if (response.statusCode == 403) {
      _genericError =
          "You don't have permission to add activities to this trip.";
      return;
    }
    try {
      final detail = (jsonDecode(response.body) as Map)['detail'];
      final errors = (detail is Map ? detail['errors'] : null) as List?;
      if (errors != null && errors.isNotEmpty) {
        _serverErrors = errors.map((e) => e.toString()).toList();
        return;
      }
      // A plan limit (402) and a refused "Import all" (400) answer with the
      // message itself.
      if (detail is String && detail.isNotEmpty) {
        _serverErrors = [detail];
        return;
      }
    } on Object {
      // fall through to the generic message
    }
    _genericError = 'The file could not be read (${response.statusCode}).';
  }

  Future<void> _submit() async {
    if (!_canSubmit) return;
    setState(() {
      _stage = _Stage.submitting;
      _serverErrors = null;
      _genericError = null;
    });

    final fields = <String, String>{
      'activity_name': _nameController.text.trim(),
      'activity_type': _activityType!,
      'track_index': '${_chosen!.index}',
    };
    // Only send times the user actually set. Sending the file's own back,
    // truncated to the minute, changes the fingerprint the server dedupes on
    // and a re-import would not be recognised.
    if (!_timesUntouched) {
      fields['date'] = _toIso(_date!);
      fields['start_time'] = _fmtTime(_startTime!);
      fields['end_time'] = _fmtTime(_endTime!);
      // They are wall-clock times in the track's zone. Not claimed for a
      // stamped track the server gave no local time for: its fields hold UTC.
      final chosen = _chosen!;
      if (chosen.startLocal != null || !chosen.hasTimes) {
        fields['times_local'] = 'true';
      }
    }

    final response = await _send('/activities/import-gpx', fields);
    if (response == null || !mounted) return;

    if (response.statusCode == 200) {
      final id = (jsonDecode(response.body) as Map)['activity_id'];
      Navigator.of(context).pop(GpxImportResult(
          activityId: (id as num).toInt(),
          name: _nameController.text.trim()));
      return;
    }
    setState(() {
      _stage = _Stage.review;
      _applyErrorBody(response);
    });
  }

  /// Import every track of the file at once. Takes the file and nothing else:
  /// names, types and times are the file's own.
  Future<void> _submitAll() async {
    setState(() {
      _stage = _Stage.submitting;
      _importingAll = true;
      _serverErrors = null;
      _genericError = null;
    });

    final response = await _send('/activities/import-gpx-tracks', const {});
    if (response == null || !mounted) return;

    if (response.statusCode == 200) {
      final body = jsonDecode(response.body) as Map;
      final imported = (body['imported'] as List?) ?? const [];
      final skipped = (body['skipped'] as List?) ?? const [];
      Navigator.of(context).pop(GpxImportResult.all(
        activityIds: [
          for (final item in imported)
            if (item is Map && item['activity_id'] is num)
              (item['activity_id'] as num).toInt(),
        ],
        skipped: skipped.length,
      ));
      return;
    }
    setState(() {
      _stage = _Stage.choose;
      _importingAll = false;
      _applyErrorBody(response);
    });
  }

  Future<void> _pickDate() async {
    final tripStart = _tripStart;
    final tripEnd = _tripEnd;
    final picked = await showDatePicker(
      context: context,
      useRootNavigator: true,
      // Aimed at the trip, not at today: a trip edited in February may well be
      // last summer's, and "today" is then never the answer.
      initialDate: _date ?? tripStart ?? DateTime.now(),
      firstDate: DateTime(2000),
      lastDate: DateTime(2100),
      helpText: tripStart != null && tripEnd != null
          ? 'Trip runs ${_fmtDate(tripStart)} – ${_fmtDate(tripEnd)}'
          : null,
    );
    if (picked != null) {
      setState(() {
        _date = picked;
        _timesUntouched = false;
      });
    }
  }

  Future<void> _pickTime({required bool isStart}) async {
    final current = isStart ? _startTime : _endTime;
    final picked = await showTimePicker(
      context: context,
      useRootNavigator: true,
      initialTime: current ?? const TimeOfDay(hour: 9, minute: 0),
    );
    if (picked == null) return;
    setState(() {
      if (isStart) {
        _startTime = picked;
      } else {
        _endTime = picked;
      }
      _timesUntouched = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    // A fixed 420 clipped the dialog on a narrow phone.
    final width = MediaQuery.sizeOf(context).width;
    return AlertDialog(
      title: Text(switch (_stage) {
        _Stage.choose => 'Which track?',
        _Stage.submitting when _importingAll => 'Importing tracks',
        _Stage.review || _Stage.submitting => 'Import this track?',
        _ => 'Import a GPX file',
      }),
      content: SizedBox(
        width: width < 460 ? width - 80 : 420,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              ...switch (_stage) {
                _Stage.pick => _pickStep(theme),
                _Stage.reading => _readingStep(theme),
                _Stage.choose => _chooseStep(theme),
                _Stage.submitting when _importingAll => _importingAllStep(theme),
                _Stage.review || _Stage.submitting => _reviewStep(theme),
              },
              if (_serverErrors != null) _errorBox(theme, _serverErrors!),
              if (_genericError != null) ...[
                const SizedBox(height: 12),
                Text(_genericError!,
                    style: TextStyle(color: theme.colorScheme.error)),
              ],
            ],
          ),
        ),
      ),
      actions: _actions(),
    );
  }

  List<Widget> _actions() {
    final busy = _stage == _Stage.reading || _stage == _Stage.submitting;
    return [
      TextButton(
        onPressed: busy ? null : () => Navigator.of(context).pop(),
        child: const Text('Cancel'),
      ),
      if (_stage == _Stage.review ||
          (_stage == _Stage.submitting && !_importingAll))
        ElevatedButton(
          key: const ValueKey('gpx_import_confirm'),
          style: ElevatedButton.styleFrom(minimumSize: const Size(80, 44)),
          onPressed: _canSubmit && !busy ? _submit : null,
          child: _stage == _Stage.submitting
              ? const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('Import'),
        ),
    ];
  }

  List<Widget> _pickStep(ThemeData theme) => [
        InkWell(
          key: const ValueKey('gpx_pick_file'),
          borderRadius: BorderRadius.circular(10),
          onTap: _pickFile,
          child: Container(
            width: double.infinity,
            padding: const EdgeInsets.symmetric(vertical: 26, horizontal: 16),
            decoration: BoxDecoration(
              border: Border.all(color: theme.colorScheme.outlineVariant),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Column(
              children: [
                Icon(Icons.upload_file, color: theme.colorScheme.primary),
                const SizedBox(height: 8),
                Text('Choose a .gpx file', style: theme.textTheme.titleSmall),
                const SizedBox(height: 4),
                Text('From Komoot, Garmin, RideWithGPS, Strava…',
                    textAlign: TextAlign.center,
                    style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant)),
              ],
            ),
          ),
        ),
      ];

  List<Widget> _readingStep(ThemeData theme) => [
        Row(
          children: [
            const SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(strokeWidth: 2)),
            const SizedBox(width: 12),
            Expanded(
                child: Text('Reading ${_fileName ?? 'the file'}…',
                    style: theme.textTheme.bodyMedium)),
          ],
        ),
      ];

  List<Widget> _importingAllStep(ThemeData theme) => [
        Row(
          children: [
            const SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(strokeWidth: 2)),
            const SizedBox(width: 12),
            Expanded(
                child: Text(
                    'Importing ${_inspection!.importAllCandidates.length} '
                    'tracks…',
                    style: theme.textTheme.bodyMedium)),
          ],
        ),
      ];

  List<Widget> _chooseStep(ThemeData theme) {
    final all = _inspection!.importAllCandidates;
    return [
        Text(
            'This file holds ${_inspection!.candidates.length} tracks. '
            'Pick the one to import.',
            style: theme.textTheme.bodyMedium),
        if (all.length > 1) ...[
          const SizedBox(height: 8),
          OutlinedButton.icon(
            key: const ValueKey('gpx_import_all'),
            icon: const Icon(Icons.library_add_outlined, size: 18),
            label: Text('Import all ${all.length} tracks'),
            onPressed: _submitAll,
          ),
        ],
        const SizedBox(height: 8),
        for (final candidate in _inspection!.candidates)
          ListTile(
            key: ValueKey('gpx_candidate_${candidate.index}'),
            dense: true,
            enabled: candidate.isImportable && !candidate.isConnection,
            leading: candidate.outline.isEmpty
                ? const Icon(Icons.route)
                : SizedBox(
                    width: 44,
                    height: 32,
                    child: CustomPaint(
                        painter: TrackOutlinePainter(
                            candidate.outline, theme.colorScheme.primary))),
            title: Text(candidate.name ?? 'Track ${candidate.index + 1}'),
            subtitle: Text(candidate.isConnection
                ? 'Connecting segment, not an activity'
                : candidate.isImportable
                    ? _factsLine(candidate)
                    : candidate.errors.first),
            onTap: candidate.isImportable && !candidate.isConnection
                ? () => setState(() => _choose(candidate))
                : null,
          ),
      ];
  }

  String _factsLine(GpxCandidate candidate) {
    final parts = <String>[
      '${(candidate.distanceM / 1000).toStringAsFixed(1)} km'
    ];
    if (candidate.elevationGainM != null) {
      parts.add('${candidate.elevationGainM!.round()} m ↑');
    }
    final moving = candidate.movingSeconds;
    if (moving != null && moving > 0) parts.add(_fmtDuration(moving));
    parts.add('${candidate.pointCount} pts');
    return parts.join('  ·  ');
  }

  /// The built-in types, plus the one the server suggested when it is none of
  /// them (a kayak is "Kayaking"): a dropdown whose value is not among its
  /// items throws, and an empty field would hide what the file said.
  List<(String, String)> _typeChoices() {
    final current = _activityType;
    return [
      ..._kActivityTypes,
      if (current != null && !_kActivityTypes.any((t) => t.$1 == current))
        (current, current),
    ];
  }

  List<Widget> _reviewStep(ThemeData theme) {
    final candidate = _chosen!;
    final duplicate = _inspection?.duplicateOf;
    final dark = theme.brightness == Brightness.dark;
    return [
      if (candidate.outline.isNotEmpty)
        Container(
          height: 96,
          width: double.infinity,
          decoration: BoxDecoration(
            color: theme.colorScheme.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(8),
          ),
          child: CustomPaint(
            painter: TrackOutlinePainter(
                candidate.outline, theme.colorScheme.primary),
          ),
        ),
      const SizedBox(height: 10),
      Text(_factsLine(candidate),
          style: theme.textTheme.bodySmall
              ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
      if (candidate.elevationGainM != null) ...[
        const SizedBox(height: 2),
        Text('Climb is estimated from the file’s elevation.',
            key: const ValueKey('gpx_climb_estimated'),
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
      ],
      if (duplicate != null) ...[
        const SizedBox(height: 10),
        _notice(theme, Icons.info_outline, kWarning,
            'This trip already has “${duplicate.name}” from the same track.',
            key: const ValueKey('gpx_duplicate_notice')),
      ],
      if (candidate.isRoute) ...[
        const SizedBox(height: 10),
        _notice(theme, Icons.event_outlined, theme.colorScheme.primary,
            'A planned route, so it carries no date or times — please set them.',
            key: const ValueKey('gpx_route_notice')),
      ],
      const SizedBox(height: 14),
      TextField(
        key: const ValueKey('gpx_name_field'),
        controller: _nameController,
        decoration: const InputDecoration(
            labelText: 'Name', isDense: true, border: OutlineInputBorder()),
        onChanged: (_) => setState(() {}),
      ),
      const SizedBox(height: 12),
      DropdownButtonFormField<String>(
        key: const ValueKey('gpx_type_field'),
        initialValue: _activityType,
        decoration: InputDecoration(
          labelText: 'Activity type',
          isDense: true,
          border: const OutlineInputBorder(),
          // Unrecognised <type> arrives null, and an Import button sitting
          // dead with no explanation is its own small cruelty.
          errorText: _activityType == null
              ? "The file doesn't say — please pick one"
              : null,
        ),
        items: [
          for (final (value, label) in _typeChoices())
            DropdownMenuItem(
              value: value,
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(_typeIcon(value),
                      size: 16,
                      color: iconBoxFg(
                          resolveTypeStyle(activityTypeBucket(value),
                                  isSegment: false)
                              .color,
                          dark: dark)),
                  const SizedBox(width: 8),
                  Text(label),
                ],
              ),
            ),
        ],
        onChanged: (v) => setState(() => _activityType = v),
      ),
      const SizedBox(height: 12),
      _field(theme, 'Date', _date == null ? null : _fmtDate(_date!),
          Icons.calendar_today, _pickDate,
          key: const ValueKey('gpx_date_field')),
      const SizedBox(height: 8),
      Row(
        children: [
          Expanded(
            child: _field(
                theme,
                'Start',
                _startTime == null ? null : _fmtTime(_startTime!),
                Icons.access_time,
                () => _pickTime(isStart: true),
                key: const ValueKey('gpx_start_field')),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: _field(
                theme,
                'End',
                _endTime == null ? null : _fmtTime(_endTime!),
                Icons.access_time,
                () => _pickTime(isStart: false),
                key: const ValueKey('gpx_end_field')),
          ),
        ],
      ),
      if (!_timeOrderValid) ...[
        const SizedBox(height: 6),
        Text('Start and end cannot be the same time.',
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.error)),
      ],
      if (candidate.timezone != null) ...[
        const SizedBox(height: 6),
        Text('Local time in ${candidate.timezone}',
            key: const ValueKey('gpx_timezone_note'),
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
      ],
      if (_crossesMidnight) ...[
        const SizedBox(height: 6),
        Text('Ends the next day.',
            key: const ValueKey('gpx_crosses_midnight_note'),
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
      ],
      if (candidate.hasTimes && _timesUntouched) ...[
        const SizedBox(height: 6),
        Text('Read from the file — edit any of them.',
            key: const ValueKey('gpx_from_file_note'),
            style: theme.textTheme.bodySmall?.copyWith(color: kSuccess)),
      ],
      for (final warning in candidate.warnings) ...[
        const SizedBox(height: 10),
        _notice(theme, Icons.warning_amber_outlined, kWarning,
            warning,
            key: const ValueKey('gpx_candidate_warning')),
      ],
      if (_outsideTrip) ...[
        const SizedBox(height: 10),
        _notice(theme, Icons.warning_amber_outlined, kWarning,
            'That date is outside this trip, which will extend it.',
            key: const ValueKey('gpx_outside_trip_notice')),
      ],
    ];
  }

  Widget _field(ThemeData theme, String label, String? value, IconData icon,
      VoidCallback onTap,
      {Key? key}) {
    return InkWell(
      key: key,
      borderRadius: BorderRadius.circular(8),
      onTap: _stage == _Stage.submitting ? null : onTap,
      child: InputDecorator(
        decoration: InputDecoration(
          labelText: label,
          isDense: true,
          border: const OutlineInputBorder(),
          errorText: value == null ? 'Required' : null,
        ),
        child: Row(
          children: [
            Icon(icon, size: 16, color: theme.colorScheme.onSurfaceVariant),
            const SizedBox(width: 8),
            Expanded(
                child: Text(value ?? '—',
                    overflow: TextOverflow.ellipsis,
                    style: theme.textTheme.bodyMedium)),
          ],
        ),
      ),
    );
  }

  Widget _notice(ThemeData theme, IconData icon, Color colour, String message,
          {Key? key}) =>
      Container(
        key: key,
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
        decoration: BoxDecoration(
          color: colour.withValues(alpha: 0.12),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(icon, size: 16, color: colour),
            const SizedBox(width: 8),
            Expanded(child: Text(message, style: theme.textTheme.bodySmall)),
          ],
        ),
      );

  Widget _errorBox(ThemeData theme, List<String> errors) => Padding(
        padding: const EdgeInsets.only(top: 12),
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: theme.colorScheme.errorContainer,
            borderRadius: BorderRadius.circular(8),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final message in errors)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 2),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(Icons.error_outline,
                          size: 16, color: theme.colorScheme.onErrorContainer),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(message,
                            style: theme.textTheme.bodySmall?.copyWith(
                                color: theme.colorScheme.onErrorContainer)),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      );
}

/// Draws a track outline scaled to fit, with its start and end marked.
///
/// A thumbnail, not a map: no tiles, no projection library, no interaction. Its
/// only job is to make "wrong file" obvious while that still costs nothing.
class TrackOutlinePainter extends CustomPainter {
  TrackOutlinePainter(this.points, this.colour);

  final List<GeoPoint> points;
  final Color colour;

  static const _pad = 8.0;

  @override
  void paint(Canvas canvas, Size size) {
    if (points.length < 2) return;

    var minLat = points.first.lat, maxLat = points.first.lat;
    var minLon = points.first.lon, maxLon = points.first.lon;
    for (final p in points) {
      minLat = math.min(minLat, p.lat);
      maxLat = math.max(maxLat, p.lat);
      minLon = math.min(minLon, p.lon);
      maxLon = math.max(maxLon, p.lon);
    }

    // Longitude degrees are narrower than latitude ones by cos(lat); ignoring
    // that makes a north-south track look wider than it is. One factor for the
    // whole thumbnail is plenty — it spans a ride, not a hemisphere.
    final squeeze = math.cos((minLat + maxLat) / 2 * math.pi / 180).abs();
    final spanLat = maxLat - minLat;
    final spanLon = (maxLon - minLon) * squeeze;

    final usableWidth = size.width - 2 * _pad;
    final usableHeight = size.height - 2 * _pad;
    if (usableWidth <= 0 || usableHeight <= 0) return;
    if (spanLat <= 0 && spanLon <= 0) return;    // a single place, not a track

    final scale = math.min(
      spanLon > 0 ? usableWidth / spanLon : double.infinity,
      spanLat > 0 ? usableHeight / spanLat : double.infinity,
    );
    final offsetX = _pad + (usableWidth - spanLon * scale) / 2;
    final offsetY = _pad + (usableHeight - spanLat * scale) / 2;

    Offset project(GeoPoint p) => Offset(
          offsetX + (p.lon - minLon) * squeeze * scale,
          offsetY + (maxLat - p.lat) * scale,
        );

    final path = Path();
    final first = project(points.first);
    path.moveTo(first.dx, first.dy);
    for (final p in points.skip(1)) {
      final o = project(p);
      path.lineTo(o.dx, o.dy);
    }
    canvas.drawPath(
      path,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.2
        ..strokeJoin = StrokeJoin.round
        ..strokeCap = StrokeCap.round
        ..color = colour,
    );
    canvas.drawCircle(first, 3, Paint()..color = kSuccess);
    canvas.drawCircle(project(points.last), 3, Paint()..color = kAccent);
  }

  @override
  bool shouldRepaint(TrackOutlinePainter old) =>
      old.points != points || old.colour != colour;
}
