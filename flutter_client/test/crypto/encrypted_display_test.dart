/// A field the app cannot decrypt is shown as unavailable, never as its
/// ciphertext envelope (issue #466).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:traxjourney_client/src/crypto/encrypted_display.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';
import 'package:traxjourney_client/src/projects/client_geo_builder.dart' as geo;
import 'package:traxjourney_client/src/projects/poster_job_notifier.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

const _envelope = 'v1.a2V5S2V5.Y2lwaGVyQ2lwaGVy';

void main() {
  group('shownText', () {
    test('an envelope is shown as unavailable', () {
      expect(shownText(_envelope), kEncryptedUnavailable);
    });

    test('anything else is shown as it is', () {
      expect(shownText('Lac Blanc'), 'Lac Blanc');
      expect(shownText('v1.2 notes'), 'v1.2 notes');
      expect(shownText(null), isNull);
    });

    test('readableOrNull drops an envelope', () {
      expect(readableOrNull(_envelope), isNull);
      expect(readableOrNull('Lac Blanc'), 'Lac Blanc');
    });
  });

  testWidgets('the activity panel shows no ciphertext', (tester) async {
    final notifier = ProjectNotifier(ProjectService())
      ..activities = [
        {
          'id': 1,
          'type': 'Ride',
          'name': _envelope,
          'distance': 5000,
          'moving_time': 1800,
          'start_date_local': '2026-06-01T08:00:00',
        },
      ]
      ..items = [
        {'item_type': 'activity', 'activity_id': 1},
        {
          'item_type': 'memory',
          'memory': {
            'id': 'm1',
            'name': _envelope,
            'description': _envelope,
            'date': '2026-06-01',
          },
        },
        {
          'item_type': 'journal',
          'journal': {'id': 'j1', 'description': _envelope, 'date': '2026-06-01'},
        },
      ];
    await tester.pumpWidget(
      ChangeNotifierProvider<ProjectNotifier>.value(
        value: notifier,
        child: MaterialApp(home: Scaffold(body: ActivityPanel(notifier: notifier))),
      ),
    );
    await tester.tap(find.byIcon(Icons.unfold_more));
    await tester.pumpAndSettle();

    expect(find.textContaining('v1.'), findsNothing);
    expect(find.textContaining(kEncryptedUnavailable), findsWidgets);
  });

  test('a map line is labelled unavailable, not with ciphertext', () {
    final fc = geo.buildLowResGeo(
      [
        {'item_type': 'activity', 'activity_id': 1},
      ],
      {
        '1': {
          'id': 1,
          'type': 'Ride',
          'name': _envelope,
          'start_latlng': [45.0, 6.0],
          'end_latlng': [45.1, 6.1],
        },
      },
    );

    final names = [
      for (final f in (fc['features'] as List))
        (f as Map)['properties']['name'],
    ];
    expect(names, [kEncryptedUnavailable]);
  });

  test('a poster is sent no ciphertext', () {
    final json = posterMemoryJson({
      'id': 1, 'lat': 45.0, 'lon': 6.0, 'date': '2026-06-01',
      'name': _envelope, 'description': _envelope,
    });

    expect(json['name'], isNull);
    expect(json['description'], isNull);
  });
}
