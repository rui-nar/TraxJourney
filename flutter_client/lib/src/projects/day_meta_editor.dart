import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/design_tokens.dart';
import 'project_notifier.dart';

// ── Chip data ──────────────────────────────────────────────────────────────
// "Not set" is now its own dashed chip (see _EDNotSetChip), so the value
// lists below hold only the real options.

const _difficultyOptions = [
  ('easy',       'Easy'),
  ('normal',     'Normal'),
  ('hard',       'Hard'),
  ('super_hard', 'Super hard'),
];

const _weekdayShort = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const _monthLong = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

// ── Public launchers ───────────────────────────────────────────────────────

void showDayMetaDialog(
  BuildContext context,
  ProjectNotifier notifier,
  String dateKey, {
  List<String> orderedDateKeys = const [],
  bool countersOnly = false,
}) {
  showDialog(
    context: context,
    useRootNavigator: true,
    barrierColor: Colors.black.withValues(alpha: 0.45),
    builder: (_) => _DayMetaDialogWrapper(
      notifier: notifier,
      dateKey: dateKey,
      orderedDateKeys: orderedDateKeys,
      countersOnly: countersOnly,
    ),
  );
}

void showDayMetaSheet(
  BuildContext context,
  ProjectNotifier notifier,
  String dateKey, {
  List<String> orderedDateKeys = const [],
  bool countersOnly = false,
}) {
  showModalBottomSheet(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.transparent,
    builder: (_) => _DayMetaSheetWrapper(
      notifier: notifier,
      dateKey: dateKey,
      orderedDateKeys: orderedDateKeys,
      countersOnly: countersOnly,
    ),
  );
}

// ── Derived hero data for a given day ───────────────────────────────────────

class _DayContext {
  final int dayNumber;
  final int totalDays;
  final DateTime date;
  final List<String> effectiveTags;
  final double distanceKm;
  final double elevationM;

  const _DayContext({
    required this.dayNumber,
    required this.totalDays,
    required this.date,
    required this.effectiveTags,
    required this.distanceKm,
    required this.elevationM,
  });

  factory _DayContext.from(
    ProjectNotifier notifier,
    String dateKey,
    List<String> orderedDateKeys,
  ) {
    final stats = notifier.itemsFacet.dayStats(dateKey);
    final n = dayTripNumbering(dateKey, orderedDateKeys, notifier.itemsFacet.tripStart);
    return _DayContext(
      dayNumber: n.dayNumber,
      totalDays: n.totalDays,
      date: DateTime.tryParse(dateKey) ?? DateTime.now(),
      effectiveTags: notifier.itemsFacet.effectiveTagsFor(dateKey),
      distanceKm: stats.distanceKm,
      elevationM: stats.elevationM,
    );
  }
}

// ── Dialog wrapper — stateful for prev/next navigation ─────────────────────

class _DayMetaDialogWrapper extends StatefulWidget {
  final ProjectNotifier notifier;
  final String dateKey;
  final List<String> orderedDateKeys;
  final bool countersOnly;

  const _DayMetaDialogWrapper({
    required this.notifier,
    required this.dateKey,
    this.orderedDateKeys = const [],
    this.countersOnly = false,
  });

  @override
  State<_DayMetaDialogWrapper> createState() => _DayMetaDialogWrapperState();
}

class _DayMetaDialogWrapperState extends State<_DayMetaDialogWrapper> {
  late String _currentDateKey;
  final _editorKey = GlobalKey<_DayMetaEditorState>();

  @override
  void initState() {
    super.initState();
    _currentDateKey = widget.dateKey;
  }

  int get _currentIdx => widget.orderedDateKeys.indexOf(_currentDateKey);
  bool get _hasPrev => _currentIdx > 0;
  bool get _hasNext => _currentIdx < widget.orderedDateKeys.length - 1;

  Future<void> _navigate(int delta) async {
    if (!await _guardUnsaved(_editorKey, context, widget.notifier, _currentDateKey)) {
      return;
    }
    final nextIdx = _currentIdx + delta;
    if (nextIdx < 0 || nextIdx >= widget.orderedDateKeys.length) return;
    setState(() => _currentDateKey = widget.orderedDateKeys[nextIdx]);
  }

  @override
  Widget build(BuildContext context) {
    final hasNav = widget.orderedDateKeys.length > 1;
    final size = MediaQuery.of(context).size;
    final ctx = _DayContext.from(widget.notifier, _currentDateKey, widget.orderedDateKeys);

    return Dialog(
      clipBehavior: Clip.antiAlias,
      insetPadding: const EdgeInsets.all(16),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      child: GestureDetector(
        onHorizontalDragEnd: hasNav
            ? (details) {
                final v = details.primaryVelocity ?? 0;
                if (v < -300) _navigate(1);
                if (v > 300) _navigate(-1);
              }
            : null,
        child: ConstrainedBox(
          constraints: BoxConstraints(
            maxWidth: 560,
            maxHeight: (size.height - 80).clamp(360.0, 820.0),
          ),
          child: DayMetaEditor(
            key: _editorKey,
            dateKey: _currentDateKey,
            initialMeta: widget.notifier.itemsFacet.dayMeta[_currentDateKey] ?? {},
            sleepingOptions: widget.notifier.itemsFacet.sleepingOptions,
            sleepingOptionGroups: widget.notifier.itemsFacet.sleepingOptionGroups,
            availableTags: widget.notifier.itemsFacet.availableTags,
            counters: widget.notifier.itemsFacet.counters,
            countersOnly: widget.countersOnly,
            dayNumber: ctx.dayNumber,
            totalDays: ctx.totalDays,
            date: ctx.date,
            effectiveTags: ctx.effectiveTags,
            distanceKm: ctx.distanceKm,
            elevationM: ctx.elevationM,
            hasPrev: hasNav && _hasPrev,
            hasNext: hasNav && _hasNext,
            onPrev: hasNav && _hasPrev ? () => _navigate(-1) : null,
            onNext: hasNav && _hasNext ? () => _navigate(1) : null,
            onSaveOnly: (meta) => _persist(widget.notifier, _currentDateKey, meta),
            onSave: (meta) {
              _persist(widget.notifier, _currentDateKey, meta);
              Navigator.of(context, rootNavigator: true).pop();
            },
            onCancel: () => Navigator.of(context, rootNavigator: true).pop(),
          ),
        ),
      ),
    );
  }
}

