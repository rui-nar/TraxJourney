// ProjectDataCache — the shared cache underneath ProjectService that (a)
// lets view mode and manage mode reuse each other's already-downloaded geo
// and elevation data instead of re-fetching on every mode toggle, and (b)
// on native platforms, survives an app restart. See project_data_cache.dart
// for the full design rationale.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';

void main() {
  setUp(() => projectDataCache.resetForTest());

  const ref = ProjectRef(name: 'Trip');

  test('a heavy payload is absent until written', () async {
    expect(await projectDataCache.readLowResGeo(ref), isNull);
    expect(await projectDataCache.readFullGeo(ref), isNull);
    expect(await projectDataCache.readFullDetails(ref), isNull);
  });

  test('onMetaFetched establishes a baseline, then a write is readable', () async {
    projectDataCache.onMetaFetched(ref, {'lock_version': 5, 'name': 'Trip'});
    projectDataCache.writeFullGeo(ref, {'type': 'FeatureCollection'});

    expect(await projectDataCache.readFullGeo(ref),
        {'type': 'FeatureCollection'});
  });

  test('a meta response with an unchanged lock_version does not invalidate '
      'what is already cached', () async {
    projectDataCache.onMetaFetched(ref, {'lock_version': 5});
    projectDataCache.writeFullGeo(ref, {'type': 'FeatureCollection'});

    // Same lock_version arrives again (e.g. a background /meta poll).
    projectDataCache.onMetaFetched(ref, {'lock_version': 5, 'name': 'Trip'});

    expect(await projectDataCache.readFullGeo(ref), isNotNull);
  });

  test('a meta response with a changed lock_version drops every heavy entry',
      () async {
    projectDataCache.onMetaFetched(ref, {'lock_version': 5});
    projectDataCache.writeLowResGeo(ref, {'a': 1});
    projectDataCache.writeFullGeo(ref, {'a': 1});
    projectDataCache.writeFullDetails(ref, {'a': 1});

    projectDataCache.onMetaFetched(ref, {'lock_version': 6}); // project changed elsewhere

    expect(await projectDataCache.readLowResGeo(ref), isNull);
    expect(await projectDataCache.readFullGeo(ref), isNull);
    expect(await projectDataCache.readFullDetails(ref), isNull);
  });

  test('a meta response with no lock_version (older server) is not recorded '
      'as a freshness baseline', () async {
    projectDataCache.onMetaFetched(ref, {'name': 'Trip'}); // no lock_version key

    expect(await projectDataCache.readMetaForOfflineFallback(ref), isNull);
  });

  test('readMetaForOfflineFallback returns the last meta seen', () async {
    projectDataCache.onMetaFetched(ref, {'lock_version': 1, 'name': 'Trip'});
    expect((await projectDataCache.readMetaForOfflineFallback(ref))?['name'],
        'Trip');
  });

  test('two different projects never share an entry', () async {
    const other = ProjectRef(name: 'Other');
    projectDataCache.onMetaFetched(ref, {'lock_version': 1});
    projectDataCache.writeFullGeo(ref, {'trip': 'Trip'});
    projectDataCache.onMetaFetched(other, {'lock_version': 1});
    projectDataCache.writeFullGeo(other, {'trip': 'Other'});

    expect(await projectDataCache.readFullGeo(ref), {'trip': 'Trip'});
    expect(await projectDataCache.readFullGeo(other), {'trip': 'Other'});
  });

  test('a shared (non-own) project is keyed by owner, not just name', () async {
    const mine = ProjectRef(name: 'Trip');
    const shared = ProjectRef(name: 'Trip', ownerId: 42);
    projectDataCache.onMetaFetched(mine, {'lock_version': 1});
    projectDataCache.writeFullGeo(mine, {'who': 'me'});

    expect(await projectDataCache.readFullGeo(shared), isNull,
        reason: 'a companion trip of the same name must not read the owner\'s cache entry');
  });

  test('setCurrentUser rescopes the cache — switching accounts on one '
      'device must not read the previous account\'s entries', () async {
    projectDataCache.setCurrentUser(1);
    projectDataCache.onMetaFetched(ref, {'lock_version': 1});
    projectDataCache.writeFullGeo(ref, {'who': 'user1'});

    projectDataCache.setCurrentUser(2);
    expect(await projectDataCache.readFullGeo(ref), isNull);
  });

  test('clearAll drops everything', () async {
    projectDataCache.onMetaFetched(ref, {'lock_version': 1});
    projectDataCache.writeFullGeo(ref, {'a': 1});
    await projectDataCache.clearAll();
    expect(await projectDataCache.readFullGeo(ref), isNull);
  });
}
