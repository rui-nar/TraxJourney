import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/core/design_tokens.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Issue #95: per-activity/segment-type colour + line-style overrides.
void main() {
  group('activityTypeBucket', () {
    test('collapses Strava ride sub-types', () {
      expect(activityTypeBucket('Ride'), 'ride');
      expect(activityTypeBucket('VirtualRide'), 'ride');
      expect(activityTypeBucket('EBikeRide'), 'ride');
    });

    test('collapses run sub-types', () {
      expect(activityTypeBucket('Run'), 'run');
      expect(activityTypeBucket('VirtualRun'), 'run');
    });

    test('collapses hike/walk', () {
      expect(activityTypeBucket('Hike'), 'hike');
      expect(activityTypeBucket('Walk'), 'hike');
    });

    test('unknown/null types fall back to other', () {
      expect(activityTypeBucket('Kayaking'), 'other');
      expect(activityTypeBucket(null), 'other');
    });
  });

  group('segmentTypeBucket', () {
    test('recognised segment types map to themselves', () {
      expect(segmentTypeBucket('flight'), 'flight');
      expect(segmentTypeBucket('Train'), 'train');
      expect(segmentTypeBucket('BUS'), 'bus');
      expect(segmentTypeBucket('boat'), 'boat');
    });

    test('unknown/null types fall back to other_segment', () {
      expect(segmentTypeBucket('ferry'), 'other_segment');
      expect(segmentTypeBucket(null), 'other_segment');
    });
  });

  group('resolveTypeStyle defaults (no override)', () {
    test('activity buckets default to the built-in palette + solid style', () {
      final r = resolveTypeStyle('ride', isSegment: false);
      expect(r.color, kColorRide);
      expect(r.style, LineStyleKind.solid);
    });

    test('segment buckets default to their palette + dashed style', () {
      final r = resolveTypeStyle('boat', isSegment: true);
      expect(r.color, kColorBoat);
      expect(r.style, LineStyleKind.dashed);
    });
  });

  group('resolveTypeStyle with overrides', () {
    test('colour-only override keeps the default line style', () {
      final r = resolveTypeStyle('ride',
          isSegment: false, overrides: {'color': '#112233'});
      expect(r.color, const Color(0xFF112233));
      expect(r.style, LineStyleKind.solid);
    });

    test('style-only override keeps the default colour', () {
      final r = resolveTypeStyle('flight',
          isSegment: true, overrides: {'style': 'dotted'});
      expect(r.color, kColorFlight);
      expect(r.style, LineStyleKind.dotted);
    });

    test('both colour and style overridden', () {
      final r = resolveTypeStyle('hike',
          isSegment: false, overrides: {'color': '#ABCDEF', 'style': 'dashed'});
      expect(r.color, const Color(0xFFABCDEF));
      expect(r.style, LineStyleKind.dashed);
    });

    test('malformed colour string falls back to the default colour', () {
      final r = resolveTypeStyle('run', isSegment: false, overrides: {'color': 'red'});
      expect(r.color, kColorRun);
    });
  });

  group('ProjectNotifier per-type style state', () {
    test('colorByType and typeStyles default to off/empty', () {
      final n = ProjectNotifier(ProjectService());
      expect(n.colorByType, false);
      expect(n.typeStyles, <String, Map<String, dynamic>>{});
    });

    test('setTrackStyle updates state locally with no project loaded', () async {
      final n = ProjectNotifier(ProjectService());
      await n.setTrackStyle(
        colorByTypeEnabled: true,
        typeStyleOverrides: {'ride': {'color': '#FF0000'}},
      );
      expect(n.colorByType, true);
      expect(n.typeStyles, {'ride': {'color': '#FF0000'}});
    });
  });
}