// ── Bottom-sheet wrapper ───────────────────────────────────────────────────

class _DayMetaSheetWrapper extends StatefulWidget {
  final ProjectNotifier notifier;
  final String dateKey;
  final List<String> orderedDateKeys;
  final bool countersOnly;

  const _DayMetaSheetWrapper({
    required this.notifier,
    required this.dateKey,
    this.orderedDateKeys = const [],
    this.countersOnly = false,
  });

  @override
  State<_DayMetaSheetWrapper> createState() => _DayMetaSheetWrapperState();
}

class _DayMetaSheetWrapperState extends State<_DayMetaSheetWrapper> {
  late String _currentDateKey;
  final _editorKey = GlobalKey<_DayMetaEditorState>();

  @override
  void initState() {
    super.initState();
    _currentDateKey = widget.dateKey;
  }

  int get _currentIdx => widget.orderedDateKeys.indexOf(_currentDateKey);
  bool get _hasPrev => _currentIdx > 0;
  bool get _hasNext => _currentIdx < widget.orderedDateKeys.length - 1;
  bool get _hasNav => widget.orderedDateKeys.length > 1;

  Future<void> _navigate(int delta) async {
    if (!await _guardUnsaved(_editorKey, context, widget.notifier, _currentDateKey)) {
      return;
    }
    final nextIdx = _currentIdx + delta;
    if (nextIdx < 0 || nextIdx >= widget.orderedDateKeys.length) return;
    setState(() => _currentDateKey = widget.orderedDateKeys[nextIdx]);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final ctx = _DayContext.from(widget.notifier, _currentDateKey, widget.orderedDateKeys);

    return DraggableScrollableSheet(
      initialChildSize: 0.85,
      minChildSize: 0.5,
      maxChildSize: 0.96,
      expand: false,
      builder: (sheetCtx, sc) => Padding(
        padding: EdgeInsets.only(bottom: MediaQuery.of(sheetCtx).viewInsets.bottom),
        child: Material(
          color: theme.colorScheme.surface,
          clipBehavior: Clip.antiAlias,
          shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
          ),
          child: GestureDetector(
            onHorizontalDragEnd: _hasNav
                ? (details) {
                    final v = details.primaryVelocity ?? 0;
                    if (v < -300) _navigate(1);
                    if (v > 300) _navigate(-1);
                  }
                : null,
            child: DayMetaEditor(
              key: _editorKey,
              dateKey: _currentDateKey,
              initialMeta: widget.notifier.itemsFacet.dayMeta[_currentDateKey] ?? {},
              sleepingOptions: widget.notifier.itemsFacet.sleepingOptions,
              sleepingOptionGroups: widget.notifier.itemsFacet.sleepingOptionGroups,
              availableTags: widget.notifier.itemsFacet.availableTags,
              counters: widget.notifier.itemsFacet.counters,
              countersOnly: widget.countersOnly,
              dayNumber: ctx.dayNumber,
              totalDays: ctx.totalDays,
              date: ctx.date,
              effectiveTags: ctx.effectiveTags,
              distanceKm: ctx.distanceKm,
              elevationM: ctx.elevationM,
              hasPrev: _hasNav && _hasPrev,
              hasNext: _hasNav && _hasNext,
              onPrev: _hasNav && _hasPrev ? () => _navigate(-1) : null,
              onNext: _hasNav && _hasNext ? () => _navigate(1) : null,
              scrollController: sc,
              onSaveOnly: (meta) => _persist(widget.notifier, _currentDateKey, meta),
              onSave: (meta) {
                _persist(widget.notifier, _currentDateKey, meta);
                Navigator.of(sheetCtx).pop();
              },
              onCancel: () => Navigator.of(sheetCtx).pop(),
            ),
          ),
        ),
      ),
    );
  }
}

// ── Shared helpers ─────────────────────────────────────────────────────────

