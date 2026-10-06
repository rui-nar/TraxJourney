/// sqflite-backed on-device cache backend (Android/iOS/desktop) — the L2
/// layer under `ProjectDataCache`'s in-memory L1, so a project already opened
/// on this device survives an app restart without re-downloading its geo and
/// elevation data. Compiled in only via `project_data_cache.dart`'s
/// conditional import (`if (dart.library.io)`); never reachable from web.
///
/// Every public function here is best-effort: this is a cache, never the
/// source of truth, so any failure to open or use the database (no plugin
/// registered — e.g. a plain `flutter test` VM run with no device backing
/// the platform channel, disk full, corrupt file) degrades to "nothing was
/// cached" rather than throwing. `ProjectDataCache`'s L1 map and the live
/// network are what the app actually depends on; this is purely a speed-up.
library;

import 'dart:convert' show jsonDecode, jsonEncode, utf8;
import 'dart:io' show GZipCodec;
import 'dart:typed_data' show Uint8List;

import 'package:flutter/foundation.dart' show compute, visibleForTesting;
import 'package:sqflite/sqflite.dart';

const _kTable = 'project_cache';

Database? _db;
bool _unavailable = false;

Future<Database?> _open() async {
  if (_unavailable) return null;
  final existing = _db;
  if (existing != null) return existing;
  try {
    final dir = await getDatabasesPath();
    final db = await openDatabase(
      '$dir/project_data_cache.db',
      version: 1,
      onCreate: (db, version) => db.execute('''
        CREATE TABLE $_kTable (
          cache_key TEXT PRIMARY KEY,
          lock_version INTEGER NOT NULL,
          schema_version INTEGER NOT NULL,
          meta_gz BLOB,
          low_res_geo_gz BLOB,
          full_geo_gz BLOB,
          full_details_gz BLOB,
          updated_at INTEGER NOT NULL
        )
      '''),
    );
    _db = db;
    return db;
  } catch (_) {
    // No sqflite plugin registered for this runtime (or any other open
    // failure) — stop retrying and behave as a permanent cache miss.
    _unavailable = true;
    return null;
  }
}

Future<void> cacheStoreInit() async {
  await _open();
}

Future<void> cacheStoreClearAll() async {
  try {
    final db = await _open();
    if (db == null) return;
    await db.delete(_kTable);
  } catch (_) {}
}

/// Deletes every row whose key starts with [prefix].
///
/// A range, not `LIKE`: `_` and `%` are wildcards there, and `LIKE` folds
/// ASCII case, so a prefix holding either could match more than itself. A
/// key starts with [prefix] exactly when it sorts in `[prefix, upper)` — see
/// [keyPrefixRange].
Future<void> cacheStoreDeleteKeyPrefix(String prefix) async {
  try {
    final db = await _open();
    if (db == null) return;
    final (lower, upper) = keyPrefixRange(prefix);
    await db.delete(_kTable,
        where: 'cache_key >= ? AND cache_key < ?', whereArgs: [lower, upper]);
  } catch (_) {}
}

/// The half-open range of keys that start with [prefix]: the prefix itself,
/// up to the prefix with its last character bumped by one. ASCII only, so
/// SQLite's byte-wise (BINARY) text order and code-unit order agree.
@visibleForTesting
(String, String) keyPrefixRange(String prefix) {
  if (prefix.isEmpty || prefix.codeUnits.any((c) => c >= 0x7F)) {
    throw ArgumentError.value(prefix, 'prefix', 'must be non-empty ASCII');
  }
  final last = prefix.codeUnitAt(prefix.length - 1);
  return (
    prefix,
    prefix.substring(0, prefix.length - 1) + String.fromCharCode(last + 1),
  );
}

/// Pure gzip+JSON encode, split out so it can run via [compute] on a
/// background isolate instead of the UI isolate: full-res geo/elevation
/// payloads can be several MB (see project_service.dart), and jsonEncode +
/// gzip of that size synchronously on the UI isolate was enough to trip an
/// Android ANR the moment the caller (a fire-and-forget cache write right
/// after project load) landed while the user started interacting with the
/// map. Exposed for testing the codec directly without a real sqflite
/// backend.
@visibleForTesting
Uint8List? gzEncode(Map<String, dynamic>? value) =>
    value == null ? null : Uint8List.fromList(GZipCodec().encode(utf8.encode(jsonEncode(value))));

Future<Uint8List?> _gz(Map<String, dynamic>? value) =>
    value == null ? Future.value(null) : compute(gzEncode, value);

/// Pure gzip+JSON decode — see [gzEncode] for why this runs via [compute].
@visibleForTesting
Map<String, dynamic>? gzDecode(List<int> blob) {
  if (blob.isEmpty) return null;
  try {
    final decoded = jsonDecode(utf8.decode(GZipCodec().decode(blob)));
    return decoded is Map<String, dynamic> ? decoded : null;
  } catch (_) {
    return null; // corrupt row — treated as a cache miss by the caller
  }
}

Future<Map<String, dynamic>?> _ungz(Object? blob) {
  if (blob is! List<int> || blob.isEmpty) return Future.value(null);
  return compute(gzDecode, blob);
}

/// Gunzip without decoding — the stored blob is already exactly the JSON
/// bytes of the payload (see [gzEncode]), so handing those back lets the
/// full-res geo take the same parse-derive-seed hop on a worker that a
/// network response takes (issue #299). Decoding it to a Map here instead
/// produced fresh coordinate lists that none of map_geometry_memo.dart's
/// identity-keyed caches had ever seen, so a trip served from disk paid the
/// full cold derivation on the UI isolate — the 2.4 s stall that tripped the
/// ANR watchdog mid-pan.
@visibleForTesting
Uint8List? gunzipToBytes(List<int> blob) =>
    blob.isEmpty ? null : Uint8List.fromList(GZipCodec().decode(blob));

