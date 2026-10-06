/// Shared project-data cache — the fix for two related costs:
///
/// 1. Switching between view mode and manage mode re-downloaded everything
///    from scratch even though the other mode had just fetched the same
///    project seconds earlier (each screen owned its own notifier instance
///    with no way to hand data to the other).
/// 2. On Android/iOS, reopening a project after the app was fully closed
///    re-downloaded the same multi-MB geo/elevation payloads every time,
///    even when nothing about the trip had changed.
///
/// This cache sits underneath [ProjectService] (see its call sites), not
/// inside the notifiers — both `ProjectNotifier` and the view-mode/shared
/// subclasses go through the same `ProjectService` methods, so caching there
/// benefits every screen for free without touching their loading logic.
///
/// Two layers:
///  - L1: an in-memory map, live for the app process. This alone fixes (1).
///  - L2: an sqflite-backed store, native platforms only (web has no
///    filesystem — see the conditional import below). This is what fixes (2).
///
/// Freshness is driven by `lock_version`, the project's optimistic-lock
/// counter (bumped server-side by content-mutating writes, now echoed in the
/// `/meta` and full-details responses — see `ProjectIO.to_dict`). `/meta` is
/// cheap and already fetched on every load, mutation-reload and background
/// poll in this app, so it doubles as the freshness oracle for free: every
/// time it's fetched, [onMetaFetched] compares the lock_version it carries
/// against what this cache has on file for the project and drops the heavy
/// entries (low-res geo, full-res geo, full details/elevation) the moment
/// they no longer match. Nothing here ever *invents* a lock_version or trusts
/// elapsed time — a stale entry is only ever detected by comparing against a
/// live server response.
///
/// One accepted race: `ProjectNotifier.load()` fires `getDetailsMeta` and
/// `getLowResGeo` in parallel (issue #178 — sequencing them would slow down
/// every fresh load to save a rare edge case). So a low-res-geo read can, in
/// the narrow window before the concurrent meta call lands, return an entry
/// that meta is about to invalidate. This is judged acceptable because the
/// low-res geo is *already* a disposable placeholder in this app's design —
/// every load unconditionally replaces it with full-res geometry moments
/// later — and that full-res fetch always runs *after* meta has resolved, so
/// it never inherits the race. Worst case: one low-res paint briefly shows a
/// trip's previous shape before the usual progressive upgrade corrects it,
/// exactly like an ordinary cold load already looks while full-res geometry
/// streams in.
library;

import 'dart:typed_data' show Uint8List;

import 'package:flutter/foundation.dart' show visibleForTesting;

import '../core/project_ref.dart';
import 'project_cache_store_stub.dart'
    if (dart.library.io) 'project_cache_store_native.dart' as store;

/// Bump when the cached JSON shapes change in a way an old cached row can't
/// safely be replayed through the current parsing code — this makes every
/// existing on-disk row a miss instead of trying to migrate it.
const _kSchemaVersion = 1;

class _Entry {
  int lockVersion;
  Map<String, dynamic>? meta;
  Map<String, dynamic>? lowResGeo;
  Map<String, dynamic>? fullGeo;
  Map<String, dynamic>? fullDetails;

  _Entry(this.lockVersion);
}

class ProjectDataCache {
  ProjectDataCache._();

  final Map<String, _Entry> _mem = {};
  int? _currentUserId;
  bool _initialized = false;

  Future<void> init() async {
    if (_initialized) return;
    _initialized = true;
    await store.cacheStoreInit();
  }

  /// `projectDataCache` is a bare process-wide singleton (like `api` and
  /// `encryption`), so multiple `test()` cases in one file share its L1 state
  /// unless a test that depends on a fresh/empty cache resets it first — the
  /// disk side (L2) never needs a matching reset since it degrades to a
  /// harmless no-op whenever there's no real sqflite plugin backing it (e.g.
  /// any plain `flutter test` run).
  @visibleForTesting
  void resetForTest() {
    _mem.clear();
    _currentUserId = null;
    diskRead = store.cacheStoreRead;
    diskWrite = store.cacheStoreWrite;
  }

