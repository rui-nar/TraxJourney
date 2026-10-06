/// Dialog to create or edit an encounter (issue #40): pick/create a person or
/// group (issue #56), a day, an optional place (defaults to the day's
/// location) and a note.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/current_location.dart';
import '../core/design_tokens.dart';
import 'group_form_dialog.dart';
import 'location_picker_dialog.dart';
import 'note_field_actions.dart';
import 'people_search.dart';
import 'person_form_dialog.dart';
import 'project_notifier.dart';

bool _isAsciiLetter(String ch) => RegExp(r'^[A-Za-z]$').hasMatch(ch);

/// Sentence-cases [input]: uppercases the first letter of the whole string,
/// and the first letter found after each '.' followed by whitespace (issue
/// #77). Pure and idempotent — safe to reapply on every keystroke/paste, and
/// never touches letters that aren't at a sentence start.
String sentenceCase(String input) {
  if (input.isEmpty) return input;
  final buffer = StringBuffer();
  bool capitalizeNext = true;
  bool afterPeriod = false;
  for (int i = 0; i < input.length; i++) {
    final ch = input.substring(i, i + 1);
    if (afterPeriod) {
      if (ch.trim().isEmpty) {
        // Whitespace run right after the period — keep waiting for the
        // first non-whitespace character to capitalize.
        buffer.write(ch);
        capitalizeNext = true;
        continue;
      }
      afterPeriod = false;
    }
    if (capitalizeNext && _isAsciiLetter(ch)) {
      buffer.write(ch.toUpperCase());
      capitalizeNext = false;
    } else {
      buffer.write(ch);
    }
    if (ch == '.') afterPeriod = true;
  }
  return buffer.toString();
}

/// Applies [sentenceCase] to the field's text on every edit. Case-only
/// changes never alter string length, so the existing selection stays valid.
class _SentenceCaseFormatter extends TextInputFormatter {
  const _SentenceCaseFormatter();

  @override
  TextEditingValue formatEditUpdate(
    TextEditingValue oldValue,
    TextEditingValue newValue,
  ) {
    return newValue.copyWith(
      text: sentenceCase(newValue.text),
      selection: newValue.selection,
    );
  }
}

class EncounterDialog extends StatefulWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic>? editEntry; // the encounter map from items
  final String? initialDate;
  final int? initialPersonId;
  final int? insertAfterIndex;

  const EncounterDialog({
    super.key,
    required this.notifier,
    this.editEntry,
    this.initialDate,
    this.initialPersonId,
    this.insertAfterIndex,
  });

  @override
  State<EncounterDialog> createState() => _EncounterDialogState();
}

class _EncounterDialogState extends State<EncounterDialog> {
  late TextEditingController _descCtrl;
  int? _personId;
  int? _groupId;
  DateTime? _date;
  TimeOfDay? _time;
  double? _lat;
  double? _lon;
  bool _saving = false;
  String? _saveError;
  bool _locating = false;
  // True while the pin is still a default (day location / device GPS) rather
  // than something the user picked on the map — controls whether changing
  // the date is allowed to move the pin.
  bool _locationAutoSet = true;

  static const _months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  static String _fmtDate(DateTime d) => '${_months[d.month - 1]} ${d.day}, ${d.year}';
  static String _toIso(DateTime d) =>
      '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';
  static String _fmtTime(TimeOfDay t) =>
      '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';

  @override
  void initState() {
    super.initState();
    final e = widget.editEntry;
    _descCtrl = TextEditingController(text: e?['description'] as String? ?? '');
    _groupId = (e?['group_id'] as num?)?.toInt();
    _personId = _groupId == null
        ? (e?['person_id'] as num?)?.toInt() ?? widget.initialPersonId
        : null;

    final ds = (e?['date'] as String?) ?? widget.initialDate;
    if (ds != null) _date = DateTime.tryParse(ds);

    if (e != null) {
      final ts = e['time'] as String?;
      if (ts != null) {
        final parts = ts.split(':');
        if (parts.length == 2) {
          _time = TimeOfDay(
            hour: int.tryParse(parts[0]) ?? 0,
            minute: int.tryParse(parts[1]) ?? 0,
          );
        }
      }
      _lat = (e['lat'] as num?)?.toDouble();
      _lon = (e['lon'] as num?)?.toDouble();
      _locationAutoSet = false;
    } else {
      // New encounter: default the time to now, and default the location to
      // where the trip was on the selected day; fall back to the device's
      // current position if that day has no GPS data (the user can still
      // adjust it either way).
      _time = TimeOfDay.now();
      final dayLoc = _date != null ? _dayLatLng(_toIso(_date!)) : null;
      if (dayLoc != null) {
        _lat = dayLoc.lat;
        _lon = dayLoc.lon;
      } else {
        _fetchDeviceLocation();
      }
    }
  }

