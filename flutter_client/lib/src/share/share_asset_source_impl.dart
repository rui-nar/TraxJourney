/// Production [ShareAssetSource] — renders the trip map via the offscreen
/// exporter and fetches memory photo bytes over authenticated HTTP.
library;

import 'dart:typed_data';

import 'package:flutter/widgets.dart';
import 'package:flutter_map/flutter_map.dart';

import '../api/client.dart';
import '../projects/image_export.dart';
import '../projects/project_notifier.dart';
import 'share_day_bounds.dart';
import 'share_interfaces.dart';

/// The authenticated route for the copy of one memory photo with location
/// and device EXIF removed — what a share link would get, fetched as the
/// signed-in user so that sharing never has to create a share link
/// (issue #430).
String shareablePhotoPath({required int memoryId, required String uuid}) =>
    '/api/memories/$memoryId/photos/$uuid/shareable';

class ShareAssetSourceImpl implements ShareAssetSource {
  final ProjectNotifier notifier;

  /// Provides a live BuildContext at render time (the share dialog's context),
  /// required by the offscreen exporter for the Overlay + MediaQuery.
  final BuildContext Function() contextProvider;

  const ShareAssetSourceImpl(this.notifier, this.contextProvider);

  @override
  Future<Uint8List?> renderMapImage(
      {required bool dayFocus, String? date}) async {
    // Fetched once here and handed to the exporter, rather than read off the
    // notifier (whose geometry is simplified to the map's zoom, issue #317):
    // the day's bounds and the image it frames have to come from the same
    // geometry, and this is also the one fetch instead of two.
    final geo = await notifier.fullResGeoForExport();
    LatLngBounds? bounds;
    if (dayFocus && date != null) {
      final points = dayRoutePoints(
        geo: geo,
        items: notifier.itemsFacet.items,
        activities: notifier.itemsFacet.activities,
        date: date,
      );
      if (points.isNotEmpty) bounds = LatLngBounds.fromPoints(points);
    }
    return performOffscreenExport(
      context: contextProvider(),
      notifier: notifier,
      projectName: notifier.projectName ?? 'trip',
      opts: const ImageExportOptions(includeChart: false, includeTitle: false),
      boundsOverride: bounds,
      geoOverride: geo,
    );
  }

  /// The bytes handed to the OS share sheet leave the app for good, so they
  /// are the stripped copies, never the originals with their GPS position
  /// (issue #430). Through the app's own authenticated client: no share
  /// link is created, and no connection is left behind.
  @override
  Future<List<Uint8List>> fetchPhotos(int memoryId, List<String> uuids) async {
    final out = <Uint8List>[];
    for (final uuid in uuids) {
      try {
        final res =
            await api.getRaw(shareablePhotoPath(memoryId: memoryId, uuid: uuid));
        out.add(res.bodyBytes);
      } catch (_) {
        // Skip a photo that fails to download rather than aborting the share.
      }
    }
    return out;
  }
}
