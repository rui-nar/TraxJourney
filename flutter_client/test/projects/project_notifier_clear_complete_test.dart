// ProjectNotifier.clear() leaves the notifier as a freshly constructed one
// would be (issue #418, Decision 2 of docs/CLIENT_STATE_MAP_PLAN.md).
//
// The notifier is app-wide and the signed-in account owns its lifetime: an
// account change clears it, and whatever clear() misses is the next account's
// to see. It used to miss people, groups, the journal selection, the share
// tokens, the sync settings, the track style, the pending segment patches and
// both background timers, among others.
//
// This compares every public getter after a load-and-clear against a fresh
// notifier's. project_notifier_clear_scan_test.dart is the half that sees a
// field added later.

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart' show Color;
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import '../helpers/signed_in.dart';

const _ref = ProjectRef(name: 'Japan');
const _day1 = '2024-04-01';
const _day2 = '2024-04-02';

Map<String, dynamic> get _emptyGeo =>
    {'type': 'FeatureCollection', 'features': <dynamic>[]};

Map<String, dynamic> _segmentFeature(String id) => {
      'type': 'Feature',
      'geometry': {
        'type': 'LineString',
        'coordinates': [
          [139.0, 35.0],
          [135.5, 34.7],
        ],
      },
      'properties': {'type': 'segment', 'segment_id': id},
    };

/// A trip carrying something for every field a load sets, typed the way a
/// decoded response is.
Map<String, dynamic> _meta() =>
    jsonDecode(jsonEncode(_metaLiteral)) as Map<String, dynamic>;

const _metaLiteral = {
      'name': 'Japan',
      'trip_start': _day1,
      // Ended, so load() schedules no background sync check.
      'trip_end': _day2,
      'activities': [
        {
          'id': 1,
          'name': 'Hike',
          'type': 'Hike',
          'start_date_local': '${_day1}T08:00:00',
          'distance': 12000,
          'moving_time': 14400,
          'total_elevation_gain': 800,
        },
      ],
      'items': [
        {'item_type': 'activity', 'activity_id': 1},
        {
          'item_type': 'segment',
          'segment': {'id': 's1', 'segment_type': 'train', 'date': _day2},
        },
        {
          'item_type': 'journal',
          'journal': {'id': 'j1', 'date': _day2, 'name': 'Day two'},
        },
      ],
      'people': [
        {'id': 1, 'name': 'Ann'},
      ],
      'groups': [
        {'id': 1, 'name': 'Family'},
      ],
      'day_meta': {
        _day1: {'sleeping': 'Hotel', 'tags': ['mountains']},
        _day2: {'sleeping': 'Camping'},
      },
      'sleeping_options': ['Hotel', 'Camping'],
      'sleeping_option_groups': {'Hotel': 'Indoors', 'Camping': 'Outdoors'},
      'counters': [
        {'name': 'Onsen', 'start': 0},
      ],
      'track_color': '#FF0000',
      'track_secondary_color': '#00FF00',
      'track_width': 4,
      'alternating_track_colors': true,
      'elevation_chart_color': '#0000FF',
      'elevation_chart_show_line': false,
      'languages': ['ja'],
      'color_by_type': true,
      'type_styles': {
        'hike': {'color': '#123456'},
      },
    };

class _Service extends ProjectService {
  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async => _meta();

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _meta();

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async => _emptyGeo;

  @override
  Future<Map<String, dynamic>> getSimplifiedGeo(ProjectRef ref, double zoom,
          {Object? bbox}) async =>
      {
        'type': 'FeatureCollection',
        'features': [_segmentFeature('s1')],
      };

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _emptyGeo;

  @override
  Future<Map<String, dynamic>> fetchFullGeoUncached(ProjectRef ref) async =>
      _emptyGeo;

  @override
  Future<Map<String, List<String>>> getMemoryPhotos(ProjectRef ref) async =>
      {};
}

/// [_Service] whose low-res geometry answers when [gate] completes.
class _HeldLowResService extends _Service {
  _HeldLowResService(this.gate);
  final Completer<Map<String, dynamic>> gate;

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) => gate.future;
}

http.Response _json(Object body) => http.Response(jsonEncode(body), 200);