  /// Where the trip was on [dateIso], derived from that day's activities —
  /// mirrors the server's `_resolve_geo` start-of-day resolution
  /// (`api/encounters.py`) so the map picker zooms to the same place.
  ({double lat, double lon})? _dayLatLng(String dateIso) {
    final dayActs = widget.notifier.activities.where((a) {
      final ds = (a['start_date_local'] as String?)?.split('T').first;
      return ds == dateIso;
    }).toList()
      ..sort((a, b) => ((a['start_date_local'] as String?) ?? '')
          .compareTo((b['start_date_local'] as String?) ?? ''));
    if (dayActs.isEmpty) return null;
    final ll = dayActs.first['start_latlng'];
    if (ll is List && ll.length >= 2) {
      return (lat: (ll[0] as num).toDouble(), lon: (ll[1] as num).toDouble());
    }
    return null;
  }

  void _onDateChanged(DateTime picked) {
    setState(() {
      _date = picked;
      if (_locationAutoSet) {
        final dayLoc = _dayLatLng(_toIso(picked));
        if (dayLoc != null) {
          _lat = dayLoc.lat;
          _lon = dayLoc.lon;
        }
      }
    });
  }

  @override
  void dispose() {
    _descCtrl.dispose();
    super.dispose();
  }

  Future<void> _fetchDeviceLocation() async {
    setState(() => _locating = true);
    final here = await currentDeviceLatLng();
    if (!mounted) return;
    setState(() {
      if (here != null && _lat == null && _lon == null) {
        _lat = here.latitude;
        _lon = here.longitude;
      }
      _locating = false;
    });
  }

  Future<void> _createPersonInline() async {
    final newId = await showPersonFormDialog(context, widget.notifier);
    if (newId != null && mounted) {
      setState(() {
        _personId = newId;
        _groupId = null;
      });
    }
  }

  Future<void> _createGroupInline() async {
    final newId = await showGroupFormDialog(context, widget.notifier);
    if (newId != null && mounted) {
      setState(() {
        _groupId = newId;
        _personId = null;
      });
    }
  }

  Future<void> _pickLocation() async {
    final result = await showDialog<LatLonResult>(
      context: context,
      useRootNavigator: true,
      builder: (_) => LocationPickerDialog(
        title: 'Pick encounter location',
        initialLat: _lat,
        initialLon: _lon,
        geo: widget.notifier.geoFacet.geo,
      ),
    );
    if (result != null && mounted) {
      setState(() {
        _lat = result.lat;
        _lon = result.lon;
        _locationAutoSet = false;
      });
    }
  }

