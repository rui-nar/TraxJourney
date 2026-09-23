import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../auth/auth_notifier.dart';
import '../auth/verify_email_banner.dart';
import '../billing/upgrade_sheet.dart';
import '../core/brand.dart';
import '../core/project_ref.dart';
import 'pending_invites_card.dart';
import 'project_file.dart';
import 'projects_notifier.dart';

class ProjectsScreen extends StatefulWidget {
  const ProjectsScreen({super.key});

  @override
  State<ProjectsScreen> createState() => _ProjectsScreenState();
}

class _ProjectsScreenState extends State<ProjectsScreen> {
  final _nameCtrl = TextEditingController();

  @override
  void dispose() {
    _nameCtrl.dispose();
    super.dispose();
  }

  Future<String?> _askImportName(
      BuildContext ctx, String defaultName) async {
    final ctrl = TextEditingController(text: defaultName);
    final name = await showDialog<String>(
      context: ctx,
      builder: (dlgCtx) => AlertDialog(
        title: const Text('Project name'),
        content: TextField(
          controller: ctrl,
          autofocus: true,
          decoration:
              const InputDecoration(hintText: 'Enter a name for this project'),
          textInputAction: TextInputAction.done,
          onSubmitted: (v) =>
              Navigator.of(dlgCtx).pop(v.trim()),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dlgCtx).pop(null),
            child: const Text('Cancel'),
          ),
          ElevatedButton(
            onPressed: () =>
                Navigator.of(dlgCtx).pop(ctrl.text.trim()),
            child: const Text('Import'),
          ),
        ],
      ),
    );
    ctrl.dispose();
    return (name == null || name.isEmpty) ? null : name;
  }

  Future<void> _confirmDelete(String name) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete project?'),
        content: Text('Delete "$name"? This cannot be undone.'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          TextButton(
            style: TextButton.styleFrom(
                foregroundColor: Theme.of(ctx).colorScheme.error),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed == true && mounted) {
      context.read<ProjectsNotifier>().delete(name);
    }
  }

  Future<void> _logout() async {
    await context.read<AuthNotifier>().logout();
    if (mounted) context.go('/login');
  }

  Future<void> _createProject() async {
    final name = _nameCtrl.text.trim();
    if (name.isEmpty) return;
    final notifier = context.read<ProjectsNotifier>();
    await notifier.create(name);
    if (!mounted) return;
    if (notifier.error == null) {
      _nameCtrl.clear();
      final encoded = Uri.encodeComponent(name);
      context.go('/view?project=$encoded');
      return;
    }
    await _maybeShowUpgrade(notifier);
  }

  /// A plan limit is not an error the user made — show the upgrade prompt
  /// instead of leaving them with a red message (issue #121).
  Future<void> _maybeShowUpgrade(ProjectsNotifier notifier) async {
    final quota = notifier.quotaError;
    if (quota == null || !mounted) return;
    notifier.clearQuotaError();
    await showUpgradeSheet(context, quota);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final auth = context.watch<AuthNotifier>();

    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            Icon(Icons.map_rounded,
                color: theme.colorScheme.primary, size: 22),
            const SizedBox(width: 8),
            const Text(kAppName),
          ],
        ),
        actions: [
          if (auth.user?.isAdmin ?? false)
            IconButton(
              icon: const Icon(Icons.admin_panel_settings_outlined),
              tooltip: 'Admin dashboard',
              onPressed: () => context.push('/admin'),
            ),
          IconButton(
            icon: const Icon(Icons.settings_outlined),
            tooltip: 'Settings',
            onPressed: () => context.push('/settings'),
          ),
          // Avatar (Google users)
          if (auth.user?.avatarUrl.isNotEmpty == true)
            Padding(
              padding: const EdgeInsets.only(right: 4),
              child: CircleAvatar(
                radius: 16,
                child: ClipOval(
                  child: Image.network(
                    auth.user!.avatarUrl,
                    width: 32,
                    height: 32,
                    fit: BoxFit.cover,
                    errorBuilder: (_, __, ___) =>
                        const Icon(Icons.person, size: 20),
                  ),
                ),
              ),
            ),
          TextButton(
            onPressed: _logout,
            child: const Text('Logout'),
          ),
          const SizedBox(width: 8),
        ],
      ),
      body: Stack(
        children: [
          // ── Background: gradient + angular geometry ───────────────────────
          Positioned.fill(
            child: DecoratedBox(
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: theme.brightness == Brightness.dark
                      ? const [Color(0xFF0D1B2A), Color(0xFF1B2838)]
                      : const [Color(0xFFF1F5F9), Color(0xFFE2E8F0)],
                ),
              ),
            ),
          ),
          Positioned.fill(
            child: CustomPaint(
              painter: _BgPainter(theme.brightness == Brightness.dark),
            ),
          ),
          // ── Content ───────────────────────────────────────────────────────
          SingleChildScrollView(
        padding: const EdgeInsets.all(20),
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                // ── Unverified-email prompt (issue #110) ──────────────────
                const VerifyEmailBanner(),
                // ── Invites addressed to me (issue #110) ──────────────────
                const PendingInvitesCard(),
                // ── Error banner ──────────────────────────────────────────
                Consumer<ProjectsNotifier>(
                  builder: (_, notifier, __) {
                    if (notifier.error == null) return const SizedBox.shrink();
                    return Container(
                      margin: const EdgeInsets.only(bottom: 16),
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: theme.colorScheme.error.withValues(alpha: 0.12),
                        borderRadius: BorderRadius.circular(8),
                        border:
                            Border.all(color: theme.colorScheme.error),
                      ),
                      child: Text(notifier.error!,
                          style:
                              TextStyle(color: theme.colorScheme.error)),
                    );
                  },
                ),

                // ── New project ───────────────────────────────────────────
                _SectionCard(
                  title: 'New Project',
                  icon: Icons.add_circle_outline,
                  child: Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _nameCtrl,
                          decoration: const InputDecoration(
                              hintText: 'Project name…'),
                          textInputAction: TextInputAction.done,
                          onSubmitted: (_) => _createProject(),
                        ),
                      ),
                      const SizedBox(width: 12),
                      Consumer<ProjectsNotifier>(
                        builder: (_, notifier, __) => ElevatedButton(
                          onPressed:
                              notifier.isLoading ? null : _createProject,
                          style: ElevatedButton.styleFrom(
                              minimumSize: const Size(96, 44)),
                          child: notifier.isLoading
                              ? const SizedBox(
                                  width: 18,
                                  height: 18,
                                  child: CircularProgressIndicator(
                                      strokeWidth: 2,
                                      color: Colors.white),
                                )
                              : const Text('Create'),
                        ),
                      ),
                    ],
                  ),
                ),

                const SizedBox(height: 16),

                // ── My trips ─────────────────────────────────────────────
                _SectionCard(
                  title: 'My Trips',
                  icon: Icons.folder_open_outlined,
                  child: Consumer<ProjectsNotifier>(
                    builder: (_, notifier, __) {
                      if (notifier.isLoading && notifier.projects.isEmpty) {
                        return const Padding(
                          padding: EdgeInsets.symmetric(vertical: 24),
                          child:
                              Center(child: CircularProgressIndicator()),
                        );
                      }
                      final mine = notifier.projects
                          .where((p) => !p.isSharedWithMe)
                          .toList();
                      if (mine.isEmpty) {
                        return Padding(
                          padding:
                              const EdgeInsets.symmetric(vertical: 24),
                          child: Center(
                            child: Text('No saved projects yet.',
                                style: theme.textTheme.bodySmall),
                          ),
                        );
                      }
                      return Column(
                        children: [
                          for (int i = 0; i < mine.length; i++) ...[
                            if (i > 0) const Divider(height: 1),
                            _ProjectTile(
                              project: mine[i],
                              onDelete: _confirmDelete,
                            ),
                          ],
                        ],
                      );
                    },
                  ),
                ),

                // ── Shared with me (issue #106) ─────────────────────────────
                Consumer<ProjectsNotifier>(
                  builder: (_, notifier, __) {
                    final shared = notifier.projects
                        .where((p) => p.isSharedWithMe)
                        .toList();
                    if (shared.isEmpty) return const SizedBox.shrink();
                    return Column(
                      children: [
                        const SizedBox(height: 16),
                        _SectionCard(
                          title: 'Shared With Me',
                          icon: Icons.group_outlined,
                          child: Column(
                            children: [
                              for (int i = 0; i < shared.length; i++) ...[
                                if (i > 0) const Divider(height: 1),
                                _ProjectTile(project: shared[i]),
                              ],
                            ],
                          ),
                        ),
                      ],
                    );
                  },
                ),

                const SizedBox(height: 16),

                // ── Import ────────────────────────────────────────────────
                _SectionCard(
                  title: 'Import',
                  icon: Icons.upload_file_outlined,
                  child: Consumer<ProjectsNotifier>(
                    builder: (_, notifier, __) => Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Import a .$kProjectFileExtension project file.',
                          style: theme.textTheme.bodySmall,
                        ),
                        const SizedBox(height: 16),
                        OutlinedButton.icon(
                          onPressed: notifier.isLoading
                              ? null
                              : () async {
                                  try {
                                    final picked =
                                        await notifier.pickProjectFile();
                                    if (picked == null || !context.mounted) {
                                      return;
                                    }
                                    final name = await _askImportName(
                                        context, picked.defaultName);
                                    if (name == null || !context.mounted) {
                                      return;
                                    }
                                    final result =
                                        await notifier.uploadProjectFile(
                                      bytes: picked.bytes,
                                      name: name,
                                    );
                                    if (result != null && context.mounted) {
                                      context.go(
                                          '/view?project=${Uri.encodeComponent(result)}');
                                    } else if (context.mounted) {
                                      await _maybeShowUpgrade(notifier);
                                    }
                                  } on Exception catch (e) {
                                    notifier.setError(e.toString());
                                  }
                                },
                          icon: const Icon(Icons.upload_rounded),
                          label: Text(notifier.isLoading
                              ? 'Importing…'
                              : 'Choose .$kProjectFileExtension file'),
                        ),
                      ],
                    ),
                  ),
                ),

              ],
            ),
          ),
        ),
        ),  // SingleChildScrollView
        ],  // Stack children
      ),   // Stack
    );
  }
}