/// Prompts to save/discard when the editor is dirty before navigating away.
/// Returns true if navigation may proceed.
Future<bool> _guardUnsaved(
  GlobalKey<_DayMetaEditorState> editorKey,
  BuildContext context,
  ProjectNotifier notifier,
  String dateKey,
) async {
  if (!(editorKey.currentState?._dirty ?? false)) return true;
  final result = await showDialog<String>(
    context: context,
    useRootNavigator: true,
    builder: (ctx) => AlertDialog(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      title: Row(
        children: [
          Icon(Icons.save_outlined,
              size: 22, color: Theme.of(ctx).colorScheme.primary),
          const SizedBox(width: 10),
          const Text('Unsaved changes'),
        ],
      ),
      content: const Text('Save changes to this day before navigating?'),
      actionsAlignment: MainAxisAlignment.spaceBetween,
      actions: [
        TextButton(
          onPressed: () => Navigator.of(ctx).pop('discard'),
          style: TextButton.styleFrom(
              foregroundColor: Theme.of(ctx).colorScheme.error),
          child: const Text('Discard'),
        ),
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop('cancel'),
              child: const Text('Cancel'),
            ),
            const SizedBox(width: 8),
            FilledButton(
              onPressed: () => Navigator.of(ctx).pop('save'),
              child: const Text('Save'),
            ),
          ],
        ),
      ],
    ),
  );
  if (result == null || result == 'cancel') return false;
  if (result == 'save') {
    final meta = editorKey.currentState?._buildMeta() ?? {};
    _persist(notifier, dateKey, meta);
  }
  return true;
}

void _persist(
  ProjectNotifier notifier,
  String dateKey,
  Map<String, dynamic> meta,
) {
  if (meta.isEmpty) {
    notifier.saveDayMeta(delete: [dateKey]);
  } else {
    notifier.saveDayMeta(days: {dateKey: meta});
  }
}

// ── Editor widget ──────────────────────────────────────────────────────────

class DayMetaEditor extends StatefulWidget {
  final String dateKey;
  final Map<String, dynamic> initialMeta;
  final List<String> sleepingOptions;
  final Map<String, String> sleepingOptionGroups;
  final List<String> availableTags;
  final List<Map<String, dynamic>> counters;

  /// When true, the body shows only the counters section (the hero, footer and
  /// day navigation are kept). Used by the add-FAB's "add counter to a day"
  /// shortcut, which reuses this editor rather than duplicating it.
  final bool countersOnly;

  // Hero context
  final int dayNumber;
  final int totalDays;
  final DateTime date;
  final List<String> effectiveTags;
  final double distanceKm;
  final double elevationM;

  // Navigation
  final bool hasPrev;
  final bool hasNext;
  final VoidCallback? onPrev;
  final VoidCallback? onNext;

  // The body scroll uses this when hosted in a draggable sheet.
  final ScrollController? scrollController;

  // Actions
  final void Function(Map<String, dynamic> meta) onSave;
  final void Function(Map<String, dynamic> meta)? onSaveOnly;
  final VoidCallback? onCancel;

  const DayMetaEditor({
    super.key,
    required this.dateKey,
    required this.initialMeta,
    required this.sleepingOptions,
    this.sleepingOptionGroups = const {},
    this.availableTags = const [],
    this.counters = const [],
    this.countersOnly = false,
    this.dayNumber = 1,
    this.totalDays = 1,
    required this.date,
    this.effectiveTags = const [],
    this.distanceKm = 0,
    this.elevationM = 0,
    this.hasPrev = false,
    this.hasNext = false,
    this.onPrev,
    this.onNext,
    this.scrollController,
    required this.onSave,
    this.onSaveOnly,
    this.onCancel,
  });

  @override
  State<DayMetaEditor> createState() => _DayMetaEditorState();
}

class _DayMetaEditorState extends State<DayMetaEditor> {
  String? _difficulty;
  String? _sleeping;
  String? _weather;
  late TextEditingController _journalCtrl;
  late List<String> _tags;
  // Whether the loaded day already owned an explicit 'tags' entry (even an
  // empty one) and whether the user has touched the Tags section this
  // session — either one means Save must persist 'tags' even if it's now
  // empty, so a day cleared down to zero tags stays cleared instead of
  // silently falling back to inheriting an earlier day's tags (issue #203).
  bool _hasOwnTagsInitially = false;
  bool _tagsTouched = false;
  final TextEditingController _tagInputCtrl = TextEditingController();
  // counter name → amount (TextEditingController for the amount field)
  late List<({String name, double value})> _counterMods;
  bool _dirty = false;

  @override
  void initState() {
    super.initState();
    _journalCtrl = TextEditingController();
    _applyMeta(widget.initialMeta);
    _journalCtrl.addListener(_onJournalChanged);
  }

