import 'package:flutter/material.dart';

import 'journal_dialog.dart';
import 'project_notifier.dart';
import '../crypto/encrypted_display.dart';

/// Opens a read-only detail view for a journal entry.
///
/// Tap "Continue writing" to pop and open [JournalDialog] in edit mode.
void showJournalDetail(
  BuildContext context,
  ProjectNotifier notifier,
  Map<String, dynamic> journal,
) {
  showDialog(
    context: context,
    useRootNavigator: true,
    builder: (_) => _JournalDetailModal(notifier: notifier, journal: journal),
  );
}

// ── Design tokens ─────────────────────────────────────────────────────────────
const _kBg       = Color(0xFF0F1A26);
const _kBgDeep   = Color(0xFF0C1722);
const _kBorder   = Color(0xFF1F2F42);
const _kBlue     = Color(0xFF60A5FA);
const _kBlueFill = Color(0xFF1D4ED8);
const _kText     = Color(0xFFE2E8F0);
const _kTextDim  = Color(0xFFCBD5E1);
const _kMuted    = Color(0xFF64748B);
const _kRed      = Color(0xFFEF4444);

// ── Widget ────────────────────────────────────────────────────────────────────

class _JournalDetailModal extends StatefulWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic> journal;

  const _JournalDetailModal({required this.notifier, required this.journal});

  @override
  State<_JournalDetailModal> createState() => _JournalDetailModalState();
}

class _JournalDetailModalState extends State<_JournalDetailModal> {
  late Map<String, dynamic> _current;
  int _photoViewerIndex = -1;

  static const _monthsFull = [
    'JANUARY', 'FEBRUARY', 'MARCH', 'APRIL', 'MAY', 'JUNE',
    'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER', 'NOVEMBER', 'DECEMBER',
  ];

  @override
  void initState() {
    super.initState();
    _current = widget.journal;
  }

  // ── Journal list helpers ───────────────────────────────────────────────────

  List<Map<String, dynamic>> get _allJournals => widget.notifier.itemsFacet.items
      .where((i) => i['item_type'] == 'journal')
      .map((i) => i['journal'] as Map<String, dynamic>?)
      .whereType<Map<String, dynamic>>()
      .toList();

  int get _currentIndex {
    final id = _current['id']?.toString();
    return _allJournals.indexWhere((j) => j['id']?.toString() == id);
  }

  void _navigate(int delta) {
    final all = _allJournals;
    if (all.isEmpty) return;
    final next = (_currentIndex + delta).clamp(0, all.length - 1);
    setState(() => _current = all[next]);
  }

  // ── Actions ────────────────────────────────────────────────────────────────

  Future<void> _edit() async {
    Navigator.of(context, rootNavigator: true).pop();
    await showDialog(
      context: context,
      useRootNavigator: true,
      builder: (_) => JournalDialog(notifier: widget.notifier, editEntry: _current),
    );
  }

