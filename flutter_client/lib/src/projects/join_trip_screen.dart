/// Invite-accept screen for `/join/{token}` (issue #106 — travel companion).
///
/// On load it previews the invite (GET /api/invites/{token}) and shows
/// "«Owner» invites you to join «Trip»" with a Join button, plus what the
/// invite's role (issue #109) will let the caller do. Accepting joins the
/// caller with that role and navigates into the trip. A 404 (unknown/revoked
/// token) shows a friendly message with a way home; a 409 means the caller
/// already owns the trip — it simply opens as owner. Unauthenticated visits
/// never reach this screen: app_router.dart redirects them to
/// `/login?return_to=/join/{token}` so the link survives the login round-trip.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../api/client.dart';
import 'members_service.dart';
import 'projects_notifier.dart';

/// User-facing summary of what [role] grants, shown on the invite preview.
String _roleDescription(String role) => switch (role) {
      'viewer' => 'You will be able to view this trip (read-only).',
      'co-owner' =>
        'You will be able to add and edit trip content, and manage sharing '
            'and companions.',
      _ /* editor */ =>
        'You will be able to add and edit trip content as an editor.',
    };

class JoinTripScreen extends StatefulWidget {
  final String token;
  final MembersService service;

  JoinTripScreen({super.key, required this.token, MembersService? service})
      : service = service ?? MembersService();

  @override
  State<JoinTripScreen> createState() => _JoinTripScreenState();
}

class _JoinTripScreenState extends State<JoinTripScreen> {
  bool _loading = true;
  bool _invalid = false; // 404 — unknown or revoked token
  bool _joining = false;
  String? _error;
  InvitePreview? _preview;

  @override
  void initState() {
    super.initState();
    _loadPreview();
  }

  Future<void> _loadPreview() async {
    try {
      final preview = await widget.service.previewInvite(widget.token);
      if (!mounted) return;
      setState(() {
        _preview = preview;
        _loading = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _invalid = e.statusCode == 404;
        _error = e.statusCode == 404 ? null : apiErrorMessage(e);
      });
    } catch (e) {
      // Not just Exception: an unreachable/misconfigured HTTP client can
      // throw Errors too — never let the invite preview crash the screen.
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = apiErrorMessage(e);
      });
    }
  }

  /// Best-effort projects-list refresh so the joined trip shows up under
  /// "Shared With Me" without a manual reload.
  void _refreshProjectsList() {
    try {
      context.read<ProjectsNotifier>().load();
    } on ProviderNotFoundException {
      // Not in the tree (tests) — the list loads on next visit anyway.
    }
  }

  Future<void> _join() async {
    setState(() {
      _joining = true;
      _error = null;
    });
    try {
      final joined = await widget.service.acceptInvite(
        widget.token,
        role: _preview?.role ?? 'editor',
      );
      if (!mounted) return;
      _refreshProjectsList();
      context.go(
          joined.withOwner('/view?project=${Uri.encodeComponent(joined.name)}'));
    } on ApiException catch (e) {
      if (!mounted) return;
      if (e.statusCode == 409) {
        // The caller owns this trip — just open it (as owner, no ?owner=).
        final name = _preview?.projectName ?? '';
        _refreshProjectsList();
        context.go('/view?project=${Uri.encodeComponent(name)}');
        return;
      }
      setState(() {
        _joining = false;
        _invalid = e.statusCode == 404;
        _error = e.statusCode == 404 ? null : apiErrorMessage(e);
      });
    } catch (e) {
      // See _loadPreview — keep any failure inline.
      if (!mounted) return;
      setState(() {
        _joining = false;
        _error = apiErrorMessage(e);
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 400),
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Card(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: _body(context),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _body(BuildContext context) {
    final theme = Theme.of(context);
    if (_loading) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 24),
        child: Center(
          child: SizedBox(
              width: 24,
              height: 24,
              child: CircularProgressIndicator(strokeWidth: 2)),
        ),
      );
    }
    if (_invalid) {
      return Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Icon(Icons.link_off, size: 40, color: theme.colorScheme.outline),
          const SizedBox(height: 16),
          Text(
            'This invite link is invalid or has been revoked.',
            style: theme.textTheme.titleMedium,
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 8),
          Text(
            'Ask the trip owner for a new link.',
            style: theme.textTheme.bodySmall,
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 20),
          ElevatedButton(
            onPressed: () => context.go('/projects'),
            child: const Text('Go to my trips'),
          ),
        ],
      );
    }
    final preview = _preview;
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Icon(Icons.map_rounded, size: 32, color: theme.colorScheme.primary),
        const SizedBox(height: 16),
        if (preview != null)
          Text.rich(
            TextSpan(
              children: [
                TextSpan(
                    text: preview.ownerName,
                    style: const TextStyle(fontWeight: FontWeight.w700)),
                const TextSpan(text: ' invites you to join '),
                TextSpan(
                    text: preview.projectName,
                    style: const TextStyle(fontWeight: FontWeight.w700)),
              ],
            ),
            style: theme.textTheme.titleMedium,
            textAlign: TextAlign.center,
          ),
        if (preview != null) ...[
          const SizedBox(height: 8),
          Text(
            _roleDescription(preview.role),
            style: theme.textTheme.bodySmall,
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 20),
          ElevatedButton(
            onPressed: _joining ? null : _join,
            child: _joining
                ? const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Join trip'),
          ),
          const SizedBox(height: 8),
          TextButton(
            onPressed: () => context.go('/projects'),
            child: const Text('Not now'),
          ),
        ],
        if (_error != null) ...[
          const SizedBox(height: 12),
          Text(
            _error!,
            style: TextStyle(color: theme.colorScheme.error, fontSize: 12.5),
            textAlign: TextAlign.center,
          ),
          if (preview == null) ...[
            const SizedBox(height: 12),
            ElevatedButton(
              onPressed: () => context.go('/projects'),
              child: const Text('Go to my trips'),
            ),
          ],
        ],
      ],
    );
  }
}