  Future<void> _save() async {
    if (_personId == null && _groupId == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Pick a person or group')),
      );
      return;
    }
    if (_date == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('A date is required')),
      );
      return;
    }
    setState(() {
      _saveError = null;
      _saving = true;
    });
    final navigator = Navigator.of(context);
    final dateStr = _toIso(_date!);
    final timeStr = _time != null ? _fmtTime(_time!) : null;
    final desc = _descCtrl.text.trim().isEmpty ? null : _descCtrl.text.trim();

    try {
      final e = widget.editEntry;
      final ok = e != null
          ? await widget.notifier.updateEncounter(
              e['id'].toString(),
              personId: _personId,
              groupId: _groupId,
              date: dateStr,
              geoMode: 'custom',
              time: timeStr,
              description: desc,
              lat: _lat,
              lon: _lon,
            )
          : await widget.notifier.createEncounter(
              personId: _personId,
              groupId: _groupId,
              date: dateStr,
              geoMode: 'custom',
              time: timeStr,
              description: desc,
              lat: _lat,
              lon: _lon,
              insertAfterIndex: widget.insertAfterIndex,
            );
      if (!ok) {
        setState(() =>
            _saveError = widget.notifier.error ?? 'Failed to save encounter');
        return;
      }
      if (mounted) navigator.pop();
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isEdit = widget.editEntry != null;
    final people = widget.notifier.people;
    final groups = widget.notifier.groups;

    // (kind, id) so a person id and a group id never collide as raw ints.
    final selected = _groupId != null
        ? ('group', _groupId!)
        : _personId != null
            ? ('person', _personId!)
            : null;
    final knownSelection = selected != null &&
        (selected.$1 == 'group'
            ? groups.any((g) => g['id'] == selected.$2)
            : people.any((p) => p['id'] == selected.$2));

    return AlertDialog(
      title: Text(isEdit ? 'Edit encounter' : 'Add encounter'),
      content: SizedBox(
        width: 460,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Person/group picker + inline create (issue #56)
              Text('Person or group *', style: theme.textTheme.labelMedium),
              const SizedBox(height: 4),
              Row(
                children: [
                  Expanded(
                    child: DropdownButtonFormField<(String, int)>(
                      key: const Key('encounter-person-group-picker'),
                      initialValue: knownSelection ? selected : null,
                      isExpanded: true,
                      decoration: const InputDecoration(
                        isDense: true,
                        border: OutlineInputBorder(),
                        hintText: 'Select a person or group',
                      ),
                      items: [
                        if (groups.isNotEmpty) ...[
                          DropdownMenuItem(
                            enabled: false,
                            value: ('header', -1),
                            child: Text('GROUPS',
                                style: theme.textTheme.labelSmall),
                          ),
                          for (final g in groups)
                            DropdownMenuItem(
                              value: ('group', (g['id'] as num).toInt()),
                              child: Row(
                                children: [
                                  const Icon(Icons.groups, size: 16),
                                  const SizedBox(width: 8),
                                  Expanded(
                                    child: Text(groupDisplayName(g),
                                        overflow: TextOverflow.ellipsis),
                                  ),
                                ],
                              ),
                            ),
                        ],
                        if (people.isNotEmpty) ...[
                          DropdownMenuItem(
                            enabled: false,
                            value: ('header', -2),
                            child: Text('PEOPLE',
                                style: theme.textTheme.labelSmall),
                          ),
                          for (final p in people)
                            DropdownMenuItem(
                              value: ('person', (p['id'] as num).toInt()),
                              child: Row(
                                children: [
                                  const Icon(Icons.person, size: 16),
                                  const SizedBox(width: 8),
                                  Expanded(
                                    child: Text(personDisplayName(p),
                                        overflow: TextOverflow.ellipsis),
                                  ),
                                ],
                              ),
                            ),
                        ],
                      ],
                      onChanged: (v) {
                        if (v == null) return;
                        setState(() {
                          _personId = v.$1 == 'person' ? v.$2 : null;
                          _groupId = v.$1 == 'group' ? v.$2 : null;
                        });
                      },
                    ),
                  ),
                  PopupMenuButton<void>(
                    tooltip: 'New person or group',
                    icon: const Icon(Icons.add_circle_outline),
                    iconColor: kAccent,
                    itemBuilder: (_) => [
                      PopupMenuItem(
                        onTap: _createPersonInline,
                        child: const ListTile(
                          leading: Icon(Icons.person_add_alt_1),
                          title: Text('New person'),
                          contentPadding: EdgeInsets.zero,
                        ),
                      ),
                      PopupMenuItem(
                        onTap: _createGroupInline,
                        child: const ListTile(
                          leading: Icon(Icons.group_add),
                          title: Text('New group'),
                          contentPadding: EdgeInsets.zero,
                        ),
                      ),
                    ],
                  ),
                ],
              ),
              const SizedBox(height: 12),
              // Date
              Text('Date *', style: theme.textTheme.labelMedium),
              InkWell(
                borderRadius: BorderRadius.circular(8),
                onTap: () async {
                  final picked = await showDatePicker(
                    context: context,
                    useRootNavigator: true,
                    initialDate: _date ?? DateTime.now(),
                    firstDate: DateTime(2000),
                    lastDate: DateTime(2100),
                  );
                  if (picked != null) _onDateChanged(picked);
                },
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Row(
                    children: [
                      Icon(Icons.calendar_today, size: 18,
                          color: theme.colorScheme.onSurfaceVariant),
                      const SizedBox(width: 12),
                      Text(_date == null ? 'Select date…' : _fmtDate(_date!),
                          style: theme.textTheme.bodyMedium?.copyWith(
                            color: _date == null ? theme.colorScheme.error : null,
                          )),
                    ],
                  ),
                ),
              ),
              // Time
              InkWell(
                borderRadius: BorderRadius.circular(8),
                onTap: () async {
                  final picked = await showTimePicker(
                    context: context,
                    useRootNavigator: true,
                    initialTime: _time ?? TimeOfDay.now(),
                  );
                  if (picked != null) setState(() => _time = picked);
                },
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 6),
                  child: Row(
                    children: [
                      Icon(Icons.access_time, size: 18,
                          color: theme.colorScheme.onSurfaceVariant),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Text(_time == null ? 'Time (optional)' : _fmtTime(_time!),
                            style: theme.textTheme.bodyMedium?.copyWith(
                              color: _time == null
                                  ? theme.colorScheme.onSurfaceVariant
                                  : null,
                            )),
                      ),
                      if (_time != null)
                        IconButton(
                          icon: const Icon(Icons.close, size: 16),
                          visualDensity: VisualDensity.compact,
                          onPressed: () => setState(() => _time = null),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _descCtrl,
                decoration: InputDecoration(
                  labelText: 'Note',
                  hintText: 'How you met, what you talked about…',
                  suffixIcon: NoteFieldActions(controller: _descCtrl),
                ),
                minLines: 2,
                maxLines: 6,
                inputFormatters: const [_SentenceCaseFormatter()],
              ),
              const SizedBox(height: 16),
              Text('Location', style: theme.textTheme.labelMedium),
              const SizedBox(height: 6),
              OutlinedButton.icon(
                icon: _locating
                    ? const SizedBox(
                        width: 16, height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.location_on_outlined, size: 16),
                label: Text(_locating
                    ? 'Getting your location…'
                    : _lat == null
                        ? 'Pick on map'
                        : '${_lat!.toStringAsFixed(5)}, ${_lon!.toStringAsFixed(5)}'),
                onPressed: _pickLocation,
              ),
              if (_saveError != null) ...[
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
                          _saveError!,
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
      ),
      actions: [
        TextButton(
          onPressed: _saving ? null : () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        ElevatedButton(
          style: ElevatedButton.styleFrom(minimumSize: const Size(80, 44)),
          onPressed: _saving ? null : _save,
          child: _saving
              ? const SizedBox(
                  width: 18, height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('Save'),
        ),
      ],
    );
  }
}
