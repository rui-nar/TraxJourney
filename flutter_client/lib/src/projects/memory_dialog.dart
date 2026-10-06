import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../billing/upgrade_sheet.dart';
import '../core/picked_file_bytes.dart';
import 'location_picker_dialog.dart';
import 'project_notifier.dart';
import '../crypto/encrypted_display.dart';

/// Dialog to create or edit a memory.
///
/// Pass [editMemory] (the existing memory map from `items`) to open in edit mode.
/// Pass [initialDate] to pre-fill the date picker (e.g. from the day header button).
class MemoryDialog extends StatefulWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic>? editMemory;
  final String? initialDate;
  final int? insertAfterIndex;

  const MemoryDialog({
    super.key,
    required this.notifier,
    this.editMemory,
    this.initialDate,
    this.insertAfterIndex,
  });

  @override
  State<MemoryDialog> createState() => _MemoryDialogState();
}

class _MemoryDialogState extends State<MemoryDialog> {
  late TextEditingController _nameCtrl;
  late TextEditingController _descCtrl;
  // A field this device cannot decrypt keeps its ciphertext envelope here
  // (issue #466): shown read-only as kEncryptedUnavailable, and saved back
  // exactly as it was, never as the label, never cleared.
  String? _nameEnvelope;
  String? _descEnvelope;
  DateTime? _date;
  TimeOfDay? _time;
  String _geoMode = 'start_of_day';
  double? _customLat;
  double? _customLon;
  bool _saving = false;
  // Create-failure error, shown inline inside the dialog while it stays open
  // for a retry — the root ScaffoldMessenger would render a SnackBar behind
  // the modal, where the user never sees it (issue #20's fix, applied here).
  String? _saveError;

  // Existing photos (UUIDs from the server, for edit mode)
  List<String> _existingPhotos = [];
  // Pending new photos to upload after save
  final List<({Uint8List bytes, String filename})> _pendingPhotos = [];
  // Photos to delete on save (edit mode)
  final Set<String> _photosToDelete = {};

  /// What an undecryptable field says under itself (#466): it is shown
  /// as [kEncryptedUnavailable], read-only, and saved back unchanged.
  static const _encryptedHelp =
      'Encrypted, and this device cannot read it, so it cannot be edited here.';
  static const _months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  static String _fmtDate(DateTime d) => '${_months[d.month - 1]} ${d.day}, ${d.year}';
  static String _toIso(DateTime d) =>
      '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';
  static String _fmtTime(TimeOfDay t) =>
      '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';

  @override
  void initState() {
    super.initState();
    final mem = widget.editMemory;
    // From the record made when the items were revealed, never from the
    // value's shape: typed text such as "v1.2.3" looks like an envelope.
    final id = mem?['id']?.toString();
    final undecrypted = widget.notifier.itemsFacet.undecryptedFields;
    if (undecrypted.contains('memory', id, 'name')) {
      _nameEnvelope = mem!['name'] as String?;
    }
    if (undecrypted.contains('memory', id, 'description')) {
      _descEnvelope = mem!['description'] as String?;
    }
    _nameCtrl = TextEditingController(
        text: _nameEnvelope != null ? kEncryptedUnavailable : mem?['name'] as String? ?? '');
    _descCtrl = TextEditingController(
        text: _descEnvelope != null
            ? kEncryptedUnavailable
            : mem?['description'] as String? ?? '');

    // Date: edit → from memory; create → initialDate or null
    if (mem != null) {
      final ds = mem['date'] as String?;
      if (ds != null) _date = DateTime.tryParse(ds);
    } else if (widget.initialDate != null) {
      _date = DateTime.tryParse(widget.initialDate!);
    }

    // Time
    if (mem != null) {
      final ts = mem['time'] as String?;
      if (ts != null) {
        final parts = ts.split(':');
        if (parts.length == 2) {
          _time = TimeOfDay(
            hour: int.tryParse(parts[0]) ?? 0,
            minute: int.tryParse(parts[1]) ?? 0,
          );
        }
      }
    }

    // Geo mode
    if (mem != null) {
      _geoMode = mem['geo_mode'] as String? ?? 'start_of_day';
      _customLat = (mem['lat'] as num?)?.toDouble();
      _customLon = (mem['lon'] as num?)?.toDouble();
    }

    // Existing photos
    if (mem != null) {
      final rawPhotos = mem['photos'];
      if (rawPhotos is List) {
        _existingPhotos = rawPhotos.cast<String>();
      }
    }
  }

