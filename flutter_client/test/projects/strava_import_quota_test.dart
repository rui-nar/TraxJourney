/// A quota refusal on Strava import must become a QuotaError, not a raw
/// exception (issue #192): the sync endpoint was fixed server-side to enforce
/// the trip-days limit, but the import screen was showing the 402's raw JSON
/// body to the user instead of the upgrade prompt every other quota-gated
/// action already gets.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/strava_import_notifier.dart';

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

StravaImportNotifier _notifierWith(MockClient mock) => StravaImportNotifier(
      apiClient: ApiClient(baseUrl: '', httpClient: mock),
    )
  ..activities = [
    {'id': 1, 'name': 'Ride', 'type': 'Ride', 'start_date_local': '2026-07-01T10:00:00Z'},
  ]
  ..selectedIds.add(1);

void main() {
  group('addSelected', () {
    test('a 402 quota refusal is parsed into quotaError, not error', () async {
      final mock = MockClient((req) async => http.Response(_quotaBody, 402));
      final notifier = _notifierWith(mock);

      final added = await notifier.addSelected(_ref);

      expect(added, 0);
      expect(notifier.error, isNull);
      expect(notifier.quotaError, isNotNull);
      expect(notifier.quotaError!.resource, 'trip_days');
      expect(notifier.quotaError!.limit, 10);
    });

    test('takeQuotaError clears it — one refusal, one prompt', () async {
      final mock = MockClient((req) async => http.Response(_quotaBody, 402));
      final notifier = _notifierWith(mock);
      await notifier.addSelected(_ref);

      expect(notifier.takeQuotaError(), isNotNull);
      expect(notifier.takeQuotaError(), isNull);
    });

    test('any other failure still becomes a plain error message', () async {
      final mock = MockClient((req) async => http.Response('boom', 500));
      final notifier = _notifierWith(mock);

      await notifier.addSelected(_ref);

      expect(notifier.quotaError, isNull);
      expect(notifier.error, isNotNull);
    });

    test('a success clears any stale quota error', () async {
      final failing = MockClient((req) async => http.Response(_quotaBody, 402));
      final notifier = _notifierWith(failing);
      await notifier.addSelected(_ref);
      expect(notifier.quotaError, isNotNull);

      final ok = _notifierWith(MockClient(
        (req) async => http.Response(jsonEncode({'added': 1}), 200),
      ));
      await ok.addSelected(_ref);

      expect(ok.quotaError, isNull);
      expect(ok.error, isNull);
    });
  });
}
