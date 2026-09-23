// Widget tests for the /join/{token} invite-accept screen (issue #106; roles:
// issue #109): preview rendering (including the role-specific description),
// invalid/revoked token handling, join → navigate into the trip with the
// invite's role, and the 409 you-own-this-trip fallback. The service is
// mocked by subclassing; navigation is exercised through a real GoRouter.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/join_trip_screen.dart';
import 'package:traxjourney_client/src/projects/members_service.dart';

class _FakeMembersService extends MembersService {
  InvitePreview? preview;
  ApiException? previewError;
  ProjectRef? accepted;
  ApiException? acceptError;

  @override
  Future<InvitePreview> previewInvite(String token) async {
    final err = previewError;
    if (err != null) throw err;
    return preview!;
  }

  @override
  Future<ProjectRef> acceptInvite(String token, {String role = 'editor'}) async {
    final err = acceptError;
    if (err != null) throw err;
    return accepted!;
  }
}

GoRouter _router(_FakeMembersService svc, void Function(Uri) onView) => GoRouter(
      initialLocation: '/join/tok123',
      routes: [
        GoRoute(
          path: '/join/:token',
          builder: (context, state) => JoinTripScreen(
              token: state.pathParameters['token']!, service: svc),
        ),
        GoRoute(
          path: '/view',
          builder: (context, state) {
            onView(state.uri);
            return const Scaffold(body: Text('VIEW SCREEN'));
          },
        ),
        GoRoute(
          path: '/projects',
          builder: (context, state) =>
              const Scaffold(body: Text('PROJECTS LIST')),
        ),
      ],
    );

void main() {
  testWidgets('shows "«Owner» invites you to join «Trip»" and a Join button',
      (tester) async {
    final svc = _FakeMembersService()
      ..preview = const InvitePreview(
          projectName: 'Alps', ownerName: 'Alice', role: 'editor');

    await tester.pumpWidget(
        MaterialApp.router(routerConfig: _router(svc, (_) {})));
    await tester.pumpAndSettle();

    expect(
        find.textContaining('Alice invites you to join Alps'), findsOneWidget);
    expect(find.widgetWithText(ElevatedButton, 'Join trip'), findsOneWidget);
  });

  testWidgets('shows what a viewer invite grants (issue #109)',
      (tester) async {
    final svc = _FakeMembersService()
      ..preview = const InvitePreview(
          projectName: 'Alps', ownerName: 'Alice', role: 'viewer');

    await tester.pumpWidget(
        MaterialApp.router(routerConfig: _router(svc, (_) {})));
    await tester.pumpAndSettle();

    expect(find.textContaining('read-only'), findsOneWidget);
  });

  testWidgets('invalid/revoked token shows a friendly message with a way home',
      (tester) async {
    final svc = _FakeMembersService()
      ..previewError = ApiException(404, '{"detail": "Invite not found"}');

    await tester.pumpWidget(
        MaterialApp.router(routerConfig: _router(svc, (_) {})));
    await tester.pumpAndSettle();

    expect(find.text('This invite link is invalid or has been revoked.'),
        findsOneWidget);
    expect(find.text('Join trip'), findsNothing);

    await tester.tap(find.text('Go to my trips'));
    await tester.pumpAndSettle();
    expect(find.text('PROJECTS LIST'), findsOneWidget);
  });

  testWidgets('Join accepts the invite and navigates into the trip as editor '
      '(?owner= carried in the URL)', (tester) async {
    Uri? viewUri;
    final svc = _FakeMembersService()
      ..preview = const InvitePreview(
          projectName: 'Alps', ownerName: 'Alice', role: 'editor')
      ..accepted = const ProjectRef(name: 'Alps', ownerId: 3, role: 'editor');

    await tester.pumpWidget(
        MaterialApp.router(routerConfig: _router(svc, (uri) => viewUri = uri)));
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(ElevatedButton, 'Join trip'));
    await tester.pumpAndSettle();

    expect(find.text('VIEW SCREEN'), findsOneWidget);
    expect(viewUri!.path, '/view');
    expect(viewUri!.queryParameters['project'], 'Alps');
    expect(viewUri!.queryParameters['owner'], '3');
  });

  testWidgets('409 on accept (caller owns the trip) opens the trip as owner',
      (tester) async {
    Uri? viewUri;
    final svc = _FakeMembersService()
      ..preview = const InvitePreview(
          projectName: 'Alps', ownerName: 'Alice', role: 'editor')
      ..acceptError = ApiException(409, '{"detail": "You already own this trip"}');

    await tester.pumpWidget(
        MaterialApp.router(routerConfig: _router(svc, (uri) => viewUri = uri)));
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(ElevatedButton, 'Join trip'));
    await tester.pumpAndSettle();

    expect(find.text('VIEW SCREEN'), findsOneWidget);
    expect(viewUri!.queryParameters['project'], 'Alps');
    expect(viewUri!.queryParameters.containsKey('owner'), isFalse);
  });
}
