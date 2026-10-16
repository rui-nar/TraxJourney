/// Admin dashboard — aggregate metrics, per-user breakdown, and a user
/// search + password-reset tool. Shown only to admins (see app_router).
///
/// Never renders memory/journal content: the backend only exposes counts,
/// sizes, and profile fields.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../core/design_tokens.dart';
import 'admin_service.dart';

/// Human-readable byte size (e.g. 1.5 MB). Kept top-level so tests can exercise it.
String humanizeBytes(int bytes) {
  if (bytes < 1024) return '$bytes B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  double value = bytes / 1024;
  int i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return '${value.toStringAsFixed(value >= 10 ? 0 : 1)} ${units[i]}';
}

/// Signup date from a Unix-seconds timestamp → "YYYY-MM-DD" (empty when 0).
String formatSignup(num createdAt) {
  if (createdAt <= 0) return '—';
  final dt = DateTime.fromMillisecondsSinceEpoch(
      (createdAt * 1000).round(),
      isUtc: true);
  final m = dt.month.toString().padLeft(2, '0');
  final d = dt.day.toString().padLeft(2, '0');
  return '${dt.year}-$m-$d';
}

/// Relative time until a log-level override auto-reverts, from a Unix-seconds
/// expiry timestamp (e.g. "reverts in 43m", "reverts in 1h 5m").
/// Kept top-level so tests can exercise it.
String formatLogLevelExpiry(num expiresAtSeconds) {
  final expires = DateTime.fromMillisecondsSinceEpoch(
      (expiresAtSeconds * 1000).round(),
      isUtc: true);
  final diff = expires.difference(DateTime.now().toUtc());
  if (diff.isNegative) return 'reverting…';
  final totalMinutes = diff.inMinutes;
  if (totalMinutes < 60) return 'reverts in ${totalMinutes}m';
  final hours = totalMinutes ~/ 60;
  final minutes = totalMinutes % 60;
  return minutes == 0 ? 'reverts in ${hours}h' : 'reverts in ${hours}h ${minutes}m';
}

/// Color for a log-level chip — DEBUG through ERROR, low to high severity.
Color logLevelColor(String level) {
  switch (level) {
    case 'DEBUG':
      return kColorFlight;
    case 'WARNING':
      return kWarning;
    case 'ERROR':
      return kAccent;
    default: // INFO
      return kSuccess;
  }
}

/// Color for an encryption-tier chip.
Color tierColor(String tier) {
  switch (tier) {
    case 'high':
      return kSuccess;
    case 'medium':
      return kWarning;
    case 'low':
      return kColorRide;
    default:
      return kColorOther;
  }
}

/// Color for a plan chip — stronger plans get a stronger color.
Color planColor(String plan) {
  switch (plan) {
    case 'tier_3':
      return kSuccess;
    case 'tier_2':
      return kWarning;
    case 'tier_1':
      return kColorRide;
    default:
      return kColorOther;
  }
}

class AdminScreen extends StatefulWidget {
  /// Injectable for tests; defaults to the real HTTP-backed service.
  final AdminService service;

  /// Opens a Stripe dashboard URL. Injected in tests; defaults to
  /// `launchUrl` (same pattern as `BillingSection._open`).
  final Future<void> Function(String url)? launcher;

  AdminScreen({super.key, AdminService? service, this.launcher})
      : service = service ?? AdminService();

  @override
  State<AdminScreen> createState() => _AdminScreenState();
}

class _AdminScreenState extends State<AdminScreen> {
  Map<String, dynamic>? _stats;
  bool _loading = true;
  String? _error;
  bool _refreshingStorage = false;

  // ── Log level state (issue #208) ────────────────────────────────────────────
  Map<String, dynamic>? _logLevel;
  bool _applyingLogLevel = false;
  String _selectedLevel = 'INFO';
  // Minutes for the next override to apply; 0 is the "Indefinite" sentinel
  // (SegmentedButton needs a non-nullable value type).
  int _selectedDurationMinutes = 15;

