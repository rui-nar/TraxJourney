// Controller tests for ProjectNotifier's travel-companion state (issue #106):
// loadMembers / createMemberInvite / revokeMemberInvite / removeMember
// (optimistic with rollback), and the reset on load()/clear().

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/members_service.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

class _FakeProjectService extends ProjectService {
  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => {
        'name': ref.name,
        'activities': <dynamic>[],
        'items': <dynamic>[],
        'people': <dynamic>[],
        'groups': <dynamic>[],
      };

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref, {bool bypassCache = false}) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref, {bool bypassCache = false}) async =>
      getDetailsMeta(ref);
}

class _FakeMembersService extends MembersService {
  List<ProjectMember> members;
  bool failRemove = false;
  final removedIds = <int>[];
  int createCalls = 0;
  String? lastRequestedRole;
  String? lastRequestedEmail;
  int revokeCalls = 0;

  _FakeMembersService(this.members);

  @override
  Future<List<ProjectMember>> listMembers(ProjectRef ref) async => members;

  @override
  Future<CreatedInvite> createInvite(ProjectRef ref,
      {String role = 'editor', String? email}) async {
    createCalls++;
    lastRequestedRole = role;
    lastRequestedEmail = email;
    return (token: 'tok123', role: role);
  }

  @override
  Future<void> revokeInvite(ProjectRef ref) async {
    revokeCalls++;
  }

  @override
  Future<void> removeMember(ProjectRef ref, int userId) async {
    if (failRemove) throw ApiException(403, '{"detail": "Not allowed"}');
    removedIds.add(userId);
  }
}

class _TestProjectNotifier extends ProjectNotifier {
  _TestProjectNotifier(super.service, {super.membersService});
  @override
  bool get loadOwnerExtras => false;
}

const _owner = ProjectMember(
    userId: 1, displayName: 'Alice', avatarUrl: '', role: 'owner');
const _editor = ProjectMember(
    userId: 7, displayName: 'Bob', avatarUrl: '', role: 'editor');

void main() {
  test('loadMembers populates members and notifies', () async {
    final svc = _FakeMembersService([_owner, _editor]);
    final n = _TestProjectNotifier(_FakeProjectService(), membersService: svc);
    n.ref = const ProjectRef(name: 'Trip');
    var notified = 0;
    n.addListener(() => notified++);

    await n.loadMembers();

    expect(n.members, hasLength(2));
    expect(n.members.first.isOwner, isTrue);
    expect(notified, 1);
  });

  test('loadMembers is a no-op with no open project', () async {
    final svc = _FakeMembersService([_owner]);
    final n = _TestProjectNotifier(_FakeProjectService(), membersService: svc);

    await n.loadMembers();

    expect(n.members, isEmpty);
  });

  test('createMemberInvite stores the token+role; revokeMemberInvite clears '
      'both', () async {
    final svc = _FakeMembersService([_owner]);
    final n = _TestProjectNotifier(_FakeProjectService(), membersService: svc);
    n.ref = const ProjectRef(name: 'Trip');

    await n.createMemberInvite();
    expect(n.memberInviteToken, 'tok123');
    expect(n.memberInviteRole, 'editor');
    expect(svc.createCalls, 1);
    expect(svc.lastRequestedRole, 'editor');

    await n.revokeMemberInvite();
    expect(n.memberInviteToken, isNull);
    expect(n.memberInviteRole, isNull);
    expect(svc.revokeCalls, 1);
  });

  test('createMemberInvite forwards a non-default role (issue #109)',
      () async {
    final svc = _FakeMembersService([_owner]);
    final n = _TestProjectNotifier(_FakeProjectService(), membersService: svc);
    n.ref = const ProjectRef(name: 'Trip');

    await n.createMemberInvite(role: 'viewer');

    expect(svc.lastRequestedRole, 'viewer');
    expect(n.memberInviteRole, 'viewer');
  });

  test('removeMember removes optimistically and calls the service', () async {
    final svc = _FakeMembersService([_owner, _editor]);
    final n = _TestProjectNotifier(_FakeProjectService(), membersService: svc);
    n.ref = const ProjectRef(name: 'Trip');
    await n.loadMembers();

    await n.removeMember(7);

    expect(n.members, [_owner]);
    expect(svc.removedIds, [7]);
  });

  test('removeMember restores the list and rethrows when the request fails',
      () async {
    final svc = _FakeMembersService([_owner, _editor])..failRemove = true;
    final n = _TestProjectNotifier(_FakeProjectService(), membersService: svc);
    n.ref = const ProjectRef(name: 'Trip');
    await n.loadMembers();

    await expectLater(n.removeMember(7), throwsA(isA<ApiException>()));

    expect(n.members, [_owner, _editor]);
  });

  test('load() resets members and invite token', () async {
    final svc = _FakeMembersService([_owner, _editor]);
    final n = _TestProjectNotifier(_FakeProjectService(), membersService: svc);
    n.ref = const ProjectRef(name: 'Trip');
    await n.loadMembers();
    await n.createMemberInvite();

    await n.load(const ProjectRef(name: 'Other'));

    expect(n.members, isEmpty);
    expect(n.memberInviteToken, isNull);
  });

  test('clear() resets members and invite token', () async {
    final svc = _FakeMembersService([_owner]);
    final n = _TestProjectNotifier(_FakeProjectService(), membersService: svc);
    n.ref = const ProjectRef(name: 'Trip');
    await n.loadMembers();
    await n.createMemberInvite();

    n.clear();

    expect(n.members, isEmpty);
    expect(n.memberInviteToken, isNull);
  });
}