  @override
  void didUpdateWidget(DayMetaEditor oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.dateKey != widget.dateKey) {
      setState(() {
        _applyMeta(widget.initialMeta);
        _dirty = false;
      });
    }
  }

  void _onJournalChanged() {
    setState(() => _dirty = true); // also refreshes the word-count hint
  }

  void _applyMeta(Map<String, dynamic> m) {
    _difficulty = m['difficulty'] as String?;
    _sleeping   = m['sleeping']   as String?;
    _weather    = m['weather']    as String?;
    _journalCtrl.text = m['journal'] as String? ?? '';
    final rawTags = m['tags'];
    _hasOwnTagsInitially = rawTags is List;
    _tagsTouched = false;
    // No own tags yet → pre-select the inherited (previous-day) tags so the
    // chips reflect what the hero already shows. Not persisted until Save.
    _tags = rawTags is List
        ? List<String>.from(rawTags.cast<String>())
        : List<String>.from(widget.effectiveTags);
    final rawC = m['counters'];
    if (rawC is List) {
      // Current form: a list of {name, value} entries — same counter may repeat.
      _counterMods = rawC
          .whereType<Map>()
          .map((e) => (name: e['name'] as String, value: (e['value'] as num).toDouble()))
          .toList();
    } else if (rawC is Map) {
      // Legacy form: a {name: value} map.
      _counterMods = rawC.entries
          .map((e) => (name: e.key as String, value: (e.value as num).toDouble()))
          .toList();
    } else {
      _counterMods = [];
    }
  }

  @override
  void dispose() {
    _journalCtrl.removeListener(_onJournalChanged);
    _journalCtrl.dispose();
    _tagInputCtrl.dispose();
    super.dispose();
  }

  void _markDirty(VoidCallback fn) => setState(() {
        fn();
        _dirty = true;
      });

  void _addTag(String tag) {
    final t = tag.trim();
    if (t.isEmpty || _tags.contains(t)) return;
    _markDirty(() {
      _tagsTouched = true;
      _tags.add(t);
      _tagInputCtrl.clear();
    });
  }

  void _toggleTag(String tag) => _markDirty(() {
        _tagsTouched = true;
        if (_tags.contains(tag)) {
          _tags.remove(tag);
        } else {
          _tags.add(tag);
        }
      });

  void _removeTagChip(String tag) => _markDirty(() {
        _tagsTouched = true;
        _tags.remove(tag);
      });

  void _setCounter(int i, double v) =>
      _markDirty(() => _counterMods[i] = (name: _counterMods[i].name, value: v));

  void _removeCounter(int i) => _markDirty(() => _counterMods.removeAt(i));

  List<String> get _definedCounterNames =>
      widget.counters.map((c) => c['name'] as String).toList();

  void _addCounterNamed(String name) =>
      _markDirty(() => _counterMods.add((name: name, value: 1)));

  Map<String, dynamic> _buildMeta() {
    final result = <String, dynamic>{};
    if (_difficulty != null) result['difficulty'] = _difficulty;
    if (_sleeping   != null) result['sleeping']   = _sleeping;
    if (_weather    != null) result['weather']    = _weather;
    final j = _journalCtrl.text.trim();
    if (j.isNotEmpty) result['journal'] = j;
    // Emit 'tags' even when empty once the day owns (or now owns) an explicit
    // tag set, so an explicit "no tags" doesn't get mistaken for "no tag data"
    // and silently re-inherit an earlier day's tags on reload (issue #203).
    if (_tags.isNotEmpty || _hasOwnTagsInitially || _tagsTouched) {
      result['tags'] = List<String>.from(_tags);
    }
    if (_counterMods.isNotEmpty) {
      result['counters'] = [
        for (final m in _counterMods) {'name': m.name, 'value': m.value},
      ];
    }
    return result;
  }

  // ── Build ────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Material(
      color: theme.colorScheme.surface,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          _buildHeader(context),
          Flexible(
            child: SingleChildScrollView(
              controller: widget.scrollController,
              padding: const EdgeInsets.fromLTRB(20, 18, 20, 16),
              child: _buildBody(context),
            ),
          ),
          _buildFooter(context),
        ],
      ),
    );
  }

  // ── Header (hero) ──────────────────────────────────────────────────────────

  Widget _buildHeader(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final d = widget.date;
    final dayShort = _weekdayShort[(d.weekday - 1).clamp(0, 6)];
    final dayLong = '${_monthLong[(d.month - 1).clamp(0, 11)]} ${d.day}, ${d.year}';
    final hasStats = widget.distanceKm > 0 || widget.elevationM > 0;
    final progress =
        widget.totalDays > 0 ? (widget.dayNumber / widget.totalDays).clamp(0.0, 1.0) : 0.0;

    return Container(
      padding: const EdgeInsets.fromLTRB(20, 14, 12, 16),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: cs.outlineVariant)),
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [cs.surface, Color.alphaBlend(cs.primary.withValues(alpha: 0.06), cs.surface)],
        ),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // eyebrow row — prev/next + label + ISO + close
          Row(
            children: [
              _EDIconBtn(icon: Icons.chevron_left, tooltip: 'Previous day', onTap: widget.onPrev),
              Container(
                padding: const EdgeInsets.fromLTRB(8, 5, 10, 5),
                decoration: BoxDecoration(
                  color: cs.primary.withValues(alpha: 0.10),
                  borderRadius: BorderRadius.circular(999),
                  border: Border.all(color: cs.primary.withValues(alpha: 0.30)),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.edit_note, size: 14, color: cs.primary),
                    const SizedBox(width: 6),
                    Text('EDIT DAY',
                        style: monoStyle(
                          fontSize: 10.5, fontWeight: FontWeight.w600,
                          color: cs.primary, letterSpacing: 1.4,
                        )),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              Text(widget.dateKey,
                  style: monoStyle(
                    fontSize: 11, fontWeight: FontWeight.w500,
                    color: cs.onSurfaceVariant,
                  )),
              const Spacer(),
              _EDIconBtn(icon: Icons.chevron_right, tooltip: 'Next day', onTap: widget.onNext),
              Container(width: 1, height: 22, color: cs.outlineVariant,
                  margin: const EdgeInsets.symmetric(horizontal: 4)),
              _EDIconBtn(icon: Icons.close, tooltip: 'Close', onTap: widget.onCancel),
            ],
          ),
          const SizedBox(height: 12),
          // hero — big mono number + date + tags + stats
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text('DAY',
                      style: monoStyle(
                        fontSize: 10.5, fontWeight: FontWeight.w600,
                        color: cs.onSurfaceVariant, letterSpacing: 1.8,
                      )),
                  Text('${widget.dayNumber}',
                      style: monoStyle(
                        fontSize: 52, fontWeight: FontWeight.w500,
                        color: cs.onSurface, letterSpacing: -2, height: 0.95,
                      )),
                ],
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text('$dayShort · $dayLong',
                        maxLines: 2, overflow: TextOverflow.ellipsis,
                        style: Theme.of(context).textTheme.titleMedium?.copyWith(
                            fontWeight: FontWeight.w700, height: 1.15)),
                    const SizedBox(height: 4),
                    _buildHeroTags(context),
                  ],
                ),
              ),
              if (hasStats) ...[
                const SizedBox(width: 10),
                if (widget.distanceKm > 0)
                  _EDStat(label: 'DIST', value: widget.distanceKm.round().toString(), unit: 'km'),
                if (widget.elevationM > 0) ...[
                  const SizedBox(width: 14),
                  _EDStat(label: 'CLIMB', value: widget.elevationM.round().toString(), unit: 'm'),
                ],
              ],
            ],
          ),
          const SizedBox(height: 14),
          // progress
          Row(
            children: [
              Expanded(
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(999),
                  child: LinearProgressIndicator(
                    value: progress,
                    minHeight: 4,
                    backgroundColor: cs.surfaceContainerHighest,
                    valueColor: AlwaysStoppedAnimation(cs.primary),
                  ),
                ),
              ),
              const SizedBox(width: 10),
              Text('${widget.dayNumber} / ${widget.totalDays}',
                  style: monoStyle(
                    fontSize: 11, fontWeight: FontWeight.w500,
                    color: cs.onSurfaceVariant,
                  )),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildHeroTags(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    if (widget.effectiveTags.isEmpty) {
      return Text('No activity recorded yet.',
          style: Theme.of(context).textTheme.bodySmall?.copyWith(
              color: cs.onSurfaceVariant, fontStyle: FontStyle.italic));
    }
    return Wrap(
      spacing: 4,
      runSpacing: 4,
      children: [
        for (final tag in widget.effectiveTags)
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
            decoration: BoxDecoration(
              color: cs.secondaryContainer,
              borderRadius: BorderRadius.circular(6),
            ),
            child: Text(tag,
                style: Theme.of(context).textTheme.labelSmall?.copyWith(
                    color: cs.onSecondaryContainer)),
          ),
      ],
    );
  }

  // ── Body ───────────────────────────────────────────────────────────────────

  Widget _buildBody(BuildContext context) {
    if (widget.countersOnly) {
      // No counters defined for the project → the section would collapse to
      // nothing, so guide the user to define one first.
      if (widget.counters.isEmpty) return _buildNoCountersDefined(context);
      return _buildCounters(context);
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        _EDChipGroup(
          label: 'Difficulty',
          options: [for (final (v, l) in _difficultyOptions) (v, l, null)],
          value: _difficulty,
          onChanged: (v) => _markDirty(() => _difficulty = v),
        ),
        _EDChipGroup(
          label: 'Sleeping',
          options: [
            for (final opt in widget.sleepingOptions)
              (opt, opt, sleepingGroupColor(widget.sleepingOptionGroups[opt])),
          ],
          value: widget.sleepingOptions.contains(_sleeping) ? _sleeping : null,
          onChanged: (v) => _markDirty(() => _sleeping = v),
        ),
        _buildTags(context),
        _buildCounters(context),
        _buildJournal(context),
      ],
    );
  }

  Widget _buildTags(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final allTags = {...widget.availableTags, ..._tags}.toList()..sort();
    return _EDSection(
      label: 'Tags',
      hint: '${_tags.length} selected',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              for (final tag in allTags)
                _EDChip(
                  label: tag,
                  active: _tags.contains(tag),
                  leadingCheck: _tags.contains(tag),
                  removable: _tags.contains(tag),
                  onRemove: () => _removeTagChip(tag),
                  onTap: () => _toggleTag(tag),
                ),
            ],
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: Container(
                  height: 38,
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  decoration: BoxDecoration(
                    color: cs.surfaceContainerHighest,
                    borderRadius: BorderRadius.circular(999),
                    border: Border.all(color: cs.outlineVariant),
                  ),
                  child: Row(
                    children: [
                      Icon(Icons.tag, size: 16, color: cs.onSurfaceVariant),
                      const SizedBox(width: 6),
                      Expanded(
                        child: TextField(
                          controller: _tagInputCtrl,
                          decoration: const InputDecoration(
                            isDense: true,
                            border: InputBorder.none,
                            hintText: 'New tag…',
                          ),
                          onSubmitted: _addTag,
                          textInputAction: TextInputAction.done,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(width: 6),
              FilledButton.icon(
                onPressed: () => _addTag(_tagInputCtrl.text),
                icon: const Icon(Icons.add, size: 16),
                label: const Text('Add'),
                style: FilledButton.styleFrom(
                  minimumSize: const Size(0, 38),
                  shape: const StadiumBorder(),
                  padding: const EdgeInsets.symmetric(horizontal: 14),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildJournal(BuildContext context) {
    final words = _journalCtrl.text.trim().split(RegExp(r'\s+')).where((w) => w.isNotEmpty).length;
    return _EDSection(
      label: 'Journal',
      hint: words > 0 ? '$words words' : null,
      child: TextField(
        controller: _journalCtrl,
        maxLines: null,
        minLines: 4,
        textInputAction: TextInputAction.newline,
        decoration: const InputDecoration(
          border: OutlineInputBorder(),
          hintText: 'Notes, highlights, thoughts…',
          contentPadding: EdgeInsets.all(12),
        ),
      ),
    );
  }

  Widget _buildNoCountersDefined(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return _EDSection(
      label: 'Counters',
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          border: Border.all(color: cs.outlineVariant),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Text(
          'This project has no counters yet. Add a counter in project '
          'settings, then you can record its value per day here.',
          textAlign: TextAlign.center,
          style: Theme.of(context).textTheme.bodySmall
              ?.copyWith(color: cs.onSurfaceVariant),
        ),
      ),
    );
  }

  Widget _buildCounters(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    // A counter may be logged several times per day, so it stays addable as
    // long as the project defines at least one counter.
    final canAdd = widget.counters.isNotEmpty;
    if (widget.counters.isEmpty && _counterMods.isEmpty) {
      return const SizedBox.shrink();
    }
    return _EDSection(
      label: 'Counters',
      hint: '${_counterMods.length} active',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (int i = 0; i < _counterMods.length; i++)
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: _EDCounterRow(
                key: ValueKey('ctr_row_$i'),
                index: i,
                name: _counterMods[i].name,
                value: _counterMods[i].value,
                onChanged: (v) => _setCounter(i, v),
                onRemove: () => _removeCounter(i),
              ),
            ),
          if (_counterMods.isEmpty)
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                border: Border.all(color: cs.outlineVariant, style: BorderStyle.solid),
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text('No counters yet for this day.',
                  textAlign: TextAlign.center,
                  style: Theme.of(context).textTheme.bodySmall
                      ?.copyWith(color: cs.onSurfaceVariant)),
            ),
          if (canAdd)
            Align(
              alignment: Alignment.centerLeft,
              child: Padding(
                padding: const EdgeInsets.only(top: 2),
                child: PopupMenuButton<String>(
                  tooltip: 'Add counter',
                  position: PopupMenuPosition.under,
                  onSelected: _addCounterNamed,
                  itemBuilder: (_) => [
                    for (final name in _definedCounterNames)
                      PopupMenuItem<String>(value: name, child: Text(name)),
                  ],
                  child: Container(
                    padding: const EdgeInsets.fromLTRB(12, 8, 14, 8),
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(999),
                      border: Border.all(color: cs.outline),
                    ),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(Icons.add, size: 16, color: cs.onSurface),
                        const SizedBox(width: 6),
                        Text('Add counter',
                            style: TextStyle(
                                fontSize: 13.5, color: cs.onSurface)),
                        Icon(Icons.arrow_drop_down, size: 18,
                            color: cs.onSurfaceVariant),
                      ],
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }

  // ── Footer ───────────────────────────────────────────────────────────────

  Widget _buildFooter(BuildContext context) {
    final theme = Theme.of(context);
    final cs = theme.colorScheme;
    final isEmpty = _buildMeta().isEmpty;
    final (statusIcon, statusText) = _dirty
        ? (Icons.circle, 'Unsaved changes')
        : isEmpty
            ? (Icons.circle_outlined, 'Nothing to save yet')
            : (Icons.check_circle_outline, 'All changes saved');

    return Container(
      padding: const EdgeInsets.fromLTRB(20, 12, 16, 12),
      decoration: BoxDecoration(
        color: cs.surfaceContainerLow,
        border: Border(top: BorderSide(color: cs.outlineVariant)),
      ),
      child: Row(
        children: [
          Icon(statusIcon, size: 12,
              color: _dirty ? cs.primary : cs.onSurfaceVariant),
          const SizedBox(width: 6),
          Flexible(
            child: Text(statusText,
                overflow: TextOverflow.ellipsis,
                style: monoStyle(
                  fontSize: 11, fontWeight: FontWeight.w500,
                  color: cs.onSurfaceVariant,
                )),
          ),
          const Spacer(),
          TextButton(
            onPressed: widget.onCancel,
            child: const Text('Close'),
          ),
          const SizedBox(width: 8),
          _SaveButton(
            enabled: _dirty,
            onPressed: () => widget.onSave(_buildMeta()),
          ),
        ],
      ),
    );
  }
}

// ── Save button (metallic) ──────────────────────────────────────────────────

class _SaveButton extends StatelessWidget {
  final bool enabled;
  final VoidCallback onPressed;

  const _SaveButton({required this.enabled, required this.onPressed});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final cs = theme.colorScheme;
    return DecoratedBox(
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(8),
        gradient: enabled ? metallicBlue(theme.brightness) : null,
        color: enabled ? null : cs.surfaceContainerHighest,
      ),
      child: TextButton.icon(
        onPressed: enabled ? onPressed : null,
        icon: Icon(Icons.check, size: 17,
            color: enabled ? Colors.white : cs.onSurfaceVariant),
        label: Text('Save day',
            style: TextStyle(
                color: enabled ? Colors.white : cs.onSurfaceVariant,
                fontWeight: FontWeight.w600)),
        style: TextButton.styleFrom(
          minimumSize: const Size(0, 36),
          padding: const EdgeInsets.symmetric(horizontal: 16),
        ),
      ),
    );
  }
}

// ── Small primitives ────────────────────────────────────────────────────────

class _EDIconBtn extends StatelessWidget {
  final IconData icon;
  final String tooltip;
  final VoidCallback? onTap;

  const _EDIconBtn({required this.icon, required this.tooltip, this.onTap});

  @override
  Widget build(BuildContext context) {
    return IconButton(
      icon: Icon(icon, size: 20),
      tooltip: tooltip,
      onPressed: onTap,
      visualDensity: VisualDensity.compact,
      color: Theme.of(context).colorScheme.onSurfaceVariant,
      constraints: const BoxConstraints(minWidth: 34, minHeight: 34),
      padding: EdgeInsets.zero,
    );
  }
}

class _EDStat extends StatelessWidget {
  final String label;
  final String value;
  final String unit;

  const _EDStat({required this.label, required this.value, required this.unit});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.end,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(label,
            style: monoStyle(
              fontSize: 10, fontWeight: FontWeight.w600,
              color: cs.onSurfaceVariant, letterSpacing: 1.4,
            )),
        Row(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.baseline,
          textBaseline: TextBaseline.alphabetic,
          children: [
            Text(value,
                style: monoStyle(
                  fontSize: 15, fontWeight: FontWeight.w600, color: cs.onSurface,
                )),
            const SizedBox(width: 1),
            Text(unit, style: monoStyle(fontSize: 10, color: cs.onSurfaceVariant)),
          ],
        ),
      ],
    );
  }
}

/// Section with a mono uppercase label + optional hint, then arbitrary child.
class _EDSection extends StatelessWidget {
  final String label;
  final String? hint;
  final Widget child;

  const _EDSection({required this.label, this.hint, required this.child});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 22),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _EDSectionLabel(label: label, hint: hint),
          const SizedBox(height: 10),
          child,
        ],
      ),
    );
  }
}

