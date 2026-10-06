/// Project statistics screen — shows pre-computed trip stats for a project.
library;

import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../auth/auth_notifier.dart';
import '../core/project_ref.dart';
import 'project_notifier.dart';
import 'project_service.dart';

part 'project_stats_body.dart';
part 'project_stats_counters.dart';
part 'project_stats_timeseries.dart';

// ── Helpers ───────────────────────────────────────────────────────────────────

String _fmtDistance(double metres) {
  final km = metres / 1000;
  return '${km.toStringAsFixed(1)} km';
}

String _fmtDuration(int seconds) {
  if (seconds <= 0) return '0m';
  final d = seconds ~/ 86400;
  final h = (seconds % 86400) ~/ 3600;
  final m = (seconds % 3600) ~/ 60;
  final parts = <String>[];
  if (d > 0) parts.add('${d}d');
  if (h > 0) parts.add('${h}h');
  if (m > 0 || parts.isEmpty) parts.add('${m}m');
  return parts.join(' ');
}

String _fmtElevation(double metres) => '${metres.toStringAsFixed(0)} m';

String _fmtDate(String? isoDate) {
  if (isoDate == null || isoDate.isEmpty) return '—';
  try {
    final dt = DateTime.parse(isoDate);
    const months = [
      'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
    ];
    return '${months[dt.month - 1]} ${dt.day}';
  } catch (_) {
    return isoDate;
  }
}

/// Capitalise first letter of a type string (e.g. "ride" → "Ride").
String _capitalize(String s) =>
    s.isEmpty ? s : s[0].toUpperCase() + s.substring(1);

// ── Sleeping group colours ─────────────────────────────────────────────────────

const _groupColors = {
  'Outdoors': Color(0xFF22C55E),
  'Indoors':  Color(0xFF3B82F6),
  'Other':    Color(0xFFA855F7),
  'No data':  Color(0xFF9E9E9E),
};

Color _outerModeColor(String group, int idxInGroup) {
  final base = _groupColors[group] ?? const Color(0xFF9E9E9E);
  final hsl = HSLColor.fromColor(base);
  return hsl.withLightness((hsl.lightness + 0.12 * idxInGroup).clamp(0.0, 0.88)).toColor();
}

// ── Mode colours ──────────────────────────────────────────────────────────────

const _modeColors = {
  'ride':   Color(0xFFFF6B35),
  'flight': Color(0xFF607D8B),
  'train':  Color(0xFF4CAF50),
  'boat':   Color(0xFF00BCD4),
  'bus':    Color(0xFF9C27B0),
};

const _modeLabels = {
  'ride':   'Ride',
  'flight': 'Flight',
  'train':  'Train',
  'boat':   'Boat',
  'bus':    'Bus',
};

// ── Widgets ───────────────────────────────────────────────────────────────────

class _StatCard extends StatelessWidget {
  final String label;
  final String value;
  final IconData icon;

  const _StatCard({required this.label, required this.value, required this.icon});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 28, color: theme.colorScheme.primary),
            const SizedBox(height: 6),
            FittedBox(
              fit: BoxFit.scaleDown,
              child: Text(value,
                  style: theme.textTheme.titleLarge
                      ?.copyWith(fontWeight: FontWeight.bold)),
            ),
            const SizedBox(height: 2),
            Text(label,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          ],
        ),
      ),
    );
  }
}

class _SectionHeader extends StatelessWidget {
  final String title;
  const _SectionHeader(this.title);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 24, bottom: 8),
      child: Text(title,
          style: Theme.of(context)
              .textTheme
              .titleMedium
              ?.copyWith(fontWeight: FontWeight.w600)),
    );
  }
}

/// A simple two-column label / value row used in count tables.
class _CountRow extends StatelessWidget {
  final String label;
  final String value;

  const _CountRow({required this.label, required this.value});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        children: [
          Expanded(child: Text(label)),
          Text(value,
              style: Theme.of(context)
                  .textTheme
                  .bodyMedium
                  ?.copyWith(fontWeight: FontWeight.w600)),
        ],
      ),
    );
  }
}

// ── Screen ────────────────────────────────────────────────────────────────────

class ProjectStatsScreen extends StatefulWidget {
  final String projectName;

  /// Owning user's id for a project shared with the caller (issue #106);
  /// null for one of the caller's own projects.
  final int? ownerId;
  final ProjectService? service;

  /// Explicit override, used only by the public/anonymous shared-project view
  /// (shared_project_screen.dart), which has its own SharedProjectNotifier and
  /// must not read the ambient (authenticated, manage-mode) ProjectNotifier
  /// singleton. When null — the authenticated /stats route case — tags/groups
  /// are read from that ambient notifier instead (issue #76 follow-up:
  /// GoRouterState.extra isn't URL-encoded, so it doesn't survive a reload).
  final List<String>? availableTags;
  final Map<String, String>? sleepingOptionGroups;

