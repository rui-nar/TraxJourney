/// Production [ShareAssetSource] — renders the trip map via the offscreen
/// exporter and fetches memory photo bytes over HTTP.
library;

import 'dart:typed_data';

import 'package:flutter/widgets.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:http/http.dart' as http;

import '../projects/image_export.dart';
import '../projects/project_notifier.dart';
import 'share_day_bounds.dart';
import 'share_interfaces.dart';

/// The share-link route for one memory photo: the copy with location and
/// device EXIF removed, never the owner's original (issue #430).
String sharePhotoUrl({
  required String base,
  required String token,
  required int memoryId,
  required String uuid,
}) =>
    '$base/api/share/$token/photos/$memoryId/$uuid';

class ShareAssetSourceImpl implements ShareAssetSource {
  final ProjectNotifier notifier;

  /// Provides a live BuildContext at render time (the share dialog's context),
  /// required by the offscreen exporter for the Overlay + MediaQuery.
  final BuildContext Function() contextProvider;

  /// Injectable for tests; production uses a plain client.
  final http.Client? client;

  const ShareAssetSourceImpl(this.notifier, this.contextProvider,
      {this.client});

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
        items: notifier.items,
        activities: notifier.activities,
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
  /// are fetched through the share link — the copy with location and device
  /// EXIF removed — and not through the authenticated owner route, which
  /// serves the original with its GPS position (issue #430). Sharing a
  /// memory publishes the memory-bearing link anyway, so the token is
  /// created here when it does not exist yet, exactly as the link resolver
  /// does; with no token there are no photos, never the originals instead.
  @override
  Future<List<Uint8List>> fetchPhotos(int memoryId, List<String> uuids) async {
    if (uuids.isEmpty) return const [];
    if (notifier.shareToken == null) {
      try {
        await notifier.createShareToken();
      } catch (_) {
        return const [];
      }
    }
    final token = notifier.shareToken;
    if (token == null) return const [];

    // Same origin pattern as the link resolver: empty baseUrl → web origin.
    final base =
        notifier.apiBaseUrl.isEmpty ? Uri.base.origin : notifier.apiBaseUrl;
    final http.Client httpClient = client ?? http.Client();
    final out = <Uint8List>[];
    for (final uuid in uuids) {
      final url = sharePhotoUrl(
          base: base, token: token, memoryId: memoryId, uuid: uuid);
      try {
        final res = await httpClient.get(Uri.parse(url));
        if (res.statusCode >= 200 && res.statusCode < 300) {
          out.add(res.bodyBytes);
        }
      } catch (_) {
        // Skip a photo that fails to download rather than aborting the share.
      }
    }
    return out;
  }
}