  /// The disk read [_readDisk] makes — a seam, since no sqflite backend runs
  /// under `flutter test` to hand back a row.
  @visibleForTesting
  Future<Map<String, dynamic>?> Function(String key) diskRead =
      store.cacheStoreRead;

  /// The disk write [_storeWrite] makes — a seam, for a test to see what
  /// would reach the store.
  @visibleForTesting
  Future<void> Function(String key, Map<String, dynamic> row) diskWrite =
      store.cacheStoreWrite;

  /// The keys held in memory, for a test to see what a read promoted.
  @visibleForTesting
  Iterable<String> get memoryKeys => _mem.keys;

  /// Scopes the cache to the signed-in user — call on sign-in/session-restore
  /// (mirrors `ApiClient.setToken`). Own-project entries are keyed by name
  /// alone server-side, so without this, switching accounts on one device
  /// could read another account's cached trip data.
  void setCurrentUser(int? userId) {
    if (_currentUserId == userId) return;
    _currentUserId = userId;
    _scope++;
    _mem.clear(); // a fresh session starts with no assumptions in memory
  }

  /// Bumped whenever the user changes (U5-R1-1, issue #418). A fetch reads it
  /// before its request and hands it to the write that stores the response:
  /// a write is keyed by whoever is signed in when the response *lands*, so a
  /// response for the last account landing after the next one signed in
  /// would otherwise be cached as the next account's.
  int get scope => _scope;
  int _scope = 0;

  /// True when [scope] was read under a user that is no longer current.
  bool _stale(int? scope) => scope != null && scope != _scope;

  /// The lock_version this cache holds for [ref], or null when it holds
  /// nothing for it.
  ///
  /// A geometry fetch reads it before its request and hands it to the write
  /// that stores the response (issue #379). An edit made while the request
  /// was in flight bumps the version, and its reload records the new one; the
  /// response, from before the edit, would otherwise be stored under the new
  /// version, where no later `/meta` could tell it was stale.
  int? lockVersionOf(ProjectRef ref) => _mem[_key(ref)]?.lockVersion;

  /// True when [lockVersion] was read for [key] and the cache no longer holds
  /// that version: the data was fetched for a version of the trip that is
  /// gone. Null — nothing was on file before the fetch — checks nothing, as
  /// before.
  bool _outdated(String key, int? lockVersion) =>
      lockVersion != null && _mem[key]?.lockVersion != lockVersion;

  /// Every disk write, counted so a test can see that a dropped one never
  /// reached the store.
  @visibleForTesting
  int storeWrites = 0;

  void _storeWrite(String key, Map<String, dynamic> row) {
    storeWrites++;
    diskWrite(key, row);
  }

  /// Drops every cached entry, in memory and on disk — call on sign-out, and
  /// expose it as a "Clear cached trip data" settings action.
  Future<void> clearAll() async {
    _mem.clear();
    await store.cacheStoreClearAll();
  }

  /// Drops every entry kept for user 0, in memory and on disk (issue #418).
  ///
  /// User 0 is what [_key] falls back to with no user, and every restored
  /// session had none until user ids were real — so those entries can be any
  /// account's trips, and nobody can claim them. Run at startup, before the
  /// first load could read one.
  Future<void> purgeUserZero() async {
    _mem.removeWhere((key, _) => key.startsWith('0:'));
    await store.cacheStoreDeleteKeyPrefix('0:');
  }

  String _key(ProjectRef ref) => '${_currentUserId ?? 0}:${ref.ownerId ?? 0}:${ref.name}';

