/// "Travel companions" settings section (issue #106; roles: issue #109;
/// invite emails: issue #113).
///
/// Rendered inside ProjectSettingsScreen's section card. Co-owner+ view:
/// member list with per-member remove (a co-owner can't remove another
/// co-owner — owner-only, to avoid co-owners locking each other out), plus
/// an invite-link block with a role picker (viewer/editor, and co-owner for
/// the strict owner only) — create → copy URL → revoke, with an optional
/// email field to have the server send the join link directly; a 409 from
/// an E2EE-enabled account surfaces the server's message inline. Editor/
/// viewer view: read-only member list plus a "Leave trip" action that
/// removes the caller's own membership and returns to the projects list.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../api/client.dart';
import '../auth/auth_notifier.dart';
import '../core/design_tokens.dart' show kMonoFontFamily, kWarningDark;
import '../core/last_opened_project.dart';
import 'members_service.dart';
import 'project_notifier.dart';
import 'projects_notifier.dart';

// Local palette mirroring project_settings_screen.dart's design tokens so the
// section blends into the settings screen it is embedded in.
const _kBg = Color(0xFF0A1320);
const _kBgCard = Color(0xFF0F1A26);
const _kBorder = Color(0xFF1F2F42);
const _kText1 = Color(0xFFF1F5F9);
const _kText2 = Color(0xFFCBD5E1);
const _kMuted = Color(0xFF94A3B8);
const _kDim = Color(0xFF64748B);
const _kBlueActive = Color(0xFF60A5FA);
const _kRed = Color(0xFFEF4444);

/// Display label for a member/invite role (issue #109).
String roleLabel(String role) => switch (role) {
      'viewer' => 'Viewer',
      'co-owner' => 'Co-owner',
      'owner' => 'Owner',
      _ => 'Editor',
    };

String _roleHint(String role) => switch (role) {
      'viewer' => 'Anyone with the link who signs in can view this trip (read-only).',
      'co-owner' =>
        'Anyone with the link who signs in can join as co-owner — full '
            'access except delete/transfer.',
      _ => 'Anyone with the link who signs in can join as editor.',
    };

class TravelCompanionsSection extends StatefulWidget {
  const TravelCompanionsSection({super.key});

  @override
  State<TravelCompanionsSection> createState() =>
      _TravelCompanionsSectionState();
}

class _TravelCompanionsSectionState extends State<TravelCompanionsSection> {
  bool _loading = true;
  String? _loadError;

  // Inline error under the invite block (409 E2EE message, network errors).
  String? _inviteError;
  bool _inviteBusy = false;
  bool _leaving = false;

  /// Pending invite currently being revoked, so only that row's button
  /// disables rather than the whole list (issue #110).
  int? _revokingId;

  // Role picked for the *next* invite creation (issue #109) — not the role
  // of an already-created link, which is memberInviteRole on the notifier.
  String _inviteRoleChoice = 'editor';

  // Recipient for the optional "email this link" action (issue #113).
  final _inviteEmailCtrl = TextEditingController();

