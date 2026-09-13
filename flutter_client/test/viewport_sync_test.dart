// Unit tests for viewportSyncPath — the pure lat/lng/zoom → query-string
// builder behind the debounced camera→URL sync (issue #76 follow-up). No
// live MapController needed.

import 'package:flutter/rendering.dart' show Size;
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';
import 'package:traxjourney_client/src/projects/viewport_sync.dart';

MapCamera _camera({double zoom = 4}) => MapCamera(
      crs: const Epsg3857(),
      center: const LatLng(45.0, 7.0),
      zoom: zoom,
      rotation: 0,
      nonRotatedSize: const Size(800, 600),
    );

void main() {
  test('builds the /app path with project + camera query params', () {
    final path = viewportSyncPath(
      basePath: '/app',
      projectName: 'Trip',
      lat: 45.5,
      lng: 6.25,
      zoom: 12.0,
    );
    expect(path, '/app?project=Trip&lat=45.5&lng=6.25&zoom=12.0');
  });

  test('builds the /view path (mirrors app_screen.dart for view_screen.dart)',
      () {
    final path = viewportSyncPath(
      basePath: '/view',
      projectName: 'Trip',
      lat: -10.0,
      lng: 160.0,
      zoom: 8.5,
    );
    expect(path, '/view?project=Trip&lat=-10.0&lng=160.0&zoom=8.5');
  });

  test('URL-encodes the project name', () {
    final path = viewportSyncPath(
      basePath: '/app',
      projectName: 'Alps & Dolomites',
      lat: 46.0,
      lng: 11.0,
      zoom: 9.0,
    );
    expect(
      path,
      '/app?project=Alps%20%26%20Dolomites&lat=46.0&lng=11.0&zoom=9.0',
    );
  });

  group('shouldSyncViewport', () {
    test('skips size changes — they never move the camera', () {
      // The map's first layout emits one of these. Syncing on it would put
      // lat/lng/zoom on the route while the trip is still loading, which
      // MapPanel reads as "user arrived with an explicit viewport" and so
      // never runs the fit-to-bounds animation.
      expect(
        shouldSyncViewport(MapEventNonRotatedSizeChange(
          source: MapEventSource.nonRotatedSizeChange,
          oldCamera: _camera(),
          camera: _camera(),
        )),
        isFalse,
      );
    });

    test('syncs real camera moves', () {
      expect(
        shouldSyncViewport(MapEventMove(
          source: MapEventSource.dragEnd,
          oldCamera: _camera(),
          camera: _camera(zoom: 9),
        )),
        isTrue,
      );
    });

    test('syncs the programmatic fit-to-bounds animation', () {
      expect(
        shouldSyncViewport(MapEventMove(
          source: MapEventSource.mapController,
          oldCamera: _camera(),
          camera: _camera(zoom: 11),
        )),
        isTrue,
      );
    });
  });
}