  /// Records a just-fetched `/meta` (or full-details) response. Always call
  /// this after any successful `getDetailsMeta`/`getDetails` network fetch —
  /// it is the only place staleness is ever detected, since every other
  /// method here just answers "what do we have on file", it never itself
  /// checks whether that's still current.
  ///
  /// [scope] is [scope] as read before the request; the write is dropped if
  /// the user changed since. Writes taking it treat it the same way.
  void onMetaFetched(ProjectRef ref, Map<String, dynamic> meta, {int? scope}) {
    if (_stale(scope)) return;
    final lockVersion = (meta['lock_version'] as num?)?.toInt();
    if (lockVersion == null) return; // older server build — no freshness signal, caching stays off for this project
    final key = _key(ref);
    final prev = _mem[key];
    final changed = prev == null || prev.lockVersion != lockVersion;
    final entry = changed ? (_mem[key] = _Entry(lockVersion)) : prev;
    entry.meta = meta;
    // Fire-and-forget: disk persistence must never add latency to the
    // request the whole app blocks on loading.
    _storeWrite(key, {
      'lockVersion': lockVersion,
      'schemaVersion': _kSchemaVersion,
      'meta': meta,
      if (changed) 'lowResGeo': null,
      if (changed) 'fullGeo': null,
      if (changed) 'fullDetails': null,
    });
  }

  /// Last known `/meta` response for [ref], used only as an offline fallback
  /// when a live fetch fails outright (see `ProjectNotifier.load`) — not part
  /// of the normal load path, so this always checks disk directly rather than
  /// trusting L1 (which may simply never have been touched this session).
  Future<Map<String, dynamic>?> readMetaForOfflineFallback(ProjectRef ref) async {
    final key = _key(ref);
    final mem = _mem[key]?.meta;
    if (mem != null) return mem;
    final disk = await _readDisk(key);
    return disk?.meta;
  }

  Future<Map<String, dynamic>?> readLowResGeo(ProjectRef ref) =>
      _readHeavy(ref, (e) => e.lowResGeo);
  /// L1 only in practice: a disk row's full geo is served as bytes rather
  /// than decoded into an entry — see [_readDisk] and [readFullGeoBytes].
  Future<Map<String, dynamic>?> readFullGeo(ProjectRef ref) =>
      _readHeavy(ref, (e) => e.fullGeo);

  /// The on-disk full-res geo as raw JSON bytes, for a caller that wants to
  /// run it through the same parse-derive-seed worker hop a network response
  /// takes (issue #299) rather than receive a Map whose geometry nothing has
  /// derived.
  ///
  /// Deliberately does NOT skip when L1 holds this ref. [_readDisk] promotes
  /// an entire disk row into L1, so L1 residency says nothing about whether
  /// the geometry was ever derived — the caller decides, by asking the
  /// geometry (see `geoGeometrySeeded`).
  Future<Uint8List?> readFullGeoBytes(ProjectRef ref) async {
    final key = _key(ref);
    final row = await store.cacheStoreReadFullGeoBytes(key);
    if (row == null || row.schemaVersion != _kSchemaVersion) return null;
    return row.bytes;
  }

  /// Records [data] in L1 only — for a caller that just decoded bytes this
  /// cache handed it, so rewriting the identical blob to disk would be pure
  /// churn. [lockVersion] is [lockVersionOf] as read before the bytes were:
  /// see [_outdated].
  void promoteFullGeo(ProjectRef ref, Map<String, dynamic> data,
      {int? scope, int? lockVersion}) {
    if (_stale(scope)) return;
    final key = _key(ref);
    if (_outdated(key, lockVersion)) return;
    (_mem[key] ??= _Entry(0)).fullGeo = data;
  }
  Future<Map<String, dynamic>?> readFullDetails(ProjectRef ref) =>
      _readHeavy(ref, (e) => e.fullDetails);

  /// Whether a full-res geo row is on disk for [ref], answered without
  /// decompressing it — the offline seed's "already done" check (issue #317).
  /// Reading it through [readFullGeo] would gunzip and decode several MB to
  /// answer a yes/no question, and [readFullGeoBytes] would gunzip it.
  ///
  /// Always false on web, which has no L2 at all.
  Future<bool> hasFullGeoOnDisk(ProjectRef ref) =>
      store.cacheStoreHasFullGeo(_key(ref));

  /// The geometry writes take [lockVersion], [lockVersionOf] as read before
  /// the fetch, and are dropped when the cache no longer holds that version.
  void writeLowResGeo(ProjectRef ref, Map<String, dynamic> data,
          {int? scope, int? lockVersion}) =>
      _writeHeavy(ref, 'lowResGeo', data, scope, lockVersion);
  void writeFullGeo(ProjectRef ref, Map<String, dynamic> data,
          {int? scope, int? lockVersion}) =>
      _writeHeavy(ref, 'fullGeo', data, scope, lockVersion);
  void writeFullDetails(ProjectRef ref, Map<String, dynamic> data, {int? scope}) =>
      _writeHeavy(ref, 'fullDetails', data, scope, null);