  @override
  void initState() {
    super.initState();
    // Deferred: loadMembers() calls notifyListeners(), which must not fire
    // during the first build (see feedback_patterns — ValueNotifier timing).
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  @override
  void dispose() {
    _inviteEmailCtrl.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    if (!mounted) return;
    setState(() {
      _loading = true;
      _loadError = null;
    });
    final notifier = context.read<ProjectNotifier>();
    try {
      await notifier.loadMembers();
      if (mounted) setState(() => _loading = false);
    } on Exception catch (e) {
      if (mounted) {
        setState(() {
          _loading = false;
          _loadError = apiErrorMessage(e);
        });
      }
      return;
    }
    // Supplementary (issue #110), and co-owner+ only — so its failure must not
    // take down the member list the section exists to show. A 403 for an
    // editor/viewer is expected and already swallowed by the notifier.
    try {
      await notifier.loadPendingInvites();
    } on Exception {
      // Leaves the pending block hidden; the member list stands.
    }
  }

  String _joinUrl(String token) {
    var origin = api.baseUrl;
    if (origin.isEmpty) {
      try {
        origin = Uri.base.origin;
      } catch (_) {
        // Non-http Uri.base (tests) — relative URL is still copyable.
      }
    }
    return '$origin/join/$token';
  }

  Future<void> _createInvite() async {
    final email = _inviteEmailCtrl.text.trim();
    setState(() {
      _inviteBusy = true;
      _inviteError = null;
    });
    try {
      await context.read<ProjectNotifier>().createMemberInvite(
          role: _inviteRoleChoice, email: email.isEmpty ? null : email);
      // The send itself is fire-and-forget background work on the server
      // (issue #113) — this confirms it was queued, not that it arrived.
      if (email.isNotEmpty && mounted) {
        _inviteEmailCtrl.clear();
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('Invite sent to $email')));
        // Show the new row in "Waiting to accept" straight away (issue #110).
        await context.read<ProjectNotifier>().loadPendingInvites();
      }
    } on Exception catch (e) {
      // 409 (E2EE-enabled account), 422 (malformed email), and any other
      // failure surface inline — no dialog.
      if (mounted) setState(() => _inviteError = apiErrorMessage(e));
    } finally {
      if (mounted) setState(() => _inviteBusy = false);
    }
  }

  Future<void> _revokePending(PendingInvite invite) async {
    setState(() {
      _revokingId = invite.id;
      _inviteError = null;
    });
    try {
      await context.read<ProjectNotifier>().revokePendingInvite(invite.id);
    } on Exception catch (e) {
      // The notifier restores the row on failure, so the list stays honest.
      if (mounted) setState(() => _inviteError = apiErrorMessage(e));
    } finally {
      if (mounted) setState(() => _revokingId = null);
    }
  }

  Future<void> _revokeInvite() async {
    setState(() {
      _inviteBusy = true;
      _inviteError = null;
    });
    try {
      await context.read<ProjectNotifier>().revokeMemberInvite();
    } on Exception catch (e) {
      if (mounted) setState(() => _inviteError = apiErrorMessage(e));
    } finally {
      if (mounted) setState(() => _inviteBusy = false);
    }
  }

  Future<void> _removeMember(ProjectMember member) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Remove companion?'),
        content: Text(
          '${member.displayName.isEmpty ? 'This member' : member.displayName} '
          'will lose access to this trip. Their journal entries stay private '
          'to them.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(minimumSize: const Size(96, 44)),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Remove'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    try {
      await context.read<ProjectNotifier>().removeMember(member.userId);
    } on Exception catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text(apiErrorMessage(e))));
      }
    }
  }

  Future<void> _leaveTrip(ProjectMember self) async {
    final notifier = context.read<ProjectNotifier>();
    final projectName = notifier.projectName ?? '';
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Leave trip?'),
        content: Text(
          'You will lose access to "$projectName" until the owner invites '
          'you again.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(minimumSize: const Size(96, 44)),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Leave'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _leaving = true);
    try {
      final leftRef = notifier.ref;
      final userId = context.read<AuthNotifier>().user?.id;
      await notifier.removeMember(self.userId);
      // Forget the last-opened project if it was this one, so the bare-root
      // redirect doesn't drop the user back into a trip they just left.
      if (leftRef != null) {
        final last = await readLastOpenedProject(userId);
        if (last != null &&
            last.name == leftRef.name &&
            last.ownerId == leftRef.ownerId) {
          await clearLastOpenedProject(userId);
        }
      }
      if (!mounted) return;
      // Refresh the projects list (drops the trip from "Shared With Me").
      try {
        context.read<ProjectsNotifier>().load();
      } on ProviderNotFoundException {
        // Not in the tree (tests) — the list reloads on next visit anyway.
      }
      context.go('/projects');
    } on Exception catch (e) {
      if (mounted) {
        setState(() => _leaving = false);
        ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text(apiErrorMessage(e))));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final notifier = context.watch<ProjectNotifier>();
    final selfId = context.watch<AuthNotifier>().user?.id;
    final canManage = notifier.canManageTrip;
    final isOwner = notifier.isProjectOwner;
    final members = notifier.members;
    final ownRow = [
      for (final m in members)
        if (m.userId.toString() == selfId) m
    ];

    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (_loading)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 20),
              child: Center(
                child: SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2)),
              ),
            )
          else if (_loadError != null)
            Text(_loadError!,
                style: const TextStyle(color: _kRed, fontSize: 12.5))
          else ...[
            for (final m in members)
              _MemberRow(
                member: m,
                isSelf: m.userId.toString() == selfId,
                // Co-owner+ can remove editors/viewers; removing a co-owner
                // is strict-owner-only (server also enforces this).
                onRemove: canManage &&
                        !m.isOwner &&
                        (m.role != 'co-owner' || isOwner)
                    ? () => _removeMember(m)
                    : null,
              ),
            if (canManage) ...[
              if (notifier.pendingInvites.isNotEmpty) ...[
                const SizedBox(height: 12),
                _pendingBlock(notifier),
              ],
              const SizedBox(height: 8),
              _inviteBlock(notifier),
            ],
            if (!isOwner && ownRow.isNotEmpty) ...[
              const SizedBox(height: 12),
              const Divider(height: 1, color: _kBorder),
              const SizedBox(height: 12),
              OutlinedButton.icon(
                style: OutlinedButton.styleFrom(
                  foregroundColor: _kRed,
                  side: const BorderSide(color: Color(0x47EF4444)),
                ),
                icon: _leaving
                    ? const SizedBox(
                        width: 14,
                        height: 14,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.logout, size: 15),
                label: const Text('Leave trip'),
                onPressed: _leaving ? null : () => _leaveTrip(ownRow.first),
              ),
            ],
          ],
        ],
      ),
    );
  }

  /// Invites emailed but not yet accepted (issue #110), each revocable on its
  /// own — revoking the shared link below would instead cut off everyone.
  Widget _pendingBlock(ProjectNotifier notifier) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text('Waiting to accept',
              style: TextStyle(
                  color: _kMuted,
                  fontSize: 11.5,
                  fontWeight: FontWeight.w600,
                  letterSpacing: 0.4)),
          const SizedBox(height: 6),
          for (final p in notifier.pendingInvites)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 3),
              child: Row(
                children: [
                  const Icon(Icons.schedule, size: 15, color: _kDim),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(p.email,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: _kText2, fontSize: 13)),
                  ),
                  Text(roleLabel(p.role),
                      style: const TextStyle(color: _kDim, fontSize: 11.5)),
                  IconButton(
                    tooltip: 'Revoke invite',
                    icon: const Icon(Icons.close, size: 15, color: _kMuted),
                    onPressed: _revokingId == p.id
                        ? null
                        : () => _revokePending(p),
                  ),
                ],
              ),
            ),
        ],
      );

  Widget _emailField() => TextField(
        controller: _inviteEmailCtrl,
        onChanged: (_) => setState(() {}), // live-update the button below
        keyboardType: TextInputType.emailAddress,
        style: const TextStyle(color: _kText2, fontSize: 13),
        decoration: InputDecoration(
          isDense: true,
          hintText: 'Email address (optional) — we\'ll send them the link',
          hintStyle: const TextStyle(color: _kDim, fontSize: 12),
          filled: true,
          fillColor: _kBgCard,
          contentPadding:
              const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(8),
            borderSide: const BorderSide(color: _kBorder),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(8),
            borderSide: const BorderSide(color: _kBorder),
          ),
        ),
      );

  Widget _inviteBlock(ProjectNotifier notifier) {
    final token = notifier.memberInviteToken;
    final grantedRole = notifier.memberInviteRole ?? _inviteRoleChoice;
    final hasEmail = _inviteEmailCtrl.text.trim().isNotEmpty;
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
                    const Text('Invite link',
                        style: TextStyle(
                            fontWeight: FontWeight.w700,
                            fontSize: 14,
                            color: _kText1)),
                    Text(
                      _roleHint(grantedRole),
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
                  onPressed: _inviteBusy ? null : _revokeInvite,
                  child: const Text('Revoke',
                      style: TextStyle(fontWeight: FontWeight.w600)),
                ),
            ],
          ),
          const SizedBox(height: 10),
          if (token == null) ...[
            Wrap(
              spacing: 6,
              children: [
                for (final role in [
                  'viewer',
                  'editor',
                  if (notifier.isProjectOwner) 'co-owner',
                ])
                  ChoiceChip(
                    label: Text(roleLabel(role),
                        style: const TextStyle(fontSize: 12)),
                    selected: _inviteRoleChoice == role,
                    visualDensity: VisualDensity.compact,
                    onSelected: (_) =>
                        setState(() => _inviteRoleChoice = role),
                    backgroundColor: _kBgCard,
                    selectedColor: _kBlueActive.withAlpha(40),
                    side: const BorderSide(color: _kBorder),
                    labelStyle: const TextStyle(color: _kText2),
                  ),
              ],
            ),
            const SizedBox(height: 10),
            _emailField(),
            const SizedBox(height: 10),
            OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                foregroundColor: _kBlueActive,
                side: const BorderSide(color: Color(0x473B82F6)),
                backgroundColor: const Color(0x1A3B82F6),
              ),
              icon: _inviteBusy
                  ? const SizedBox(
                      width: 14,
                      height: 14,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : Icon(hasEmail ? Icons.mail_outline : Icons.person_add_alt,
                      size: 15),
              label: Text(hasEmail ? 'Send invite' : 'Create invite link'),
              onPressed: _inviteBusy ? null : _createInvite,
            ),
          ] else ...[
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
                      _joinUrl(token),
                      style: const TextStyle(
                        fontFamily: kMonoFontFamily,
                        fontSize: 12,
                        color: _kMuted,
                        overflow: TextOverflow.ellipsis,
                      ),
                      maxLines: 1,
                    ),
                  ),
                  const SizedBox(width: 8),
                  InkWell(
                    onTap: () => Clipboard.setData(
                        ClipboardData(text: _joinUrl(token))),
                    child: Container(
                      width: 28,
                      height: 28,
                      decoration: BoxDecoration(
                        color: const Color(0x1A3B82F6),
                        border: Border.all(color: const Color(0x473B82F6)),
                        borderRadius: BorderRadius.circular(6),
                      ),
                      child: const Icon(Icons.content_copy,
                          size: 14, color: _kBlueActive),
                    ),
                  ),
                ],
              ),
            ),
            // Email the already-existing link to someone new — same POST,
            // idempotent for the token itself (issue #113).
            const SizedBox(height: 10),
            _emailField(),
            const SizedBox(height: 10),
            OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                foregroundColor: _kBlueActive,
                side: const BorderSide(color: Color(0x473B82F6)),
                backgroundColor: const Color(0x1A3B82F6),
              ),
              icon: _inviteBusy
                  ? const SizedBox(
                      width: 14,
                      height: 14,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.mail_outline, size: 15),
              label: const Text('Send invite'),
              onPressed: (_inviteBusy || !hasEmail) ? null : _createInvite,
            ),
          ],
          if (_inviteError != null) ...[
            const SizedBox(height: 8),
            Text(
              _inviteError!,
              style: const TextStyle(color: kWarningDark, fontSize: 12.5),
            ),
          ],
        ],
      ),
    );
  }
}

