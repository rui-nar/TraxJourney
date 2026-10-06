// The facet write/notify contract (issue #294, Decision 17 of
// docs/CLIENT_STATE_MAP_PLAN.md).
//
// A facet write marks the facet changed and notifies nobody; the notifier's
// notifyListeners() notifies each changed facet once, then its own listeners
// if root state changed too (U19: the root no longer re-notifies facet
// changes; test/projects/facets/no_bubble_test.dart). That keeps every
// existing notify call site's timing when state moves into facets: a write
// whose notify a stale check skips still tells nobody, and one operation
// writing several facets tells each once, together.

import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/facets/project_facet.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Every notification [n] and its facets send, in order, by name.
List<String> _record(ProjectNotifier n) {
  final heard = <String>[];
  n.addListener(() => heard.add('root'));
  n.geoFacet.addListener(() => heard.add('geo'));
  n.selectionFacet.addListener(() => heard.add('selection'));
  n.styleFacet.addListener(() => heard.add('style'));
  n.itemsFacet.addListener(() => heard.add('items'));
  n.elevationFacet.addListener(() => heard.add('elevation'));
  return heard;
}

void main() {
  late ProjectNotifier n;

  setUp(() => n = ProjectNotifier(ProjectService()));

  test('the notifier owns five distinct facets, each with its own writer', () {
    final facets = <ProjectFacet>[
      n.geoFacet, n.selectionFacet, n.styleFacet, n.itemsFacet,
      n.elevationFacet,
    ];
    expect(facets.toSet(), hasLength(5));
    expect(n.geoFacet, same(n.geoFacetWriter.facet));
    expect(n.selectionFacet, same(n.selectionFacetWriter.facet));
    expect(n.styleFacet, same(n.styleFacetWriter.facet));
    expect(n.itemsFacet, same(n.itemsFacetWriter.facet));
    expect(n.elevationFacet, same(n.elevationFacetWriter.facet));
    // Another notifier — another account's — has facets of its own.
    expect(ProjectNotifier(ProjectService()).geoFacet, isNot(same(n.geoFacet)));
    n.dispose();
  });

  test('a write bumps the version and notifies nobody by itself', () {
    final heard = _record(n);

    n.geoFacetWriter.markChanged();

    expect(n.geoFacet.version, 1);
    expect(n.selectionFacet.version, 0);
    expect(heard, isEmpty);
    n.dispose();
  });

  test('a write whose notify is skipped flushes nothing until the next notify',
      () {
    final heard = _record(n);

    n.styleFacetWriter.markChanged(); // …and the caller's stale check returns
    n.isLoading = true; // a root write, likewise

    expect(heard, isEmpty);
    // As a root field written without a notify is today: whoever notifies
    // next tells its listeners, the facet's included.
    n.notifyListeners();
    expect(heard, ['style', 'root']);
    n.dispose();
  });

  test('two facets written, one notify: each facet once, then the root once',
      () {
    final heard = _record(n);

    n.geoFacetWriter.markChanged();
    n.itemsFacetWriter.markChanged();
    n.itemsFacetWriter.markChanged(); // a second write to the same facet
    n.error = 'x'; // and root state
    n.notifyListeners();

    expect(heard, ['geo', 'items', 'root'],
        reason: 'facet listeners hear first, then the root; the unwritten '
            'facets hear nothing');
    expect(n.itemsFacet.version, 2);

    // The flush cleared the marks, the root's included: the next notify,
    // with nothing written since, tells nobody.
    heard.clear();
    n.notifyListeners();
    expect(heard, isEmpty);
    n.dispose();
  });

  test("a facet's listeners hear before the root's", () {
    // Decision 17's order: by the time a root listener runs, every facet
    // listener has already been told of the write.
    final order = <String>[];
    n.selectionFacet
        .addListener(() => order.add('facet v${n.selectionFacet.version}'));
    n.addListener(() => order.add('root'));

    n.selectionFacetWriter.markChanged();
    n.isLoading = true;
    n.notifyListeners();

    expect(order, ['facet v1', 'root']);
    n.dispose();
  });

  test('a write after dispose marks nothing and flushes nothing', () {
    final heard = _record(n);
    final geo = n.geoFacet;

    n.dispose();
    n.geoFacetWriter.markChanged();
    n.notifyListeners();

    expect(geo.version, 0);
    expect(heard, isEmpty);
  });

  test('dispose disposes every facet', () {
    final facets = <ChangeNotifier>[
      n.geoFacet, n.selectionFacet, n.styleFacet, n.itemsFacet,
      n.elevationFacet,
    ];
    for (final f in facets) {
      expect(ChangeNotifier.debugAssertNotDisposed(f), isTrue);
    }

    n.dispose();

    for (final f in facets) {
      expect(() => ChangeNotifier.debugAssertNotDisposed(f), throwsFlutterError);
    }
  });

  test('clear() keeps the facets: they belong to the instance, not the trip',
      () {
    final geo = n.geoFacet;

    n.clear();

    expect(n.geoFacet, same(geo));
    expect(() => ChangeNotifier.debugAssertNotDisposed(geo), returnsNormally);
    n.dispose();
  });
}
