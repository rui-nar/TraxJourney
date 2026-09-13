// Unit tests for the self-hosting server URL override (login screen's
// "Self-hosting? Click here" link): persistence round-trip, trailing-slash
// normalisation, clearing via an empty/null value, and URL-format validation.

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:traxjourney_client/src/core/server_config.dart';

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('readCustomServerUrl is null when nothing was ever saved', () async {
    expect(await readCustomServerUrl(), isNull);
  });

  test('writeCustomServerUrl then readCustomServerUrl round-trips', () async {
    await writeCustomServerUrl('https://trax.example.com');
    expect(await readCustomServerUrl(), 'https://trax.example.com');
  });

  test('a trailing slash is stripped so it composes with ApiClient', () async {
    await writeCustomServerUrl('https://trax.example.com/');
    expect(await readCustomServerUrl(), 'https://trax.example.com');
  });

  test('writing an empty string clears a previously saved override',
      () async {
    await writeCustomServerUrl('https://trax.example.com');
    await writeCustomServerUrl('');
    expect(await readCustomServerUrl(), isNull);
  });

  test('writing null clears a previously saved override', () async {
    await writeCustomServerUrl('https://trax.example.com');
    await writeCustomServerUrl(null);
    expect(await readCustomServerUrl(), isNull);
  });

  test('isValidServerUrl accepts http(s):// with a host', () {
    expect(isValidServerUrl('https://trax.example.com'), isTrue);
    expect(isValidServerUrl('http://192.168.1.10:8000'), isTrue);
  });

  test('isValidServerUrl rejects a scheme-less or malformed value', () {
    expect(isValidServerUrl('trax.example.com'), isFalse);
    expect(isValidServerUrl('ftp://trax.example.com'), isFalse);
    expect(isValidServerUrl(''), isFalse);
  });
}
