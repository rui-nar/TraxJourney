/// "You've been invited" prompt on the projects list (issue #110).
///
/// An invite addressed to your confirmed email shows here rather than the trip
/// simply appearing in your list. Accepting is an explicit act: someone who
/// knows your address should not be able to put content in your account
/// without your say-so, and declining needs somewhere to live.
///
/// Silent for an unverified account — the server returns nothing, since
/// matching on an unconfirmed address is how someone else's invite gets taken.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../api/client.dart' show apiErrorMessage;
import 'members_service.dart';
import 'projects_notifier.dart';
import 'travel_companions_section.dart' show roleLabel;

class PendingInvitesCard extends StatefulWidget {
  /// Injected in tests; defaults to the app-wide service.
  final MembersService? service;

  const PendingInvitesCard({super.key, this.service});

  @override
  State<PendingInvitesCard> createState() => _PendingInvitesCardState();
}

class _PendingInvitesCardState extends State<PendingInvitesCard> {
  late final MembersService _service = widget.service ?? MembersService();

  List<MyPendingInvite> _invites = [];
  String? _busyToken;
  String? _error;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  Future<void> _load() async {
    try {
      final invites = await _service.listMyPendingInvites();
      if (mounted) setState(() => _invites = invites);
    } catch (_) {
      // A failure here must not break the projects list — the card simply
      // doesn't appear, and the invite is still reachable from its email link.
    }
  }

  Future<void> _act(
    MyPendingInvite invite,
    Future<void> Function() action, {
    required bool refreshProjects,
  }) async {
    setState(() {
      _busyToken = invite.token;
      _error = null;
    });
    try {
      await action();
      if (!mounted) return;
      setState(() => _invites =
          [for (final i in _invites) if (i.token != invite.token) i]);
      if (refreshProjects && mounted) {
        await context.read<ProjectsNotifier>().load();
      }
    } on Exception catch (e) {
      if (mounted) setState(() => _error = apiErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busyToken = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_invites.isEmpty) return const SizedBox.shrink();
    final theme = Theme.of(context);
    const blue = Color(0xFF60A5FA);

    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: blue.withValues(alpha: 0.10),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: blue.withValues(alpha: 0.6)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (final invite in _invites)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Row(
                children: [
                  const Icon(Icons.group_add_outlined, size: 18, color: blue),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      '${invite.ownerName} invited you to '
                      '"${invite.projectName}" as '
                      '${roleLabel(invite.role).toLowerCase()}',
                      style: theme.textTheme.bodySmall,
                    ),
                  ),
                  if (_busyToken == invite.token)
                    const Padding(
                      padding: EdgeInsets.symmetric(horizontal: 12),
                      child: SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2)),
                    )
                  else ...[
                    TextButton(
                      onPressed: () => _act(
                        invite,
                        () => _service.acceptInvite(invite.token,
                            role: invite.role),
                        refreshProjects: true,
                      ),
                      child: const Text('Accept'),
                    ),
                    TextButton(
                      onPressed: () => _act(
                        invite,
                        () => _service.declineInvite(invite.token),
                        refreshProjects: false,
                      ),
                      child: const Text('Decline'),
                    ),
                  ],
                ],
              ),
            ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(_error!,
                  style: TextStyle(
                      color: theme.colorScheme.error, fontSize: 12.5)),
            ),
        ],
      ),
    );
  }
}
