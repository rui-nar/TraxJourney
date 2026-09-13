// Widget tests for the "Travel companions" settings section (issue #106;
// roles: issue #109): capability gating (owner/co-owner see remove + invite
// controls with a role picker, editor/viewer see a read-only list plus
// Leave trip), the invite create → copy URL → revoke block, the inline 409
// (E2EE) message, the remove/leave confirm flows, and the co-owner-can't-
// remove-a-co-owner guard. Services are mocked by subclassing, like
// projects_screen_shared_test.dart.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/members_service.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/travel_companions_section.dart';

class _FakeMembersService extends MembersService {
  List<ProjectMember> members;
  ApiException? createError;
  final removedIds = <int>[];
  int revokeCalls = 0;

  _FakeMembersService(this.members);

  @override
  Future<List<ProjectMember>> listMembers(ProjectRef ref) async => members;

  String? lastRequestedRole;
  String? lastRequestedEmail;

  @override
  Future<CreatedInvite> createInvite(ProjectRef ref,
      {String role = 'editor', String? email}) async {
    lastRequestedRole = role;
    lastRequestedEmail = email;
    final err = createError;
    if (err != null) throw err;
    return (token: 'tok123', role: role);
  }

  @override
  Future<void> revokeInvite(ProjectRef ref) async {
    revokeCalls++;
  }

  /// Pending email invites (issue #110). Empty unless a test sets them.
  List<PendingInvite> pending = [];
  final revokedPendingIds = <int>[];

  @override
  Future<List<PendingInvite>> listPendingInvites(ProjectRef ref) async =>
      pending;

  @override
  Future<void> revokePendingInvite(ProjectRef ref, int inviteId) async {
    revokedPendingIds.add(inviteId);
    pending = [for (final p in pending) if (p.id != inviteId) p];
  }

  @override
  Future<void> removeMember(ProjectRef ref, int userId) async {
    removedIds.add(userId);
  }
}

AuthNotifier _loggedInAuth(String userId) {
  final auth = AuthNotifier(AuthService());
  auth.updateUser({
    'id': userId,
    'email': 'a@x.com',
    'display_name': 'A',
    'auth_provider': 'local',
  });
  return auth;
}

const _owner = ProjectMember(
    userId: 1, displayName: 'Alice', avatarUrl: '', role: 'owner');
const _editor = ProjectMember(
    userId: 7, displayName: 'Bob', avatarUrl: '', role: 'editor');
const _coowner = ProjectMember(
    userId: 3, displayName: 'Cara', avatarUrl: '', role: 'co-owner');
const _viewer = ProjectMember(
    userId: 9, displayName: 'Vic', avatarUrl: '', role: 'viewer');

ProjectNotifier _notifierWith(_FakeMembersService svc, ProjectRef ref) {
  final notifier = ProjectNotifier(ProjectService(), membersService: svc);
  notifier.ref = ref;
  return notifier;
}