class _EDSectionLabel extends StatelessWidget {
  final String label;
  final String? hint;

  const _EDSectionLabel({required this.label, this.hint});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.baseline,
      textBaseline: TextBaseline.alphabetic,
      children: [
        Text(label.toUpperCase(),
            style: monoStyle(
              fontSize: 10.5, fontWeight: FontWeight.w600,
              color: cs.onSurfaceVariant, letterSpacing: 1.6,
            )),
        if (hint != null) ...[
          const SizedBox(width: 10),
          Text(hint!,
              style: monoStyle(
                fontSize: 10.5,
                color: cs.onSurfaceVariant.withValues(alpha: 0.7),
              )),
        ],
      ],
    );
  }
}

/// A chip group with a leading dashed "Not set" chip then the options.
/// Each option is (value, label, dotColor?).
class _EDChipGroup extends StatelessWidget {
  final String label;
  final List<(String, String, Color?)> options;
  final String? value;
  final ValueChanged<String?> onChanged;

  const _EDChipGroup({
    required this.label,
    required this.options,
    required this.value,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    return _EDSection(
      label: label,
      child: Wrap(
        spacing: 6,
        runSpacing: 6,
        children: [
          _EDNotSetChip(active: value == null, onTap: () => onChanged(null)),
          for (final (val, lbl, dot) in options)
            _EDChip(
              label: lbl,
              dot: dot,
              active: value == val,
              onTap: () => onChanged(val),
            ),
        ],
      ),
    );
  }
}

class _EDChip extends StatelessWidget {
  final String label;
  final bool active;
  final Color? dot;
  final bool leadingCheck;
  final bool removable;
  final VoidCallback onTap;
  final VoidCallback? onRemove;

