// Tests the trip-video request flow (docs/TRIP_VIDEO_PLAN.md, U7) against a
// fake HTTP client: the polyline encoder the consent geometry is sent in,
// building that geometry on the device, and VideoRequestNotifier's phases —
// above all that decrypted geometry leaves the device only after consent.

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/map/polyline_decoder.dart';
import 'package:traxjourney_client/src/projects/video_job_notifier.dart';

http.Response _json(int status, Object body) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

const quotaOne = {'limit': 1, 'used': 0, 'remaining': 1};
const quotaNone = {'limit': 1, 'used': 1, 'remaining': 0};
const quotaUnlimited = {'limit': null, 'used': 3, 'remaining': null};

Map<String, dynamic> planJson(
        {bool available = true, Map<String, dynamic> quota = quotaOne}) =>
    {
      'available': available,
      'length_s': 60,
      'legs': 3,
      'clips': [],
      'clip_counts': {'30': 2, '60': 3, '90': 3},
      'skipped': [],
      'consent_required': [],
      'quota': quota,
      'resolutions': [720],
    };

/// What the server answers for an encrypted trip it won't ask consent for
/// (no video left, or unavailable): 200 with an empty plan.
Map<String, dynamic> emptyPlanJson(
        {bool available = true, Map<String, dynamic> quota = quotaNone}) =>
    {
      ...planJson(available: available, quota: quota),
      'legs': 0,
      'clip_counts': {'30': 0, '60': 0, '90': 0},
    };

Map<String, dynamic> consent409With(
        {Map<String, dynamic> quota = quotaOne, bool available = true}) =>
    {
      'detail': {
        ...(consent409['detail'] as Map),
        'quota': quota,
        'available': available,
      }
    };

final consent409 = {
  'detail': {
    'code': 'consent_required',
    'message': 'This trip is encrypted.',
    'consent_required': [7, 8],
  }
};

// Activity 7's track is already merged and decrypted. 8's polyline is
// deferred by /meta (null); its stored geometry says it has no track, only
// encrypted endpoints.
const track7 = '_p~iF~ps|U_ulLnnqC_mqNvxq`@';
List<Map<String, dynamic>> activities() => [
      {
        'id': 7,
        'map': {'summary_polyline': track7},
      },
      {
        'id': 8,
        'map': {'summary_polyline': null},
        'start_latlng': null,
        'end_latlng': null,
        'start_latlng_enc': 'v1.a.b',
        'end_latlng_enc': 'v1.c.d',
      },
    ];

Future<String?> fakeReveal(String? v) async => switch (v) {
      'v1.a.b' => '[48.85, 2.35]',
      'v1.c.d' => '[48.86, 2.36]',
      'v1.track.enc' => track7,
      _ => v,
    };

/// What `GET …/activities/8/track` returns for the trackless activity 8.
Map<String, dynamic> stored8() => {
      'id': 8,
      'map': {'summary_polyline': null},
      'start_latlng': null,
      'end_latlng': null,
      'start_latlng_enc': 'v1.a.b',
      'end_latlng_enc': 'v1.c.d',
    };

/// Records the ids fetched; serves [stored] (activity 8 by default).
TrackFetcher fakeFetch(List<int> fetched,
        [Map<int, Map<String, dynamic>>? stored]) =>
    (id) async {
      fetched.add(id);
      return (stored ?? {8: stored8()})[id];
    };

