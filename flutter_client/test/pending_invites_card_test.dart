// Widget tests for the "you've been invited" card on the projects list
// (issue #110): an invite addressed to your confirmed address is offered for
// explicit Accept/Decline rather than silently appearing as a trip.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/members_service.dart';
import 'package:traxjourney_client/src/projects/pending_invites_card.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';

class _FakeMembersService extends MembersService {
  List<MyPendingInvite> invites;
  ApiException? acceptError;
  final accepted = <String>[];
  final declined = <String>[];

  _FakeMembersService(this.invites);

  @override
  Future<List<MyPendingInvite>> listMyPendingInvites() async => invites;

  @override
  Future<ProjectRef> acceptInvite(String token, {String role = 'editor'}) async {
    final err = acceptError;
    if (err != null) throw err;
    accepted.add(token);
    return ProjectRef(name: 'Trip', ownerId: 1, role: role);
  }

  @override
  Future<void> declineInvite(String token) async {
    declined.add(token);
  }
}

/// ProjectsNotifier.load() would hit the network; this records instead.
class _FakeProjectsNotifier extends ProjectsNotifier {
  int loadCalls = 0;

  _FakeProjectsNotifier() : super(ProjectsService());

  @override
  Future<void> load() async {
    loadCalls++;
  }
}

MyPendingInvite _invite({
  String token = 'tok1',
  String project = 'Japan 2026',
  String owner = 'Alice',
  String role = 'editor',
}) =>
    MyPendingInvite(
      token: token,
      projectName: project,
      ownerName: owner,
      role: role,
      createdAt: DateTime(2026, 7, 1),
    );

Widget _harness(_FakeMembersService svc, _FakeProjectsNotifier projects) =>
    MaterialApp(
      home: Scaffold(
        body: ChangeNotifierProvider<ProjectsNotifier>.value(
          value: projects,
          child: PendingInvitesCard(service: svc),
        ),
      ),
    );

void main() {
  testWidgets('shows who invited you, to what, and as what', (tester) async {
    final svc = _FakeMembersService([_invite()]);
    await tester.pumpWidget(_harness(svc, _FakeProjectsNotifier()));
    await tester.pumpAndSettle();

    expect(find.textContaining('Alice invited you to "Japan 2026"'),
        findsOneWidget);
    expect(find.textContaining('editor'), findsOneWidget);
    expect(find.text('Accept'), findsOneWidget);
    expect(find.text('Decline'), findsOneWidget);
  });

  testWidgets('renders nothing when there are no invites', (tester) async {
    final svc = _FakeMembersService([]);
    await tester.pumpWidget(_harness(svc, _FakeProjectsNotifier()));
    await tester.pumpAndSettle();

    expect(find.text('Accept'), findsNothing);
  });

  testWidgets('accept joins the trip and refreshes the project list',
      (tester) async {
    final svc = _FakeMembersService([_invite()]);
    final projects = _FakeProjectsNotifier();
    await tester.pumpWidget(_harness(svc, projects));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Accept'));
    await tester.pumpAndSettle();

    expect(svc.accepted, ['tok1']);
    expect(projects.loadCalls, 1, reason: 'the new trip must appear at once');
    expect(find.text('Accept'), findsNothing);
  });

  testWidgets('decline removes the card without joining', (tester) async {
    final svc = _FakeMembersService([_invite()]);
    final projects = _FakeProjectsNotifier();
    await tester.pumpWidget(_harness(svc, projects));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Decline'));
    await tester.pumpAndSettle();

    expect(svc.declined, ['tok1']);
    expect(svc.accepted, isEmpty);
    expect(projects.loadCalls, 0);
    expect(find.text('Decline'), findsNothing);
  });

  testWidgets('acting on one invite leaves the others', (tester) async {
    final svc = _FakeMembersService([
      _invite(token: 'tok1', project: 'Japan 2026'),
      _invite(token: 'tok2', project: 'Peru 2027'),
    ]);
    await tester.pumpWidget(_harness(svc, _FakeProjectsNotifier()));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Accept').first);
    await tester.pumpAndSettle();

    expect(find.textContaining('Japan 2026'), findsNothing);
    expect(find.textContaining('Peru 2027'), findsOneWidget);
  });

  testWidgets('a failed accept surfaces the server message and keeps the row',
      (tester) async {
    final svc = _FakeMembersService([_invite()])
      ..acceptError = ApiException(
          403, '{"detail":"This invite was sent to other@example.com."}');
    await tester.pumpWidget(_harness(svc, _FakeProjectsNotifier()));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Accept'));
    await tester.pumpAndSettle();

    expect(find.text('This invite was sent to other@example.com.'),
        findsOneWidget);
    expect(find.text('Accept'), findsOneWidget);
  });

  testWidgets('a failed listing hides the card instead of breaking the screen',
      (tester) async {
    final svc = _ThrowingService();
    await tester.pumpWidget(_harness(svc, _FakeProjectsNotifier()));
    await tester.pumpAndSettle();

    expect(find.text('Accept'), findsNothing);
    expect(tester.takeException(), isNull);
  });
}

class _ThrowingService extends _FakeMembersService {
  _ThrowingService() : super([]);

  @override
  Future<List<MyPendingInvite>> listMyPendingInvites() async =>
      throw ApiException(500, 'boom');
}
