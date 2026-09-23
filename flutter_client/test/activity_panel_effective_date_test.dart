// Unit tests for ActivityPanel.effectiveSegmentDate — the day-bucket date a
// segment renders under. Regression for "create a train segment, it appears in
// the activity panel, then disappears": a dateless segment is bucketed under the
// inherited (preceding-activity) date, but the old reveal code matched on the
// segment's raw null date and so never un-collapsed the day it lived in.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/activity_panel.dart';

void main() {
  Map<dynamic, Map<String, dynamic>> activityById(
          List<Map<String, dynamic>> acts) =>
      {for (final a in acts) a['id']: a};

  Map<String, dynamic> activityItem(int id) =>
      {'item_type': 'activity', 'activity_id': id};
  Map<String, dynamic> segmentItem(String id, {String? date}) =>
      {'item_type': 'segment', 'segment': {'id': id, 'date': date}};

  group('effectiveSegmentDate', () {
    test('segment with its own date returns that date', () {
      final items = [segmentItem('s1', date: '2026-06-10')];
      expect(
        ActivityPanel.effectiveSegmentDate(items, const {}, 's1'),
        '2026-06-10',
      );
    });

    test('dateless segment inherits the preceding activity date (the bug fix)',
        () {
      final acts = [
        {'id': 7, 'start_date_local': '2026-06-12T09:00:00'},
      ];
      final items = [activityItem(7), segmentItem('s1')]; // s1 has no date
      expect(
        ActivityPanel.effectiveSegmentDate(items, activityById(acts), 's1'),
        '2026-06-12', // the day it actually buckets/renders under
      );
    });

    test('only activities advance the running date (mirrors bucketing)', () {
      final acts = [
        {'id': 7, 'start_date_local': '2026-06-12T09:00:00'},
      ];
      // A dated memory between the activity and the segment must NOT become the
      // inherited date — only activities propagate it in _buildDisplayList.
      final items = [
        activityItem(7),
        {'item_type': 'memory', 'memory': {'id': 1, 'date': '2026-06-20'}},
        segmentItem('s1'),
      ];
      expect(
        ActivityPanel.effectiveSegmentDate(items, activityById(acts), 's1'),
        '2026-06-12',
      );
    });

    test('dateless segment with nothing dated before it returns null', () {
      final items = [segmentItem('s1')];
      expect(ActivityPanel.effectiveSegmentDate(items, const {}, 's1'), isNull);
    });

    test('unknown segment id returns null', () {
      final items = [segmentItem('s1', date: '2026-06-10')];
      expect(
        ActivityPanel.effectiveSegmentDate(items, const {}, 'nope'),
        isNull,
      );
    });
  });
}
