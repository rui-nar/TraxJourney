// Regression (issue #277): when a train's schedule lookup fails *and* the
// resolve degrades to a straight endpoint chord — the usual pairing once a
// provider goes dark — the snackbar used to report only "approximate route
// shown", hiding the real cause from the user. The message must name the
// train-lookup failure first, say both things at once, and carry the
// provider's own reason when the server kept one.

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/segment_dialog.dart';

void main() {
  group('resolveOutcomeMessage', () {
    test('names the train-lookup failure when the route also degraded', () {
      final msg = resolveOutcomeMessage(
        degraded: true,
        hafasFailed: true,
        routeMode: 'rail',
        stopCount: 2,
      );
      expect(msg, contains('train could not be looked up'));
      // …and still says the shown line is only approximate.
      expect(msg.toLowerCase(), contains('approximate'));
    });

    test('includes the provider reason when the server kept one', () {
      final msg = resolveOutcomeMessage(
        degraded: true,
        hafasFailed: true,
        routeMode: 'rail',
        stopCount: 2,
        routeError: 'Train lookup failed: HAFAS request failed: 503',
      );
      expect(msg, contains('503'));
    });

    test('omits the parenthetical when there is no reason', () {
      final msg = resolveOutcomeMessage(
        degraded: false,
        hafasFailed: true,
        routeMode: 'rail',
        stopCount: 2,
        routeError: '',
      );
      expect(msg, isNot(contains('(')));
      expect(msg, contains('showing a generic route instead'));
    });

    test('degraded alone keeps the approximate-route wording', () {
      expect(
        resolveOutcomeMessage(
          degraded: true,
          hafasFailed: false,
          routeMode: 'rail',
          stopCount: 0,
        ),
        'Approximate route shown — no detailed track found for this segment',
      );
    });

    test('a clean resolve reports the mode and stop count', () {
      expect(
        resolveOutcomeMessage(
          degraded: false, hafasFailed: false, routeMode: 'rail', stopCount: 7),
        'Rail route resolved · 7 stops',
      );
      expect(
        resolveOutcomeMessage(
          degraded: false, hafasFailed: false, routeMode: 'ferry', stopCount: 2),
        'Ferry route resolved',
      );
      expect(
        resolveOutcomeMessage(
          degraded: false, hafasFailed: false, routeMode: 'bus', stopCount: 2),
        'Bus route resolved',
      );
    });
  });
}
