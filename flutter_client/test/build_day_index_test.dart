// buildDayIndex — every day's activity/segment ids, computed in one pass
// over `items`. Replaces looping the old per-day `_dayItemIds` scan once for
// every selected day on every map rebuild, which — stacked on top of
// re-parsing the whole trip's GeoJSON — is what made a second day-carousel
// tap in quick succession block the UI isolate long enough to ANR. See
// map_panel.dart's doc comment on buildDayIndex for the full story.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/map_panel.dart';

void main() {
  Map<String, dynamic> act(int id, String date) =>
      {'id': id, 'start_date_local': '${date}T08:00:00'};
  Map<String, dynamic> actItem(int id) =>
      {'item_type': 'activity', 'activity_id': id};
  Map<String, dynamic> segItem(int id, {String? date}) => {
        'item_type': 'segment',
        'segment': {'id': id, if (date != null) 'date': date},
      };

  test('buckets activities by their own day', () {
    final byId = {
      1: act(1, '2026-06-01'),
      2: act(2, '2026-06-01'),
      3: act(3, '2026-06-02'),
    };
    final index = buildDayIndex([actItem(1), actItem(2), actItem(3)], byId);
    expect(index['2026-06-01']!.actIds, {'1', '2'});
    expect(index['2026-06-02']!.actIds, {'3'});
    expect(index['2026-06-01']!.segIds, isEmpty);
  });

  test('a dateless segment inherits the previous activity\'s day', () {
    final byId = {1: act(1, '2026-06-01'), 2: act(2, '2026-06-02')};
    final index = buildDayIndex(
      [actItem(1), segItem(10), actItem(2)],
      byId,
    );
    // The segment sits between the two activities with no date of its own —
    // it belongs to the day of the most recently seen activity (matches the
    // old `_dayItemIds` helper's `lastDate` fallback).
    expect(index['2026-06-01']!.segIds, {'10'});
  });

  test('a segment with its own date is not affected by lastDate', () {
    final byId = {1: act(1, '2026-06-01')};
    final index = buildDayIndex(
      [actItem(1), segItem(10, date: '2026-06-05')],
      byId,
    );
    expect(index['2026-06-05']!.segIds, {'10'});
    expect(index['2026-06-01']!.segIds, isEmpty);
  });

  test('dateless activities with no preceding day are skipped', () {
    final index = buildDayIndex([actItem(1)], const {1: {'id': 1}});
    expect(index, isEmpty);
  });

  test('empty input yields an empty index', () {
    expect(buildDayIndex(const [], const {}), isEmpty);
  });

  test('a day with both activities and segments has both sets populated',
      () {
    final byId = {1: act(1, '2026-06-01')};
    final index =
        buildDayIndex([actItem(1), segItem(10)], byId);
    expect(index['2026-06-01']!.actIds, {'1'});
    expect(index['2026-06-01']!.segIds, {'10'});
  });

  // dayForSelection — the inverse lookup the view-mode day carousel uses to
  // follow a map tap (issue #322). Routed through buildDayIndex on purpose,
  // so the day the carousel scrolls to is the same day the map highlights.
  group('dayForSelection', () {
    final activities = [act(1, '2026-06-01'), act(2, '2026-06-02')];
    final items = [actItem(1), segItem(10), actItem(2)];

    test('finds the day of a tapped activity', () {
      expect(dayForSelection(items, activities, activityId: 2), '2026-06-02');
    });

    test('matches ids across int/String, like the rest of the selection code',
        () {
      expect(dayForSelection(items, activities, activityId: '1'),
          '2026-06-01');
    });

    test('finds a segment\'s day via the same carry-forward rule', () {
      // Segment 10 has no date of its own; buildDayIndex gives it the day of
      // the activity before it, and this lookup must agree.
      expect(dayForSelection(items, activities, segmentId: '10'),
          '2026-06-01');
    });

    test('returns null for an activity that is on no day', () {
      // Not in `items` at all — e.g. a feature the map draws but the day
      // index never bucketed. The carousel must not jump for this.
      expect(dayForSelection(items, activities, activityId: 99), isNull);
    });

    test('returns null when nothing is selected', () {
      expect(dayForSelection(items, activities), isNull);
    });

    test('returns null for an empty trip', () {
      expect(dayForSelection(const [], const [], activityId: 1), isNull);
    });
  });
}