Widget _harness({
  required ProjectNotifier notifier,
  required String authUserId,
}) =>
    MaterialApp(
      home: Scaffold(
        body: MultiProvider(
          providers: [
            ChangeNotifierProvider<AuthNotifier>.value(
                value: _loggedInAuth(authUserId)),
            ChangeNotifierProvider<ProjectNotifier>.value(value: notifier),
          ],
          child: const SingleChildScrollView(child: TravelCompanionsSection()),
        ),
      ),
    );

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('owner sees members, a remove button on non-owner rows only, '
      'and the invite-link block — no Leave trip', (tester) async {
    final svc = _FakeMembersService([_owner, _editor]);
    final notifier = _notifierWith(svc, const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '1'));
    await tester.pump(); // post-frame loadMembers
    await tester.pump();

    expect(find.text('Alice (you)'), findsOneWidget);
    expect(find.text('Bob'), findsOneWidget);
    // Exactly one remove button — for Bob, not for the owner row.
    expect(find.byIcon(Icons.person_remove_outlined), findsOneWidget);
    expect(find.text('Create invite link'), findsOneWidget);
    expect(find.text('Leave trip'), findsNothing);
  });

  testWidgets('owner: Create invite link shows the /join URL with copy and '
      'Revoke; Revoke returns to the create button', (tester) async {
    final svc = _FakeMembersService([_owner, _editor]);
    final notifier = _notifierWith(svc, const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '1'));
    await tester.pump();
    await tester.pump();

    await tester.tap(find.text('Create invite link'));
    await tester.pumpAndSettle();

    expect(find.textContaining('/join/tok123'), findsOneWidget);
    expect(find.byIcon(Icons.content_copy), findsOneWidget);
    expect(find.text('Revoke'), findsOneWidget);

    await tester.tap(find.text('Revoke'));
    await tester.pumpAndSettle();

    expect(svc.revokeCalls, 1);
    expect(find.textContaining('/join/tok123'), findsNothing);
    expect(find.text('Create invite link'), findsOneWidget);
  });

  testWidgets('owner: a 409 from invite creation (E2EE account) surfaces the '
      "server's message inline, without a dialog", (tester) async {
    final svc = _FakeMembersService([_owner])
      ..createError = ApiException(
          409, '{"detail": "Travel companions are not available on encrypted accounts"}');
    final notifier = _notifierWith(svc, const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '1'));
    await tester.pump();
    await tester.pump();

    await tester.tap(find.text('Create invite link'));
    await tester.pumpAndSettle();

    expect(find.textContaining('encrypted accounts'), findsOneWidget);
    expect(find.byType(AlertDialog), findsNothing);
    // Still no invite URL, and the create button remains available.
    expect(find.textContaining('/join/'), findsNothing);
    expect(find.text('Create invite link'), findsOneWidget);
  });

  testWidgets('owner: removing a member asks for confirmation, then removes '
      'the row and calls the service', (tester) async {
    final svc = _FakeMembersService([_owner, _editor]);
    final notifier = _notifierWith(svc, const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '1'));
    await tester.pump();
    await tester.pump();

    await tester.tap(find.byIcon(Icons.person_remove_outlined));
    await tester.pumpAndSettle();
    expect(find.text('Remove companion?'), findsOneWidget);

    await tester.tap(find.widgetWithText(ElevatedButton, 'Remove'));
    await tester.pumpAndSettle();

    expect(svc.removedIds, [7]);
    expect(find.text('Bob'), findsNothing);
  });

  testWidgets('editor sees a read-only member list and Leave trip — no '
      'remove buttons, no invite block', (tester) async {
    final svc = _FakeMembersService([_owner, _editor]);
    final notifier = _notifierWith(
        svc, const ProjectRef(name: 'Trip', ownerId: 1, role: 'editor'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '7'));
    await tester.pump();
    await tester.pump();

    expect(find.text('Alice'), findsOneWidget);
    expect(find.text('Bob (you)'), findsOneWidget);
    expect(find.byIcon(Icons.person_remove_outlined), findsNothing);
    expect(find.text('Create invite link'), findsNothing);
    expect(find.text('Leave trip'), findsOneWidget);
  });

  testWidgets('editor: Leave trip confirms, deletes own membership and '
      'navigates to the projects list', (tester) async {
    final svc = _FakeMembersService([_owner, _editor]);
    final notifier = _notifierWith(
        svc, const ProjectRef(name: 'Trip', ownerId: 1, role: 'editor'));

    final router = GoRouter(
      initialLocation: '/settings',
      routes: [
        GoRoute(
          path: '/settings',
          builder: (context, state) => Scaffold(
            body: SingleChildScrollView(child: TravelCompanionsSection()),
          ),
        ),
        GoRoute(
          path: '/projects',
          builder: (context, state) =>
              const Scaffold(body: Text('PROJECTS LIST')),
        ),
      ],
    );

    await tester.pumpWidget(
      MultiProvider(
        providers: [
          ChangeNotifierProvider<AuthNotifier>.value(value: _loggedInAuth('7')),
          ChangeNotifierProvider<ProjectNotifier>.value(value: notifier),
        ],
        child: MaterialApp.router(routerConfig: router),
      ),
    );
    await tester.pump();
    await tester.pump();

    await tester.tap(find.text('Leave trip'));
    await tester.pumpAndSettle();
    expect(find.text('Leave trip?'), findsOneWidget);

    await tester.tap(find.widgetWithText(ElevatedButton, 'Leave'));
    await tester.pumpAndSettle();

    expect(svc.removedIds, [7]);
    expect(find.text('PROJECTS LIST'), findsOneWidget);
  });

  // ── Roles (issue #109) ────────────────────────────────────────────────────

  testWidgets('viewer sees a read-only member list and Leave trip — same as '
      'editor', (tester) async {
    final svc = _FakeMembersService([_owner, _viewer]);
    final notifier = _notifierWith(
        svc, const ProjectRef(name: 'Trip', ownerId: 1, role: 'viewer'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '9'));
    await tester.pump();
    await tester.pump();

    expect(find.byIcon(Icons.person_remove_outlined), findsNothing);
    expect(find.text('Create invite link'), findsNothing);
    expect(find.text('Leave trip'), findsOneWidget);
  });

  testWidgets('co-owner sees the invite/remove controls like an owner, but '
      'the role picker has no Co-owner option', (tester) async {
    final svc = _FakeMembersService([_owner, _coowner, _editor]);
    final notifier = _notifierWith(
        svc, const ProjectRef(name: 'Trip', ownerId: 1, role: 'co-owner'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '3'));
    await tester.pump();
    await tester.pump();

    // Bob (editor) is removable; Alice (owner) and the caller's own row are not.
    expect(find.byIcon(Icons.person_remove_outlined), findsOneWidget);
    expect(find.text('Create invite link'), findsOneWidget);
    // A co-owner is not the strict owner, so they can still leave too.
    expect(find.text('Leave trip'), findsOneWidget);
    // …and isn't allowed to grant co-owner access via invite (member-row
    // role badges can also read "Editor"/"Viewer", so scope to the chips).
    expect(find.widgetWithText(ChoiceChip, 'Viewer'), findsOneWidget);
    expect(find.widgetWithText(ChoiceChip, 'Editor'), findsOneWidget);
    expect(find.widgetWithText(ChoiceChip, 'Co-owner'), findsNothing);
  });

  testWidgets('owner sees a Co-owner invite option; selecting it requests '
      'role=co-owner', (tester) async {
    final svc = _FakeMembersService([_owner]);
    final notifier = _notifierWith(svc, const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '1'));
    await tester.pump();
    await tester.pump();

    expect(find.text('Co-owner'), findsOneWidget);
    await tester.tap(find.text('Co-owner'));
    await tester.pump();
    await tester.tap(find.text('Create invite link'));
    await tester.pumpAndSettle();

    expect(svc.lastRequestedRole, 'co-owner');
  });

  testWidgets('selecting Viewer before creating requests role=viewer',
      (tester) async {
    final svc = _FakeMembersService([_owner]);
    final notifier = _notifierWith(svc, const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '1'));
    await tester.pump();
    await tester.pump();

    await tester.tap(find.text('Viewer'));
    await tester.pump();
    await tester.tap(find.text('Create invite link'));
    await tester.pumpAndSettle();

    expect(svc.lastRequestedRole, 'viewer');
  });

  testWidgets('co-owner cannot remove another co-owner (owner-only)',
      (tester) async {
    final svc = _FakeMembersService([_owner, _coowner, const ProjectMember(
        userId: 5, displayName: 'Dee', avatarUrl: '', role: 'co-owner')]);
    final notifier = _notifierWith(
        svc, const ProjectRef(name: 'Trip', ownerId: 1, role: 'co-owner'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '3'));
    await tester.pump();
    await tester.pump();

    // Neither the owner row, the caller's own co-owner row, nor Dee's
    // co-owner row is removable by a fellow (non-strict-owner) co-owner.
    expect(find.byIcon(Icons.person_remove_outlined), findsNothing);
  });

  // ── Pending email invites (issue #110) ──────────────────────────────────

  testWidgets('owner sees invites waiting to be accepted', (tester) async {
    final svc = _FakeMembersService([_owner])
      ..pending = [
        PendingInvite(
            id: 1,
            email: 'friend@example.com',
            role: 'editor',
            createdAt: DateTime(2026, 7, 1)),
      ];
    final notifier = _notifierWith(svc, const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '1'));
    await tester.pumpAndSettle();

    expect(find.text('Waiting to accept'), findsOneWidget);
    expect(find.text('friend@example.com'), findsOneWidget);
  });

  testWidgets('no pending block when nobody is waiting', (tester) async {
    final svc = _FakeMembersService([_owner]);
    final notifier = _notifierWith(svc, const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '1'));
    await tester.pumpAndSettle();

    expect(find.text('Waiting to accept'), findsNothing);
  });

  testWidgets('revoking one pending invite leaves the others', (tester) async {
    // The whole point of #110: revoking the shared link cuts off everyone.
    final svc = _FakeMembersService([_owner])
      ..pending = [
        PendingInvite(
            id: 1, email: 'a@example.com', role: 'editor',
            createdAt: DateTime(2026, 7, 1)),
        PendingInvite(
            id: 2, email: 'b@example.com', role: 'viewer',
            createdAt: DateTime(2026, 7, 2)),
      ];
    final notifier = _notifierWith(svc, const ProjectRef(name: 'Trip'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '1'));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Revoke invite').first);
    await tester.pumpAndSettle();

    expect(svc.revokedPendingIds, [1]);
    expect(find.text('a@example.com'), findsNothing);
    expect(find.text('b@example.com'), findsOneWidget);
  });

  testWidgets('editor sees no pending block', (tester) async {
    final svc = _FakeMembersService([_owner, _editor])
      ..pending = [
        PendingInvite(
            id: 1, email: 'friend@example.com', role: 'editor',
            createdAt: DateTime(2026, 7, 1)),
      ];
    final notifier = _notifierWith(
        svc, const ProjectRef(name: 'Trip', ownerId: 1, role: 'editor'));

    await tester.pumpWidget(_harness(notifier: notifier, authUserId: '7'));
    await tester.pumpAndSettle();

    expect(find.text('Waiting to accept'), findsNothing);
  });
}