void main() {
  group('encodePolyline', () {
    test('round-trips through decodePolyline, negatives included', () {
      final pts = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)];
      // The reference example from Google's polyline docs.
      expect(encodePolyline(pts), '_p~iF~ps|U_ulLnnqC_mqNvxq`@');
      final back = decodePolyline(encodePolyline([(-33.9, 151.2), (51.5, -0.1)]));
      expect(back.map((p) => (p.lat, p.lon)).toList(),
          [(-33.9, 151.2), (51.5, -0.1)]);
    });
  });

  group('buildConsentGeometry', () {
    test('sends the merged track; a stored geometry with no track sends the '
        'decrypted endpoints as a 2-point line', () async {
      final fetched = <int>[];
      final progress = <int>[];
      final r = await buildConsentGeometry([7, 8], activities(),
          fetchTrack: fakeFetch(fetched),
          reveal: fakeReveal,
          onProgress: progress.add);
      expect(r.missing, isEmpty);
      expect(r.fetchFailed, isFalse);
      expect(fetched, [8], reason: "7's track is already merged");
      expect(r.geometry[7], track7);
      final line = decodePolyline(r.geometry[8]!);
      expect(line.map((p) => (p.lat, p.lon)).toList(),
          [(48.85, 2.35), (48.86, 2.36)]);
      expect(progress, [1, 2]);
    });

    test('a track /meta deferred is fetched and decrypted, not replaced by '
        'its endpoints', () async {
      final fetched = <int>[];
      final r = await buildConsentGeometry([8], activities(),
          fetchTrack: fakeFetch(fetched, {
            8: {
              ...stored8(),
              'map': {'summary_polyline': 'v1.track.enc'},
            },
          }),
          reveal: fakeReveal);
      expect(fetched, [8]);
      expect(r.missing, isEmpty);
      expect(r.geometry[8], track7);
      expect(decodePolyline(r.geometry[8]!), hasLength(3));
    });

    test('a fetched track that stays ciphertext is missing, not 2 points',
        () async {
      final r = await buildConsentGeometry([8], activities(),
          fetchTrack: fakeFetch([], {
            8: {
              ...stored8(),
              'map': {'summary_polyline': 'v1.other.key'},
            },
          }),
          reveal: fakeReveal);
      expect(r.geometry, isEmpty);
      expect(r.missing, [8]);
    });

    test('an activity still encrypted on this device is missing', () async {
      final fetched = <int>[];
      final r = await buildConsentGeometry(
        [7, 9],
        [
          {
            'id': 7,
            'map': {'summary_polyline': 'v1.locked.cipher'},
          },
        ],
        fetchTrack: fakeFetch(fetched, {}),
        reveal: (v) async => v, // locked: reveal is the identity
      );
      expect(fetched, [9]);
      expect(r.geometry, isEmpty);
      expect(r.missing, [7, 9]);
    });

    test('a failed fetch stops there and says so', () async {
      final fetched = <int>[];
      final r = await buildConsentGeometry(
        [8, 10],
        activities(),
        fetchTrack: (id) async {
          fetched.add(id);
          throw Exception('offline');
        },
        reveal: fakeReveal,
      );
      expect(r.fetchFailed, isTrue);
      expect(fetched, [8]);
    });
  });

  group('VideoRequestNotifier', () {
    late List<http.Request> sent;

    VideoRequestNotifier notifier(
        Future<http.Response> Function(http.Request) handler,
        {FieldRevealer reveal = fakeReveal, TrackFetcher? fetchTrack}) {
      sent = [];
      final client = ApiClient(httpClient: MockClient((req) async {
        sent.add(req);
        return handler(req);
      }))
        ..setToken('jwt');
      return VideoRequestNotifier(
        ref: const ProjectRef(name: 'Trip'),
        activities: activities,
        client: client,
        reveal: reveal,
        fetchTrack: fetchTrack ?? fakeFetch([]),
      );
    }

    Map<String, dynamic> body(http.Request r) =>
        jsonDecode(r.body) as Map<String, dynamic>;

    test('plan ready: picks the tallest allowed resolution', () async {
      final n = notifier((_) async => _json(200, planJson()));
      await n.loadPlan();
      expect(n.phase, VideoRequestPhase.ready);
      expect(n.height, 720);
      expect(n.plan!.clipCounts, {30: 2, 60: 3, 90: 3});
      expect(n.plan!.quota.remaining, 1);
      expect(sent.single.url.path, '/api/projects/Trip/video/plan');
      expect(body(sent.single).containsKey('decrypted_geometry'), isFalse);
    });

    test('plan says unavailable', () async {
      final n = notifier((_) async => _json(200, planJson(available: false)));
      await n.loadPlan();
      expect(n.phase, VideoRequestPhase.unavailable);
      expect(n.canSubmit, isFalse);
    });

    test('409 asks for consent and sends nothing until accepted', () async {
      final n = notifier((req) async =>
          body(req).containsKey('decrypted_geometry')
              ? _json(200, planJson())
              : _json(409, consent409));
      await n.loadPlan();
      expect(n.phase, VideoRequestPhase.consentNeeded);
      expect(n.consentIds, [7, 8]);
      expect(sent, hasLength(1));
      expect(body(sent.single).containsKey('decrypted_geometry'), isFalse);

      await n.acceptConsent();
      expect(n.phase, VideoRequestPhase.ready);
      expect(sent, hasLength(2));
      final geometry = body(sent.last)['decrypted_geometry'] as Map;
      expect(geometry.keys, unorderedEquals(['7', '8']));
      expect(geometry['7'], track7);
      expect(n.consentedCount, 2);

      // The job carries the same geometry.
      await n.submit();
      expect((body(sent.last)['decrypted_geometry'] as Map).keys,
          unorderedEquals(['7', '8']));
    });

    test('declining sends nothing more', () async {
      final n = notifier((_) async => _json(409, consent409));
      await n.loadPlan();
      n.declineConsent();
      expect(n.phase, VideoRequestPhase.declined);
      expect(sent, hasLength(1));
      expect(body(sent.single).containsKey('decrypted_geometry'), isFalse);
    });

    test('accept with an undecryptable activity sends nothing', () async {
      final n = notifier((_) async => _json(409, consent409),
          reveal: (v) async => v); // locked: reveal is the identity
      await n.loadPlan();
      await n.acceptConsent();
      expect(n.phase, VideoRequestPhase.error);
      expect(sent, hasLength(1));
    });

    test('409 on create: consent then the create is resent with geometry',
        () async {
      final n = notifier((req) async {
        if (req.url.path.endsWith('/plan')) return _json(200, planJson());
        return body(req).containsKey('decrypted_geometry')
            ? _json(201, {'job_id': 5})
            : _json(409, consent409);
      });
      await n.loadPlan();
      await n.submit();
      expect(n.phase, VideoRequestPhase.consentNeeded);
      await n.acceptConsent();
      expect(n.phase, VideoRequestPhase.started);
      expect(n.jobId, 5);
      expect(sent.last.url.path, '/api/projects/Trip/video');
      expect(body(sent.last)['length_s'], 60);
      expect(body(sent.last)['height'], 720);
    });

    test('402 on create keeps the refusal for the upgrade message', () async {
      final n = notifier((req) async {
        if (req.url.path.endsWith('/plan')) return _json(200, planJson());
        return _json(402, {
          'detail': 'Your plan includes 1 video per month. Upgrade to make more.',
          'code': 'quota_exceeded',
          'resource': 'videos',
          'plan': 'free',
          'limit': 1,
          'used': 1,
          'needed': 2,
        });
      });
      await n.loadPlan();
      await n.submit();
      expect(n.phase, VideoRequestPhase.quotaExceeded);
      expect(n.quotaError!.resource, 'videos');
      expect(n.canSubmit, isFalse);
    });

    test('a refused resolution is cleared by picking another', () async {
      final n = notifier((req) async {
        if (req.url.path.endsWith('/plan')) return _json(200, planJson());
        return _json(402, {
          'detail': 'Your plan makes videos up to 720p. Upgrade for 1080p.',
          'code': 'quota_exceeded',
          'resource': 'video_height',
          'plan': 'free',
          'limit': 720,
          'used': 720,
          'needed': 1080,
        });
      });
      await n.loadPlan();
      n.setHeight(1080);
      await n.submit();
      expect(n.phase, VideoRequestPhase.quotaExceeded);
      n.setHeight(720);
      expect(n.phase, VideoRequestPhase.ready);
      expect(n.quotaError, isNull);
    });

    test('503 on create is the unavailable state', () async {
      final n = notifier((req) async {
        if (req.url.path.endsWith('/plan')) return _json(200, planJson());
        return _json(503, {'detail': 'Video rendering is not available right now'});
      });
      await n.loadPlan();
      await n.submit();
      expect(n.phase, VideoRequestPhase.unavailable);
    });

    test('without an injected fetcher the track comes from the per-activity '
        'endpoint, then goes out decrypted', () async {
      sent = [];
      final client = ApiClient(httpClient: MockClient((req) async {
        sent.add(req);
        if (req.method == 'GET') {
          return _json(200, {
            ...stored8(),
            'map': {'summary_polyline': 'v1.track.enc'},
          });
        }
        return body(req).containsKey('decrypted_geometry')
            ? _json(200, planJson())
            : _json(409, consent409);
      }))
        ..setToken('jwt');
      final n = VideoRequestNotifier(
        ref: const ProjectRef(name: 'Trip'),
        activities: activities,
        client: client,
        reveal: fakeReveal,
      );
      await n.loadPlan();
      await n.acceptConsent();
      expect(n.phase, VideoRequestPhase.ready);
      expect(sent[1].method, 'GET');
      expect(sent[1].url.path, '/api/projects/Trip/activities/8/track');
      final geometry = body(sent.last)['decrypted_geometry'] as Map;
      expect(geometry['8'], track7);
    });

    test('a failed track fetch sends nothing and says so', () async {
      final n = notifier((_) async => _json(409, consent409),
          fetchTrack: (_) async => throw Exception('offline'));
      await n.loadPlan();
      await n.acceptConsent();
      expect(n.phase, VideoRequestPhase.error);
      expect(n.errorMessage, contains("Couldn't load this trip's encrypted"));
      expect(n.consentProgress, isNull);
      expect(sent, hasLength(1));
    });

    test('disposed while fetching tracks: the create is never sent', () async {
      final gate = Completer<Map<String, dynamic>?>();
      final n = notifier((req) async {
        if (req.url.path.endsWith('/plan')) return _json(200, planJson());
        return body(req).containsKey('decrypted_geometry')
            ? _json(201, {'job_id': 5})
            : _json(409, consent409);
      }, fetchTrack: (_) => gate.future);
      await n.loadPlan();
      await n.submit();
      expect(n.phase, VideoRequestPhase.consentNeeded);
      final accepting = n.acceptConsent();
      await Future<void>.delayed(Duration.zero);
      expect(n.consentProgress, 1, reason: '7 needs no fetch');
      n.dispose();
      gate.complete(stored8());
      await accepting;
      expect(sent.where((r) => r.url.path == '/api/projects/Trip/video'),
          hasLength(1), reason: 'only the create the 409 answered');
    });

    group('refused early (F-b)', () {
      late List<int> fetched;

      VideoRequestNotifier refusing(
          Future<http.Response> Function(http.Request) handler) {
        fetched = [];
        return notifier((req) async {
          expect(body(req).containsKey('decrypted_geometry'), isFalse,
              reason: 'no decrypted geometry may leave the device');
          return handler(req);
        }, fetchTrack: fakeFetch(fetched));
      }

      test('no video left on an encrypted trip: no consent, nothing fetched '
          'or sent, cannot create', () async {
        final n = refusing((_) async => _json(200, emptyPlanJson()));
        await n.loadPlan();
        expect(n.phase, VideoRequestPhase.noneLeft);
        expect(n.quota!.remaining, 0);
        expect(n.consentIds, isEmpty);
        expect(n.canSubmit, isFalse);
        await n.submit();
        expect(sent, hasLength(1), reason: 'submit sends nothing');
        expect(fetched, isEmpty);
      });

      test('a 409 whose quota is used up does not ask for consent', () async {
        final n = refusing(
            (_) async => _json(409, consent409With(quota: quotaNone)));
        await n.loadPlan();
        expect(n.phase, VideoRequestPhase.noneLeft);
        expect(n.quota!.remaining, 0);
        expect(n.consentIds, isEmpty);
        expect(n.canSubmit, isFalse);
        expect(fetched, isEmpty);
        expect(sent, hasLength(1));
      });

      test('a 409 that says unavailable does not ask for consent', () async {
        final n = refusing(
            (_) async => _json(409, consent409With(available: false)));
        await n.loadPlan();
        expect(n.phase, VideoRequestPhase.unavailable);
        expect(n.consentIds, isEmpty);
        expect(n.canSubmit, isFalse);
        expect(fetched, isEmpty);
      });

      test('unavailable on an encrypted trip: nothing fetched or sent',
          () async {
        final n = refusing((_) async =>
            _json(200, emptyPlanJson(available: false, quota: quotaOne)));
        await n.loadPlan();
        expect(n.phase, VideoRequestPhase.unavailable);
        expect(n.canSubmit, isFalse);
        expect(fetched, isEmpty);
      });
    });

    test('unlimited quota: consent, plan and create as before', () async {
      final n = notifier((req) async {
        if (!body(req).containsKey('decrypted_geometry')) {
          return _json(409, consent409With(quota: quotaUnlimited));
        }
        return req.url.path.endsWith('/plan')
            ? _json(200, planJson(quota: quotaUnlimited))
            : _json(201, {'job_id': 9});
      });
      await n.loadPlan();
      expect(n.phase, VideoRequestPhase.consentNeeded);
      await n.acceptConsent();
      expect(n.phase, VideoRequestPhase.ready);
      expect(n.quota!.unlimited, isTrue);
      expect(n.canSubmit, isTrue);
      await n.submit();
      expect(n.phase, VideoRequestPhase.started);
      expect(n.jobId, 9);
    });

    test('422 shows the server detail', () async {
      final n = notifier((_) async =>
          _json(422, {'detail': 'Nothing in this trip can be animated'}));
      await n.loadPlan();
      expect(n.phase, VideoRequestPhase.error);
      expect(n.errorMessage, 'Nothing in this trip can be animated');
    });
  });
}
