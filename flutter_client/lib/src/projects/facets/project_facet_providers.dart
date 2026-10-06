import 'package:flutter/widgets.dart';
import 'package:provider/provider.dart';

import '../project_notifier.dart';
import 'project_facet.dart';

/// Provides the five facets of the nearest [T] to [child] (issue #294,
/// Decision 18 of docs/CLIENT_STATE_MAP_PLAN.md).
///
/// [T] names the provider to follow, because a screen with its own notifier
/// provides it under its own type (`ViewProjectNotifier`,
/// `SharedProjectNotifier`) below the app-wide [ProjectNotifier]: inside such
/// a screen, `context.watch<GeoFacet>()` must reach the screen's facets, not
/// the app's.
///
/// The facets belong to the notifier, which creates and disposes them; this
/// only hands them out, and never disposes them. When the provider above
/// swaps its notifier — the app-wide one does on every account change — this
/// rebuilds and provides the new notifier's facets. It does not rebuild when
/// that notifier merely notifies.
class ProjectFacetProviders<T extends ProjectNotifier> extends StatelessWidget {
  const ProjectFacetProviders({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    final notifier = context.select<T, T>((n) => n);
    return MultiProvider(
      providers: [
        ChangeNotifierProvider<GeoFacet>.value(value: notifier.geoFacet),
        ChangeNotifierProvider<SelectionFacet>.value(
            value: notifier.selectionFacet),
        ChangeNotifierProvider<StyleFacet>.value(value: notifier.styleFacet),
        ChangeNotifierProvider<ItemsFacet>.value(value: notifier.itemsFacet),
        ChangeNotifierProvider<ElevationFacet>.value(
            value: notifier.elevationFacet),
      ],
      child: child,
    );
  }
}
