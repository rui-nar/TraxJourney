/// Billing parsing and requests (issue #121).
///
/// The rules worth pinning down are the defensive ones: a missing field must
/// not crash the settings screen, and only a genuine quota refusal may be
/// turned into an upgrade prompt.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/billing/billing_service.dart';

BillingService _service(MockClient mock) =>
    BillingService(ApiClient(baseUrl: '', httpClient: mock));

void main() {
  group('BillingStatus.fromJson', () {
    test('reads plan, limits and usage', () {
      final status = BillingStatus.fromJson({
        'billing_enabled': true,
        'quotas_enforced': true,
        'plan': 'cloud',
        'status': 'active',
        'cancel_at_period_end': false,
        'current_period_end': 1893456000.0,
        'admin_override': false,
        'limits': {'max_projects': null, 'max_storage_bytes': 1000},
        'usage': {'projects': 3, 'storage_bytes': 250},
      });
      expect(status.isPaid, isTrue);
      expect(status.limits.maxProjects, isNull);
      expect(status.limits.maxStorageBytes, 1000);
      expect(status.projects, 3);
      expect(status.storageFraction, 0.25);
    });

    test('an empty payload does not throw', () {
      final status = BillingStatus.fromJson({});
      expect(status.billingEnabled, isFalse);
      expect(status.plan, 'free');
      expect(status.projects, 0);
    });

    test('unlimited storage has no fraction to render', () {
      final status = BillingStatus.fromJson({
        'limits': {'max_storage_bytes': null},
        'usage': {'storage_bytes': 999},
      });
      expect(status.storageFraction, isNull);
    });

    test('usage past the limit clamps to a full bar', () {
      final status = BillingStatus.fromJson({
        'limits': {'max_storage_bytes': 100},
        'usage': {'storage_bytes': 500},
      });
      expect(status.storageFraction, 1.0);
    });

    test('reads the trip-day limit and the plan name', () {
      final status = BillingStatus.fromJson({
        'plan': 'tier_2',
        'plan_name': 'Tier 2',
        'limits': {'max_trip_days': 365},
      });
      expect(status.planName, 'Tier 2');
      expect(status.limits.maxTripDays, 365);
      expect(status.isPaid, isTrue);
    });

    test('any tier counts as paid, free does not', () {
      expect(BillingStatus.fromJson({'plan': 'tier_1'}).isPaid, isTrue);
      expect(BillingStatus.fromJson({'plan': 'free'}).isPaid, isFalse);
    });

    test('canManage is false before the first purchase', () {
      expect(BillingStatus.fromJson({'status': 'none'}).canManage, isFalse);
      expect(BillingStatus.fromJson({'status': 'canceled'}).canManage, isTrue);
    });
  });

  group('PlanLimits.covers', () {
    const limits = PlanLimits(
        maxProjects: 2, maxStorageBytes: 1000, maxTripDays: 100);

    test('within the limit', () {
      expect(limits.covers('trip_days', 100), isTrue);
      expect(limits.covers('projects', 2), isTrue);
    });

    test('past the limit', () {
      expect(limits.covers('trip_days', 101), isFalse);
      expect(limits.covers('storage', 1001), isFalse);
    });

    test('an unlimited limit covers anything', () {
      expect(const PlanLimits().covers('trip_days', 99999), isTrue);
    });

    test('an unknown resource is not a limit', () {
      expect(limits.covers('bananas', 99999), isTrue);
    });
  });

  group('QuotaError.fromApiException', () {
    test('parses a 402 quota refusal', () {
      final quota = QuotaError.fromApiException(ApiException(402, jsonEncode({
        'detail': 'Your plan includes 1 trip.',
        'code': 'quota_exceeded',
        'resource': 'projects',
        'plan': 'free',
        'limit': 1,
        'used': 1,
        'needed': 2,
      })));
      expect(quota, isNotNull);
      expect(quota!.resource, 'projects');
      expect(quota.limit, 1);
      expect(quota.detail, 'Your plan includes 1 trip.');
      expect(quota.needed, 2);
    });

    test('ignores other status codes', () {
      expect(QuotaError.fromApiException(ApiException(409, '{}')), isNull);
    });

    test('ignores a 402 that is not a quota refusal', () {
      final body = jsonEncode({'detail': 'card declined'});
      expect(QuotaError.fromApiException(ApiException(402, body)), isNull);
    });

    test('a malformed body does not throw', () {
      expect(QuotaError.fromApiException(ApiException(402, 'not json')), isNull);
    });
  });

  group('BillingService', () {
    test('status() reads /api/billing/me', () async {
      late String path;
      final mock = MockClient((req) async {
        path = req.url.path;
        return http.Response(
            jsonEncode({'billing_enabled': true, 'plan': 'cloud'}), 200);
      });
      final status = await _service(mock).status();
      expect(path, '/api/billing/me');
      expect(status.plan, 'cloud');
    });

    test('checkoutUrl() names the tier being bought', () async {
      late Map<String, dynamic> body;
      final mock = MockClient((req) async {
        body = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response(jsonEncode({'url': 'https://pay.test/x'}), 200);
      });
      await _service(mock).checkoutUrl(plan: 'tier_2');
      expect(body['plan'], 'tier_2');
    });

    test('checkoutUrl() omits the plan when none was chosen', () async {
      late Map<String, dynamic> body;
      final mock = MockClient((req) async {
        body = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response(jsonEncode({'url': 'https://pay.test/x'}), 200);
      });
      await _service(mock).checkoutUrl();
      expect(body.containsKey('plan'), isFalse);
    });

    test('checkoutUrl() posts the return path and returns the url', () async {
      late Map<String, dynamic> body;
      final mock = MockClient((req) async {
        body = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response(jsonEncode({'url': 'https://pay.test/x'}), 200);
      });
      final url = await _service(mock).checkoutUrl(returnPath: '/projects');
      expect(body['return_path'], '/projects');
      expect(url, 'https://pay.test/x');
    });

    test('portalUrl() hits the portal endpoint', () async {
      late String path;
      final mock = MockClient((req) async {
        path = req.url.path;
        return http.Response(jsonEncode({'url': 'https://pay.test/p'}), 200);
      });
      await _service(mock).portalUrl();
      expect(path, '/api/billing/portal');
    });

    test('a 402 surfaces as an ApiException the caller can classify', () async {
      final mock = MockClient((req) async => http.Response(
          jsonEncode({'code': 'quota_exceeded', 'detail': 'full'}), 402));
      await expectLater(
        _service(mock).status(),
        throwsA(isA<ApiException>().having((e) => e.statusCode, 'status', 402)),
      );
    });

    test('plans() maps the catalogue, including a non-ASCII price', () async {
      // The server sends UTF-8; a price label with "€" in it must survive.
      final mock = MockClient((req) async => http.Response.bytes(
          utf8.encode(jsonEncode([
            {'id': 'free', 'name': 'Free', 'price_label': 'Free',
             'features': ['One trip']},
            {'id': 'tier_1', 'name': 'Tier 1', 'price_label': '€4 / month',
             'features': ['Unlimited trips'],
             'limits': {'max_trip_days': 100}},
          ])),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'}));
      final plans = await _service(mock).plans();
      expect(plans.map((p) => p.id), ['free', 'tier_1']);
      expect(plans[1].priceLabel, '€4 / month');
      expect(plans[1].limits.maxTripDays, 100);
      expect(plans[0].features, ['One trip']);
      expect(plans[0].isFree, isTrue);
    });
  });

  group('formatBytes', () {
    test('bytes stay bytes', () => expect(formatBytes(512), '512 B'));
    test('kilobytes', () => expect(formatBytes(2048), '2.0 KB'));
    test('megabytes', () => expect(formatBytes(5 * 1024 * 1024), '5.0 MB'));
    test('drops the decimal once it stops mattering',
        () => expect(formatBytes(20 * 1024 * 1024), '20 MB'));
    test('gigabytes', () => expect(formatBytes(3 * 1024 * 1024 * 1024), '3.0 GB'));
  });
}
