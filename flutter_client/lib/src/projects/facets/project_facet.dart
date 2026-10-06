/// The facets of a `ProjectNotifier`'s state (issue #294, Decisions 7, 17 and
/// 18 of docs/CLIENT_STATE_MAP_PLAN.md).
///
/// A facet is one slice of a trip's state — geometry, selection, style,
/// items, elevation — that widgets can listen to without being told about
/// every other slice. The notifier owns its five facets and stays the only
/// writer: every write still goes through its methods and their supersession
/// checks; the facets only scope who gets told.
///
/// **Who may write.** A facet's public API is read-only: its getters,
/// [ProjectFacet.version] and the [Listenable] methods. Every write goes
/// through the facet's [ProjectFacetWriter], which only the notifier creates
/// and holds. The writer is the capability: code that cannot name a writer
/// cannot write a facet. The writers are public members of the notifier,
/// because its mixins live in other libraries and Dart has no access level
/// between "library" and "public" — so
/// `test/projects/facets/facet_write_restriction_test.dart` fails the build
/// on any mention of a `…FacetWriter` in `lib/` outside the notifier, its
/// `project_*_mixin.dart` files and this library. That covers the widgets and
/// also the notifier's two subclasses (`ViewProjectNotifier`,
/// `SharedProjectNotifier`): they reach facet state only through protected
/// methods of `ProjectNotifier`.
///
/// **When listeners hear of it.** A write marks its facet changed — the
/// [ProjectFacet.version] goes up — and notifies nobody. The notifier's
/// `notifyListeners()` first notifies each facet changed since the last one,
/// then its own listeners. So a write followed by a check that skips the
/// notify still notifies nobody, and an operation that writes several facets
/// notifies each of them once, together, as it did before facets existed.
library;

import 'package:flutter/foundation.dart';

part 'elevation_facet.dart';
part 'geo_facet.dart';
part 'items_facet.dart';
part 'selection_facet.dart';
part 'style_facet.dart';

/// One slice of a `ProjectNotifier`'s state, read-only to everything but its
/// [ProjectFacetWriter].
abstract class ProjectFacet extends ChangeNotifier {
  int _version = 0;
  bool _changed = false;
  bool _disposed = false;

  /// Goes up on every write, so a widget can key a cache on it: an unchanged
  /// version means unchanged state. A notifier swapped on an account change
  /// brings new facets, whose versions start again from 0.
  int get version => _version;

  void _markChanged() {
    // A write landing after the notifier was discarded changes nothing that
    // anyone can see, and must not notify a listener that is gone.
    if (_disposed) return;
    _version++;
    _changed = true;
  }

  void _flush() {
    if (!_changed || _disposed) return;
    _changed = false;
    notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}

/// The write access to one [ProjectFacet], held by the `ProjectNotifier` that
/// owns the facet. See the library comment for who may use it.
abstract class ProjectFacetWriter<F extends ProjectFacet> {
  ProjectFacetWriter(this.facet);

  /// The facet this writes, which the notifier hands out for reading.
  final F facet;

  /// Records that [facet] changed: its version goes up now and its listeners
  /// are told on the notifier's next `notifyListeners()`.
  void markChanged() => facet._markChanged();

  /// Notifies [facet]'s listeners if it changed since the last flush. Called
  /// by the notifier's `notifyListeners()` only.
  void flush() => facet._flush();

  /// Puts [facet] back to its state before any load, for
  /// `ProjectNotifier.clear()`.
  void reset();

  /// Disposes [facet]. Called by the notifier's `dispose()` only.
  void dispose() => facet.dispose();
}