  /// Records [data] as the offline full-res geo for [ref] on disk **only**,
  /// deliberately not in L1 (issue #317).
  ///
  /// The seed exists so a later *offline* open has a detailed track. Putting
  /// the payload in L1 as well would make it the answer to every read for the
  /// rest of the session (see [_readHeavy]), so the map would go back to
  /// holding full-resolution geometry — the ~180 MB the zoom level-of-detail
  /// path removed.
  ///
  /// Skipped when nothing is on file for [ref]: without a lock_version
  /// confirmed by a live `/meta`, a row would be written that no later load
  /// could tell was stale. Skipped too when the version on file is no longer
  /// [lockVersion], [lockVersionOf] as read before the seed's fetch: an edit
  /// landed meanwhile, and the fetched geometry is from before it.
  void seedFullGeoToDisk(ProjectRef ref, Map<String, dynamic> data,
      {required int lockVersion}) {
    final key = _key(ref);
    final entry = _mem[key];
    if (entry == null || entry.lockVersion != lockVersion) return;
    _storeWrite(key, {
      'lockVersion': entry.lockVersion,
      'schemaVersion': _kSchemaVersion,
      'fullGeo': data,
    });
  }

  Future<Map<String, dynamic>?> _readHeavy(
      ProjectRef ref, Map<String, dynamic>? Function(_Entry) pick) async {
    final key = _key(ref);
    final mem = _mem[key];
    // L1 is authoritative for the rest of this session once established —
    // whatever onMetaFetched has (or hasn't) invalidated is the live answer.
    if (mem != null) return pick(mem);
    // Cold path (nothing touched this ref yet this session): fall back to
    // disk so a reopened app can paint from what was cached last time,
    // pending the next onMetaFetched confirming or correcting it.
    final disk = await _readDisk(key);
    return disk == null ? null : pick(disk);
  }

  void _writeHeavy(ProjectRef ref, String field, Map<String, dynamic> data,
      int? scope, int? lockVersion) {
    if (_stale(scope)) return;
    final key = _key(ref);
    if (_outdated(key, lockVersion)) return;
    final entry = _mem.putIfAbsent(key, () => _Entry(0));
    switch (field) {
      case 'lowResGeo':
        entry.lowResGeo = data;
        break;
      case 'fullGeo':
        entry.fullGeo = data;
        break;
      case 'fullDetails':
        entry.fullDetails = data;
        break;
    }
    _storeWrite(key, {
      'lockVersion': entry.lockVersion,
      'schemaVersion': _kSchemaVersion,
      field: data,
    });
  }

  Future<_Entry?> _readDisk(String key) async {
    final scope = _scope;
    final row = await diskRead(key);
    if (row == null || row['schemaVersion'] != _kSchemaVersion) return null;
    // [key] is the user's who asked; promoting it after a switch would put
    // their row in the next user's memory.
    if (_stale(scope)) return null;
    // fullGeo is deliberately absent: [cacheStoreRead] no longer decodes that
    // column, because the Map it produced was never usable. Its coordinate
    // lists are fresh objects that none of map_geometry_memo.dart's
    // identity-keyed caches have seen, so `readCachedGeo` rejects them and
    // goes to [readFullGeoBytes] anyway (issue #299) — which meant every cold
    // load of a cached trip decoded and then retained a multi-MB payload
    // nothing ever read (issue #317).
    final entry = _Entry(row['lockVersion'] as int)
      ..meta = row['meta'] as Map<String, dynamic>?
      ..lowResGeo = row['lowResGeo'] as Map<String, dynamic>?
      ..fullDetails = row['fullDetails'] as Map<String, dynamic>?;
    _mem[key] = entry; // promote so this session doesn't hit disk again
    return entry;
  }
}

final ProjectDataCache projectDataCache = ProjectDataCache._();
