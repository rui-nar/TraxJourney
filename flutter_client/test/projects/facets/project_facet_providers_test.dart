// ProjectFacetProviders hands out the facets of the right notifier (issue
// #294, Decision 18 of docs/CLIENT_STATE_MAP_PLAN.md): the app-wide one's,
// following it across account changes, and inside a ViewScreen the view
// notifier's.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:traxjourney_client/main.dart' show accountScopedProjectNotifier;
import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/auth/auth_notifier.dart';
import 'package:traxjourney_client/src/auth/auth_service.dart';
import 'package:traxjourney_client/src/projects/facets/project_facet.dart';
import 'package:traxjourney_client/src/projects/facets/project_facet_providers.dart';
import 'package:traxjourney_client/src/projects/project_data_cache.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/view_screen.dart';

void _signIn(AuthNotifier auth, String id) => auth.updateUser({
      'id': id,
      'email': '$id@x.com',
      'display_name': id,
      'auth_provider': 'local',
    });

/// The five facets [context] gets from the providers.
List<ProjectFacet> _provided(BuildContext context) => [
      context.read<GeoFacet>(),
      context.read<SelectionFacet>(),
      context.read<StyleFacet>(),
      context.read<ItemsFacet>(),
      context.read<ElevationFacet>(),
    ];

List<ProjectFacet> _owned(ProjectNotifier n) => [
      n.geoFacet,
      n.selectionFacet,
      n.styleFacet,
      n.itemsFacet,
      n.elevationFacet,
    ];

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    projectDataCache.resetForTest();
    api = ApiClient();
  });

  testWidgets(
      "an account change provides the new notifier's facets and disposes the "
      "old ones", (tester) async {
    final auth = AuthNotifier(AuthService());
    late BuildContext context;
    var builds = 0;
    // As main.dart composes them.
    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<AuthNotifier>.value(value: auth),
        accountScopedProjectNotifier(() => ProjectNotifier(ProjectService())),
      ],
      child: ProjectFacetProviders<ProjectNotifier>(
        child: Builder(builder: (c) {
          context = c;
          // Rebuilds only when the provided geo facet is replaced.
          c.watch<GeoFacet>();
          builds++;
          return const SizedBox();
        }),
      ),
    ));
    _signIn(auth, 'user-1');
    await tester.pump();

    final a = context.read<ProjectNotifier>();
    final aFacets = _provided(context);
    expect(aFacets, orderedEquals(_owned(a)));

    // The notifier notifying is not a facet change: nothing that listens to
    // a facet alone rebuilds.
    final before = builds;
    a.notifyListeners();
    await tester.pump();
    expect(builds, before);

    _signIn(auth, 'user-2');
    await tester.pump();

    final b = context.read<ProjectNotifier>();
    expect(b, isNot(same(a)));
    expect(_provided(context), orderedEquals(_owned(b)));
    for (final f in aFacets) {
      expect(_provided(context), isNot(contains(f)));
      expect(() => ChangeNotifier.debugAssertNotDisposed(f), throwsFlutterError,
          reason: "the old notifier's dispose() disposes its facets");
    }
    for (final f in _owned(b)) {
      expect(ChangeNotifier.debugAssertNotDisposed(f), isTrue);
    }
    expect(builds, before + 1, reason: 'a dependent rebuilds onto the new facet');

    await tester.pumpWidget(const SizedBox());
  });

  testWidgets(
      "inside a ViewScreen the providers resolve to the view notifier's "
      "facets, not the app-wide notifier's", (tester) async {
    // ViewScreen loads its project on init; every request fails in the test
    // sandbox, and one of the pair load() fires is never awaited — its
    // rejection is unrelated to what is under test here (see
    // view_screen_test.dart).
    final previousReporter = reportTestException;
    reportTestException = (details, description) {
      if (details.exception is! ApiException) {
        previousReporter(details, description);
      }
    };
    addTearDown(() => reportTestException = previousReporter);

    final auth = AuthNotifier(AuthService());
    _signIn(auth, 'user-1');
    final app = ProjectNotifier(ProjectService());
    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<AuthNotifier>.value(value: auth),
        ChangeNotifierProvider<ProjectNotifier>.value(value: app),
      ],
      child: const ProjectFacetProviders<ProjectNotifier>(
        child: MaterialApp(home: ViewScreen(projectName: 'Trip')),
      ),
    ));
    await tester.pump();
    reportTestException = previousReporter;

    final inside = tester.element(find.byTooltip('Encounters'));
    final view = inside.read<ViewProjectNotifier>();
    expect(view, isNot(same(app)));
    expect(_provided(inside), orderedEquals(_owned(view)));
    for (final f in _owned(app)) {
      expect(_provided(inside), isNot(contains(f)));
    }

    // Flush every retry/one-shot timer the failed load schedules (see
    // view_screen_test.dart), then unmount: the screen's notifier and its
    // facets go with it.
    await tester.pump(const Duration(seconds: 20));
    final viewFacets = _owned(view);
    await tester.pumpWidget(const SizedBox());
    for (final f in viewFacets) {
      expect(() => ChangeNotifier.debugAssertNotDisposed(f), throwsFlutterError);
    }
    app.dispose();
  });
}
