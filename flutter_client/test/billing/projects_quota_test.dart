/// ProjectsNotifier must tell a plan limit apart from a real failure (#121).
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/projects/projects_notifier.dart';
import 'package:traxjourney_client/src/projects/projects_service.dart';

class _ThrowingService extends ProjectsService {
  final Exception failure;
  _ThrowingService(this.failure);

  @override
  Future<Map<String, dynamic>> create(String name) async => throw failure;
}

class _OkService extends ProjectsService {
  @override
  Future<Map<String, dynamic>> create(String name) async => {'name': name};
}

ApiException _quota402() => ApiException(402, jsonEncode({
      'detail': 'Your plan includes 1 trip. Upgrade to create more.',
      'code': 'quota_exceeded',
      'resource': 'projects',
      'plan': 'free',
      'limit': 1,
      'used': 1,
    }));

void main() {
  test('a 402 becomes a quota error the screen can act on', () async {
    final notifier = ProjectsNotifier(_ThrowingService(_quota402()));
    await notifier.create('Trip 2');
    expect(notifier.quotaError, isNotNull);
    expect(notifier.quotaError!.resource, 'projects');
    expect(notifier.error, contains('Upgrade'));
  });

  test('any other failure leaves quotaError unset', () async {
    final notifier = ProjectsNotifier(
        _ThrowingService(ApiException(409, '{"detail":"already exists"}')));
    await notifier.create('Trip 2');
    expect(notifier.quotaError, isNull);
    expect(notifier.error, 'already exists');
  });

  test('a success clears a previous quota error', () async {
    final notifier = ProjectsNotifier(_ThrowingService(_quota402()));
    await notifier.create('Trip 2');
    expect(notifier.quotaError, isNotNull);

    final ok = ProjectsNotifier(_OkService());
    await ok.create('Trip 2');
    expect(ok.quotaError, isNull);
    expect(ok.error, isNull);
  });

  test('clearQuotaError stops the prompt from reopening', () async {
    final notifier = ProjectsNotifier(_ThrowingService(_quota402()));
    await notifier.create('Trip 2');
    notifier.clearQuotaError();
    expect(notifier.quotaError, isNull);
  });
}