class _MemberRow extends StatelessWidget {
  final ProjectMember member;
  final bool isSelf;
  final VoidCallback? onRemove;

  const _MemberRow({required this.member, required this.isSelf, this.onRemove});

  @override
  Widget build(BuildContext context) {
    final name = member.displayName.isEmpty ? '(unknown)' : member.displayName;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: _kBg,
          border: Border.all(color: _kBorder),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Row(
          children: [
            CircleAvatar(
              radius: 14,
              backgroundColor: const Color(0xFF1D4ED8),
              foregroundImage: member.avatarUrl.isNotEmpty
                  ? NetworkImage(member.avatarUrl)
                  : null,
              child: Text(
                name[0].toUpperCase(),
                style: const TextStyle(fontSize: 12, color: Colors.white),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                isSelf ? '$name (you)' : name,
                style: const TextStyle(color: _kText2, fontSize: 13.5),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              decoration: BoxDecoration(
                color: member.isOwner ? const Color(0x1A3B82F6) : _kBgCard,
                border: Border.all(
                    color: member.isOwner
                        ? const Color(0x473B82F6)
                        : _kBorder),
                borderRadius: BorderRadius.circular(20),
              ),
              child: Text(
                roleLabel(member.role),
                style: TextStyle(
                  color: member.isOwner ? _kBlueActive : _kMuted,
                  fontSize: 11,
                  fontFamily: kMonoFontFamily,
                ),
              ),
            ),
            if (onRemove != null) ...[
              const SizedBox(width: 4),
              IconButton(
                icon: const Icon(Icons.person_remove_outlined,
                    size: 16, color: _kDim),
                visualDensity: VisualDensity.compact,
                tooltip: 'Remove from trip',
                onPressed: onRemove,
              ),
            ],
          ],
        ),
      ),
    );
  }
}