  const ProjectStatsScreen({
    super.key,
    required this.projectName,
    this.ownerId,
    this.availableTags,
    this.sleepingOptionGroups,
    this.service,
  });

  /// Addressing for [projectName]/[ownerId] — issue #106.
  ProjectRef get projectRef => ProjectRef(name: projectName, ownerId: ownerId);

  @override
  State<ProjectStatsScreen> createState() => _ProjectStatsScreenState();
}

class _ProjectStatsScreenState extends State<ProjectStatsScreen> {
  late Future<Map<String, dynamic>> _statsFuture;
  Set<String> _selectedTags = {};
  List<String> _tagOptions = [];

  @override
  void initState() {
    super.initState();
    final overrideTags = widget.availableTags;
    if (overrideTags != null) {
      _tagOptions = List.of(overrideTags);
    } else {
      // Authenticated /stats route path — on a direct reload of the /stats
      // URL the (singleton, manage-mode) ProjectNotifier may not have this
      // project loaded yet — load it first, mirroring what
      // AppScreen.initState() already does. Deferred to a post-frame callback
      // (like AppScreen) since load() calls notifyListeners() synchronously
      // before its first await, which would otherwise fire mid-build here.
      final notifier = context.read<ProjectNotifier>();
      // Compare/load with the role resolved against the signed-in user —
      // the notifier's ref carries the resolved role (issue #106), while the
      // URL-derived widget.projectRef defaults to "owner".
      String? selfId;
      try {
        selfId = context.read<AuthNotifier>().user?.id;
      } on ProviderNotFoundException {
        // No AuthNotifier in the tree (tests) — own-project refs resolve the
        // same either way.
      }
      final target = widget.projectRef.resolveRoleFor(selfId);
      if (notifier.ref != target) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted) context.read<ProjectNotifier>().load(target);
        });
      }
      _tagOptions = List.of(notifier.itemsFacet.availableTags);
    }
    _load();
  }

  void _load() {
    final future = (widget.service ?? ProjectService())
        .getStats(widget.projectRef, tags: _selectedTags.toList());
    future.then((data) {
      if (!mounted) return;
      final opts = data['tag_options'];
      if (opts is List) {
        setState(() => _tagOptions = opts.cast<String>());
      }
    });
    _statsFuture = future;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('${widget.projectName} — Statistics')),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // ── Tag filter ───────────────────────────────────────────────────
          if (_tagOptions.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
              child: Wrap(
                spacing: 6,
                runSpacing: 4,
                children: [
                  FilterChip(
                    label: const Text('All'),
                    selected: _selectedTags.isEmpty,
                    onSelected: (_) => setState(() {
                      _selectedTags = {};
                      _load();
                    }),
                  ),
                  for (final tag in _tagOptions)
                    FilterChip(
                      label: Text(tag),
                      selected: _selectedTags.contains(tag),
                      onSelected: (on) => setState(() {
                        if (on) {
                          _selectedTags = {..._selectedTags, tag};
                        } else {
                          _selectedTags = {..._selectedTags}..remove(tag);
                        }
                        _load();
                      }),
                    ),
                ],
              ),
            ),
          // ── Stats content ─────────────────────────────────────────────
          Expanded(
            child: FutureBuilder<Map<String, dynamic>>(
              future: _statsFuture,
              builder: (context, snap) {
                if (snap.connectionState != ConnectionState.done) {
                  return const Center(child: CircularProgressIndicator());
                }
                if (snap.hasError) {
                  return Center(
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text('Failed to load stats: ${snap.error}',
                            textAlign: TextAlign.center),
                        const SizedBox(height: 12),
                        ElevatedButton(
                          onPressed: () => setState(_load),
                          child: const Text('Retry'),
                        ),
                      ],
                    ),
                  );
                }

                final s = snap.data!;
                final given = widget.sleepingOptionGroups;
                if (given != null) {
                  return _StatsBody(
                    stats: s,
                    projectName: widget.projectName,
                    sleepingOptionGroups: given,
                  );
                }
                // The ambient notifier's groups, followed as they change
                // (#294). Selecting the notifier itself rebuilds this on an
                // account change, not on its notifies.
                final notifier =
                    context.select<ProjectNotifier, ProjectNotifier>((n) => n);
                return ListenableBuilder(
                  listenable: notifier.itemsFacet,
                  builder: (_, __) => _StatsBody(
                    stats: s,
                    projectName: widget.projectName,
                    sleepingOptionGroups:
                        notifier.itemsFacet.sleepingOptionGroups,
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}
