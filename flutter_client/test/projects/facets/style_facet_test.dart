// The style facet holds how the trip is drawn (issue #294, Decisions 17 and
// 19 of docs/CLIENT_STATE_MAP_PLAN.md): a style change bumps only its version,
// each notifier call still notifies once, load() fills it and clear() puts it
// all back.

import 'package:flutter/painting.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

import '../../helpers/signed_in.dart';

const _me = 3;
const _ref = ProjectRef(name: 'Trip');
const _defaultColor = Color(0xFF6B7280);

class _Service extends ProjectService {
  Map<String, dynamic> _payload() => {
        'name': 'Trip',
        'lock_version': 1,
        'activities': <dynamic>[],
        'items': <dynamic>[],
        'people': <dynamic>[],
        'groups': <dynamic>[],
        'track_color': '#112233',
        'track_secondary_color': '#445566',
        'track_width': 4,
        'alternating_track_colors': true,
        'elevation_chart_color': '#AABBCC',
        'elevation_chart_show_line': false,
        'color_by_type': true,
        'type_styles': {
          'ride': {'color': '#FF0000'},
        },
        'languages': ['fr', 'ja'],
      };

  @override
  Future<Map<String, dynamic>> getDetailsMeta(ProjectRef ref) async =>
      _payload();

  @override
  Future<Map<String, dynamic>> getDetails(ProjectRef ref,
          {bool bypassCache = false}) async =>
      _payload();

  @override
  Future<Map<String, dynamic>> getLowResGeo(ProjectRef ref) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<Map<String, dynamic>> getGeo(ProjectRef ref,
          {bool bypassCache = false}) async =>
      {'type': 'FeatureCollection', 'features': <dynamic>[]};

  @override
  Future<void> saveTrackStyle(
    ProjectRef ref, {
    String? trackColor,
    Object? trackSecondaryColor,
    double? trackWidth,
    bool? alternating,
    Object? elevationChartColor,
    bool? elevationChartShowLine,
    bool? colorByType,
    Map<String, Map<String, dynamic>>? typeStyles,
  }) async {}

  @override
  Future<void> saveLanguages(ProjectRef ref, List<String> languages) async {}
}

Future<ProjectNotifier> _loaded() async {
  final n = ProjectNotifier(_Service())
    ..loadRetryBackoff = const [Duration(milliseconds: 1)];
  await n.load(_ref);
  await pumpEventQueue();
  return n;
}

void main() {
  setUp(() {
    signInAs(_me);
    SharedPreferences.setMockInitialValues({});
  });

  test('a fresh facet starts with the defaults', () {
    final n = ProjectNotifier(_Service());
    addTearDown(n.dispose);
    final s = n.styleFacet;
    expect(s.trackColor, _defaultColor);
    expect(s.trackSecondaryColor, isNull);
    expect(s.trackWidth, 2.5);
    expect(s.alternatingTrackColors, false);
    expect(s.elevationChartColor, isNull);
    expect(s.effectiveElevationChartColor, _defaultColor);
    expect(s.elevationChartShowLine, true);
    expect(s.colorByType, false);
    expect(s.typeStyles, isEmpty);
    expect(s.languages, isEmpty);
  });

  test('load() fills the style from the project details', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    final s = n.styleFacet;
    expect(s.trackColor, const Color(0xFF112233));
    expect(s.trackSecondaryColor, const Color(0xFF445566));
    expect(s.trackWidth, 4.0);
    expect(s.alternatingTrackColors, true);
    expect(s.elevationChartColor, const Color(0xFFAABBCC));
    expect(s.elevationChartShowLine, false);
    expect(s.colorByType, true);
    expect(s.typeStyles, {
      'ride': {'color': '#FF0000'},
    });
    expect(s.languages, ['fr', 'ja']);
  });

  test('a style change bumps only the style version', () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    final before = (
      n.geoFacet.version,
      n.selectionFacet.version,
      n.itemsFacet.version,
      n.elevationFacet.version,
      n.styleFacet.version,
    );

    await n.setTrackStyle(color: const Color(0xFF010203), width: 6);

    expect(n.styleFacet.version, before.$5 + 1);
    expect(
        (
          n.geoFacet.version,
          n.selectionFacet.version,
          n.itemsFacet.version,
          n.elevationFacet.version,
        ),
        (before.$1, before.$2, before.$3, before.$4));
    expect(n.styleFacet.trackColor, const Color(0xFF010203));
    expect(n.styleFacet.trackWidth, 6.0);
  });

  test('setTrackStyle leaves an unset colour alone and clears a null one',
      () async {
    final n = await _loaded();
    addTearDown(n.dispose);

    await n.setTrackStyle(width: 5);
    expect(n.styleFacet.trackSecondaryColor, const Color(0xFF445566));
    expect(n.styleFacet.elevationChartColor, const Color(0xFFAABBCC));

    await n.setTrackStyle(secondaryColor: null, elevationColor: null);
    expect(n.styleFacet.trackSecondaryColor, isNull);
    expect(n.styleFacet.elevationChartColor, isNull);
    expect(n.styleFacet.effectiveElevationChartColor, n.styleFacet.trackColor);
  });

  test('setTrackStyle and saveLanguages notify the facet once, not the root',
      () async {
    final n = await _loaded();
    addTearDown(n.dispose);
    var root = 0, facet = 0;
    n.addListener(() => root++);
    n.styleFacet.addListener(() => facet++);

    await n.setTrackStyle(
        color: const Color(0xFF010203), colorByTypeEnabled: false);
    // A style change is the facet's alone: the root stays quiet (U19).
    expect((root, facet), (0, 1));
    await n.saveLanguages(['de']);
    expect((root, facet), (0, 2));
    expect(n.styleFacet.languages, ['de']);
  });

  test('clear() puts the style back to the defaults', () async {
    final n = await _loaded();
    addTearDown(n.dispose);

    n.clear();

    final s = n.styleFacet;
    expect(s.trackColor, _defaultColor);
    expect(s.trackSecondaryColor, isNull);
    expect(s.trackWidth, 2.5);
    expect(s.alternatingTrackColors, false);
    expect(s.elevationChartColor, isNull);
    expect(s.elevationChartShowLine, true);
    expect(s.colorByType, false);
    expect(s.typeStyles, isEmpty);
    expect(s.languages, isEmpty);
  });
}