  Future<void> _delete() async {
    final confirmed = await showDialog<bool>(
      context: context,
      useRootNavigator: true,
      builder: (_) => AlertDialog(
        title: const Text('Delete journal entry?'),
        content: const Text('This will also delete all attached photos.'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Cancel'),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: _kRed,
              foregroundColor: Colors.white,
            ),
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed == true) {
      await widget.notifier.deleteJournal(_current['id']?.toString() ?? '');
      if (mounted) Navigator.of(context, rootNavigator: true).pop();
    }
  }

  // ── Photo URL helpers ──────────────────────────────────────────────────────

  String _photoThumbUrl(String uuid) {
    final jId = _current['id']?.toString() ?? '';
    return '${widget.notifier.apiBaseUrl}/api/journal/$jId/photos/$uuid/thumb';
  }

  String _photoFullUrl(String uuid) {
    final jId = _current['id']?.toString() ?? '';
    return '${widget.notifier.apiBaseUrl}/api/journal/$jId/photos/$uuid';
  }

  Map<String, String> get _authHeaders => widget.notifier.photoAuthHeaders;

  // ── Trip-day info (derived from dayMeta) ───────────────────────────────────

  ({int dayNum, int totalDays})? _tripDayInfo(String? dateStr) {
    if (dateStr == null) return null;
    final dayMeta = widget.notifier.itemsFacet.dayMeta;
    if (dayMeta.isEmpty) return null;
    final sorted = dayMeta.keys.toList()..sort();
    final prefix = dateStr.length >= 10 ? dateStr.substring(0, 10) : dateStr;
    final idx = sorted.indexOf(prefix);
    if (idx < 0) return null;
    return (dayNum: idx + 1, totalDays: sorted.length);
  }

  // ── Build ──────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final photos = (_current['photos'] as List?)?.cast<String>() ?? [];

    if (_photoViewerIndex >= 0) {
      return _PhotoViewer(
        photos: photos,
        initialIndex: _photoViewerIndex,
        fullUrlOf: _photoFullUrl,
        authHeaders: _authHeaders,
        onClose: () => setState(() => _photoViewerIndex = -1),
      );
    }

    final all      = _allJournals;
    final idx      = _currentIndex;
    final hasPrev  = idx > 0;
    final hasNext  = idx < all.length - 1;
    final desc     = shownText(_current['description'] as String?) ?? '';
    final dateStr  = _current['date'] as String?;
    final date     = dateStr != null ? DateTime.tryParse(dateStr) : null;
    final tripDay  = _tripDayInfo(dateStr);

    return Dialog(
      backgroundColor: _kBg,
      insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 940, maxHeight: 680),
        child: ClipRRect(
          borderRadius: BorderRadius.circular(16),
          child: LayoutBuilder(builder: (ctx, constraints) {
            final wide = constraints.maxWidth >= 640;
            return wide
                ? _desktopLayout(photos, desc, date, tripDay, hasPrev, hasNext, idx, all.length)
                : _mobileLayout(photos, desc, date, tripDay, hasPrev, hasNext, idx, all.length);
          }),
        ),
      ),
    );
  }

  // ── Desktop layout (two-column) ────────────────────────────────────────────

  Widget _desktopLayout(
    List<String> photos, String desc, DateTime? date,
    ({int dayNum, int totalDays})? tripDay,
    bool hasPrev, bool hasNext, int idx, int total,
  ) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        // Left: vertical photo stack
        Flexible(
          flex: 10,
          child: Container(
            color: _kBgDeep,
            child: photos.isEmpty
                ? Center(
                    child: Icon(Icons.photo_camera_outlined,
                        size: 64, color: _kMuted.withValues(alpha: 0.4)),
                  )
                : ListView.separated(
                    padding: EdgeInsets.zero,
                    itemCount: photos.length,
                    separatorBuilder: (_, __) =>
                        const Divider(height: 1, color: _kBorder),
                    itemBuilder: (_, i) => _photoCard(photos[i], i, photos.length),
                  ),
          ),
        ),
        Container(width: 1, color: _kBorder),
        // Right: content pane
        Flexible(
          flex: 14,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              _navRow(hasPrev, hasNext, idx, total),
              Expanded(
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(28, 16, 28, 0),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _journalBadge(),
                      const SizedBox(height: 16),
                      _dateHero(date, tripDay),
                      const SizedBox(height: 24),
                      if (desc.isNotEmpty) _descriptionWithDropCap(desc),
                      if (desc.isEmpty) _emptyState(),
                      const SizedBox(height: 24),
                      _statsStrip(desc, photos.length),
                      const SizedBox(height: 8),
                    ],
                  ),
                ),
              ),
              _footerRow(),
            ],
          ),
        ),
      ],
    );
  }

  // ── Mobile layout (stacked) ────────────────────────────────────────────────

  Widget _mobileLayout(
    List<String> photos, String desc, DateTime? date,
    ({int dayNum, int totalDays})? tripDay,
    bool hasPrev, bool hasNext, int idx, int total,
  ) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _navRow(hasPrev, hasNext, idx, total),
        Expanded(
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (photos.isNotEmpty) _photoCard(photos[0], 0, photos.length),
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 20, 20, 0),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _journalBadge(),
                      const SizedBox(height: 16),
                      _dateHero(date, tripDay),
                      const SizedBox(height: 20),
                      if (desc.isNotEmpty) _descriptionWithDropCap(desc),
                      if (desc.isEmpty) _emptyState(),
                    ],
                  ),
                ),
                // Remaining photos interleaved after text
                if (photos.length > 1) ...[
                  const SizedBox(height: 16),
                  ...List.generate(
                    photos.length - 1,
                    (i) => Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: _photoCard(photos[i + 1], i + 1, photos.length),
                    ),
                  ),
                ],
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 20, 20, 8),
                  child: _statsStrip(desc, photos.length),
                ),
              ],
            ),
          ),
        ),
        _footerRow(),
      ],
    );
  }

  // ── Sub-widgets ────────────────────────────────────────────────────────────

  Widget _photoCard(String uuid, int index, int total) {
    return GestureDetector(
      onTap: () => setState(() => _photoViewerIndex = index),
      child: Stack(
        children: [
          Image.network(
            _photoThumbUrl(uuid),
            width: double.infinity,
            height: 200,
            fit: BoxFit.cover,
            headers: _authHeaders,
            errorBuilder: (_, __, ___) => Container(
              height: 200,
              color: _kBgDeep,
              child: const Center(
                child: Icon(Icons.broken_image_outlined, color: _kMuted),
              ),
            ),
          ),
          // Photo index badge
          Positioned(
            top: 8, left: 8,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
              decoration: BoxDecoration(
                color: _kBg.withValues(alpha: 0.78),
                borderRadius: BorderRadius.circular(4),
              ),
              child: Text(
                '${(index + 1).toString().padLeft(2, '0')} / '
                '${total.toString().padLeft(2, '0')}',
                style: const TextStyle(
                  color: _kTextDim,
                  fontSize: 10,
                  fontFamily: 'monospace',
                  letterSpacing: 0.5,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _navRow(bool hasPrev, bool hasNext, int idx, int total) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: _kBorder)),
      ),
      child: Row(
        children: [
          IconButton(
            icon: const Icon(Icons.chevron_left, color: _kTextDim),
            tooltip: 'Previous entry',
            onPressed: hasPrev ? () => _navigate(-1) : null,
          ),
          IconButton(
            icon: const Icon(Icons.chevron_right, color: _kTextDim),
            tooltip: 'Next entry',
            onPressed: hasNext ? () => _navigate(1) : null,
          ),
          if (total > 0)
            Text('${idx + 1} / $total',
                style: const TextStyle(color: _kMuted, fontSize: 12)),
          const Spacer(),
          IconButton(
            icon: const Icon(Icons.close, color: _kTextDim),
            onPressed: () => Navigator.of(context, rootNavigator: true).pop(),
          ),
        ],
      ),
    );
  }

  Widget _journalBadge() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: _kBlue.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: _kBlue.withValues(alpha: 0.28)),
      ),
      child: const Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.menu_book_rounded, size: 13, color: _kBlue),
          SizedBox(width: 5),
          Text(
            'Journal entry',
            style: TextStyle(
              color: _kBlue,
              fontSize: 11,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.3,
            ),
          ),
        ],
      ),
    );
  }

  Widget _dateHero(DateTime? date, ({int dayNum, int totalDays})? tripDay) {
    final dayOfMonth = date?.day ?? 0;
    final monthStr   = date != null ? _monthsFull[date.month - 1] : '';
    final yearStr    = date?.year.toString() ?? '';

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (tripDay != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text(
              'DAY ${tripDay.dayNum} OF ${tripDay.totalDays}',
              style: const TextStyle(
                color: _kBlue,
                fontSize: 11,
                fontFamily: 'monospace',
                letterSpacing: 2,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        // Giant day number + month/year
        Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Text(
              dayOfMonth.toString(),
              style: const TextStyle(
                color: _kText,
                fontSize: 72,
                fontFamily: 'monospace',
                fontWeight: FontWeight.w500,
                letterSpacing: -3,
                height: 0.9,
              ),
            ),
            const SizedBox(width: 12),
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    monthStr,
                    style: const TextStyle(
                      color: _kTextDim,
                      fontSize: 14,
                      fontFamily: 'monospace',
                      fontWeight: FontWeight.w500,
                      letterSpacing: 1,
                    ),
                  ),
                  Text(
                    yearStr,
                    style: const TextStyle(
                      color: _kMuted,
                      fontSize: 13,
                      fontFamily: 'monospace',
                      letterSpacing: 0.5,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
        if (date != null)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(
              _fmtDateFull(date),
              style: const TextStyle(
                color: _kBlue,
                fontSize: 11,
                fontFamily: 'monospace',
                letterSpacing: 1.5,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
      ],
    );
  }

  Widget _descriptionWithDropCap(String desc) {
    final firstChar = desc[0];
    final rest = desc.length > 1 ? desc.substring(1) : '';
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          firstChar,
          style: const TextStyle(
            color: _kBlue,
            fontSize: 56,
            fontWeight: FontWeight.w700,
            height: 0.85,
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            rest,
            style: const TextStyle(
              color: _kTextDim,
              fontSize: 15,
              height: 1.75,
            ),
          ),
        ),
      ],
    );
  }

  Widget _emptyState() {
    return const Text(
      'No text yet — tap "Continue writing" to add your thoughts.',
      style: TextStyle(
        color: _kMuted,
        fontSize: 14,
        fontStyle: FontStyle.italic,
      ),
    );
  }

  Widget _statsStrip(String desc, int photoCount) {
    final wordCount = desc.trim().isEmpty
        ? 0
        : desc.trim().split(RegExp(r'\s+')).length;
    final readMin = wordCount == 0 ? 0 : (wordCount / 200).ceil();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Divider(color: _kBorder, height: 1),
        const SizedBox(height: 10),
        Wrap(
          spacing: 20,
          children: [
            _statChip(Icons.text_fields_outlined, '$wordCount words'),
            _statChip(Icons.schedule_outlined, '$readMin min read'),
            if (photoCount > 0)
              _statChip(Icons.photo_library_outlined,
                  '$photoCount photo${photoCount == 1 ? '' : 's'}'),
          ],
        ),
      ],
    );
  }

  Widget _statChip(IconData icon, String label) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 11, color: _kMuted),
        const SizedBox(width: 4),
        Text(
          label.toUpperCase(),
          style: const TextStyle(
            color: _kMuted,
            fontSize: 10,
            fontFamily: 'monospace',
            letterSpacing: 0.8,
          ),
        ),
      ],
    );
  }

  Widget _footerRow() {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: const BoxDecoration(
        color: _kBgDeep,
        border: Border(top: BorderSide(color: _kBorder)),
      ),
      child: Row(
        children: [
          OutlinedButton.icon(
            style: OutlinedButton.styleFrom(
              foregroundColor: _kRed,
              side: const BorderSide(color: _kRed),
              minimumSize: const Size(0, 36),
            ),
            icon: const Icon(Icons.delete_outline, size: 16),
            label: const Text('Delete'),
            onPressed: _delete,
          ),
          const Spacer(),
          ElevatedButton.icon(
            style: ElevatedButton.styleFrom(
              backgroundColor: _kBlueFill,
              foregroundColor: Colors.white,
              minimumSize: const Size(0, 36),
            ),
            icon: const Icon(Icons.edit_outlined, size: 16),
            label: const Text('Continue writing'),
            onPressed: _edit,
          ),
        ],
      ),
    );
  }

  static String _fmtDateFull(DateTime d) {
    const weekdays = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
    const months   = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
                      'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
    final wd  = weekdays[d.weekday - 1];
    final day = d.day.toString().padLeft(2, '0');
    final mo  = months[d.month - 1];
    return '$wd · $day $mo ${d.year}';
  }
}