  const _EDChip({
    required this.label,
    required this.active,
    this.dot,
    this.leadingCheck = false,
    this.removable = false,
    required this.onTap,
    this.onRemove,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final fg = active ? cs.primary : cs.onSurfaceVariant;
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(999),
      child: Container(
        height: 32,
        padding: EdgeInsets.fromLTRB(dot != null || leadingCheck ? 8 : 12, 0, removable ? 4 : 12, 0),
        decoration: BoxDecoration(
          color: active ? cs.primary.withValues(alpha: 0.10) : cs.surface,
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: active ? cs.primary : cs.outlineVariant),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (leadingCheck) ...[
              Icon(Icons.check, size: 14, color: fg),
              const SizedBox(width: 4),
            ],
            if (dot != null) ...[
              Container(width: 7, height: 7,
                  decoration: BoxDecoration(color: dot, shape: BoxShape.circle)),
              const SizedBox(width: 6),
            ],
            Text(label,
                style: TextStyle(
                    fontSize: 13, fontWeight: FontWeight.w500, color: fg)),
            if (removable) ...[
              const SizedBox(width: 2),
              InkWell(
                onTap: onRemove,
                borderRadius: BorderRadius.circular(999),
                child: Padding(
                  padding: const EdgeInsets.all(3),
                  child: Icon(Icons.close, size: 14, color: fg.withValues(alpha: 0.7)),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _EDNotSetChip extends StatelessWidget {
  final bool active;
  final VoidCallback onTap;

  const _EDNotSetChip({required this.active, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(999),
      child: Container(
        height: 32,
        padding: const EdgeInsets.symmetric(horizontal: 12),
        decoration: BoxDecoration(
          color: active ? cs.surfaceContainerHighest : Colors.transparent,
          borderRadius: BorderRadius.circular(999),
          border: Border.all(
            color: active ? cs.outline : cs.outlineVariant,
            style: BorderStyle.solid,
          ),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.remove, size: 14,
                color: active ? cs.onSurface : cs.onSurfaceVariant),
            const SizedBox(width: 4),
            Text('Not set',
                style: TextStyle(
                    fontSize: 13, fontWeight: FontWeight.w500,
                    color: active ? cs.onSurface : cs.onSurfaceVariant)),
          ],
        ),
      ),
    );
  }
}

class _EDCounterRow extends StatefulWidget {
  final int index;
  final String name;
  final double value;
  final ValueChanged<double> onChanged;
  final VoidCallback onRemove;

  const _EDCounterRow({
    super.key,
    required this.index,
    required this.name,
    required this.value,
    required this.onChanged,
    required this.onRemove,
  });

  @override
  State<_EDCounterRow> createState() => _EDCounterRowState();
}

class _EDCounterRowState extends State<_EDCounterRow> {
  late final TextEditingController _ctrl;
  late final FocusNode _focusNode;

  static String _display(double v) =>
      v == v.truncateToDouble() ? v.toInt().toString() : v.toString();

  @override
  void initState() {
    super.initState();
    _ctrl = TextEditingController(text: _display(widget.value));
    _focusNode = FocusNode()..addListener(_onFocusChange);
  }

  void _onFocusChange() {
    if (!_focusNode.hasFocus) _submit();
  }

  @override
  void didUpdateWidget(_EDCounterRow oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Keep the field in sync with external changes (the +/- steppers), but
    // don't clobber text the user is actively typing.
    if (oldWidget.value != widget.value && !_focusNode.hasFocus) {
      _ctrl.text = _display(widget.value);
    }
  }

  @override
  void dispose() {
    _focusNode.removeListener(_onFocusChange);
    _focusNode.dispose();
    _ctrl.dispose();
    super.dispose();
  }

  /// Parses the field (accepting "." or "," as the decimal separator) and
  /// commits it, or reverts to the last valid value if it isn't a valid
  /// non-negative number.
  void _submit() {
    final normalized = _ctrl.text.trim().replaceAll(',', '.');
    final parsed = double.tryParse(normalized);
    if (parsed == null) {
      _ctrl.text = _display(widget.value);
      return;
    }
    _ctrl.text = _display(parsed);
    if (parsed != widget.value) widget.onChanged(parsed);
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 6, 8, 6),
      decoration: BoxDecoration(
        color: cs.surface,
        border: Border.all(color: cs.outlineVariant),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(widget.name,
                maxLines: 1, overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 13.5)),
          ),
          const SizedBox(width: 10),
          Container(
            decoration: BoxDecoration(
              color: cs.surfaceContainerHighest,
              border: Border.all(color: cs.outlineVariant),
              borderRadius: BorderRadius.circular(8),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                _stepBtn(context, Icons.remove, ValueKey('ctr_dec_${widget.index}_${widget.name}'),
                    widget.value <= 0
                        ? null
                        : () => widget.onChanged((widget.value - 1).clamp(0, double.infinity))),
                SizedBox(
                  width: 46,
                  child: TextField(
                    key: ValueKey('ctr_val_${widget.index}_${widget.name}'),
                    controller: _ctrl,
                    focusNode: _focusNode,
                    textAlign: TextAlign.center,
                    keyboardType: const TextInputType.numberWithOptions(decimal: true),
                    inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.,]'))],
                    onSubmitted: (_) => _submit(),
                    style: monoStyle(
                      fontSize: 13, fontWeight: FontWeight.w600,
                      color: widget.value == 0 ? cs.onSurfaceVariant : cs.onSurface,
                    ),
                    decoration: const InputDecoration(
                      isDense: true,
                      contentPadding: EdgeInsets.symmetric(vertical: 4),
                      border: InputBorder.none,
                    ),
                  ),
                ),
                _stepBtn(context, Icons.add, ValueKey('ctr_inc_${widget.index}_${widget.name}'),
                    () => widget.onChanged(widget.value + 1)),
              ],
            ),
          ),
          IconButton(
            icon: const Icon(Icons.close, size: 15),
            tooltip: 'Remove counter',
            visualDensity: VisualDensity.compact,
            color: cs.onSurfaceVariant,
            onPressed: widget.onRemove,
          ),
        ],
      ),
    );
  }

  Widget _stepBtn(BuildContext context, IconData icon, Key key, VoidCallback? onTap) {
    final cs = Theme.of(context).colorScheme;
    return InkWell(
      key: key,
      onTap: onTap,
      child: SizedBox(
        width: 28, height: 28,
        child: Icon(icon, size: 16,
            color: onTap == null ? cs.onSurfaceVariant.withValues(alpha: 0.4) : cs.onSurfaceVariant),
      ),
    );
  }
}
