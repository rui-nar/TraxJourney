import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/crypto/share_crypto.dart';
import 'package:traxjourney_client/src/share/share_content_generator.dart';

void main() {
  group('ShareContentGenerator.generate', () {
    test('encrypts only actually-encrypted memories under a new share key '
        'and uploads the envelopes', () async {
      Map<String, dynamic>? putBody;
      String? putPath;

      final mock = MockClient((req) async {
        if (req.method == 'GET' && req.url.path == '/api/projects/Trip1') {
          return http.Response(
            jsonEncode({
              'items': [
                {
                  'item_type': 'memory',
                  'memory': {
                    'id': 1,
                    'name': 'v1.AAAA.BBBB', // ciphertext on the server
                    'description': 'v1.CCCC.DDDD',
                  },
                },
                {
                  'item_type': 'memory',
                  'memory': {
                    'id': 2,
                    'name': 'Plain place', // never encrypted -> skip
                    'description': 'Plain notes',
                  },
                },
              ],
            }),
            200,
          );
        }
        if (req.method == 'PUT') {
          putPath = req.url.path;
          putBody = jsonDecode(req.body) as Map<String, dynamic>;
          return http.Response('{"updated": 1}', 200);
        }
        return http.Response('not found', 404);
      });

      final api = ApiClient(baseUrl: '', httpClient: mock);
      // Caller's already-decrypted items (mirrors ProjectNotifier.items after
      // _revealItems ran) — memory 1's ciphertext is now plaintext here.
      final List<Map<String, dynamic>> decryptedItems = [
        {
          'item_type': 'memory',
          'memory': {'id': 1, 'name': 'Beach', 'description': 'Sunny day'},
        },
        {
          'item_type': 'memory',
          'memory': {'id': 2, 'name': 'Plain place', 'description': 'Plain notes'},
        },
      ];

      final key = await generateShareKey();
      final base64Key = await ShareContentGenerator(api)
          .generate(const ProjectRef(name: 'Trip1'), decryptedItems);

      expect(base64Key, isNotNull);
      expect(putPath, '/api/projects/Trip1/share/content');
      final items = (putBody!['items'] as List).cast<Map<String, dynamic>>();
      expect(items, hasLength(1)); // only memory 1 (actually encrypted)
      expect(items.single['memory_id'], 1);

      // The uploaded envelopes decrypt back to the original plaintext under
      // the returned key (not the throwaway `key` generated above for setup).
      final returnedKey = shareKeyFromBase64(base64Key!);
      expect(
        await decryptTextWithKey(items.single['name_ciphertext'] as String, returnedKey),
        'Beach',
      );
      expect(
        await decryptTextWithKey(
            items.single['description_ciphertext'] as String, returnedKey),
        'Sunny day',
      );

      // Sanity: the unused local `key` variable above is a distinct key.
      expect(await key.extractBytes(), isNot(await returnedKey.extractBytes()));
    });

    test('returns null and uploads nothing when no memory is encrypted', () async {
      var putCalled = false;
      final mock = MockClient((req) async {
        if (req.method == 'GET') {
          return http.Response(
            jsonEncode({
              'items': [
                {
                  'item_type': 'memory',
                  'memory': {'id': 1, 'name': 'Plain', 'description': 'Notes'},
                },
              ],
            }),
            200,
          );
        }
        putCalled = true;
        return http.Response('', 204);
      });

      final api = ApiClient(baseUrl: '', httpClient: mock);
      final result = await ShareContentGenerator(api)
          .generate(const ProjectRef(name: 'Trip1'), const [
        {
          'item_type': 'memory',
          'memory': {'id': 1, 'name': 'Plain', 'description': 'Notes'},
        },
      ]);

      expect(result, isNull);
      expect(putCalled, isFalse);
    });

    test('skips an encrypted memory the caller has no decrypted copy of',
        () async {
      Map<String, dynamic>? putBody;
      final mock = MockClient((req) async {
        if (req.method == 'GET') {
          return http.Response(
            jsonEncode({
              'items': [
                {
                  'item_type': 'memory',
                  'memory': {'id': 9, 'name': 'v1.AA.BB', 'description': null},
                },
              ],
            }),
            200,
          );
        }
        putBody = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response('{"updated": 0}', 200);
      });

      final api = ApiClient(baseUrl: '', httpClient: mock);
      // decryptedItems does not contain memory 9 at all.
      final result = await ShareContentGenerator(api)
          .generate(const ProjectRef(name: 'Trip1'), const []);

      expect(result, isNull);
      expect(putBody, isNull);
    });
  });
}