Future<Uint8List?> _ungzBytes(Object? blob) {
  if (blob is! List<int> || blob.isEmpty) return Future.value(null);
  return compute(gunzipToBytes, blob);
}

/// The stored full-res geo as raw JSON bytes, with the row's versions so the
/// caller can apply the same freshness checks [cacheStoreRead] does.
Future<({int lockVersion, int schemaVersion, Uint8List? bytes})?>
    cacheStoreReadFullGeoBytes(String key) =>
        _readColumnBytes(key, 'full_geo_gz');


Future<({int lockVersion, int schemaVersion, Uint8List? bytes})?>
    _readColumnBytes(String key, String column) async {
  try {
    final db = await _open();
    if (db == null) return null;
    final rows = await db.query(_kTable,
        columns: ['lock_version', 'schema_version', column],
        where: 'cache_key = ?',
        whereArgs: [key],
        limit: 1);
    if (rows.isEmpty) return null;
    final row = rows.first;
    return (
      lockVersion: row['lock_version'] as int,
      schemaVersion: row['schema_version'] as int,
      bytes: await _ungzBytes(row[column]),
    );
  } catch (_) {
    return null;
  }
}

/// Whether a full-res geo blob is stored for [key] — a length check on the
/// column, with no gunzip and no decode (issue #317). This is asked on every
/// load to decide whether the offline seed still has work to do, so it must
/// not cost what reading the payload costs.
Future<bool> cacheStoreHasFullGeo(String key) async {
  try {
    final db = await _open();
    if (db == null) return false;
    final rows = await db.rawQuery(
        'SELECT length(full_geo_gz) AS n FROM $_kTable WHERE cache_key = ? LIMIT 1',
        [key]);
    if (rows.isEmpty) return false;
    final n = rows.first['n'];
    return n is int && n > 0;
  } catch (_) {
    return false;
  }
}

/// Note what is *not* here: `full_geo_gz`. Decoding it produced a Map whose
/// coordinate lists no geometry cache had ever seen, so every reader rejected
/// it and went to [cacheStoreReadFullGeoBytes] instead (issue #299) — while
/// the promoted copy sat in L1 for the rest of the session, several MB that
/// nothing read (issue #317). Callers that want the geometry ask for the
/// bytes.
Future<Map<String, dynamic>?> cacheStoreRead(String key) async {
  try {
    final db = await _open();
    if (db == null) return null;
    final rows = await db.query(_kTable,
        columns: [
          'lock_version',
          'schema_version',
          'meta_gz',
          'low_res_geo_gz',
          'full_details_gz',
          'updated_at',
        ],
        where: 'cache_key = ?',
        whereArgs: [key],
        limit: 1);
    if (rows.isEmpty) return null;
    final row = rows.first;
    return {
      'lockVersion': row['lock_version'] as int,
      'schemaVersion': row['schema_version'] as int,
      'meta': await _ungz(row['meta_gz']),
      'lowResGeo': await _ungz(row['low_res_geo_gz']),
      'fullDetails': await _ungz(row['full_details_gz']),
      'updatedAt': row['updated_at'] as int,
    };
  } catch (_) {
    return null;
  }
}

/// Merges [row] into whatever is already stored under [key] — a key present
/// in [row] replaces that column; a key absent from [row] is left as-is. This
/// lets a meta write and a later geo write for the same project each touch
/// only the column they own, without clobbering the other.
Future<void> cacheStoreWrite(String key, Map<String, dynamic> row) async {
  try {
    final db = await _open();
    if (db == null) return;
    // Merge at the BLOB level, not through decoded maps.
    //
    // The old version read the whole row back through cacheStoreRead —
    // gunzipping and jsonDecoding every column — then re-encoded and
    // re-gzipped all of them to change one. A load calls this three times
    // (low-res geo, full geo, details), so opening a trip decoded and
    // re-encoded the 33 MB details payload three times over to rewrite bytes
    // that never changed. It runs off the UI isolate, so it never showed up
    // in a blocking span, but transient allocation at that scale is what
    // drives the GC behind issue #276's 2.17 GB RSS.
    //
    // Carrying untouched columns across as stored bytes also removes the
    // decode step's failure mode: a column this write does not name cannot be
    // lost to a decode that returned null.
    final existing = await db.query(_kTable,
        where: 'cache_key = ?', whereArgs: [key], limit: 1);
    final prev = existing.isEmpty ? null : existing.first;

    Future<Object?> blob(String field, String column) async =>
        row.containsKey(field)
            ? await _gz(row[field] as Map<String, dynamic>?)
            : prev?[column];

    await db.insert(
      _kTable,
      {
        'cache_key': key,
        // An absent lockVersion means "unknown — do not touch". Writing a
        // literal 0 clobbered the real version, which made the whole row
        // unusable on the next cold start.
        'lock_version': row['lockVersion'] ?? prev?['lock_version'] ?? 0,
        'schema_version': row['schemaVersion'] ?? prev?['schema_version'] ?? 0,
        'meta_gz': await blob('meta', 'meta_gz'),
        'low_res_geo_gz': await blob('lowResGeo', 'low_res_geo_gz'),
        'full_geo_gz': await blob('fullGeo', 'full_geo_gz'),
        'full_details_gz': await blob('fullDetails', 'full_details_gz'),
        'updated_at': DateTime.now().millisecondsSinceEpoch,
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  } catch (_) {}
}