// ── Background painter ────────────────────────────────────────────────────────

class _BgPainter extends CustomPainter {
  final bool dark;
  const _BgPainter(this.dark);

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    final h = size.height;

    // Diagonal hairline grid (45°)
    final gp = Paint()
      ..color = dark
          ? const Color(0xFFFFFFFF).withValues(alpha: 0.03)
          : const Color(0xFF1D6CF6).withValues(alpha: 0.04)
      ..strokeWidth = 1;
    const step = 48.0;
    for (double t = -h; t < w + h; t += step) {
      canvas.drawLine(Offset(t, 0), Offset(t + h, h), gp);
    }

    // Angular corner wedge (top-right)
    final ap = Paint()
      ..color = const Color(0xFF1D6CF6)
          .withValues(alpha: dark ? 0.05 : 0.04);
    canvas.drawPath(
      Path()
        ..moveTo(w * 0.42, 0)
        ..lineTo(w, 0)
        ..lineTo(w, h * 0.36)
        ..close(),
      ap,
    );
  }

  @override
  bool shouldRepaint(_BgPainter old) => old.dark != dark;
}

// ── Project tile (own or shared — issue #106) ─────────────────────────────────

/// One row in "My Trips" / "Shared With Me": name (+ owner chip when shared),
/// an Open button, and — only for [onDelete] != null, i.e. owned projects —
/// a delete button. Shared tiles have no delete/rename here; leaving a shared
/// trip is member-management UI, out of scope for this unit (see #106 U5).
class _ProjectTile extends StatelessWidget {
  final Map<String, dynamic> project;
  final void Function(String name)? onDelete;

