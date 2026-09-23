import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/shared/shared_project_screen.dart';

/// Guards issue #147: the guest and view-only banners on the shared-trip
/// screen must wrap instead of overflowing at phone width.
void main() {
  Future<void> pumpAtPhoneWidth(WidgetTester tester, Widget banner) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: banner)));
    await tester.pump();
  }

  testWidgets('AnonBanner does not overflow at phone width', (tester) async {
    await pumpAtPhoneWidth(tester, const AnonBanner(token: 'tok123'));
    expect(tester.takeException(), isNull);
  });

  testWidgets('ViewOnlyBanner does not overflow at phone width',
      (tester) async {
    await pumpAtPhoneWidth(tester, const ViewOnlyBanner());
    expect(tester.takeException(), isNull);
  });

  testWidgets('AnonBanner does not overflow at large text scale',
      (tester) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(
      MaterialApp(
        home: MediaQuery(
          data: const MediaQueryData(textScaler: TextScaler.linear(2.0)),
          child: const Scaffold(body: AnonBanner(token: 'tok123')),
        ),
      ),
    );
    await tester.pump();
    expect(tester.takeException(), isNull);
  });
}