  // ── Search state ────────────────────────────────────────────────────────────
  final _searchCtrl = TextEditingController();
  List<Map<String, dynamic>> _results = [];
  bool _searching = false;
  final Set<int> _resetting = {};
  final Set<int> _togglingAdmin = {};
  final Set<int> _deleting = {};

  // ── Broadcast email state ────────────────────────────────────────────────
  final Set<int> _selectedUserIds = {};
  final _emailSubjectCtrl = TextEditingController();
  final _emailBodyCtrl = TextEditingController();
  bool _sendingEmail = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _searchCtrl.dispose();
    _emailSubjectCtrl.dispose();
    _emailBodyCtrl.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final results = await Future.wait([
        widget.service.getStats(),
        widget.service.getLogLevel(),
      ]);
      if (mounted) {
        setState(() {
          _stats = results[0];
          _logLevel = results[1];
          _selectedLevel = (_logLevel?['override_level'] ??
              _logLevel?['env_level'] ??
              'INFO') as String;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() => _error = e.toString().replaceFirst('Exception: ', ''));
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _applyLogLevel() async {
    final indefinite = _selectedDurationMinutes == 0;
    if (_selectedLevel == 'DEBUG' && indefinite) {
      final confirmed = await showDialog<bool>(
            context: context,
            builder: (ctx) => AlertDialog(
              title: const Text('Apply indefinite DEBUG logging?'),
              content: const Text(
                  'This logs verbosely across every request until you '
                  'revert it or the process restarts. Continue?'),
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(ctx).pop(false),
                  child: const Text('Cancel'),
                ),
                FilledButton(
                  onPressed: () => Navigator.of(ctx).pop(true),
                  child: const Text('Apply anyway'),
                ),
              ],
            ),
          ) ??
          false;
      if (!confirmed) return;
    }

    setState(() => _applyingLogLevel = true);
    try {
      final result = await widget.service.setLogLevel(
        _selectedLevel,
        durationMinutes: indefinite ? null : _selectedDurationMinutes,
      );
      if (mounted) setState(() => _logLevel = result);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.toString().replaceFirst('Exception: ', ''))),
        );
      }
    } finally {
      if (mounted) setState(() => _applyingLogLevel = false);
    }
  }

  Future<void> _revertLogLevel() async {
    setState(() => _applyingLogLevel = true);
    try {
      final result = await widget.service.clearLogLevelOverride();
      if (mounted) setState(() => _logLevel = result);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.toString().replaceFirst('Exception: ', ''))),
        );
      }
    } finally {
      if (mounted) setState(() => _applyingLogLevel = false);
    }
  }

  Future<void> _recalculateStorage() async {
    setState(() => _refreshingStorage = true);
    try {
      await widget.service.refreshStorage();
      await _load();
    } catch (_) {
    } finally {
      if (mounted) setState(() => _refreshingStorage = false);
    }
  }

  Future<void> _openStripeLink(String url) async {
    final launch = widget.launcher ??
        (String u) async =>
            launchUrl(Uri.parse(u), mode: LaunchMode.externalApplication);
    try {
      await launch(url);
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Could not open Stripe.')),
        );
      }
    }
  }

  Future<void> _search() async {
    final q = _searchCtrl.text.trim();
    if (q.isEmpty) {
      setState(() => _results = []);
      return;
    }
    setState(() => _searching = true);
    try {
      final res = await widget.service.searchUsers(q);
      if (mounted) setState(() => _results = res);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Search failed: '
              '${e.toString().replaceFirst('Exception: ', '')}')),
        );
      }
    } finally {
      if (mounted) setState(() => _searching = false);
    }
  }

  Future<void> _resetPassword(Map<String, dynamic> user) async {
    final id = user['id'] as int;
    setState(() => _resetting.add(id));
    try {
      final temp = await widget.service.resetPassword(id);
      if (mounted) _showTempPasswordDialog(user, temp);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.toString().replaceFirst('Exception: ', ''))),
        );
      }
    } finally {
      if (mounted) setState(() => _resetting.remove(id));
    }
  }

  Future<void> _toggleAdmin(Map<String, dynamic> user) async {
    final id = user['id'] as int;
    final grant = !(user['is_admin'] == true);
    setState(() => _togglingAdmin.add(id));
    try {
      await widget.service.setAdmin(id, grant);
      if (mounted) setState(() => user['is_admin'] = grant);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.toString().replaceFirst('Exception: ', ''))),
        );
      }
    } finally {
      if (mounted) setState(() => _togglingAdmin.remove(id));
    }
  }

  Future<void> _deleteUser(Map<String, dynamic> user) async {
    final id = user['id'] as int;
    final label = (user['display_name'] as String?)?.isNotEmpty == true
        ? user['display_name'] as String
        : (user['email'] as String? ?? '#$id');
    final confirmed = await showDialog<bool>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: const Text('Delete user?'),
            content: Text(
                'This permanently deletes $label\'s account, projects, and all '
                'associated data. This cannot be undone.'),
            actions: [
              TextButton(
                onPressed: () => Navigator.of(ctx).pop(false),
                child: const Text('Cancel'),
              ),
              FilledButton(
                style: FilledButton.styleFrom(backgroundColor: Theme.of(ctx).colorScheme.error),
                onPressed: () => Navigator.of(ctx).pop(true),
                child: const Text('Delete'),
              ),
            ],
          ),
        ) ??
        false;
    if (!confirmed) return;

    setState(() => _deleting.add(id));
    try {
      await widget.service.deleteUser(id);
      if (mounted) setState(() => _results.removeWhere((u) => u['id'] == id));
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.toString().replaceFirst('Exception: ', ''))),
        );
      }
    } finally {
      if (mounted) setState(() => _deleting.remove(id));
    }
  }

  void _toggleUserSelected(int id, bool selected) {
    setState(() {
      if (selected) {
        _selectedUserIds.add(id);
      } else {
        _selectedUserIds.remove(id);
      }
    });
  }

  void _toggleSelectAll(bool selectAll, List<int> allIds) {
    setState(() {
      if (selectAll) {
        _selectedUserIds.addAll(allIds);
      } else {
        _selectedUserIds.clear();
      }
    });
  }

  Future<void> _sendBroadcastEmail({required bool sendToAll, required int totalUsers}) async {
    final subject = _emailSubjectCtrl.text.trim();
    final body = _emailBodyCtrl.text.trim();
    if (subject.isEmpty || body.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Subject and body are required.')),
      );
      return;
    }
    final recipientCount = sendToAll ? totalUsers : _selectedUserIds.length;
    if (recipientCount == 0) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('No recipients selected.')),
      );
      return;
    }

    final confirmed = await showDialog<bool>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: const Text('Send email?'),
            content: Text(
                'This will send "$subject" to $recipientCount user${recipientCount == 1 ? '' : 's'}.'),
            actions: [
              TextButton(
                onPressed: () => Navigator.of(ctx).pop(false),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: () => Navigator.of(ctx).pop(true),
                child: const Text('Send'),
              ),
            ],
          ),
        ) ??
        false;
    if (!confirmed) return;

    setState(() => _sendingEmail = true);
    try {
      final sent = await widget.service.broadcastEmail(
        subject: subject,
        body: body,
        userIds: _selectedUserIds.toList(),
        sendToAll: sendToAll,
      );
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Queued for $sent recipient${sent == 1 ? '' : 's'}.')),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.toString().replaceFirst('Exception: ', ''))),
        );
      }
    } finally {
      if (mounted) setState(() => _sendingEmail = false);
    }
  }

  void _showTempPasswordDialog(Map<String, dynamic> user, String temp) {
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Temporary password'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('For ${user['display_name'] ?? user['email'] ?? 'user'}:'),
            const SizedBox(height: 12),
            SelectableText(temp, style: monoStyle(fontSize: 16)),
            const SizedBox(height: 12),
            Text(
              'Shown once. The user must change it on next login.',
              style: Theme.of(ctx).textTheme.bodySmall,
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () {
              Clipboard.setData(ClipboardData(text: temp));
              ScaffoldMessenger.of(ctx).showSnackBar(
                const SnackBar(content: Text('Copied')),
              );
            },
            child: const Text('Copy'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Done'),
          ),
        ],
      ),
    );
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: BackButton(onPressed: () => context.pop()),
        title: const Text('Admin'),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
              ? _ErrorView(message: _error!, onRetry: _load)
              : _buildContent(context),
    );
  }

  Widget _buildContent(BuildContext context) {
    final totals = (_stats?['totals'] as Map?)?.cast<String, dynamic>() ?? {};
    final users = ((_stats?['users'] as List?) ?? [])
        .cast<Map<String, dynamic>>();

    return SingleChildScrollView(
      padding: const EdgeInsets.all(20),
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 960),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              _SectionCard(
                title: 'Overview',
                icon: Icons.dashboard_outlined,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Wrap(
                      spacing: 12,
                      runSpacing: 12,
                      children: [
                        _MetricTile(label: 'Users', value: '${totals['users'] ?? 0}'),
                        _MetricTile(label: 'Projects', value: '${totals['projects'] ?? 0}'),
                        _MetricTile(label: 'Activities', value: '${totals['activities'] ?? 0}'),
                        _MetricTile(label: 'Memories', value: '${totals['memories'] ?? 0}'),
                        _MetricTile(
                            label: 'Storage',
                            value: humanizeBytes(
                                (totals['storage_bytes'] as num?)?.toInt() ?? 0)),
                        _MetricTile(
                            label: 'New (7d)',
                            value: '${totals['recent_signups_7d'] ?? 0}'),
                      ],
                    ),
                    const SizedBox(height: 12),
                    Align(
                      alignment: Alignment.centerRight,
                      child: OutlinedButton.icon(
                        onPressed: _refreshingStorage ? null : _recalculateStorage,
                        icon: _refreshingStorage
                            ? const SizedBox(
                                width: 16,
                                height: 16,
                                child: CircularProgressIndicator(strokeWidth: 2))
                            : const Icon(Icons.refresh),
                        label: const Text('Recalculate storage'),
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),
              _SectionCard(
                title: 'Logging',
                icon: Icons.bug_report_outlined,
                child: _buildLogLevel(context),
              ),
              const SizedBox(height: 16),
              _SectionCard(
                title: 'Users',
                icon: Icons.people_outline,
                child: users.isEmpty
                    ? const Text('No users.')
                    : _UserTable(
                        users: users,
                        selected: _selectedUserIds,
                        onToggle: _toggleUserSelected,
                        onToggleAll: (v) => _toggleSelectAll(
                            v, users.map((u) => u['id'] as int).toList()),
                        onOpenStripe: _openStripeLink,
                      ),
              ),
              const SizedBox(height: 16),
              _SectionCard(
                title: 'Send email',
                icon: Icons.email_outlined,
                child: _buildBroadcastEmail(context, users.length),
              ),
              const SizedBox(height: 16),
              _SectionCard(
                title: 'User search & password reset',
                icon: Icons.search,
                child: _buildSearch(context),
              ),
              const SizedBox(height: 32),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildLogLevel(BuildContext context) {
    final theme = Theme.of(context);
    final logLevel = _logLevel;
    if (logLevel == null) {
      return const Text('Unavailable.');
    }
    final effective = logLevel['effective_level'] as String? ?? 'INFO';
    final source = logLevel['source'] as String? ?? 'env';
    final envLevel = logLevel['env_level'] as String? ?? 'INFO';
    final expiresAt = logLevel['override_expires_at'] as num?;
    final hasOverride = source == 'override';

    final subtitle = !hasOverride
        ? (envLevel == 'INFO'
            ? 'Default (INFO)'
            : 'Set via LOG_LEVEL ($envLevel)')
        : 'Live override — '
            '${expiresAt != null ? formatLogLevelExpiry(expiresAt) : 'until manually reverted'}';

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            _LevelChip(level: effective),
            const SizedBox(width: 10),
            Expanded(child: Text(subtitle, style: theme.textTheme.bodySmall)),
            if (hasOverride)
              _applyingLogLevel
                  ? const SizedBox(
                      width: 20, height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : OutlinedButton(
                      onPressed: _revertLogLevel,
                      child: const Text('Revert to default'),
                    ),
          ],
        ),
        const SizedBox(height: 16),
        Text('Apply a level', style: theme.textTheme.labelMedium),
        const SizedBox(height: 8),
        SegmentedButton<String>(
          segments: const [
            ButtonSegment(value: 'DEBUG', label: Text('DEBUG')),
            ButtonSegment(value: 'INFO', label: Text('INFO')),
            ButtonSegment(value: 'WARNING', label: Text('WARNING')),
            ButtonSegment(value: 'ERROR', label: Text('ERROR')),
          ],
          selected: {_selectedLevel},
          onSelectionChanged: (s) => setState(() => _selectedLevel = s.first),
        ),
        const SizedBox(height: 12),
        Text('For', style: theme.textTheme.labelMedium),
        const SizedBox(height: 8),
        SegmentedButton<int>(
          segments: const [
            ButtonSegment(value: 15, label: Text('15 min')),
            ButtonSegment(value: 60, label: Text('1 hour')),
            ButtonSegment(value: 240, label: Text('4 hours')),
            ButtonSegment(value: 0, label: Text('Indefinite')),
          ],
          selected: {_selectedDurationMinutes},
          onSelectionChanged: (s) =>
              setState(() => _selectedDurationMinutes = s.first),
        ),
        const SizedBox(height: 12),
        Align(
          alignment: Alignment.centerRight,
          child: FilledButton(
            onPressed: _applyingLogLevel ? null : _applyLogLevel,
            child: _applyingLogLevel
                ? const SizedBox(
                    width: 16, height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Apply'),
          ),
        ),
      ],
    );
  }

  Widget _buildBroadcastEmail(BuildContext context, int totalUsers) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          '${_selectedUserIds.length} of $totalUsers user${totalUsers == 1 ? '' : 's'} selected '
          '(check rows in the Users table above).',
          style: theme.textTheme.bodySmall,
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _emailSubjectCtrl,
          decoration: const InputDecoration(
            labelText: 'Subject',
            border: OutlineInputBorder(),
            isDense: true,
          ),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _emailBodyCtrl,
          maxLines: 6,
          decoration: const InputDecoration(
            labelText: 'Message',
            border: OutlineInputBorder(),
            alignLabelWithHint: true,
          ),
        ),
        const SizedBox(height: 12),
        Wrap(
          spacing: 12,
          runSpacing: 8,
          alignment: WrapAlignment.end,
          children: [
            OutlinedButton(
              onPressed: _sendingEmail || totalUsers == 0
                  ? null
                  : () => _sendBroadcastEmail(sendToAll: true, totalUsers: totalUsers),
              child: Text('Send to all $totalUsers'),
            ),
            FilledButton(
              onPressed: _sendingEmail || _selectedUserIds.isEmpty
                  ? null
                  : () => _sendBroadcastEmail(sendToAll: false, totalUsers: totalUsers),
              child: _sendingEmail
                  ? const SizedBox(
                      width: 16, height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : Text('Send to selected (${_selectedUserIds.length})'),
            ),
          ],
        ),
      ],
    );
  }

  Widget _buildSearch(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        TextField(
          key: const Key('admin-search-field'),
          controller: _searchCtrl,
          onSubmitted: (_) => _search(),
          decoration: InputDecoration(
            labelText: 'Search email / username / name',
            border: const OutlineInputBorder(),
            isDense: true,
            suffixIcon: IconButton(
              icon: _searching
                  ? const SizedBox(
                      width: 16, height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.arrow_forward),
              onPressed: _searching ? null : _search,
            ),
          ),
        ),
        const SizedBox(height: 12),
        if (_results.isEmpty)
          Text('No results.', style: theme.textTheme.bodySmall)
        else
          ..._results.map((u) => _SearchRow(
                user: u,
                busyReset: _resetting.contains(u['id']),
                busyAdmin: _togglingAdmin.contains(u['id']),
                busyDelete: _deleting.contains(u['id']),
                onReset: () => _resetPassword(u),
                onToggleAdmin: () => _toggleAdmin(u),
                onDelete: () => _deleteUser(u),
                onOpenStripe: _openStripeLink,
              )),
      ],
    );
  }
}

