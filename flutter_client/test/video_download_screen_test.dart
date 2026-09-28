// `/video/{token}` (docs/TRIP_VIDEO_PLAN.md, U7): the emailed link opens
// without a session and offers the download. Exercised through the real
// buildRouter() — the auth guard and the route both — plus the screen's own
// states.

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/app_router.dart';
import 'package:traxjourney_client/src/core/onboarding_notifier.dart';
import 'package:traxjourney_client/src/projects/video_download_screen.dart';
import 'package:traxjourney_client/src/settings/theme_notifier.dart';

http.Response _json(int status, Object body) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Future<void> _frames(WidgetTester tester) async {
  for (var i = 0; i < 8; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

void main() {
  late ApiClient savedApi;
  late List<http.Request> sent;
  late http.Response Function(http.Request) respond;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    savedApi = api;
    sent = [];
    respond = (_) => _json(200, {'status': 'done', 'progress': 1.0});
    api = ApiClient(httpClient: MockClient((req) async {
      sent.add(req);
      return respond(req);
    }));
  });
  tearDown(() => api = savedApi);

  test('the auth guard lets /video/{token} through without a session',
      () async {
    final auth = AuthNotifier(AuthService()); // no user, not loading
    expect(await authRedirectTarget(auth, Uri.parse('/video/tok')), isNull);
  });

  testWidgets('/video/<token> opens without a session and shows the download',
      (tester) async {
    final auth = AuthNotifier(AuthService());
    expect(auth.user, isNull);
    late GoRouter router;
    await tester.pumpWidget(
      MultiProvider(
        providers: [
          ChangeNotifierProvider<AuthNotifier>.value(value: auth),
          ChangeNotifierProvider<OnboardingNotifier>(
              create: (_) => OnboardingNotifier(true)),
          ChangeNotifierProvider<ThemeNotifier>(create: (_) => ThemeNotifier()),
        ],
        child: Builder(builder: (context) {
          router = buildRouter(context);
          return MaterialApp.router(routerConfig: router);
        }),
      ),
    );
    await tester.pump();

    router.go('/video/tok-123');
    await _frames(tester);

    expect(find.byType(VideoDownloadScreen), findsOneWidget);
    expect(router.routerDelegate.currentConfiguration.uri.path, '/video/tok-123');
    expect(find.text('Your video is ready'), findsOneWidget);
    expect(find.text('Download video'), findsOneWidget);
    expect(sent.single.url.path, '/api/video/tok-123');
    expect(sent.single.headers.containsKey('Authorization'), isFalse);
  });

  Future<List<Uri>> pumpScreen(WidgetTester tester) async {
    final opened = <Uri>[];
    await tester.pumpWidget(MaterialApp(
      home: VideoDownloadScreen(
        key: UniqueKey(), // a fresh screen, and fetch, per call
        token: 'tok',
        openUrl: (u) async {
          opened.add(u);
          return true;
        },
      ),
    ));
    await _frames(tester);
    return opened;
  }

  testWidgets('Download opens the token download URL', (tester) async {
    final opened = await pumpScreen(tester);
    await tester.tap(find.text('Download video'));
    await tester.pump();
    expect(opened.single.path, '/api/video/tok/download');
  });

  testWidgets('still rendering: progress and a refresh', (tester) async {
    respond = (_) =>
        _json(200, {'status': 'running', 'stage': 'Drawing frames', 'progress': 0.4});
    await pumpScreen(tester);
    expect(find.text('Still making your video…'), findsOneWidget);
    expect(find.text('40% · Drawing frames'), findsOneWidget);
    expect(find.text('Refresh'), findsOneWidget);
  });

  testWidgets('failed, expired and unknown tokens', (tester) async {
    respond = (_) => _json(200, {'status': 'failed', 'progress': 0.0});
    await pumpScreen(tester);
    expect(find.text('Video generation failed'), findsOneWidget);

    respond = (_) => _json(200, {'status': 'expired', 'progress': 0.0});
    await pumpScreen(tester);
    expect(find.text('This video has expired'), findsOneWidget);

    respond = (_) => _json(404, {'detail': 'Video job not found'});
    await pumpScreen(tester);
    expect(find.text('This link is invalid or has expired'), findsOneWidget);
  });
}