  const _ProjectTile({required this.project, this.onDelete});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final name = project['name'] as String? ?? 'Untitled';
    final ref = project.ref;
    final ownerName = project.ownerName;
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(horizontal: 0, vertical: 4),
      leading: Icon(Icons.map_outlined, color: theme.colorScheme.primary),
      title: Text(name, style: theme.textTheme.bodyMedium),
      subtitle: ownerName != null && ownerName.isNotEmpty
          ? Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Chip(
                visualDensity: VisualDensity.compact,
                materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                avatar: const Icon(Icons.person_outline, size: 14),
                label: Text('Shared by $ownerName',
                    style: theme.textTheme.bodySmall),
                padding: const EdgeInsets.symmetric(horizontal: 4),
              ),
            )
          : null,
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (onDelete != null) ...[
            IconButton(
              icon: const Icon(Icons.delete_outline, size: 20),
              tooltip: 'Delete project',
              color: theme.colorScheme.error,
              onPressed: () => onDelete!(name),
            ),
            const SizedBox(width: 4),
          ],
          ElevatedButton(
            onPressed: () => context.go(
                ref.withOwner('/view?project=${Uri.encodeComponent(name)}')),
            style: ElevatedButton.styleFrom(
              minimumSize: const Size(72, 36),
              padding: const EdgeInsets.symmetric(horizontal: 16),
            ),
            child: const Text('Open'),
          ),
        ],
      ),
    );
  }
}

// ── Reusable section card ─────────────────────────────────────────────────────

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
                Text(title, style: theme.textTheme.titleLarge),
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
