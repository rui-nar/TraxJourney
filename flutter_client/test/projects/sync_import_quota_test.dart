/// The "new activities found" auto-sync dialog hits the same /activities
/// endpoint as the manual Strava import screen, so a quota refusal there must
/// be parsed the same way (issue #192) — not left as a raw exception string.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/sync_import_notifier.dart';

const _ref = ProjectRef(name: 'Trip 1');

final _quotaBody = jsonEncode({
  'detail': 'That would make this trip 27 days long, and your plan covers '
      '10. Upgrade for longer trips.',
  'code': 'quota_exceeded',
  'resource': 'trip_days',
  'plan': 'free',
  'limit': 10,
  'used': 10,
  'needed': 27,
});

SyncImportNotifier _notifierWith(MockClient mock) => SyncImportNotifier(
      stravaActivities: [
        {'id': 1, 'name': 'Ride', 'type': 'Ride'},
      ],
      psSteps: const [],
      apiClient: ApiClient(baseUrl: '', httpClient: mock),
    );

void main() {
  group('importSelected — Strava batch', () {
    test('a 402 quota refusal is parsed into quotaError, not error', () async {
      final mock = MockClient((req) async => http.Response(_quotaBody, 402));
      final notifier = _notifierWith(mock);

      final created = await notifier.importSelected(_ref);

      expect(created, 0);
      expect(notifier.error, isNull);
      expect(notifier.quotaError, isNotNull);
      expect(notifier.quotaError!.resource, 'trip_days');
    });

    test('takeQuotaError clears it — one refusal, one prompt', () async {
      final mock = MockClient((req) async => http.Response(_quotaBody, 402));
      final notifier = _notifierWith(mock);
      await notifier.importSelected(_ref);

      expect(notifier.takeQuotaError(), isNotNull);
      expect(notifier.takeQuotaError(), isNull);
    });

    test('any other failure still becomes a plain error message', () async {
      final mock = MockClient((req) async => http.Response('boom', 500));
      final notifier = _notifierWith(mock);

      await notifier.importSelected(_ref);

      expect(notifier.quotaError, isNull);
      expect(notifier.error, isNotNull);
    });
  });
}