/// Every public getter that holds state, by name.
Map<String, Object?> _state(ProjectNotifier n) => {
      'ref': n.ref,
      'projectName': n.projectName,
      'isViewer': n.isViewer,
      'canEditContent': n.canEditContent,
      'canManageTrip': n.canManageTrip,
      'isProjectOwner': n.isProjectOwner,
      'activities': n.activities,
      'items': n.items,
      'people': n.people,
      'groups': n.groups,
      'geo': n.geoFacet.geo,
      'geoLod': n.geoFacet.lod,
      'geoServedFrom': n.geoFacet.servedFrom,
      'isLoading': n.isLoading,
      'error': n.error,
      'loadErrorStatus': n.loadErrorStatus,
      'offlineFromCache': n.offlineFromCache,
      'isMetaLoaded': n.isMetaLoaded,
      'isElevationLoaded': n.isElevationLoaded,
      'isGeoLoaded': n.geoFacet.isLoaded,
      'isSyncMetaLoaded': n.isSyncMetaLoaded,
      'selectedActivityId': n.selectionFacet.selectedActivityId,
      'selectedSegmentId': n.selectionFacet.selectedSegmentId,
      'selectedMemoryId': n.selectionFacet.selectedMemoryId,
      'selectedJournalId': n.selectionFacet.selectedJournalId,
      'showJournals': n.selectionFacet.showJournals,
      'selectedDay': n.selectionFacet.selectedDay,
      'selectedDays': n.selectionFacet.selectedDays,
      'tripStart': n.tripStart,
      'tripEnd': n.tripEnd,
      'dayMeta': n.dayMeta,
      'orderedDayKeys': n.orderedDayKeys(),
      'sleepingOptions': n.sleepingOptions,
      'sleepingOptionGroups': n.sleepingOptionGroups,
      'counters': n.counters,
      'shareToken': n.shareToken,
      'shareTokenNoMemories': n.shareTokenNoMemories,
      'autoSyncEnabled': n.autoSyncEnabled,
      'linkedPsTripId': n.linkedPsTripId,
      'lastStravaSyncAt': n.lastStravaSyncAt,
      'lastPsSyncAt': n.lastPsSyncAt,
      'pendingSync': n.pendingSync,
      'degradedRouteUpgradeAvailable': n.degradedRouteUpgradeAvailable,
      'trackColor': n.styleFacet.trackColor,
      'trackSecondaryColor': n.styleFacet.trackSecondaryColor,
      'trackWidth': n.styleFacet.trackWidth,
      'alternatingTrackColors': n.styleFacet.alternatingTrackColors,
      'elevationChartColor': n.styleFacet.elevationChartColor,
      'effectiveElevationChartColor': n.styleFacet.effectiveElevationChartColor,
      'elevationChartShowLine': n.styleFacet.elevationChartShowLine,
      'colorByType': n.styleFacet.colorByType,
      'typeStyles': n.styleFacet.typeStyles,
      'languages': n.styleFacet.languages,
      'totalDistanceM': n.totalDistanceM,
      'totalMovingSeconds': n.totalMovingSeconds,
      'totalElevationGainM': n.totalElevationGainM,
      'dayStats': n.dayStats(_day1),
      'fullTrack': n.fullTrack,
      'perActivityTracks': n.perActivityTracks,
      'previewArc': n.previewArcNotifier.value,
      'elevationCursor': n.elevationCursorNotifier.value,
      'mapCursorDist': n.mapCursorDistNotifier.value,
      'members': n.members,
      'pendingInvites': n.pendingInvites,
      'memberInviteToken': n.memberInviteToken,
      'memberInviteRole': n.memberInviteRole,
      'quotaError': n.quotaError,
      'polarstepsOverlaySteps': n.polarstepsOverlaySteps,
      'polarstepsOverlayLabel': n.polarstepsOverlayLabel,
      'tagFilter': n.selectionFacet.tagFilter,
      'sleepingFilter': n.selectionFacet.sleepingFilter,
      'activityTypeFilter': n.selectionFacet.activityTypeFilter,
      'sourceFilter': n.selectionFacet.sourceFilter,
      'transportFilter': n.selectionFacet.transportFilter,
      'activeFilterCount': n.selectionFacet.activeFilterCount,
      'hasActiveFilter': n.selectionFacet.hasActiveFilter,
      'hasFilterableContent': n.hasFilterableContent,
      'availableTags': n.availableTags,
      'availableSleepingModes': n.availableSleepingModes,
      // The durable segment overlay, read through the only door it has: a
      // stale server snapshot still carrying the tombstoned segment, merged.
      'segmentOverlay': n.mergePendingSegmentPatches([_segmentFeature('s1')]),
      'undecrypted': n.undecryptedFields.contains('journal', 'j1', 'name'),
    };

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    projectDataCache.resetForTest();
    signInAs(1,
        httpClient: MockClient((req) async {
          final path = req.url.path;
          if (path.endsWith('/sync-meta')) {
            return _json({
              'auto_sync_enabled': false,
              'linked_ps_trip_id': 7,
              'last_strava_sync_at': 1700000000.0,
              'last_ps_sync_at': 1700000001.0,
            });
          }
          if (path.endsWith('/share-info')) {
            return _json({
              'share_token': 'tok-full',
              'share_token_no_memories': 'tok-no-mem',
            });
          }
          return _json({});
        }));
  });

  test('clear() after a full load leaves every getter as a fresh notifier has it, '
      'and no timer running', () async {
    final timers = <Timer>[];
    final fresh = _state(ProjectNotifier(_Service()));

    late Map<String, Object?> loaded;
    late Map<String, Object?> cleared;
    await runZoned(
      () async {
        final n = ProjectNotifier(_Service())
          ..loadRetryBackoff = const []
          ..zoomRefetchDebounce = const Duration(hours: 1)
          ..degradedRouteCheckInterval = const Duration(hours: 1);
        await n.load(_ref);
        for (var i = 0; i < 50 && !(n.isSyncMetaLoaded && n.geoFacet.isLoaded); i++) {
          await Future<void>.delayed(const Duration(milliseconds: 10));
        }
        await pumpEventQueue();

        // What a session does to the trip after it has loaded.
        n.setFilters(sleeping: {'Hotel'});
        n.selectJournal('j1');
        n.selectDays({_day1, _day2});
        n.toggleJournals();
        n.removeSegmentFromGeo('s1'); // a tombstone
        n.upsertSegmentInGeo('s2', _segmentFeature('s2')); // a pending patch
        n.undecryptedFields.mark('journal', 'j1', 'name');
        n.pendingSync = (strava: [<String, dynamic>{'id': 9}], polarsteps: []);
        n.degradedRouteUpgradeAvailable = true;
        n.offlineFromCache = true;
        n.loadErrorStatus = 503;
        n.error = 'boom';
        n.memberInviteToken = 'inv';
        n.memberInviteRole = 'editor';
        n.polarstepsOverlaySteps = [
          {'lat': 35.0, 'lon': 139.0},
        ];
        n.polarstepsOverlayLabel = 'Ann · Japan';
        n.previewArcNotifier.value = const [];
        n.mapCursorDistNotifier.value = 1.5;
        // The three background timers a session can have running.
        n.setMapZoom(14); // stale for the loaded bucket: arms the refetch
        n.startPhotoPolling(_ref, interval: const Duration(hours: 1));
        n.startDegradedRouteWatch(_ref);
        loaded = _state(n);

        n.clear();
        cleared = _state(n);
      },
      zoneSpecification: ZoneSpecification(
        createTimer: (self, parent, zone, duration, f) {
          final t = parent.createTimer(zone, duration, f);
          timers.add(t);
          return t;
        },
        createPeriodicTimer: (self, parent, zone, duration, f) {
          final t = parent.createPeriodicTimer(zone, duration, f);
          timers.add(t);
          return t;
        },
      ),
    );

    // The fixture has to have set what it claims to, or the comparison below
    // proves nothing for that getter.
    const untouchedByFixture = {
      'isLoading', 'isMetaLoaded', 'isElevationLoaded', 'isGeoLoaded',
      'isSyncMetaLoaded', 'isViewer', 'canEditContent', 'canManageTrip',
      'isProjectOwner', 'selectedActivityId', 'selectedSegmentId',
      'selectedMemoryId', 'selectedDay', 'elevationCursor', 'members',
      'pendingInvites', 'quotaError', 'tagFilter', 'activityTypeFilter',
      'sourceFilter', 'transportFilter', 'fullTrack', 'perActivityTracks',
    };
    final same = {
      for (final k in fresh.keys)
        if (_equal(loaded[k], fresh[k])) k,
    };
    expect(same, untouchedByFixture);

    for (final k in fresh.keys) {
      expect(cleared[k], equals(fresh[k]), reason: '$k survived clear()');
    }
    expect(timers.where((t) => t.isActive), isEmpty,
        reason: 'a timer started for the cleared trip is still running');
  });

  test('low-res geometry landing after clear() is not applied (U5-R1-3)',
      () async {
    final gate = Completer<Map<String, dynamic>>();
    final n = ProjectNotifier(_HeldLowResService(gate))
      ..loadRetryBackoff = const [];
    final loading = n.load(_ref);
    await pumpEventQueue();

    n.clear(); // the account changed while the load waited
    gate.complete({
      'type': 'FeatureCollection',
      'features': [_segmentFeature('s1')],
    });
    await loading;

    expect(n.geoFacet.geo, isNull);
  });
}

bool _equal(Object? a, Object? b) {
  if (a is Color || b is Color) return a == b;
  return equals(b).matches(a, {});
}