// ── Full-screen photo viewer ──────────────────────────────────────────────────

class _PhotoViewer extends StatefulWidget {
  final List<String> photos;
  final int initialIndex;
  final String Function(String uuid) fullUrlOf;
  final Map<String, String> authHeaders;
  final VoidCallback onClose;

  const _PhotoViewer({
    required this.photos,
    required this.initialIndex,
    required this.fullUrlOf,
    required this.authHeaders,
    required this.onClose,
  });

  @override
  State<_PhotoViewer> createState() => _PhotoViewerState();
}

class _PhotoViewerState extends State<_PhotoViewer> {
  late final PageController _page;

  @override
  void initState() {
    super.initState();
    _page = PageController(initialPage: widget.initialIndex);
  }

  @override
  void dispose() {
    _page.dispose();
    super.dispose();
  }

  int get _current => _page.hasClients
      ? (_page.page?.round() ?? widget.initialIndex)
      : widget.initialIndex;

  @override
  Widget build(BuildContext context) {
    return Dialog(
      backgroundColor: Colors.black,
      insetPadding: EdgeInsets.zero,
      child: SizedBox.expand(
        child: Stack(
          children: [
            PageView.builder(
              controller: _page,
              itemCount: widget.photos.length,
              onPageChanged: (_) => setState(() {}),
              itemBuilder: (_, i) => Image.network(
                widget.fullUrlOf(widget.photos[i]),
                fit: BoxFit.contain,
                headers: widget.authHeaders,
              ),
            ),
            Positioned(
              top: 16, right: 16,
              child: IconButton(
                icon: const Icon(Icons.close, color: Colors.white),
                onPressed: widget.onClose,
              ),
            ),
            if (widget.photos.length > 1) ...[
              Positioned(
                left: 8, top: 0, bottom: 0,
                child: Center(
                  child: IconButton(
                    icon: const Icon(Icons.chevron_left,
                        color: Colors.white, size: 40),
                    onPressed: _current > 0
                        ? () => _page.previousPage(
                              duration: const Duration(milliseconds: 200),
                              curve: Curves.easeInOut,
                            )
                        : null,
                  ),
                ),
              ),
              Positioned(
                right: 8, top: 0, bottom: 0,
                child: Center(
                  child: IconButton(
                    icon: const Icon(Icons.chevron_right,
                        color: Colors.white, size: 40),
                    onPressed: _current < widget.photos.length - 1
                        ? () => _page.nextPage(
                              duration: const Duration(milliseconds: 200),
                              curve: Curves.easeInOut,
                            )
                        : null,
                  ),
                ),
              ),
            ],
            Positioned(
              bottom: 16, left: 0, right: 0,
              child: Center(
                child: Text(
                  '${_current + 1} / ${widget.photos.length}',
                  style: const TextStyle(color: Colors.white70),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
