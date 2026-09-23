import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_colorpicker/flutter_colorpicker.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../api/client.dart';
import '../core/app_version.dart';
import '../core/brand.dart';
import '../core/design_tokens.dart' show LineStyleKind, resolveTypeStyle, lineStyleName;
import 'project_notifier.dart';
import 'travel_companions_section.dart';
import 'trip_end_days.dart';

// ── Design tokens ──────────────────────────────────────────────────────────────
const _kBg        = Color(0xFF0A1320);
const _kBgCard    = Color(0xFF0F1A26);
const _kBgSidebar = Color(0xFF0C1722);
const _kBorder    = Color(0xFF1F2F42);
const _kText1     = Color(0xFFF1F5F9);
const _kText2     = Color(0xFFCBD5E1);
const _kMuted     = Color(0xFF94A3B8);
const _kDim       = Color(0xFF64748B);
const _kBlueActive = Color(0xFF60A5FA);
const _kRed       = Color(0xFFEF4444);

// ── Screen ─────────────────────────────────────────────────────────────────────

class ProjectSettingsScreen extends StatefulWidget {
  final String projectName;

  /// Owning user's id for a project shared with the caller (issue #106);
  /// null for one of the caller's own projects. The screen itself reads/writes
  /// through the ambient ProjectNotifier (already addressing the right
  /// project — see [ProjectNotifier.ref]), so this is accepted only for
  /// router-signature symmetry with the other project routes.
  final int? ownerId;

  const ProjectSettingsScreen({super.key, required this.projectName, this.ownerId});

  @override
  State<ProjectSettingsScreen> createState() => _ProjectSettingsScreenState();
}

class _ProjectSettingsScreenState extends State<ProjectSettingsScreen> {
  int _section = 0;

  late TextEditingController _nameCtrl;
  DateTime? _tripStart;
  DateTime? _tripEnd;
  bool _saving = false;

  late List<TextEditingController> _optCtrls;
  late List<String> _optGroups;
  static const _groups = ['Outdoors', 'Indoors', 'Other'];

  late List<TextEditingController> _counterNameCtrls;
  late List<TextEditingController> _counterStartCtrls;

  bool _autoSync = true;
  int? _linkedPsTripId;
  List<Map<String, dynamic>> _psTrips = [];
  bool _psTripsLoading = false;

  bool _visitorsLoading = false;
  Map<String, dynamic>? _visitors;

  // Per-share content key (issue #28) — set once generateShareContent()
  // succeeds; appended as the `#key=...` URL fragment on the full-project
  // share link. Held only in memory: the server never sees the key itself,
  // so leaving this screen (without copying the link) means regenerating.
  String? _shareContentKey;
  bool _generatingShareContent = false;

  late Color _trackColor;
  Color? _trackSecondaryColor; // null = auto-derive
  late double _trackWidth;
  late bool _alternating;
  Color? _elevationChartColor; // null = use black
  late bool _elevationChartShowLine;
  late List<String> _languages;
  late bool _colorByType;
  late Map<String, Map<String, dynamic>> _typeStyles;

  // Local deep-copy of dayMeta so tag edits are applied immediately and
  // passed to saveDayMeta on Save without touching the notifier until then.
  late Map<String, Map<String, dynamic>> _dayMeta;

  static const _allSectionLabels = [
    (icon: Icons.tune,          label: 'General',      key: 0),
    (icon: Icons.hub,           label: 'Integrations', key: 1),
    (icon: Icons.share,         label: 'Sharing',       key: 2),
    (icon: Icons.polyline,      label: 'Track style',  key: 3),
    (icon: Icons.hotel,         label: 'Sleeping',     key: 4),
    (icon: Icons.tag,           label: 'Counters',     key: 5),
    (icon: Icons.label_outlined, label: 'Tags',        key: 6),
    (icon: Icons.group_outlined, label: 'Companions',  key: 7),
  ];

  /// Section tabs, keyed so [_sectionContent] can switch on a stable id
  /// rather than a position that shifts when a tab is hidden. The Sharing
  /// tab (create/revoke links, per-share content) is co-owner+ (issue #109)
  /// — editors/viewers get 403s from those endpoints, so it's hidden rather
  /// than shown disabled.
  List<({IconData icon, String label, int key})> get _sectionLabels =>
      _notifier.canManageTrip
          ? _allSectionLabels
          : [for (final s in _allSectionLabels) if (s.key != 2) s];

  static const _months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  static String _fmtDate(DateTime d) => '${_months[d.month - 1]} ${d.day}, ${d.year}';
  static String _toIso(DateTime d) =>
      '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';