// ── Sub-widgets ───────────────────────────────────────────────────────────────

class _MetricTile extends StatelessWidget {
  final String label;
  final String value;
  const _MetricTile({required this.label, required this.value});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      width: 130,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerHighest.withValues(alpha: 0.4),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(value, style: monoStyle(fontSize: 22, fontWeight: FontWeight.w700)),
          const SizedBox(height: 4),
          Text(label, style: theme.textTheme.bodySmall),
        ],
      ),
    );
  }
}

class _UserTable extends StatelessWidget {
  final List<Map<String, dynamic>> users;
  final Set<int> selected;
  final void Function(int id, bool selected) onToggle;
  final void Function(bool selectAll) onToggleAll;
  final void Function(String url) onOpenStripe;

  const _UserTable({
    required this.users,
    required this.selected,
    required this.onToggle,
    required this.onToggleAll,
    required this.onOpenStripe,
  });

  @override
  Widget build(BuildContext context) {
    final allSelected = users.isNotEmpty &&
        users.every((u) => selected.contains(u['id'] as int));
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: DataTable(
        columns: [
          DataColumn(
            label: Checkbox(
              value: allSelected,
              onChanged: (v) => onToggleAll(v ?? false),
            ),
          ),
          const DataColumn(label: Text('User')),
          const DataColumn(label: Text('Provider')),
          const DataColumn(label: Text('Signup')),
          const DataColumn(label: Text('Projects'), numeric: true),
          const DataColumn(label: Text('Activities'), numeric: true),
          const DataColumn(label: Text('Memories'), numeric: true),
          const DataColumn(label: Text('Storage')),
          const DataColumn(label: Text('Plan')),
          const DataColumn(label: Text('Encryption')),
        ],
        rows: users.map((u) {
          final id = u['id'] as int;
          final tier = u['encryption_tier'] as String? ?? 'none';
          final name = (u['display_name'] as String?)?.isNotEmpty == true
              ? u['display_name'] as String
              : (u['email'] as String? ?? '#${u['id']}');
          return DataRow(
            selected: selected.contains(id),
            cells: [
              DataCell(Checkbox(
                value: selected.contains(id),
                onChanged: (v) => onToggle(id, v ?? false),
              )),
              DataCell(Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (u['is_admin'] == true) ...[
                    const _AdminShieldIcon(),
                    const SizedBox(width: 6),
                  ],
                  Text(name),
                ],
              )),
              DataCell(Text(u['auth_provider'] as String? ?? 'local')),
              DataCell(Text(formatSignup((u['created_at'] as num?) ?? 0))),
              DataCell(Text('${u['project_count'] ?? 0}')),
              DataCell(Text('${u['activity_count'] ?? 0}')),
              DataCell(Text('${u['memory_count'] ?? 0}')),
              DataCell(Text(humanizeBytes((u['storage_bytes'] as num?)?.toInt() ?? 0))),
              DataCell(_PlanCell(user: u, onOpenStripe: onOpenStripe)),
              DataCell(_TierChip(tier: tier)),
            ],
          );
        }).toList(),
      ),
    );
  }
}