  @override
  void dispose() {
    _nameCtrl.dispose();
    _descCtrl.dispose();
    super.dispose();
  }

  Future<void> _pickPhotos() async {
    final files = await FilePicker.pickFiles(type: FileType.image);
    for (final f in files) {
      final bytes = await f.readAsBytesOrNull();
      if (bytes == null) continue;
      setState(() {
        _pendingPhotos.add((bytes: bytes, filename: f.name));
      });
    }
  }

  Future<void> _pickCustomLocation() async {
    final result = await showDialog<LatLonResult>(
      context: context,
      useRootNavigator: true,
      builder: (_) => LocationPickerDialog(
        title: 'Pick memory location',
        initialLat: _customLat,
        initialLon: _customLon,
        geo: widget.notifier.geoFacet.geo,
      ),
    );
    if (result != null && mounted) {
      setState(() {
        _customLat = result.lat;
        _customLon = result.lon;
      });
    }
  }

  Future<void> _save() async {
    if (_date == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('A date is required')),
      );
      return;
    }
    // Captured before the dialog pops: an upload refused on a plan limit opens
    // the upgrade sheet afterwards, and this dialog's context is gone by then.
    final hostContext = Navigator.of(context, rootNavigator: true).context;
    final messenger = ScaffoldMessenger.of(context);
    setState(() {
      _saveError = null;
      _saving = true;
    });
    try {
      final dateStr = _toIso(_date!);
      final timeStr = _time != null ? _fmtTime(_time!) : null;
      final name = _nameEnvelope ??
          (_nameCtrl.text.trim().isEmpty ? null : _nameCtrl.text.trim());
      final desc = _descEnvelope ??
          (_descCtrl.text.trim().isEmpty ? null : _descCtrl.text.trim());
      var failedPhotos = 0;

      final mem = widget.editMemory;
      if (mem != null) {
        // Edit mode
        final memId = mem['id']?.toString() ?? '';

        // Delete removed photos first
        for (final uuid in _photosToDelete) {
          await widget.notifier.deleteMemoryPhoto(memId, uuid, reload: false);
        }

        await widget.notifier.updateMemory(
          memId,
          date: dateStr,
          geoMode: _geoMode,
          name: name,
          time: timeStr,
          description: desc,
          lat: _geoMode == 'custom' ? _customLat : null,
          lon: _geoMode == 'custom' ? _customLon : null,
          keepStoredName: _nameEnvelope != null,
          keepStoredDescription: _descEnvelope != null,
        );

        // Upload new photos
        for (final p in _pendingPhotos) {
          final uuid =
              await widget.notifier.uploadMemoryPhoto(memId, p.bytes, p.filename);
          if (uuid == null) failedPhotos++;
        }

        // Final reload to pick up all changes
        if (widget.notifier.projectName != null) {
          // already done inside updateMemory → _silentReload
        }
      } else {
        // Create mode — we need the new memory's ID to upload photos.
        // Create first, then use _silentReload to get the ID, then upload.
        final ok = await widget.notifier.createMemory(
          date: dateStr,
          geoMode: _geoMode,
          name: name,
          time: timeStr,
          description: desc,
          lat: _geoMode == 'custom' ? _customLat : null,
          lon: _geoMode == 'custom' ? _customLon : null,
          insertAfterIndex: widget.insertAfterIndex,
        );
        if (!ok) {
          setState(() =>
              _saveError = widget.notifier.error ?? 'Failed to save memory');
          return;
        }

        // If there are pending photos, find the newly created memory ID
        if (_pendingPhotos.isNotEmpty) {
          final newMemory = widget.notifier.itemsFacet.items
              .where((i) => i['item_type'] == 'memory')
              .map((i) => i['memory'] as Map<String, dynamic>?)
              .where((m) =>
                  m != null &&
                  m['date'] == dateStr &&
                  m['id']?.toString() != '__optimistic__')
              .lastOrNull;

          if (newMemory != null) {
            final memId = newMemory['id']?.toString() ?? '';
            for (final p in _pendingPhotos) {
              final uuid = await widget.notifier
                  .uploadMemoryPhoto(memId, p.bytes, p.filename);
              if (uuid == null) failedPhotos++;
            }
          }
        }
      }

      if (mounted) Navigator.of(context).pop();
      if (failedPhotos > 0) {
        messenger.showSnackBar(SnackBar(
          content: Text(failedPhotos == 1
              ? 'Memory saved — 1 photo failed to upload'
              : 'Memory saved — $failedPhotos photos failed to upload'),
        ));
      }
      final quota = widget.notifier.takeQuotaError();
      if (hostContext.mounted) {
        await maybeShowUpgradeSheet(hostContext, quota);
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  String _photoThumbUrl(String memoryId, String uuid) =>
      '${widget.notifier.apiBaseUrl}/api/memories/$memoryId/photos/$uuid/thumb';

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isEdit = widget.editMemory != null;
    final memId = widget.editMemory?['id']?.toString();

    return AlertDialog(
      title: Text(isEdit ? 'Edit Memory' : 'Add Memory'),
      content: SizedBox(
        width: 480,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Name
              TextField(
                controller: _nameCtrl,
                readOnly: _nameEnvelope != null,
                decoration: InputDecoration(
                  labelText: 'Title (optional)',
                  hintText: 'e.g. Sunrise at Col du Tourmalet',
                  helperText: _nameEnvelope != null ? _encryptedHelp : null,
                ),
              ),
              const SizedBox(height: 12),
              // Date
              Text('Date *', style: theme.textTheme.labelMedium),
              const SizedBox(height: 4),
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
                  if (picked != null) setState(() => _date = picked);
                },
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 6),
                  child: Row(
                    children: [
                      Icon(Icons.calendar_today, size: 18,
                          color: theme.colorScheme.onSurfaceVariant),
                      const SizedBox(width: 12),
                      Text(
                        _date == null ? 'Select date…' : _fmtDate(_date!),
                        style: theme.textTheme.bodyMedium?.copyWith(
                          color: _date == null
                              ? theme.colorScheme.error
                              : null,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              // Time (optional)
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
                        child: Text(
                          _time == null
                              ? 'Time (optional)'
                              : _fmtTime(_time!),
                          style: theme.textTheme.bodyMedium?.copyWith(
                            color: _time == null
                                ? theme.colorScheme.onSurfaceVariant
                                : null,
                          ),
                        ),
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
              // Description
              TextField(
                controller: _descCtrl,
                readOnly: _descEnvelope != null,
                decoration: InputDecoration(
                  labelText: 'Description (optional)',
                  hintText: 'What happened here…',
                  helperText: _descEnvelope != null ? _encryptedHelp : null,
                ),
                minLines: 2,
                maxLines: 5,
              ),
              const SizedBox(height: 16),
              // Geo mode
              Text('Location', style: theme.textTheme.labelMedium),
              const SizedBox(height: 6),
              SizedBox(
                width: double.infinity,
                child: SegmentedButton<String>(
                  segments: const [
                    ButtonSegment(value: 'start_of_day', label: Text('Start of day')),
                    ButtonSegment(value: 'end_of_day',   label: Text('End of day')),
                    ButtonSegment(value: 'custom',        label: Text('Custom')),
                  ],
                  selected: {_geoMode},
                  onSelectionChanged: (s) =>
                      setState(() => _geoMode = s.first),
                  multiSelectionEnabled: false,
                ),
              ),
              if (_geoMode == 'custom') ...[
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  icon: const Icon(Icons.location_on_outlined, size: 16),
                  label: Text(_customLat == null
                      ? 'Pick on map'
                      : '${_customLat!.toStringAsFixed(5)}, ${_customLon!.toStringAsFixed(5)}'),
                  onPressed: _pickCustomLocation,
                ),
              ],
              const SizedBox(height: 16),
              // Photos
              Text('Photos', style: theme.textTheme.labelMedium),
              const SizedBox(height: 8),
              // Existing photos (edit mode)
              if (_existingPhotos.isNotEmpty && memId != null) ...[
                SizedBox(
                  height: 80,
                  child: ListView.separated(
                    scrollDirection: Axis.horizontal,
                    itemCount: _existingPhotos.length,
                    separatorBuilder: (_, __) => const SizedBox(width: 8),
                    itemBuilder: (_, i) {
                      final uuid = _existingPhotos[i];
                      final markedForDelete = _photosToDelete.contains(uuid);
                      return Stack(
                        children: [
                          Opacity(
                            opacity: markedForDelete ? 0.3 : 1.0,
                            child: ClipRRect(
                              borderRadius: BorderRadius.circular(6),
                              child: Image.network(
                                _photoThumbUrl(memId, uuid),
                                width: 80,
                                height: 80,
                                fit: BoxFit.cover,
                                headers: widget.notifier.apiToken != null
                                    ? {'Authorization': 'Bearer ${widget.notifier.apiToken}'}
                                    : {},
                              ),
                            ),
                          ),
                          Positioned(
                            top: 2, right: 2,
                            child: GestureDetector(
                              onTap: () => setState(() {
                                if (markedForDelete) {
                                  _photosToDelete.remove(uuid);
                                } else {
                                  _photosToDelete.add(uuid);
                                }
                              }),
                              child: Container(
                                decoration: BoxDecoration(
                                  color: markedForDelete
                                      ? Colors.green
                                      : Colors.red.withValues(alpha: 0.8),
                                  shape: BoxShape.circle,
                                ),
                                child: Icon(
                                  markedForDelete ? Icons.undo : Icons.close,
                                  size: 16,
                                  color: Colors.white,
                                ),
                              ),
                            ),
                          ),
                        ],
                      );
                    },
                  ),
                ),
                const SizedBox(height: 8),
              ],
              // Pending new photos
              if (_pendingPhotos.isNotEmpty) ...[
                SizedBox(
                  height: 80,
                  child: ListView.separated(
                    scrollDirection: Axis.horizontal,
                    itemCount: _pendingPhotos.length,
                    separatorBuilder: (_, __) => const SizedBox(width: 8),
                    itemBuilder: (_, i) {
                      final p = _pendingPhotos[i];
                      return Stack(
                        children: [
                          ClipRRect(
                            borderRadius: BorderRadius.circular(6),
                            child: Image.memory(
                              p.bytes,
                              width: 80,
                              height: 80,
                              fit: BoxFit.cover,
                            ),
                          ),
                          Positioned(
                            top: 2, right: 2,
                            child: GestureDetector(
                              onTap: () =>
                                  setState(() => _pendingPhotos.removeAt(i)),
                              child: Container(
                                decoration: BoxDecoration(
                                  color: Colors.red.withValues(alpha: 0.8),
                                  shape: BoxShape.circle,
                                ),
                                child: const Icon(Icons.close, size: 16,
                                    color: Colors.white),
                              ),
                            ),
                          ),
                        ],
                      );
                    },
                  ),
                ),
                const SizedBox(height: 8),
              ],
              OutlinedButton.icon(
                icon: const Icon(Icons.add_photo_alternate_outlined, size: 18),
                label: const Text('Add photos'),
                onPressed: _pickPhotos,
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