  ProjectNotifier get _notifier => context.read<ProjectNotifier>();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _loadVisitors();
      _loadPsTrips();
    });
    final n = context.read<ProjectNotifier>();
    _nameCtrl = TextEditingController(text: n.projectName ?? '');
    final ts = n.tripStart;
    if (ts != null) _tripStart = DateTime.tryParse(ts);
    final te = n.tripEnd;
    if (te != null) _tripEnd = DateTime.tryParse(te);
    _optCtrls = n.sleepingOptions
        .map((opt) => TextEditingController(text: opt))
        .toList();
    _optGroups = n.sleepingOptions
        .map((opt) => n.sleepingOptionGroups[opt] ?? 'Other')
        .toList();
    _autoSync = n.autoSyncEnabled;
    _linkedPsTripId = n.linkedPsTripId;
    _trackColor = n.trackColor;
    _trackSecondaryColor = n.trackSecondaryColor;
    _trackWidth = n.trackWidth;
    _alternating = n.alternatingTrackColors;
    _elevationChartColor = n.elevationChartColor;
    _elevationChartShowLine = n.elevationChartShowLine;
    _languages = List<String>.from(n.languages);
    _colorByType = n.colorByType;
    _typeStyles = {
      for (final e in n.typeStyles.entries) e.key: Map<String, dynamic>.from(e.value)
    };
    _dayMeta = {
      for (final e in n.dayMeta.entries)
        e.key: {
          ...e.value,
          if (e.value['tags'] is List)
            'tags': List<String>.from((e.value['tags'] as List).cast<String>()),
        }
    };
    _counterNameCtrls = n.counters
        .map((c) => TextEditingController(text: c['name'] as String? ?? ''))
        .toList();
    _counterStartCtrls = n.counters
        .map((c) => TextEditingController(text: (c['start'] ?? 0).toString()))
        .toList();
  }

  @override
  void dispose() {
    _nameCtrl.dispose();
    for (final c in _optCtrls) { c.dispose(); }
    for (final c in _counterNameCtrls) { c.dispose(); }
    for (final c in _counterStartCtrls) { c.dispose(); }
    super.dispose();
  }

  void _addCounter() {
    setState(() {
      _counterNameCtrls.add(TextEditingController());
      _counterStartCtrls.add(TextEditingController(text: '0'));
    });
  }

  void _removeCounter(int i) {
    setState(() {
      _counterNameCtrls.removeAt(i).dispose();
      _counterStartCtrls.removeAt(i).dispose();
    });
  }

  void _addOption() {
    setState(() {
      _optCtrls.add(TextEditingController());
      _optGroups.add('Other');
    });
  }

  void _removeOption(int i) {
    setState(() {
      _optCtrls.removeAt(i).dispose();
      _optGroups.removeAt(i);
    });
  }

  Future<void> _loadVisitors() async {
    if (!mounted) return;
    setState(() => _visitorsLoading = true);
    try {
      final data = await _notifier.getShareVisitors();
      if (mounted) setState(() => _visitors = data);
    } catch (_) {
    } finally {
      if (mounted) setState(() => _visitorsLoading = false);
    }
  }

  Future<void> _loadPsTrips() async {
    if (!mounted) return;
    setState(() => _psTripsLoading = true);
    try {
      final raw = await api.get('/api/polarsteps/trips') as List<dynamic>;
      if (mounted) setState(() => _psTrips = raw.cast<Map<String, dynamic>>());
    } catch (_) {
    } finally {
      if (mounted) setState(() => _psTripsLoading = false);
    }
  }

  /// Day keys the trip holds content on for *any* member, from the server
  /// (issue #372) — null when the question could not be answered.
  ///
  /// Journal entries are per-user, so the caller's own item list is blind to
  /// a day another member's journal keeps on screen. A null here must never
  /// be read as "that day is empty": the caller-local view is exactly the one
  /// that deletes a still-visible day's shared day-meta.
  Future<({Set<String>? days, ContentCheckFailure? failure})>
      _fetchContentDays() async {
    final ref = _notifier.ref;
    if (ref == null) {
      return (days: null, failure: ContentCheckFailure.refused);
    }
    try {
      final data =
          await api.get(ref.path('/content-days')) as Map<String, dynamic>;
      return (
        days: (data['days'] as List<dynamic>).cast<String>().toSet(),
        failure: null,
      );
    } on ApiException catch (_) {
      // The server answered — an older one without this route (404), a role
      // refusal, a 500. The user is online; telling them otherwise sends them
      // to fix the wrong thing.
      return (days: null, failure: ContentCheckFailure.refused);
    } catch (_) {
      return (days: null, failure: ContentCheckFailure.unreachable);
    }
  }

  Future<void> _save() async {
    setState(() => _saving = true);

    final tripEndStr = _tripEnd == null ? null : _toIso(_tripEnd!);
    if (tripEndStr != null) {
      final n = _notifier;
      // A day is only actually removable when nothing but day-meta puts it
      // there. Candidates union four sources, and the union matters: the
      // classifier only ever reports days it is handed, so `candidates` must
      // stay a superset of `daysWithContent` or a pinned day goes unmentioned.
      //  - the local day-meta copy, which carries edits the notifier hasn't
      //    seen yet;
      //  - the notifier's day list, itself the union of day-meta and the
      //    caller-visible content days since issue #370;
      //  - that same local extraction, so an unsaved local edit pins its day;
      //  - the server's answer, which unlike every source above sees *every*
      //    member's content (issue #372) — journals are per-user server-side,
      //    so nothing on this client can see a day another member's journal
      //    keeps on screen. This one is not redundant with orderedDayKeys().
      //
      // When the server can't be reached, every candidate counts as pinned:
      // nothing is deleted and the dialog says why. Falling back to the
      // caller-local set there is the bug itself.
      final localPinned = contentDayKeys(n.activities, n.items);
      final localCandidates = {
        ..._dayMeta.keys,
        ...n.orderedDayKeys(),
        ...localPinned,
      };
      // Days that can actually be removed are worth confirming on every save —
      // one confirmation resolves them. Days that only *stay* are worth saying
      // once, when the end date is set or moved: nothing the dialog offers can
      // clear them, so raising it on an unrelated save (a colour tweak, say)
      // would nag forever and throw the edit away if the user cancels.
      final endMoved = tripEndStr != n.tripEnd?.split('T').first;
      // Skip the round trip when its answer cannot matter: the end date hasn't
      // moved (so the stay-only half is suppressed) and nothing local sits past
      // it (so there is nothing to delete either). Otherwise every settings
      // save on a dated trip would block on the network — up to the 30 s API
      // timeout — to compute a dialog it will not show.
      final needsCheck = endMoved ||
          localCandidates.any((k) => k.compareTo(tripEndStr) > 0);
      final checked = needsCheck
          ? await _fetchContentDays()
          : (days: <String>{}, failure: null);
      if (!mounted) return;
      final serverDays = checked.days;
      final failure = checked.failure;
      final candidates = {...localCandidates, ...?serverDays};
      final orphans = classifyTripEndOrphans(
        dayKeys: candidates,
        daysWithContent:
            serverDays == null ? candidates : {...localPinned, ...serverDays},
        tripEnd: tripEndStr,
      );
      final gone = orphans.removable.length;
      final kept = orphans.pinned.length;
      // A pinned day whose *content* this user can see is one they can act on;
      // otherwise what pins it is another member's, and telling them to "move
      // or delete that content" asks for something they can neither see nor
      // touch. The test is localPinned, not localCandidates: a day is in
      // localCandidates merely by having a day-meta row, and having notes on a
      // day says nothing about whose content keeps it on screen. The #372 case
      // is exactly a day that has both — shared day-meta *and* a companion's
      // journal — so testing membership of the wider set silently excludes it.
      final hiddenKept =
          orphans.pinned.where((k) => !localPinned.contains(k)).length;
      if (gone > 0 || (kept > 0 && endMoved)) {
        final when = _fmtDate(_tripEnd!);
        final message = tripEndWarningMessage(
          gone: gone,
          visibleKept: kept - hiddenKept,
          hiddenKept: hiddenKept,
          when: when,
          failure: failure,
        );
        final confirmed = await showDialog<bool>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: Text(gone > 0
                ? 'Remove days after the end date?'
                : 'Days after the end date will stay'),
            content: Text(message),
            actions: [
              TextButton(
                onPressed: () => Navigator.of(ctx).pop(false),
                child: const Text('Cancel'),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(minimumSize: const Size(80, 44)),
                onPressed: () => Navigator.of(ctx).pop(true),
                child: Text(gone > 0 ? 'Delete' : 'Continue'),
              ),
            ],
          ),
        );
        if (confirmed != true) {
          setState(() => _saving = false);
          return;
        }
        // Prune the local copy rather than writing a filtered map here: the
        // single saveDayMeta below sends _dayMeta, and a separate write would
        // just be undone by that stale snapshot (issue #358).
        final removable = orphans.removable.toSet();
        _dayMeta.removeWhere((k, _) => removable.contains(k));
      }
    }

    final n = _notifier;
    final newName = _nameCtrl.text.trim();
    if (n.canManageTrip && newName.isNotEmpty && newName != n.projectName) {
      await n.renameProject(newName);
    }

    final updatedOpts = <String>[];
    final updatedGroups = <String, String>{};
    for (int i = 0; i < _optCtrls.length; i++) {
      final name = _optCtrls[i].text.trim();
      if (name.isNotEmpty) {
        updatedOpts.add(name);
        updatedGroups[name] = _optGroups[i];
      }
    }
    final updatedCounters = <Map<String, dynamic>>[];
    for (int i = 0; i < _counterNameCtrls.length; i++) {
      final name = _counterNameCtrls[i].text.trim();
      final start = double.tryParse(_counterStartCtrls[i].text) ?? 0.0;
      if (name.isNotEmpty) updatedCounters.add({'name': name, 'start': start});
    }

    n.setTripDates(
      _tripStart == null ? null : _toIso(_tripStart!),
      tripEndStr,
    );
    n.saveDayMeta(
      newDayMeta: _dayMeta,
      newSleepingOptions: updatedOpts,
      newSleepingOptionGroups: updatedGroups,
      newCounters: updatedCounters,
    );
    n.setTrackStyle(
      color: _trackColor,
      secondaryColor: _trackSecondaryColor,
      width: _trackWidth,
      alternating: _alternating,
      elevationColor: _elevationChartColor,
      elevationShowLine: _elevationChartShowLine,
      colorByTypeEnabled: _colorByType,
      typeStyleOverrides: _typeStyles,
    );
    n.saveLanguages(_languages);
    // _autoSync/_linkedPsTripId were seeded from the notifier in initState()
    // (below) — now that ProjectNotifier fetches sync-meta in the background
    // rather than before the panel becomes interactive, isSyncMetaLoaded can
    // still be false here on a very fast save. Skip writing these two fields
    // in that case rather than push their still-default snapshot and silently
    // revert the real auto-sync setting / unlink the real Polarsteps trip.
    if (n.isSyncMetaLoaded) {
      n.saveSyncMeta(
        autoSyncEnabled: _autoSync,
        linkedPsTripId: _linkedPsTripId,
        clearLinkedTrip: _linkedPsTripId == null,
      );
    }
    if (mounted) context.pop();
  }

  Future<void> _pickColor({
    required Color current,
    required ValueChanged<Color> onPicked,
    required String title,
  }) async {
    Color temp = current;
    await showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: SingleChildScrollView(
          child: HueRingPicker(
            pickerColor: current,
            onColorChanged: (c) => temp = c,
            enableAlpha: false,
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancel'),
          ),
          ElevatedButton(
            onPressed: () {
              onPicked(temp);
              Navigator.of(ctx).pop();
            },
            child: const Text('Select'),
          ),
        ],
      ),
    );
  }

  static String _colorToHex(Color c) =>
      '#${(c.toARGB32() & 0xFFFFFF).toRadixString(16).padLeft(6, '0').toUpperCase()}';

  /// One row of the "Per-type styling" subsection (issue #95): a colour
  /// swatch + solid/dashed/dotted picker for [bucket], falling back to the
  /// built-in default (see design_tokens.dart resolveTypeStyle) until the
  /// user overrides it.
  Widget _typeStyleRow(String bucket, String label, {required bool isSegment}) {
    final override = _typeStyles[bucket];
    final resolved = resolveTypeStyle(bucket, isSegment: isSegment, overrides: override);
    void updateOverride(void Function(Map<String, dynamic> entry) mutate) {
      setState(() {
        final entry = Map<String, dynamic>.from(_typeStyles[bucket] ?? {});
        mutate(entry);
        _typeStyles[bucket] = entry;
      });
    }

    return Padding(
      padding: const EdgeInsets.fromLTRB(22, 14, 22, 0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Divider(height: 1, color: _kBorder),
          const SizedBox(height: 14),
          _ColorPickerRow(
            label: label,
            color: resolved.color,
            badge: override == null ? 'default' : null,
            onTap: () => _pickColor(
              current: resolved.color,
              title: '$label colour',
              onPicked: (c) =>
                  updateOverride((entry) => entry['color'] = _colorToHex(c)),
            ),
            onClear: override != null
                ? () => setState(() => _typeStyles.remove(bucket))
                : null,
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              for (final s in LineStyleKind.values)
                Padding(
                  padding: const EdgeInsets.only(right: 6),
                  child: ChoiceChip(
                    label: Text(
                      lineStyleName(s)[0].toUpperCase() + lineStyleName(s).substring(1),
                      style: const TextStyle(fontSize: 11),
                    ),
                    visualDensity: VisualDensity.compact,
                    selected: resolved.style == s,
                    onSelected: (_) => updateOverride(
                        (entry) => entry['style'] = lineStyleName(s)),
                  ),
                ),
            ],
          ),
        ],
      ),
    );
  }

  // ── Section content ──────────────────────────────────────────────────────────

  Widget _sectionContent() {
    return switch (_section) {
      0 => _generalSection(),
      1 => _integrationsSection(),
      2 => _sharingSection(),
      3 => _trackSection(),
      4 => _sleepingSection(),
      5 => _countersSection(),
      6 => _tagsSection(),
      7 => _companionsSection(),
      _ => const SizedBox.shrink(),
    };
  }

  Widget _companionsSection() {
    return _SectionCard(
      eyebrow: '08',
      title: 'Travel companions',
      subtitle: _notifier.canManageTrip
          ? 'Invite people to add and edit trip content with you.'
          : 'People with access to this trip.',
      child: const TravelCompanionsSection(),
    );
  }

  Widget _generalSection() {
    return _SectionCard(
      eyebrow: '01',
      title: 'General',
      subtitle: 'Project name and date range.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // Project name
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text('Project name', style: TextStyle(
                  color: _kText2, fontSize: 13.5, fontWeight: FontWeight.w500)),
                const SizedBox(height: 8),
                TextField(
                  controller: _nameCtrl,
                  // Rename is co-owner+ (issue #109) — editors/viewers get a
                  // 403 from the server, so the field is read-only for them.
                  readOnly: !_notifier.canManageTrip,
                  style: const TextStyle(
                    color: _kText1, fontSize: 15, fontWeight: FontWeight.w600),
                  decoration: InputDecoration(
                    isDense: true,
                    filled: true,
                    fillColor: _kBg,
                    contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(8),
                      borderSide: const BorderSide(color: _kBorder),
                    ),
                    enabledBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(8),
                      borderSide: const BorderSide(color: _kBorder),
                    ),
                  ),
                ),
              ],
            ),
          ),
          const Divider(height: 1, color: _kBorder),
          // Trip start
          _fieldRow(
            label: 'Trip start',
            hint: 'Day 1 is inferred from the earliest activity date.',
            right: _DateChip(
              icon: Icons.event,
              label: _tripStart == null ? 'Inferred' : _fmtDate(_tripStart!),
              muted: _tripStart == null,
              onTap: () async {
                final picked = await showDatePicker(
                  context: context,
                  useRootNavigator: true,
                  initialDate: _tripStart ?? DateTime.now(),
                  firstDate: DateTime(2000),
                  lastDate: DateTime(2100),
                );
                if (picked != null) setState(() => _tripStart = picked);
              },
              onClear: _tripStart != null ? () => setState(() => _tripStart = null) : null,
            ),
          ),
          const Divider(height: 1, color: _kBorder),
          _fieldRow(
            label: 'Trip end',
            hint: 'While unset, empty days are auto-created up to today.',
            right: _DateChip(
              icon: Icons.event_available,
              label: _tripEnd == null ? 'Ongoing' : _fmtDate(_tripEnd!),
              muted: _tripEnd == null,
              onTap: () async {
                final picked = await showDatePicker(
                  context: context,
                  useRootNavigator: true,
                  initialDate: _tripEnd ?? DateTime.now(),
                  firstDate: DateTime(2000),
                  lastDate: DateTime(2100),
                );
                if (picked != null) setState(() => _tripEnd = picked);
              },
              onClear: _tripEnd != null ? () => setState(() => _tripEnd = null) : null,
            ),
          ),
          const Divider(height: 1, color: _kBorder),
          _languagesRow(),
        ],
      ),
    );
  }

  static const _kSupportedLanguages = {
    'en': '🇬🇧 English',   'pt-PT': '🇵🇹 Portuguese', 'fr': '🇫🇷 French',
    'de': '🇩🇪 German',    'es': '🇪🇸 Spanish',     'it': '🇮🇹 Italian',
    'nl': '🇳🇱 Dutch',     'ja': '🇯🇵 Japanese',    'zh': '🇨🇳 Chinese',
    'ru': '🇷🇺 Russian',   'ar': '🇸🇦 Arabic',      'ko': '🇰🇷 Korean',
  };

  Widget _languagesRow() {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Translation languages',
              style: TextStyle(color: _kText2, fontSize: 13.5, fontWeight: FontWeight.w500)),
          const SizedBox(height: 4),
          const Text(
            'Guests can switch memory text to any of these languages.',
            style: TextStyle(color: _kDim, fontSize: 12),
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              ..._languages.map((code) => FilterChip(
                label: Text(_kSupportedLanguages[code] ?? code,
                    style: const TextStyle(fontSize: 12)),
                selected: true,
                onSelected: (_) => setState(() => _languages.remove(code)),
                deleteIcon: const Icon(Icons.close, size: 14),
                onDeleted: () => setState(() => _languages.remove(code)),
                backgroundColor: _kBgCard,
                selectedColor: _kBlueActive.withAlpha(40),
                checkmarkColor: _kBlueActive,
                side: const BorderSide(color: _kBorder),
                labelStyle: const TextStyle(color: _kText1),
              )),
              ActionChip(
                avatar: const Icon(Icons.add, size: 16, color: _kBlueActive),
                label: const Text('Add', style: TextStyle(color: _kBlueActive, fontSize: 12)),
                backgroundColor: _kBgCard,
                side: const BorderSide(color: _kBorder),
                onPressed: () => _showAddLanguageDialog(),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Future<void> _showAddLanguageDialog() async {
    final available = _kSupportedLanguages.entries
        .where((e) => !_languages.contains(e.key))
        .toList();
    if (available.isEmpty) return;
    final picked = await showDialog<String>(
      context: context,
      builder: (ctx) => SimpleDialog(
        backgroundColor: _kBgCard,
        title: const Text('Add language', style: TextStyle(color: _kText1)),
        children: available.map((e) => SimpleDialogOption(
          onPressed: () => Navigator.of(ctx).pop(e.key),
          child: Text(e.value, style: const TextStyle(color: _kText2, fontSize: 14)),
        )).toList(),
      ),
    );
    if (picked != null) setState(() => _languages.add(picked));
  }

  Widget _integrationsSection() {
    return _SectionCard(
      eyebrow: '02',
      title: 'Integrations',
      subtitle: 'Connected services that feed activities and memories.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SwitchListTile(
            contentPadding: const EdgeInsets.symmetric(horizontal: 22, vertical: 4),
            title: const Text('Auto-sync on project open',
                style: TextStyle(color: _kText2, fontSize: 13.5, fontWeight: FontWeight.w600)),
            subtitle: const Text(
              'Check for new Strava activities and Polarsteps steps when opening this project.',
              style: TextStyle(color: _kDim, fontSize: 12),
            ),
            value: _autoSync,
            onChanged: (v) => setState(() => _autoSync = v),
          ),
          if (_psTripsLoading)
            const Padding(
              padding: EdgeInsets.symmetric(horizontal: 22, vertical: 8),
              child: LinearProgressIndicator(),
            )
          else if (_psTrips.isNotEmpty) ...[
            const Divider(height: 1, color: _kBorder),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('Linked Polarsteps trip',
                      style: TextStyle(color: _kMuted, fontSize: 12, fontWeight: FontWeight.w500)),
                  const SizedBox(height: 6),
                  DropdownButton<int?>(
                    isExpanded: true,
                    dropdownColor: _kBgCard,
                    style: const TextStyle(color: _kText2),
                    value: _psTrips.any((t) => t['id'] == _linkedPsTripId)
                        ? _linkedPsTripId
                        : null,
                    items: [
                      const DropdownMenuItem<int?>(
                        value: null,
                        child: Text('None', style: TextStyle(color: _kText2)),
                      ),
                      ..._psTrips.map((t) => DropdownMenuItem<int?>(
                            value: t['id'] as int?,
                            child: Text(
                              t['name'] as String? ?? 'Trip ${t['id']}',
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(color: _kText2),
                            ),
                          )),
                    ],
                    onChanged: (v) => setState(() => _linkedPsTripId = v),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _sharingSection() {
    final n = _notifier;
    return _SectionCard(
      eyebrow: '03',
      title: 'Sharing',
      subtitle: 'Public links. Visitors are anonymous unless they sign in.',
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _ShareLinkCard(
              label: 'Full project',
              token: n.shareToken,
              shareKeyFragment: _shareContentKey,
              onCreateLink: () async {
                await n.createShareToken();
                setState(() {});
                _loadVisitors();
              },
              onRevoke: () async {
                await n.revokeShareToken();
                setState(() => _shareContentKey = null);
                _loadVisitors();
              },
            ),
            if (n.shareToken != null) ...[
              const SizedBox(height: 8),
              _ShareContentGenerator(
                busy: _generatingShareContent,
                generated: _shareContentKey != null,
                onGenerate: () async {
                  setState(() => _generatingShareContent = true);
                  final messenger = ScaffoldMessenger.of(context);
                  try {
                    final key = await n.generateShareContent();
                    if (!mounted) return;
                    setState(() => _shareContentKey = key);
                    if (key == null) {
                      messenger.showSnackBar(const SnackBar(content: Text(
                          'No encrypted memories to include — the link works as-is.')));
                    } else {
                      messenger.showSnackBar(const SnackBar(content: Text(
                          'Encrypted content included — copy the link again to share it.')));
                    }
                  } catch (e) {
                    if (mounted) {
                      messenger.showSnackBar(SnackBar(
                          content: Text('Failed to include encrypted content: $e')));
                    }
                  } finally {
                    if (mounted) setState(() => _generatingShareContent = false);
                  }
                },
              ),
            ],
            const SizedBox(height: 8),
            _ShareLinkCard(
              label: 'Without memories',
              token: n.shareTokenNoMemories,
              onCreateLink: () async {
                await n.createShareTokenNoMemories();
                setState(() {});
                _loadVisitors();
              },
              onRevoke: () async {
                await n.revokeShareTokenNoMemories();
                setState(() {});
                _loadVisitors();
              },
            ),
            if (_visitorsLoading) ...[
              const SizedBox(height: 12),
              const Center(child: SizedBox(width: 18, height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2))),
            ] else if (_visitors != null) ...[
              const SizedBox(height: 16),
              _VisitorStats(visitors: _visitors!),
            ],
          ],
        ),
      ),
    );
  }

  Widget _trackSection() {
    final autoSecondary = _MapPanelStateColorHelper.alternateColor(_trackColor);
    final effectiveSecondary = _trackSecondaryColor ?? autoSecondary;
    return _SectionCard(
      eyebrow: '04',
      title: 'Track style',
      subtitle: 'How activity polylines render on the map.',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // Primary colour picker
          Padding(
            padding: const EdgeInsets.fromLTRB(22, 16, 22, 0),
            child: _ColorPickerRow(
              label: 'Primary colour',
              color: _trackColor,
              onTap: () => _pickColor(
                current: _trackColor,
                title: 'Primary colour',
                onPicked: (c) => setState(() => _trackColor = c),
              ),
            ),
          ),
          const SizedBox(height: 16),
          // Thickness slider
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 22),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    const Text('Thickness', style: TextStyle(
                      fontFamily: 'monospace', fontSize: 10, fontWeight: FontWeight.w600,
                      color: _kDim, letterSpacing: 1.4,
                    )),
                    const Spacer(),
                    Text(
                      '${_trackWidth.toStringAsFixed(1)} px',
                      style: const TextStyle(fontFamily: 'monospace', fontSize: 13, color: _kText1),
                    ),
                  ],
                ),
                Slider(
                  value: _trackWidth,
                  min: 1.0, max: 6.0, divisions: 10,
                  activeColor: _kBlueActive,
                  onChanged: (v) => setState(() => _trackWidth = v),
                ),
              ],
            ),
          ),
          // Alternating toggle
          const Divider(height: 1, color: _kBorder),
          Padding(
            padding: const EdgeInsets.fromLTRB(22, 14, 22, 14),
            child: Row(
              children: [
                Expanded(
                  child: GestureDetector(
                    onTap: () => setState(() => _alternating = !_alternating),
                    child: const Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('Alternating colours',
                            style: TextStyle(color: _kText2, fontSize: 13.5, fontWeight: FontWeight.w600)),
                        SizedBox(height: 2),
                        Text('Every other activity uses the secondary colour.',
                            style: TextStyle(color: _kDim, fontSize: 12)),
                      ],
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Switch(
                  value: _alternating,
                  activeThumbColor: _kBlueActive,
                  onChanged: (v) => setState(() => _alternating = v),
                ),
              ],
            ),
          ),
          // Secondary colour picker (only when alternating is on)
          if (_alternating) ...[
            const Divider(height: 1, color: _kBorder),
            Padding(
              padding: const EdgeInsets.fromLTRB(22, 16, 22, 16),
              child: _ColorPickerRow(
                label: 'Secondary colour',
                color: effectiveSecondary,
                badge: _trackSecondaryColor == null ? 'auto' : null,
                onTap: () => _pickColor(
                  current: effectiveSecondary,
                  title: 'Secondary colour',
                  onPicked: (c) => setState(() => _trackSecondaryColor = c),
                ),
                onClear: _trackSecondaryColor != null
                    ? () => setState(() => _trackSecondaryColor = null)
                    : null,
              ),
            ),
          ],

          // ── Elevation chart ─────────────────────────────────────────────
          const Divider(height: 1, color: _kBorder),
          Padding(
            padding: const EdgeInsets.fromLTRB(22, 14, 22, 0),
            child: Text('Elevation chart', style: const TextStyle(
              fontFamily: 'monospace', fontSize: 10, fontWeight: FontWeight.w600,
              color: _kDim, letterSpacing: 1.4,
            )),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(22, 12, 22, 0),
            child: _ColorPickerRow(
              label: 'Chart colour',
              // "auto" mirrors the map track line (issue #22), so preview it
              // with the track colour rather than a misleading black swatch.
              color: _elevationChartColor ?? _trackColor,
              badge: _elevationChartColor == null ? 'auto' : null,
              onTap: () => _pickColor(
                current: _elevationChartColor ?? _trackColor,
                title: 'Chart colour',
                onPicked: (c) => setState(() => _elevationChartColor = c),
              ),
              onClear: _elevationChartColor != null
                  ? () => setState(() => _elevationChartColor = null)
                  : null,
            ),
          ),
          const Divider(height: 1, color: _kBorder),
          Padding(
            padding: const EdgeInsets.fromLTRB(22, 14, 22, 14),
            child: Row(
              children: [
                Expanded(
                  child: GestureDetector(
                    onTap: () => setState(() => _elevationChartShowLine = !_elevationChartShowLine),
                    child: const Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('Show line',
                            style: TextStyle(color: _kText2, fontSize: 13.5, fontWeight: FontWeight.w600)),
                        SizedBox(height: 2),
                        Text('Draw the line on top of the filled area.',
                            style: TextStyle(color: _kDim, fontSize: 12)),
                      ],
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Switch(
                  value: _elevationChartShowLine,
                  activeThumbColor: _kBlueActive,
                  onChanged: (v) => setState(() => _elevationChartShowLine = v),
                ),
              ],
            ),
          ),

          // ── Per-type styling (issue #95) ────────────────────────────────
          const Divider(height: 1, color: _kBorder),
          Padding(
            padding: const EdgeInsets.fromLTRB(22, 14, 22, 14),
            child: Row(
              children: [
                Expanded(
                  child: GestureDetector(
                    onTap: () => setState(() => _colorByType = !_colorByType),
                    child: const Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('Colour by activity type',
                            style: TextStyle(color: _kText2, fontSize: 13.5, fontWeight: FontWeight.w600)),
                        SizedBox(height: 2),
                        Text('Each activity/segment type gets its own colour and line style.',
                            style: TextStyle(color: _kDim, fontSize: 12)),
                      ],
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Switch(
                  value: _colorByType,
                  activeThumbColor: _kBlueActive,
                  onChanged: (v) => setState(() => _colorByType = v),
                ),
              ],
            ),
          ),
          if (_colorByType) ...[
            _typeStyleRow('ride', 'Ride', isSegment: false),
            _typeStyleRow('run', 'Run', isSegment: false),
            _typeStyleRow('hike', 'Hike / walk', isSegment: false),
            _typeStyleRow('other', 'Other activity', isSegment: false),
            _typeStyleRow('flight', 'Flight', isSegment: true),
            _typeStyleRow('train', 'Train', isSegment: true),
            _typeStyleRow('bus', 'Bus', isSegment: true),
            _typeStyleRow('boat', 'Boat', isSegment: true),
          ],
        ],
      ),
    );
  }

  Widget _sleepingSection() {
    return _SectionCard(
      eyebrow: '05',
      title: 'Sleeping options',
      subtitle: 'Tags available when editing a day.',
      action: _addButton('Add option', _addOption),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            for (int i = 0; i < _optCtrls.length; i++)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                  decoration: BoxDecoration(
                    color: _kBg, border: Border.all(color: _kBorder),
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _optCtrls[i],
                          style: const TextStyle(color: _kText2, fontSize: 13.5),
                          decoration: const InputDecoration(
                            isDense: true, border: InputBorder.none,
                            contentPadding: EdgeInsets.zero,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      _categoryDropdown(i),
                      const SizedBox(width: 4),
                      IconButton(
                        icon: const Icon(Icons.delete_outline, size: 16, color: _kDim),
                        visualDensity: VisualDensity.compact,
                        onPressed: () => _removeOption(i),
                      ),
                    ],
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _countersSection() {
    return _SectionCard(
      eyebrow: '06',
      title: 'Counters',
      subtitle: 'Track running tallies across the trip.',
      action: _addButton('Add counter', _addCounter),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                for (int i = 0; i < _counterNameCtrls.length; i++)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Container(
                      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                      decoration: BoxDecoration(
                        color: _kBg, border: Border.all(color: _kBorder),
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: Row(
                        children: [
                          Expanded(
                            child: TextField(
                              controller: _counterNameCtrls[i],
                              style: const TextStyle(color: _kText2, fontSize: 13.5),
                              decoration: const InputDecoration(
                                isDense: true, border: InputBorder.none,
                                hintText: 'Counter name',
                                hintStyle: TextStyle(color: _kDim),
                                contentPadding: EdgeInsets.zero,
                              ),
                            ),
                          ),
                          SizedBox(
                            width: 72,
                            child: TextField(
                              controller: _counterStartCtrls[i],
                              style: const TextStyle(
                                color: _kText1, fontSize: 18,
                                fontFamily: 'monospace', fontWeight: FontWeight.w500,
                              ),
                              keyboardType: const TextInputType.numberWithOptions(
                                  decimal: true, signed: true),
                              textAlign: TextAlign.right,
                              decoration: const InputDecoration(
                                isDense: true, border: InputBorder.none,
                                hintText: '0',
                                contentPadding: EdgeInsets.zero,
                              ),
                            ),
                          ),
                          const SizedBox(width: 4),
                          IconButton(
                            icon: const Icon(Icons.delete_outline, size: 16, color: _kDim),
                            visualDensity: VisualDensity.compact,
                            onPressed: () => _removeCounter(i),
                          ),
                        ],
                      ),
                    ),
                  ),
              ],
            ),
          ),
          // Version footer
          const Divider(height: 1, color: _kBorder),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 12),
            child: VersionText(
              prefix: '© ${DateTime.now().year} $kAppName · ',
              style: const TextStyle(
                fontFamily: 'monospace', fontSize: 10.5, color: _kDim, letterSpacing: 0.5,
              ),
              textAlign: TextAlign.center,
            ),
          ),
        ],
      ),
    );
  }

  // ── Tags section ─────────────────────────────────────────────────────────────

  List<String> _allTags() {
    final counts = <String, int>{};
    for (final day in _dayMeta.values) {
      for (final tag in (day['tags'] as List?)?.cast<String>() ?? <String>[]) {
        counts[tag] = (counts[tag] ?? 0) + 1;
      }
    }
    return counts.keys.toList()..sort((a, b) => counts[b]!.compareTo(counts[a]!));
  }

  int _tagCount(String tag) => _dayMeta.values
      .where((d) => ((d['tags'] as List?)?.cast<String>() ?? <String>[]).contains(tag))
      .length;

  void _renameTag(String oldName, String newName) {
    final trimmed = newName.trim();
    if (trimmed.isEmpty || trimmed == oldName) return;
    setState(() {
      for (final day in _dayMeta.values) {
        final tags = (day['tags'] as List?)?.cast<String>().toList() ?? <String>[];
        final idx = tags.indexOf(oldName);
        if (idx >= 0) { tags[idx] = trimmed; day['tags'] = tags; }
      }
    });
  }

  void _deleteTag(String name) {
    setState(() {
      for (final day in _dayMeta.values) {
        final tags = (day['tags'] as List?)?.cast<String>().toList() ?? <String>[];
        if (tags.remove(name)) day['tags'] = tags;
      }
    });
  }

  Future<void> _showRenameTagDialog(String current) async {
    final ctrl = TextEditingController(text: current);
    final result = await showDialog<String>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('Rename tag'),
        content: TextField(
          controller: ctrl,
          autofocus: true,
          decoration: const InputDecoration(hintText: 'New name'),
          onSubmitted: (v) => Navigator.of(context).pop(v),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.of(context).pop(ctrl.text), child: const Text('Rename')),
        ],
      ),
    );
    ctrl.dispose();
    if (result != null) _renameTag(current, result);
  }

  Widget _tagsSection() {
    final tags = _allTags();
    return _SectionCard(
      eyebrow: '07',
      title: 'Tags',
      subtitle: 'Rename or remove tags across all days.',
      child: tags.isEmpty
          ? Padding(
              padding: const EdgeInsets.all(22),
              child: Text(
                'No tags yet. Add tags to days from the activity panel.',
                style: const TextStyle(color: _kDim, fontSize: 13),
              ),
            )
          : Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  for (final tag in tags)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: Container(
                        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                        decoration: BoxDecoration(
                          color: _kBg,
                          border: Border.all(color: _kBorder),
                          borderRadius: BorderRadius.circular(10),
                        ),
                        child: Row(
                          children: [
                            const Icon(Icons.label_outline, size: 15, color: _kDim),
                            const SizedBox(width: 10),
                            Expanded(
                              child: Text(tag,
                                  style: const TextStyle(color: _kText2, fontSize: 13.5)),
                            ),
                            Container(
                              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                              decoration: BoxDecoration(
                                color: _kBorder,
                                borderRadius: BorderRadius.circular(20),
                              ),
                              child: Text(
                                '${_tagCount(tag)} day${_tagCount(tag) == 1 ? '' : 's'}',
                                style: const TextStyle(
                                  color: _kMuted, fontSize: 11,
                                  fontFamily: 'monospace',
                                ),
                              ),
                            ),
                            const SizedBox(width: 4),
                            IconButton(
                              icon: const Icon(Icons.edit_outlined, size: 15, color: _kDim),
                              visualDensity: VisualDensity.compact,
                              tooltip: 'Rename',
                              onPressed: () => _showRenameTagDialog(tag),
                            ),
                            IconButton(
                              icon: const Icon(Icons.delete_outline, size: 15, color: _kDim),
                              visualDensity: VisualDensity.compact,
                              tooltip: 'Delete from all days',
                              onPressed: () => _deleteTag(tag),
                            ),
                          ],
                        ),
                      ),
                    ),
                ],
              ),
            ),
    );
  }

  // ── Small helpers ────────────────────────────────────────────────────────────

  static Widget _fieldRow({required String label, String? hint, required Widget right}) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 14),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(label, style: const TextStyle(color: _kText2, fontSize: 13.5, fontWeight: FontWeight.w500)),
                if (hint != null) ...[
                  const SizedBox(height: 2),
                  Text(hint, style: const TextStyle(color: _kDim, fontSize: 12)),
                ],
              ],
            ),
          ),
          const SizedBox(width: 12),
          right,
        ],
      ),
    );
  }

  Widget _categoryDropdown(int i) {
    const categoryColors = {
      'Indoors':  (fg: Color(0xFF93C5FD), bg: Color(0x1F3B82F6), bd: Color(0x473B82F6)),
      'Outdoors': (fg: Color(0xFF86EFAC), bg: Color(0x1F22C55E), bd: Color(0x4722C55E)),
      'Other':    (fg: Color(0xFFFCD34D), bg: Color(0x1FEAB308), bd: Color(0x47EAB308)),
    };
    final c = categoryColors[_optGroups[i]] ?? categoryColors['Other']!;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: c.bg, border: Border.all(color: c.bd),
        borderRadius: BorderRadius.circular(99),
      ),
      child: DropdownButtonHideUnderline(
        child: DropdownButton<String>(
          value: _optGroups[i],
          isDense: true,
          style: TextStyle(color: c.fg, fontSize: 11, fontWeight: FontWeight.w600),
          dropdownColor: _kBgCard,
          icon: Icon(Icons.arrow_drop_down, size: 14, color: c.fg),
          items: _groups.map((g) => DropdownMenuItem(
            value: g,
            child: Text(g, style: TextStyle(color: c.fg)),
          )).toList(),
          onChanged: (v) => setState(() => _optGroups[i] = v!),
        ),
      ),
    );
  }

  static Widget _addButton(String label, VoidCallback onPressed) {
    return TextButton.icon(
      style: TextButton.styleFrom(
        foregroundColor: _kBlueActive,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        side: const BorderSide(color: Color(0x473B82F6)),
        backgroundColor: const Color(0x1A3B82F6),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
      ),
      icon: const Icon(Icons.add, size: 15),
      label: Text(label, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
      onPressed: onPressed,
    );
  }

  // ── Build ─────────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _kBg,
      body: LayoutBuilder(
        builder: (context, constraints) {
          final wide = constraints.maxWidth >= 600;
          return Row(
            children: [
              _Sidebar(
                sections: _sectionLabels,
                active: _section,
                wide: wide,
                projectName: _nameCtrl.text,
                saving: _saving,
                // A pure viewer (issue #109) can't persist any of these
                // settings — day-meta, track-style, languages, sync-meta,
                // and rename/sharing all require editor+ (or co-owner+).
                // Disable rather than let every field silently fail to save.
                canSave: _notifier.canEditContent,
                onSelect: (i) => setState(() => _section = i),
                onCancel: () => context.pop(),
                onSave: _save,
              ),
              Expanded(
                child: Column(
                  children: [
                    _StickyHeader(nameCtrl: _nameCtrl),
                    Expanded(
                      child: SingleChildScrollView(
                        padding: const EdgeInsets.all(20),
                        child: _sectionContent(),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

// ── Sidebar ────────────────────────────────────────────────────────────────────

class _Sidebar extends StatelessWidget {
  final List<({IconData icon, String label, int key})> sections;
  final int active;
  final bool wide;
  final String projectName;
  final bool saving;

  /// False for a pure viewer (issue #109) — every setting in this screen
  /// requires editor+ to persist, so Save is disabled outright rather than
  /// letting each field fail silently.
  final bool canSave;
  final void Function(int) onSelect;
  final VoidCallback onCancel;
  final VoidCallback onSave;

  const _Sidebar({
    required this.sections,
    required this.active,
    required this.wide,
    required this.projectName,
    required this.saving,
    required this.canSave,
    required this.onSelect,
    required this.onCancel,
    required this.onSave,
  });

  @override
  Widget build(BuildContext context) {
    final width = wide ? 240.0 : 64.0;
    return Container(
      width: width,
      decoration: const BoxDecoration(
        color: _kBgSidebar,
        border: Border(right: BorderSide(color: _kBorder)),
      ),
      child: Column(
        children: [
          // Header
          Container(
            padding: EdgeInsets.symmetric(horizontal: wide ? 18 : 10, vertical: 16),
            decoration: const BoxDecoration(border: Border(bottom: BorderSide(color: _kBorder))),
            child: Row(
              children: [
                _iconBtn(Icons.arrow_back, 'Back', onCancel),
                if (wide) ...[
                  const SizedBox(width: 10),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('Project settings', style: TextStyle(
                          fontFamily: 'monospace', fontSize: 10, fontWeight: FontWeight.w500,
                          color: _kDim, letterSpacing: 1.4, height: 1,
                        )),
                        const SizedBox(height: 4),
                        Text(
                          projectName,
                          style: const TextStyle(
                            fontWeight: FontWeight.w700, fontSize: 14, color: _kText1,
                            overflow: TextOverflow.ellipsis, letterSpacing: -0.2,
                          ),
                          maxLines: 1,
                        ),
                      ],
                    ),
                  ),
                ],
              ],
            ),
          ),
          // Nav
          Expanded(
            child: ListView.builder(
              padding: EdgeInsets.symmetric(vertical: 12, horizontal: wide ? 12 : 8),
              itemCount: sections.length,
              itemBuilder: (_, i) {
                final s = sections[i];
                final isActive = active == s.key;
                return Padding(
                  padding: const EdgeInsets.only(bottom: 2),
                  child: InkWell(
                    borderRadius: BorderRadius.circular(8),
                    onTap: () => onSelect(s.key),
                    child: AnimatedContainer(
                      duration: const Duration(milliseconds: 150),
                      padding: EdgeInsets.symmetric(
                        horizontal: wide ? 12 : 0,
                        vertical: wide ? 10 : 12,
                      ),
                      decoration: BoxDecoration(
                        color: isActive ? const Color(0x1A3B82F6) : Colors.transparent,
                        borderRadius: BorderRadius.circular(8),
                        border: isActive
                            ? Border.all(color: const Color(0x473B82F6))
                            : null,
                      ),
                      child: wide
                          ? Row(
                              children: [
                                Icon(s.icon, size: 20,
                                    color: isActive ? _kBlueActive : _kDim),
                                const SizedBox(width: 12),
                                Text(s.label, style: TextStyle(
                                  fontSize: 14,
                                  fontWeight: isActive ? FontWeight.w600 : FontWeight.w500,
                                  color: isActive ? _kText1 : _kMuted,
                                )),
                              ],
                            )
                          : Center(
                              child: Icon(s.icon, size: 22,
                                  color: isActive ? _kBlueActive : _kDim),
                            ),
                    ),
                  ),
                );
              },
            ),
          ),
          // Footer: Cancel + Save
          Container(
            padding: EdgeInsets.symmetric(horizontal: wide ? 12 : 8, vertical: 12),
            decoration: const BoxDecoration(border: Border(top: BorderSide(color: _kBorder))),
            child: wide
                ? Row(
                    children: [
                      Expanded(child: _cancelBtn(onCancel)),
                      const SizedBox(width: 6),
                      Expanded(child: _saveBtn(saving, canSave, onSave)),
                    ],
                  )
                : Column(
                    children: [
                      _iconBtn(Icons.close, 'Cancel', onCancel),
                      const SizedBox(height: 6),
                      _iconSaveBtn(saving, canSave, onSave),
                    ],
                  ),
          ),
        ],
      ),
    );
  }

  static Widget _iconBtn(IconData icon, String tooltip, VoidCallback onPressed) {
    return Tooltip(
      message: tooltip,
      child: Container(
        width: 34, height: 34,
        decoration: BoxDecoration(
          color: const Color(0x1A0F1A26),
          border: Border.all(color: _kBorder),
          borderRadius: BorderRadius.circular(8),
        ),
        child: IconButton(
          padding: EdgeInsets.zero,
          icon: Icon(icon, size: 18, color: _kMuted),
          onPressed: onPressed,
        ),
      ),
    );
  }

  static Widget _cancelBtn(VoidCallback onPressed) {
    return SizedBox(
      height: 38,
      child: OutlinedButton(
        style: OutlinedButton.styleFrom(
          foregroundColor: _kText2,
          side: const BorderSide(color: Color(0xFF2D4A6A)),
          padding: EdgeInsets.zero,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
        ),
        onPressed: onPressed,
        child: const Text('Cancel', style: TextStyle(fontWeight: FontWeight.w600, fontSize: 13.5)),
      ),
    );
  }

  static Widget _saveBtn(bool saving, bool canSave, VoidCallback onSave) {
    final disabled = saving || !canSave;
    return SizedBox(
      height: 38,
      child: DecoratedBox(
        decoration: BoxDecoration(
          gradient: disabled ? null : const LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [Color(0xFF60A5FA), Color(0xFF1D4ED8)],
          ),
          borderRadius: BorderRadius.circular(8),
        ),
        child: ElevatedButton(
          style: ElevatedButton.styleFrom(
            backgroundColor: disabled ? const Color(0xFF2D4A6A) : Colors.transparent,
            shadowColor: Colors.transparent,
            foregroundColor: Colors.white,
            padding: EdgeInsets.zero,
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
          ),
          onPressed: disabled ? null : onSave,
          child: saving
              ? const SizedBox(width: 16, height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
              : Text(canSave ? 'Save' : 'View only',
                  style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13.5)),
        ),
      ),
    );
  }

  static Widget _iconSaveBtn(bool saving, bool canSave, VoidCallback onSave) {
    final disabled = saving || !canSave;
    return SizedBox(
      width: 34, height: 34,
      child: DecoratedBox(
        decoration: BoxDecoration(
          gradient: disabled ? null : const LinearGradient(
            colors: [Color(0xFF60A5FA), Color(0xFF1D4ED8)],
          ),
          borderRadius: BorderRadius.circular(8),
        ),
        child: IconButton(
          padding: EdgeInsets.zero,
          icon: saving
              ? const SizedBox(width: 14, height: 14,
                  child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
              : Icon(canSave ? Icons.check : Icons.lock_outline, size: 18, color: Colors.white),
          onPressed: disabled ? null : onSave,
          tooltip: 'Save',
        ),
      ),
    );
  }
}

// ── Sticky header ──────────────────────────────────────────────────────────────

class _StickyHeader extends StatelessWidget {
  final TextEditingController nameCtrl;

  const _StickyHeader({required this.nameCtrl});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
      decoration: const BoxDecoration(
        color: Color(0xEB0A1320),
        border: Border(bottom: BorderSide(color: _kBorder)),
      ),
      child: Row(
        children: [
          // Project name
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text('PROJECT SETTINGS', style: TextStyle(
                  fontFamily: 'monospace', fontSize: 10, fontWeight: FontWeight.w600,
                  color: _kDim, letterSpacing: 1.4, height: 1,
                )),
                const SizedBox(height: 4),
                Text(
                  nameCtrl.text,
                  style: const TextStyle(
                    fontSize: 20, fontWeight: FontWeight.w700, color: _kText1,
                    letterSpacing: -0.5,
                  ),
                ),
              ],
            ),
          ),
          // Version
          const SizedBox(width: 16),
          // Header chip: the app's own version only. The pair is in this
          // screen's footer, and there is no room for it beside a project
          // name on a phone (#275).
          const Text(
            kClientVersion,
            style: TextStyle(fontFamily: 'monospace', fontSize: 10, color: _kDim),
          ),
        ],
      ),
    );
  }

}

// ── Section card wrapper ───────────────────────────────────────────────────────

class _SectionCard extends StatelessWidget {
  final String eyebrow;
  final String title;
  final String? subtitle;
  final Widget? action;
  final Widget child;

  const _SectionCard({
    required this.eyebrow,
    required this.title,
    this.subtitle,
    this.action,
    required this.child,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      decoration: BoxDecoration(
        color: _kBgCard,
        border: Border.all(color: _kBorder),
        borderRadius: BorderRadius.circular(14),
      ),
      clipBehavior: Clip.hardEdge,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            padding: const EdgeInsets.fromLTRB(22, 16, 16, 14),
            decoration: const BoxDecoration(
              border: Border(bottom: BorderSide(color: _kBorder)),
            ),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(eyebrow, style: const TextStyle(
                        fontFamily: 'monospace', fontSize: 10, fontWeight: FontWeight.w600,
                        color: _kDim, letterSpacing: 1.4, height: 1,
                      )),
                      const SizedBox(height: 4),
                      Text(title, style: const TextStyle(
                        fontSize: 17, fontWeight: FontWeight.w700, color: _kText1,
                        letterSpacing: -0.3,
                      )),
                      if (subtitle != null) ...[
                        const SizedBox(height: 3),
                        Text(subtitle!, style: const TextStyle(
                          fontSize: 13, color: _kMuted, height: 1.4,
                        )),
                      ],
                    ],
                  ),
                ),
                if (action != null) action!,
              ],
            ),
          ),
          child,
        ],
      ),
    );
  }
}

