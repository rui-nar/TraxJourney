/// A .gpx opened from another app (issue #368): how it is read, held and
/// kept away from the router.
///
/// A shared file is untrusted input from any app on the phone, so the size
/// guard is tested both ways: refused unread when its size is known to be too
/// large, and cut off at the cap when the size it reported was a lie.
library;

import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/core/app_router.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/incoming_gpx.dart';

final Uint8List _gpx = utf8.encode('<gpx></gpx>');

IncomingFileRead _read(String name, Uint8List bytes) =>
    IncomingFileRead.file(IncomingGpxFile(name, bytes));

void main() {
  group('reading a received file', () {
    test('a file over the cap is refused without being read', () async {
      var opened = false;
      final read = await readIncomingFile(
        name: 'huge.gpx',
        length: () async => kGpxImportMaxBytes + 1,
        open: (_) {
          opened = true;
          return const Stream.empty();
        },
      );

      expect(read.file, isNull);
      expect(read.refusal, kGpxTooLargeMessage);
      expect(opened, isFalse);
    });

    test('a file whose reported size was a lie is cut off at the cap',
        () async {
      int? askedFor;
      final read = await readIncomingFile(
        name: 'liar.gpx',
        length: () async => 10,
        maxBytes: 100,
        open: (end) {
          askedFor = end;
          return Stream.fromIterable([List.filled(60, 0), List.filled(60, 0)]);
        },
      );

      expect(read.refusal, kGpxTooLargeMessage);
      expect(askedFor, 101);
    });

    test('a file within the cap is read whole', () async {
      final read = await readIncomingFile(
        name: 'ride.gpx',
        length: () async => _gpx.length,
        open: (_) => Stream.value(_gpx),
      );

      expect(read.file!.name, 'ride.gpx');
      expect(read.file!.bytes, _gpx);
    });

    test('a file:// URL is read from disk, named after its file', () async {
      final dir = await Directory.systemTemp.createTemp('incoming_gpx');
      addTearDown(() => dir.delete(recursive: true));
      final file = File('${dir.path}/Morning ride.gpx')..writeAsBytesSync(_gpx);

      final read = await readFileUri(file.uri);

      expect(read.file!.name, 'Morning ride.gpx');
      expect(read.file!.bytes, _gpx);
    });

    test('a missing file:// file is refused as unreadable', () async {
      final read = await readFileUri(Uri.parse('file:///no/such/file.gpx'));

      expect(read.refusal, kGpxUnreadableMessage);
    });

    test("Android's answers: bytes, too large, unreadable", () {
      final ok = readFromPlatformAnswer({'name': 'a.gpx', 'bytes': _gpx});
      expect(ok.file!.name, 'a.gpx');
      expect(ok.file!.bytes, _gpx);

      expect(readFromPlatformAnswer({'name': 'a.gpx', 'tooLarge': true})
          .refusal, kGpxTooLargeMessage);
      expect(readFromPlatformAnswer({'name': null, 'error': 'boom'}).refusal,
          kGpxUnreadableMessage);
      expect(readFromPlatformAnswer({'name': null, 'bytes': _gpx}).file!.name,
          'track.gpx');
    });
  });

  group('the route guard', () {
    Future<bool> push(IncomingFileRouteGuard guard, String location) =>
        guard.didPushRouteInformation(
            RouteInformation(uri: Uri.parse(location)));

    test('a content:// location is taken, and is not a file to read',
        () async {
      final files = <Uri>[];
      final guard = IncomingFileRouteGuard(files.add);

      expect(await push(guard, 'content://com.android.providers/document/9'),
          isTrue);
      expect(files, isEmpty);
    });

    test('a file:// location is taken and handed over', () async {
      final files = <Uri>[];
      final guard = IncomingFileRouteGuard(files.add);

      expect(await push(guard, 'file:///private/var/Inbox/ride.gpx'), isTrue);
      expect(files.single.path, '/private/var/Inbox/ride.gpx');
    });

    test('App Links and plain routes pass through to the router', () async {
      final guard = IncomingFileRouteGuard((_) => fail('not a file'));

      expect(await push(guard, 'https://traxjourney.com/share/tok'), isFalse);
      expect(await push(guard, '/join/tok'), isFalse);
    });
  });

  testWidgets('registered before the router, the guard keeps a pushed file '
      'location from ever reaching go_router', (tester) async {
    final seen = <Uri>[];
    final files = <Uri>[];
    final guard = IncomingFileRouteGuard(files.add);
    // The order buildRouter uses: the guard first, then the router.
    tester.binding.addObserver(guard);
    addTearDown(() => tester.binding.removeObserver(guard));
    final router = GoRouter(
      redirect: (_, state) {
        seen.add(state.uri);
        return null;
      },
      routes: [
        GoRoute(path: '/', builder: (_, __) => const Text('home')),
        GoRoute(
            path: '/share/:token',
            builder: (_, state) => Text('share ${state.pathParameters['token']}')),
      ],
    );
    await tester.pumpWidget(MaterialApp.router(routerConfig: router));
    seen.clear();

    await tester.binding
        .handlePushRoute('content://com.google.android.gm.sapi/att/1');
    await tester.binding.handlePushRoute('file:///var/Inbox/ride.gpx');
    await tester.pumpAndSettle();
    expect(seen, isEmpty);
    expect(find.text('home'), findsOneWidget);
    expect(files.single.scheme, 'file');

    await tester.binding.handlePushRoute('https://traxjourney.com/share/tok');
    await tester.pumpAndSettle();
    expect(find.text('share tok'), findsOneWidget);
  });

  group('the holder', () {
    test('gives the file once, and only to the trip chosen for it', () async {
      final holder = IncomingGpx();
      await holder.receive(Future.value(_read('ride.gpx', _gpx)));
      const alps = ProjectRef(name: 'Alps');

      expect(holder.takeFor(alps), isNull, reason: 'no trip chosen yet');
      holder.chooseTrip(alps);
      expect(holder.takeFor(const ProjectRef(name: 'Other')), isNull);
      expect(holder.takeFor(const ProjectRef(name: 'Alps', ownerId: 7)),
          isNull);
      expect(holder.takeFor(alps)!.name, 'ride.gpx');
      expect(holder.takeFor(alps), isNull);
      expect(holder.file, isNull);
    });

    test('a later file replaces the earlier one and its chosen trip',
        () async {
      final holder = IncomingGpx();
      await holder.receive(Future.value(_read('one.gpx', _gpx)));
      holder.chooseTrip(const ProjectRef(name: 'Alps'));
      await holder.receive(Future.value(_read('two.gpx', _gpx)));

      expect(holder.file!.name, 'two.gpx');
      expect(holder.takeFor(const ProjectRef(name: 'Alps')), isNull);
    });

    test('a refused file leaves no file, only the reason', () async {
      final holder = IncomingGpx();
      await holder.receive(Future.value(_read('one.gpx', _gpx)));
      await holder.receive(Future.value(
          const IncomingFileRead.refused(kGpxTooLargeMessage)));

      expect(holder.file, isNull);
      expect(holder.refusal, kGpxTooLargeMessage);
    });
  });

  group('signing in on the way to the trip picker', () {
    test('the file waits while the picker route rides through login',
        () async {
      final holder = IncomingGpx();
      await holder.receive(Future.value(_read('ride.gpx', _gpx)));

      final signedOut = AuthNotifier(AuthService());
      final toLogin =
          await authRedirectTarget(signedOut, Uri.parse(kIncomingGpxRoute));
      expect(toLogin, '/login?return_to=%2Fimport-gpx');

      final signedIn = AuthNotifier(AuthService())
        ..updateUser({
          'id': 'user-1',
          'email': 'a@x.com',
          'display_name': 'A',
          'auth_provider': 'local',
        });
      expect(await authRedirectTarget(signedIn, Uri.parse(toLogin!)),
          kIncomingGpxRoute);
      expect(await authRedirectTarget(signedIn, Uri.parse(kIncomingGpxRoute)),
          isNull);
      expect(holder.file!.name, 'ride.gpx');
    });
  });
}