/// Marks an admin user, both in the Users table and search results.
class _AdminShieldIcon extends StatelessWidget {
  const _AdminShieldIcon();

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: 'Admin',
      child: Icon(Icons.shield, size: 16, color: Theme.of(context).colorScheme.primary),
    );
  }
}

const Map<String, String> _tierExplanations = {
  'none': 'No encryption. Content is stored and searchable in plaintext.',
  'low': 'Server-recoverable encryption.',
  'medium': 'Zero-knowledge encryption, recoverable with security questions.',
  'high': 'Zero-knowledge encryption, recoverable only with the user\'s '
      'recovery key or passphrase.',
};

class _LevelChip extends StatelessWidget {
  final String level;
  const _LevelChip({required this.level});

  @override
  Widget build(BuildContext context) {
    final c = logLevelColor(level);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: c.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(level,
          style: TextStyle(color: c, fontSize: 12, fontWeight: FontWeight.w600)),
    );
  }
}

class _TierChip extends StatelessWidget {
  final String tier;
  const _TierChip({required this.tier});

  @override
  Widget build(BuildContext context) {
    final c = tierColor(tier);
    return Tooltip(
      message: 'Encryption tier: ${_tierExplanations[tier] ?? tier}',
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        decoration: BoxDecoration(
          color: c.withValues(alpha: 0.15),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Text(tier, style: TextStyle(color: c, fontSize: 12)),
      ),
    );
  }
}

