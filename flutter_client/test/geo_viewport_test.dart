// Issue #324: the viewport box the client asks the server for.
//
// Zoom bounds the *detail* of the geometry the server sends and does nothing
// about the *extent*, so a deep zoom still fetched — and made the server
// simplify — the whole trip: measured at 4.0 s per request for a
// 219-activity trip at zoom 15 against 0.26 s at zoom 9. The cost rises with
// zoom while the saving falls (issue #324).

import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/geo_viewport.dart';

const _alps = GeoBox(7.3, 45.3, 7.5, 45.5);

void main() {
  group('viewportBox', () {
    test('accepts an ordinary camera box', () {
      expect(viewportBox(7.3, 45.3, 7.5, 45.5), _alps);
    });

    test('rejects an antimeridian crossing rather than guessing', () {
      // west >= east. Null means "ask for the whole trip", which is what the
      // code did before the box existed — so the rare case degrades to the
      // previous behaviour instead of to a wrong box.
      expect(viewportBox(179.0, 45.0, -179.0, 46.0), isNull);
    });

    test('rejects a degenerate or non-finite box', () {
      expect(viewportBox(7.0, 45.0, 7.0, 46.0), isNull);
      expect(viewportBox(7.0, 45.0, 8.0, 45.0), isNull);
      expect(viewportBox(double.nan, 45.0, 8.0, 46.0), isNull);
      expect(viewportBox(double.infinity, 45.0, 8.0, 46.0), isNull);
    });

    test('rejects an out-of-world box', () {
      // flutter_map can report unwrapped longitudes; the server rejects those
      // with a 400, and a 400 on every camera event would be worse than none.
      expect(viewportBox(-181.0, 45.0, 8.0, 46.0), isNull);
      expect(viewportBox(7.0, 45.0, 181.0, 46.0), isNull);
      expect(viewportBox(7.0, -91.0, 8.0, 46.0), isNull);
    });
  });

  group('fetchBoxFor', () {
    test('is a superset of the viewport, never a subset', () {
      // Snapping inward would draw a gap at the edge of the screen.
      final box = fetchBoxFor(_alps, 12);
      expect(box.contains(_alps), isTrue);
    });

    test('neighbouring viewports snap to the same box', () {
      // Raw viewport floats mean a distinct server cache entry per pan pixel.
      // This project has already OOM-killed its API container once with a
      // payload cache bounded by the wrong thing.
      const nudged = GeoBox(7.3001, 45.3001, 7.5001, 45.5001);
      expect(fetchBoxFor(nudged, 12), fetchBoxFor(_alps, 12));
    });

    test('a far pan does get a different box', () {
      expect(fetchBoxFor(const GeoBox(9.3, 45.3, 9.5, 45.5), 12),
          isNot(fetchBoxFor(_alps, 12)));
    });

    test('a small pan stays inside the box that was fetched', () {
      // This is the refetch trigger, so it is the property that decides
      // whether panning costs a request.
      final fetched = fetchBoxFor(_alps, 12);
      const panned = GeoBox(7.31, 45.31, 7.51, 45.51);
      expect(fetched.contains(panned), isTrue);
    });

    test('a pan of more than a screen leaves it', () {
      final fetched = fetchBoxFor(_alps, 12);
      const far = GeoBox(8.3, 45.3, 8.5, 45.5);
      expect(fetched.contains(far), isFalse);
    });

    test('a whole-world viewport degenerates to the world', () {
      // At a zoom that shows the entire trip the box contains it, so nothing
      // is scoped away. This must fall out, not need a special mode.
      final box = fetchBoxFor(const GeoBox(-180, -85, 180, 85), 2);
      expect(box.west, -180.0);
      expect(box.east, 180.0);
      expect(box.contains(const GeoBox(-179, -84, 179, 84)), isTrue);
    });

    test('a viewport thinner than a tile still gets a whole tile', () {
      final box = fetchBoxFor(const GeoBox(7.0, 45.0, 7.000001, 45.000001), 18);
      expect(box.east, greaterThan(box.west));
      expect(box.north, greaterThan(box.south));
    });

    test('a deeper level gives a tighter box', () {
      double area(GeoBox b) => (b.east - b.west) * (b.north - b.south);
      expect(area(fetchBoxFor(_alps, 14)), lessThan(area(fetchBoxFor(_alps, 8))));
    });

    test('every level from 0 to 22 produces a valid superset', () {
      for (var z = 0; z <= 22; z++) {
        final box = fetchBoxFor(_alps, z);
        expect(box.contains(_alps), isTrue, reason: 'level $z');
        expect(box.west, greaterThanOrEqualTo(-180.0));
        expect(box.east, lessThanOrEqualTo(180.0));
      }
    });
  });

  group('the wire format', () {
    test('is minLon,minLat,maxLon,maxLat', () {
      expect(const GeoBox(7.5, 45.25, 8.5, 46.75).param,
          '7.500000,45.250000,8.500000,46.750000');
    });

    test('is stable, so it can key the in-flight-request dedup', () {
      expect(const GeoBox(7.5, 45.25, 8.5, 46.75).param,
          const GeoBox(7.5, 45.25, 8.5, 46.75).param);
    });
  });

  group('the postcondition the refetch loop depends on', () {
    // Latitudes chosen around the failure: inside the Mercator range, exactly
    // at it, and beyond it where no tile-aligned box can reach.
    const lats = <double>[
      0.0, 45.0, -45.0, 60.0, 84.0, 85.0, 85.05112878, 85.06, 88.0, 89.9,
      -84.0, -85.05112878, -89.9,
    ];
    const lons = <double>[-180.0, -179.9, -7.0, 0.0, 7.0, 179.9, 180.0];

    test('a fetched box contains its own viewport, at every zoom', () {
      for (var level = 0; level <= 22; level++) {
        for (var i = 0; i + 1 < lats.length; i++) {
          for (var j = 0; j + 1 < lons.length; j++) {
            final v = viewportBox(lons[j], lats[i], lons[j + 1], lats[i + 1]);
            if (v == null) continue; // degenerate or unrepresentable: no box
            final box = fetchBoxFor(v, level);
            expect(box.contains(v), isTrue,
                reason: 'level $level, viewport $v produced $box — a box that '
                    'cannot contain its viewport makes the camera '
                    'permanently stale');
          }
        }
      }
    });

    test('a viewport thinner than a tile still gets a containing box', () {
      for (var level = 0; level <= 22; level++) {
        final v = viewportBox(7.0, 45.0, 7.0000001, 45.0000001);
        if (v == null) continue;
        expect(fetchBoxFor(v, level).contains(v), isTrue, reason: 'level $level');
      }
    });

    test('a camera past the Mercator limit is clamped, not rejected outright',
        () {
      // 88N is off the tile grid. The viewport must come back usable and
      // inside the range, so the box built from it can contain it.
      final v = viewportBox(-10.0, 80.0, 10.0, 88.0);
      expect(v, isNotNull);
      expect(v!.north, lessThanOrEqualTo(85.05112878));
      expect(fetchBoxFor(v, 6).contains(v), isTrue);
    });

    test('a viewport entirely beyond the limit degrades to no box', () {
      // Both ends clamp to the same latitude, which is not a box. Null means
      // "ask for the whole trip" — the behaviour before scoping existed.
      expect(viewportBox(-10.0, 86.0, 10.0, 89.0), isNull);
    });

    test('the box still snaps outward, so it is never a subset', () {
      final v = viewportBox(7.1, 45.1, 7.2, 45.2)!;
      final box = fetchBoxFor(v, 10);
      expect(box.west, lessThanOrEqualTo(v.west));
      expect(box.south, lessThanOrEqualTo(v.south));
      expect(box.east, greaterThanOrEqualTo(v.east));
      expect(box.north, greaterThanOrEqualTo(v.north));
    });
  });

}