// ── Date chip (used in General section) ───────────────────────────────────────

class _DateChip extends StatelessWidget {
  final IconData icon;
  final String label;
  final bool muted;
  final VoidCallback onTap;
  final VoidCallback? onClear;

  const _DateChip({
    required this.icon,
    required this.label,
    required this.muted,
    required this.onTap,
    this.onClear,
  });

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(8),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: _kBg,
          border: Border.all(color: const Color(0xFF2D4A6A)),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 15, color: _kDim),
            const SizedBox(width: 6),
            Text(label, style: TextStyle(
              fontSize: 13.5,
              color: muted ? _kMuted : _kText2,
              fontStyle: muted ? FontStyle.italic : FontStyle.normal,
            )),
            if (onClear != null) ...[
              const SizedBox(width: 6),
              GestureDetector(
                onTap: onClear,
                child: const Icon(Icons.close, size: 14, color: _kDim),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

// ── Share link card ────────────────────────────────────────────────────────────

class _ShareLinkCard extends StatelessWidget {
  final String label;
  final String? token;
  final VoidCallback onCreateLink;
  final VoidCallback onRevoke;

  /// base64 per-share content key (issue #28), if one has been generated
  /// this session — appended as `#key=...` so the link decrypts encrypted
  /// memories for anyone who opens it.
  final String? shareKeyFragment;

  const _ShareLinkCard({
    required this.label,
    required this.token,
    required this.onCreateLink,
    required this.onRevoke,
    this.shareKeyFragment,
  });

  String _shareUrl(String t) {
    final origin = api.baseUrl.isEmpty ? Uri.base.origin : api.baseUrl;
    final base = '$origin/share/$t';
    return shareKeyFragment != null ? '$base#key=$shareKeyFragment' : base;
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: _kBg,
        border: Border.all(color: _kBorder),
        borderRadius: BorderRadius.circular(12),
      ),
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(label, style: const TextStyle(
                      fontWeight: FontWeight.w700, fontSize: 14, color: _kText1,
                    )),
                    Text(
                      label == 'Full project'
                          ? 'All days, memories and photos.'
                          : 'Map + activities only — no photos or notes.',
                      style: const TextStyle(fontSize: 12, color: _kDim),
                    ),
                  ],
                ),
              ),
              if (token != null)
                TextButton(
                  style: TextButton.styleFrom(
                    foregroundColor: _kRed,
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                    visualDensity: VisualDensity.compact,
                  ),
                  onPressed: onRevoke,
                  child: const Text('Revoke', style: TextStyle(fontWeight: FontWeight.w600)),
                ),
            ],
          ),
          const SizedBox(height: 10),
          if (token == null)
            OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                foregroundColor: _kBlueActive,
                side: const BorderSide(color: Color(0x473B82F6)),
                backgroundColor: const Color(0x1A3B82F6),
              ),
              icon: const Icon(Icons.add_link, size: 15),
              label: const Text('Create link'),
              onPressed: onCreateLink,
            )
          else ...[
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              decoration: BoxDecoration(
                color: const Color(0x220A1320),
                border: Border.all(color: _kBorder),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      _shareUrl(token!),
                      style: const TextStyle(
                        fontFamily: 'monospace', fontSize: 12, color: _kMuted,
                        overflow: TextOverflow.ellipsis,
                      ),
                      maxLines: 1,
                    ),
                  ),
                  const SizedBox(width: 8),
                  InkWell(
                    onTap: () => Clipboard.setData(ClipboardData(text: _shareUrl(token!))),
                    child: Container(
                      width: 28, height: 28,
                      decoration: BoxDecoration(
                        color: const Color(0x1A3B82F6),
                        border: Border.all(color: const Color(0x473B82F6)),
                        borderRadius: BorderRadius.circular(6),
                      ),
                      child: const Icon(Icons.content_copy, size: 14, color: _kBlueActive),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}

// ── Share content generator ─────────────────────────────────────────────────────

/// Explicit, owner-triggered action (issue #28) that re-encrypts this
/// project's encrypted memories under a fresh per-share key and uploads them,
/// so the full-project link decrypts for anyone who opens it — not automatic,
/// and not kept in sync with later edits; re-run after editing an encrypted
/// memory to update what the link reveals.
class _ShareContentGenerator extends StatelessWidget {
  final bool busy;
  final bool generated;
  final VoidCallback onGenerate;

  const _ShareContentGenerator({
    required this.busy,
    required this.generated,
    required this.onGenerate,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: const Color(0x220A1320),
        border: Border.all(color: _kBorder),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        children: [
          Icon(generated ? Icons.enhanced_encryption : Icons.lock_outline,
              size: 16, color: _kMuted),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              generated
                  ? 'Encrypted content included in this link.'
                  : 'Encrypted memories show as private in this link by default.',
              style: const TextStyle(fontSize: 12, color: _kMuted),
            ),
          ),
          const SizedBox(width: 8),
          if (busy)
            const SizedBox(
                width: 16, height: 16,
                child: CircularProgressIndicator(strokeWidth: 2))
          else
            TextButton(
              style: TextButton.styleFrom(
                foregroundColor: _kBlueActive,
                visualDensity: VisualDensity.compact,
              ),
              onPressed: onGenerate,
              child: Text(generated ? 'Regenerate' : 'Include encrypted content'),
            ),
        ],
      ),
    );
  }
}