/// Plan chip: colored by plan strength, visually distinct for a comped
/// account (mirrors `BillingSection`'s "Granted" chip) vs. a real subscription.
class _PlanChip extends StatelessWidget {
  final String plan;
  final String planName;
  final bool isComped;
  final String subscriptionStatus;

  const _PlanChip({
    required this.plan,
    required this.planName,
    required this.isComped,
    required this.subscriptionStatus,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final tooltip = isComped
        ? 'Granted by an admin (override) — not a paid subscription.'
        : plan == 'free'
            ? 'No paid subscription.'
            : 'Paid subscription — status: $subscriptionStatus.';
    return Tooltip(
      message: 'Plan: $planName. $tooltip',
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        decoration: BoxDecoration(
          color: isComped
              ? theme.colorScheme.secondaryContainer
              : planColor(plan).withValues(alpha: 0.15),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (isComped) ...[
              Icon(Icons.card_giftcard,
                  size: 12, color: theme.colorScheme.onSecondaryContainer),
              const SizedBox(width: 4),
            ],
            Text(
              isComped ? '$planName · Granted' : planName,
              style: TextStyle(
                color: isComped
                    ? theme.colorScheme.onSecondaryContainer
                    : planColor(plan),
                fontSize: 12,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Plan chip + an optional "open in Stripe" button, shared by the Users
/// table and the search-result rows.
class _PlanCell extends StatelessWidget {
  final Map<String, dynamic> user;
  final void Function(String url) onOpenStripe;

  const _PlanCell({required this.user, required this.onOpenStripe});

  @override
  Widget build(BuildContext context) {
    final plan = user['plan'] as String? ?? 'free';
    final planName = user['plan_name'] as String? ?? 'Free';
    final isComped = user['is_comped'] == true;
    final status = user['subscription_status'] as String? ?? 'none';
    final stripeUrl = user['stripe_customer_url'] as String?;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        _PlanChip(
          plan: plan,
          planName: planName,
          isComped: isComped,
          subscriptionStatus: status,
        ),
        if (stripeUrl != null && stripeUrl.isNotEmpty)
          Tooltip(
            message: 'Manage in Stripe',
            child: IconButton(
              icon: const Icon(Icons.open_in_new, size: 16),
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
              onPressed: () => onOpenStripe(stripeUrl),
            ),
          ),
      ],
    );
  }
}

class _SearchRow extends StatelessWidget {
  final Map<String, dynamic> user;
  final bool busyReset;
  final bool busyAdmin;
  final bool busyDelete;
  final VoidCallback onReset;
  final VoidCallback onToggleAdmin;
  final VoidCallback onDelete;
  final void Function(String url) onOpenStripe;

  const _SearchRow({
    required this.user,
    required this.busyReset,
    required this.busyAdmin,
    required this.busyDelete,
    required this.onReset,
    required this.onToggleAdmin,
    required this.onDelete,
    required this.onOpenStripe,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final tier = user['encryption_tier'] as String? ?? 'none';
    final isAdmin = user['is_admin'] == true;
    final label = (user['display_name'] as String?)?.isNotEmpty == true
        ? user['display_name'] as String
        : (user['email'] as String? ?? '#${user['id']}');

    final info = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (isAdmin) ...[const _AdminShieldIcon(), const SizedBox(width: 6)],
            Flexible(
              child: Text(label,
                  style: theme.textTheme.bodyMedium, overflow: TextOverflow.ellipsis),
            ),
          ],
        ),
        // An email is a single unbreakable token: ellipsize rather than let it
        // wrap (per-character, in a squeezed slot).
        Text(user['email'] as String? ?? '',
            style: theme.textTheme.bodySmall,
            maxLines: 1,
            overflow: TextOverflow.ellipsis),
      ],
    );

    final resetButton = busyReset
        ? const SizedBox(
            width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
        : OutlinedButton(
            onPressed: onReset,
            child: const Text('Reset password'),
          );

    final adminButton = busyAdmin
        ? const SizedBox(
            width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
        : OutlinedButton(
            onPressed: onToggleAdmin,
            child: Text(isAdmin ? 'Remove admin' : 'Make admin'),
          );

    final deleteButton = busyDelete
        ? const SizedBox(
            width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
        : OutlinedButton(
            style: OutlinedButton.styleFrom(
                foregroundColor: Theme.of(context).colorScheme.error),
            onPressed: onDelete,
            child: const Text('Delete user'),
          );

    // A Wrap (not a Row of fixed-width buttons) so on narrow widths the
    // controls reflow onto a second line instead of squeezing `info` to zero.
    final controls = Wrap(
      spacing: 12,
      runSpacing: 8,
      alignment: WrapAlignment.end,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: [
        _PlanCell(user: user, onOpenStripe: onOpenStripe),
        _TierChip(tier: tier),
        adminButton,
        deleteButton,
        resetButton,
      ],
    );

    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: LayoutBuilder(
        builder: (context, constraints) {
          // Below this width the controls alone need most of the row, so stack
          // instead of letting `info` get squeezed toward zero width.
          if (constraints.maxWidth >= 560) {
            return Row(
              crossAxisAlignment: CrossAxisAlignment.center,
              children: [
                Expanded(flex: 2, child: info),
                const SizedBox(width: 12),
                // Also flexed: as a non-flex child the Wrap would be laid out
                // with unbounded width, put every control on one line and take
                // the whole row — leaving `info` 0 px wide.
                Expanded(flex: 3, child: controls),
              ],
            );
          }
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [info, const SizedBox(height: 8), controls],
          );
        },
      ),
    );
  }
}

class _ErrorView extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;
  const _ErrorView({required this.message, required this.onRetry});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.error_outline, color: kAccent, size: 40),
          const SizedBox(height: 12),
          Text(message),
          const SizedBox(height: 12),
          FilledButton(onPressed: onRetry, child: const Text('Retry')),
        ],
      ),
    );
  }
}

// ── Reusable section card (mirrors settings_screen) ──────────────────────────

class _SectionCard extends StatelessWidget {
  final String title;
  final IconData icon;
  final Widget child;

  const _SectionCard({
    required this.title,
    required this.icon,
    required this.child,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, color: theme.colorScheme.primary, size: 20),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(title,
                      style: theme.textTheme.titleLarge,
                      overflow: TextOverflow.ellipsis),
                ),
              ],
            ),
            const Divider(height: 24),
            child,
          ],
        ),
      ),
    );
  }
}