// ── Visitor stats ──────────────────────────────────────────────────────────────

class _VisitorStats extends StatelessWidget {
  final Map<String, dynamic> visitors;
  const _VisitorStats({required this.visitors});

  static String _relativeTime(double? unixSecs) {
    if (unixSecs == null || unixSecs == 0) return 'never';
    final dt = DateTime.fromMillisecondsSinceEpoch((unixSecs * 1000).toInt());
    final diff = DateTime.now().difference(dt);
    if (diff.inMinutes < 1) return 'just now';
    if (diff.inHours < 1) return '${diff.inMinutes} min ago';
    if (diff.inDays < 1) return '${diff.inHours} h ago';
    if (diff.inDays < 7) return '${diff.inDays} day${diff.inDays == 1 ? '' : 's'} ago';
    final weeks = (diff.inDays / 7).floor();
    return '$weeks week${weeks == 1 ? '' : 's'} ago';
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _bucket('full', 'Full project visitors'),
        const SizedBox(height: 12),
        _bucket('no_memories', 'Without memories visitors'),
      ],
    );
  }

  Widget _bucket(String key, String bucketLabel) {
    final data = visitors[key] as Map<String, dynamic>? ?? {};
    final anonCount = data['anonymous_count'] as int? ?? 0;
    final registered = (data['registered'] as List? ?? [])
        .cast<Map<String, dynamic>>();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(bucketLabel, style: const TextStyle(
          fontFamily: 'monospace', fontSize: 10, fontWeight: FontWeight.w600,
          color: _kDim, letterSpacing: 1.2,
        )),
        const SizedBox(height: 4),
        Text('Anonymous: $anonCount unique visitor${anonCount == 1 ? '' : 's'}',
            style: const TextStyle(color: _kMuted, fontSize: 12)),
        if (registered.isNotEmpty) ...[
          const SizedBox(height: 4),
          for (final r in registered)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Row(
                children: [
                  CircleAvatar(
                    radius: 12,
                    backgroundColor: const Color(0xFF1D4ED8),
                    child: Text(
                      (r['display_name'] as String? ?? '').isNotEmpty
                          ? (r['display_name'] as String)[0].toUpperCase()
                          : '?',
                      style: const TextStyle(fontSize: 11, color: Colors.white),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      (r['display_name'] as String? ?? '').isNotEmpty
                          ? r['display_name'] as String
                          : r['email'] as String? ?? '',
                      style: const TextStyle(color: _kText2, fontSize: 12),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  Text(
                    _relativeTime((r['last_seen_at'] as num?)?.toDouble()),
                    style: const TextStyle(color: _kDim, fontSize: 11),
                  ),
                ],
              ),
            ),
        ],
      ],
    );
  }
}

// ── Color helper ───────────────────────────────────────────────────────────────

class _MapPanelStateColorHelper {
  static Color alternateColor(Color base) {
    final hsl = HSLColor.fromColor(base);
    return hsl
        .withSaturation((hsl.saturation * 0.42).clamp(0.0, 1.0))
        .withLightness((hsl.lightness * 1.18).clamp(0.0, 1.0))
        .toColor();
  }
}

// ── Color picker row ───────────────────────────────────────────────────────────

class _ColorPickerRow extends StatelessWidget {
  final String label;
  final Color color;
  final String? badge; // e.g. "auto"
  final VoidCallback onTap;
  final VoidCallback? onClear;

  const _ColorPickerRow({
    required this.label,
    required this.color,
    required this.onTap,
    this.badge,
    this.onClear,
  });

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(
          child: Text(
            label,
            style: const TextStyle(
              fontFamily: 'monospace', fontSize: 10, fontWeight: FontWeight.w600,
              color: _kDim, letterSpacing: 1.4,
            ),
          ),
        ),
        if (badge != null)
          Container(
            margin: const EdgeInsets.only(right: 8),
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
            decoration: BoxDecoration(
              color: const Color(0xFF1F2F42),
              borderRadius: BorderRadius.circular(4),
            ),
            child: Text(badge!,
                style: const TextStyle(fontSize: 10, color: _kMuted)),
          ),
        if (onClear != null)
          IconButton(
            icon: const Icon(Icons.refresh, size: 16, color: _kMuted),
            tooltip: 'Reset to auto',
            visualDensity: VisualDensity.compact,
            onPressed: onClear,
          ),
        GestureDetector(
          onTap: onTap,
          child: Container(
            width: 36, height: 36,
            decoration: BoxDecoration(
              color: color,
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: Colors.white24, width: 1.5),
              boxShadow: [BoxShadow(color: color.withAlpha(100), blurRadius: 6)],
            ),
            child: const Icon(Icons.colorize, size: 16, color: Colors.white70),
          ),
        ),
      ],
    );
  }
}
