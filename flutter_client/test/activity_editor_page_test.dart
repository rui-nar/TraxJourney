import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/api/client.dart';
import 'package:traxjourney_client/src/core/project_ref.dart';
import 'package:traxjourney_client/src/map/geo_point.dart';
import 'package:traxjourney_client/src/projects/activity_editor_page.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';
import 'package:traxjourney_client/src/projects/track_editor_controller.dart';
import 'package:traxjourney_client/src/projects/track_map_editor.dart';

/// Minimal Google-polyline encoder for test fixtures.
String _encode(List<GeoPoint> pts) {
  final sb = StringBuffer();
  int lastLat = 0, lastLng = 0;
  for (final p in pts) {
    final lat = (p.lat * 1e5).round();
    final lng = (p.lon * 1e5).round();
    _delta(sb, lat - lastLat);
    _delta(sb, lng - lastLng);
    lastLat = lat;
    lastLng = lng;
  }
  return sb.toString();
}

void _delta(StringBuffer sb, int v) {
  int zig = v < 0 ? ~(v << 1) : (v << 1);
  while (zig >= 0x20) {
    sb.writeCharCode((0x20 | (zig & 0x1f)) + 63);
    zig >>= 5;
  }
  sb.writeCharCode(zig + 63);
}

Map<String, dynamic> _activity({
  bool edited = false,
  int id = 111,
  int? splitParentId,
}) {
  final track = <GeoPoint>[
    (lat: 48.0, lon: 2.00),
    (lat: 48.0, lon: 2.01),
    (lat: 48.0, lon: 2.02),
    (lat: 48.0, lon: 2.03),
  ];
  return {
    'id': id,
    'name': 'Test Ride',
    'is_edited': edited,
    'split_parent_id': splitParentId,
    'map': {'summary_polyline': _encode(track)},
    'elevation_profile': [
      [0.0, 100.0],
      [1.0, 110.0],
      [2.0, 120.0],
      [3.0, 130.0],
    ],
  };
}

/// A longer fixture (6 points, lon 2.00..2.05) for the split/cut tests: after a
/// deletion the track must still leave a valid "Cut & add transport" boundary,
/// which needs index <= length - 3.
Map<String, dynamic> _longActivity() {
  final track = <GeoPoint>[
    for (var i = 0; i < 6; i++) (lat: 48.0, lon: 2.0 + i * 0.01),
  ];
  return {
    'id': 111,
    'name': 'Test Ride',
    'is_edited': false,
    'map': {'summary_polyline': _encode(track)},
    'elevation_profile': [for (var i = 0; i < 6; i++) [i.toDouble(), 100.0 + i * 10]],
  };
}

/// Captures the split/reset calls the page makes instead of reaching the network.
class _RecordingNotifier extends ProjectNotifier {
  _RecordingNotifier() : super(ProjectService()) {
    ref = const ProjectRef(name: 'Trip');
  }

  final List<({int index, bool dropBoundary, Map<String, dynamic>? payload})>
      splits = [];
  final List<int> resets = [];

  @override
  Future<void> splitActivity(
    int activityId,
    int splitIndex, {
    bool dropBoundary = false,
    Map<String, dynamic>? payload,
    int? lockVersion,
  }) async {
    splits.add(
        (index: splitIndex, dropBoundary: dropBoundary, payload: payload));
  }

  @override
  Future<void> resetActivityTrack(int activityId) async {
    resets.add(activityId);
  }
}

/// A notifier whose save hangs until the test lets it land, so a second action
/// can be attempted while the first write is still in flight.
class _SlowSaveNotifier extends _RecordingNotifier {
  final Completer<void> saveDone = Completer<void>();
  int saves = 0;

  @override
  Future<void> saveActivityTrack(int activityId, Map<String, dynamic> payload,
      {int? lockVersion}) {
    saves++;
    return saveDone.future;
  }
}

/// A notifier whose saveActivityTrack/splitActivity always fail with the 409
/// the server returns on a stale lock_version (issue #31) — for exercising
/// ActivityEditorPage's conflict handling without a real server round trip.
class _StaleVersionNotifier extends ProjectNotifier {
  _StaleVersionNotifier() : super(ProjectService()) {
    ref = const ProjectRef(name: 'Trip');
  }

  @override
  Future<void> saveActivityTrack(
    int activityId, Map<String, dynamic> payload, {int? lockVersion}) async {
    throw ApiException(409, '{"detail":"stale"}');
  }

  @override
  Future<void> splitActivity(
    int activityId,
    int splitIndex, {
    bool dropBoundary = false,
    Map<String, dynamic>? payload,
    int? lockVersion,
  }) async {
    throw ApiException(409, '{"detail":"stale"}');
  }
}

/// A notifier holding a flat split family: root 111 with [pieces] children.
_RecordingNotifier _familyNotifier(int pieces) => _RecordingNotifier()
  ..activities = [
    {'id': 111, 'split_parent_id': null},
    for (var i = 1; i <= pieces; i++) {'id': -i, 'split_parent_id': 111},
  ];

/// A notifier holding a CHAIN: 111 → -1 → -2, each cut out of the one before.
/// The flat fixture cannot tell a transitive walk from a single-level one.
_RecordingNotifier _chainNotifier() => _RecordingNotifier()
  ..activities = [
    {'id': 111, 'split_parent_id': null},
    {'id': -1, 'split_parent_id': 111},
    {'id': -2, 'split_parent_id': -1},
  ];

/// Push the editor onto a route (as ActivityPanel does) so the page's pop-on-
/// success has a route to return to.
Future<void> _pumpPushed(
  WidgetTester tester,
  Map<String, dynamic> activity,
  ProjectNotifier notifier, {
  Size size = const Size(1200, 1000),
  void Function(Object? result)? onPopped,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (ctx) => TextButton(
          onPressed: () => Navigator.of(ctx)
              .push(MaterialPageRoute(
                builder: (_) =>
                    ActivityEditorPage(notifier: notifier, activity: activity),
              ))
              .then((result) => onPopped?.call(result)),
          child: const Text('open editor'),
        ),
      ),
    ),
  ));
  await tester.tap(find.text('open editor'));
  await tester.pumpAndSettle();
}

/// Long-press the vertex handle at [index] and choose [label] from its menu.
Future<void> _pointMenu(WidgetTester tester, int index, String label) async {
  await tester.longPress(find.byKey(ValueKey('vertex_$index')));
  await tester.pumpAndSettle();
  await tester.tap(find.text(label));
  await tester.pumpAndSettle();
}

Future<void> _pump(WidgetTester tester, Map<String, dynamic> activity,
    {Size size = const Size(1200, 1000)}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  final notifier = ProjectNotifier(ProjectService())..ref = const ProjectRef(name: 'Trip');
  await tester.pumpWidget(MaterialApp(
    home: ActivityEditorPage(notifier: notifier, activity: activity),
  ));
  await tester.pump();
}

TrackEditorController _controllerOf(WidgetTester tester) {
  final state = tester.state<State>(find.byType(ActivityEditorPage));
  // ignore: invalid_use_of_protected_member
  return (state as dynamic).editorControllerForTest as TrackEditorController;
}

void main() {
  group('modelForActivity', () {
    test('parses polyline and elevation into aligned points', () {
      final m = modelForActivity(_activity());
      expect(m.length, 4);
      expect(m.points.first.lat, closeTo(48.0, 1e-4));
      expect(m.points.first.elev, isNotNull);
    });

    test('handles a missing elevation profile', () {
      final a = _activity()..remove('elevation_profile');
      final m = modelForActivity(a);
      expect(m.length, 4);
      expect(m.points.first.elev, isNull);
    });
  });

  testWidgets('renders the Add-points toggle, hint and a disabled Save',
      (tester) async {
    await _pump(tester, _activity());
    expect(find.text('Add points'), findsOneWidget);
    expect(find.textContaining('Long-press'), findsOneWidget);

    final save = tester.widget<TextButton>(
      find.ancestor(of: find.text('Save'), matching: find.byType(TextButton)),
    );
    expect(save.onPressed, isNull); // nothing edited yet
  });

  testWidgets('toggling Add points flips the hint text', (tester) async {
    await _pump(tester, _activity());
    expect(find.textContaining('Long-press'), findsOneWidget);
    await tester.tap(find.text('Add points'));
    await tester.pump();
    expect(find.textContaining('Tap the map to insert'), findsOneWidget);
  });

  // ── Provenance follows the activity into the editor (issue #260, unit 6) ──
  //
  // The list drew a source badge, the editor said nothing: open a track and you
  // could no longer tell whether its shape came from a Strava sync (where Reset
  // fetches the original back) or from a file you imported (where nothing
  // remote exists to fetch).
  testWidgets('an imported track says so in the editor, out loud',
      (tester) async {
    final semantics = tester.ensureSemantics();

    await _pump(tester, _activity()..['source'] = 'gpx');

    expect(find.byKey(const ValueKey('gpx_editor_badge')), findsOneWidget);
    expect(find.bySemanticsLabel(RegExp('Imported from a GPX file')),
        findsAtLeastNWidgets(1));
    expect(find.byTooltip('Imported from a GPX file'), findsOneWidget);

    semantics.dispose();
  });

  testWidgets('the badge never takes the name down to nothing', (tester) async {
    // A Row cannot hand space back, so wherever the badge shows the name must
    // still have room, and the Row must never need more than it is given.
    // Before the guard, with the labelled Reset button in a phone bar, 470 px
    // gave the badge 24 of the title's 43 px and left the name 19; 440 px
    // overflowed into the actions outright (test font). Stated as the invariant
    // across a sweep rather than at one width: a few pixels of action-row drift
    // moves the flip, not the contract.
    //
    // Since Reset moved into the overflow menu (#407), a phone at default text
    // size always leaves the title more than 48 px, so only scaled-up text
    // reaches the guard. The sweep runs both, and has to be seen to cross the
    // guard: a sweep that only ever shows the badge cannot notice the guard
    // going missing.
    var shown = 0, stoodDown = 0;
    for (final scale in [1.0, 1.3]) {
      tester.platformDispatcher.textScaleFactorTestValue = scale;
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      for (final width in [
        320.0, 340.0, 360.0, 400.0, 420.0, 440.0, 470.0, 500.0, 560.0,
      ]) {
        await tester.pumpWidget(const SizedBox());
        await _pumpPushed(
            tester,
            _activity(edited: true)..['source'] = 'gpx',
            _RecordingNotifier(),
            size: Size(width, 900));

        expect(tester.takeException(), isNull,
            reason: 'the AppBar overflowed at $width px, text x$scale');
        if (find.byKey(const ValueKey('gpx_editor_badge')).evaluate().isEmpty) {
          stoodDown++;
          continue;
        }
        shown++;
        // The guard's own contract: it shows the badge from 48 px of title
        // space, which leaves the name maxWidth - 24. Asserting `>` rather
        // than `>=` would call the boundary a violation.
        expect(tester.getSize(find.textContaining('Test Ride')).width,
            greaterThanOrEqualTo(24.0),
            reason: 'the badge left the name less than its own width '
                'at $width px, text x$scale');
      }
    }
    expect(stoodDown, greaterThan(0), reason: 'the sweep never hit the guard');
    expect(shown, greaterThan(0), reason: 'the sweep never showed the badge');
  });

  testWidgets('a synced track carries no badge in the editor', (tester) async {
    await _pump(tester, _activity());

    expect(find.byKey(const ValueKey('gpx_editor_badge')), findsNothing);
  });

  testWidgets('Reset to Strava only shows for an edited activity',
      (tester) async {
    await _pump(tester, _activity(edited: false));
    expect(find.text('Reset to Strava'), findsNothing);

    await _pump(tester, _activity(edited: true));
    expect(find.text('Reset to Strava'), findsOneWidget);
  });

  testWidgets('a local activity gets the non-Strava reset label',
      (tester) async {
    // Split tails and added transport carry a synthetic negative id and never
    // came from Strava, so reset only undoes their own edits (issue #131).
    await _pump(tester, _activity(edited: true, id: -1));
    expect(find.text('Reset to Strava'), findsNothing);
    expect(find.text('Reset track'), findsOneWidget);
  });

  // ── Reset on a split root warns first (issue #141) ────────────────────────
  //
  // Resetting the root restores the whole pre-split track, so the server undoes
  // the split rather than leave the pieces duplicating it. That destroys those
  // pieces and any edits on them, so the editor must say so before calling.

  testWidgets('resetting a split root warns that the pieces will be removed',
      (tester) async {
    final notifier = _familyNotifier(1);
    await _pumpPushed(tester, _activity(edited: true), notifier);

    await tester.tap(find.text('Reset to Strava'));
    await tester.pumpAndSettle();

    expect(find.textContaining('before it was split'), findsOneWidget);
    expect(find.textContaining('The piece cut out of it'), findsOneWidget);
    expect(find.textContaining('cannot be undone'), findsOneWidget);
    expect(notifier.resets, isEmpty); // nothing sent until confirmed
  });

  testWidgets('cancelling the warning leaves the split alone', (tester) async {
    final notifier = _familyNotifier(2);
    await _pumpPushed(tester, _activity(edited: true), notifier);

    await tester.tap(find.text('Reset to Strava'));
    await tester.pumpAndSettle();
    expect(find.textContaining('The 2 pieces cut out of it'), findsOneWidget);

    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
    expect(notifier.resets, isEmpty);
  });

  testWidgets('confirming the warning sends the reset', (tester) async {
    final notifier = _familyNotifier(1);
    await _pumpPushed(tester, _activity(edited: true), notifier);

    await tester.tap(find.text('Reset to Strava'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Reset'));
    await tester.pumpAndSettle();

    expect(notifier.resets, [111]);
  });

  testWidgets('resetting a piece with nothing under it does not warn',
      (tester) async {
    // A leaf piece takes nothing with it — reset just undoes its edits (#131).
    final notifier = _familyNotifier(1);
    await _pumpPushed(
        tester, _activity(edited: true, id: -1, splitParentId: 111), notifier);

    await tester.tap(find.text('Reset track'));
    await tester.pumpAndSettle();

    expect(find.byType(AlertDialog), findsNothing);
    expect(notifier.resets, [-1]);
  });

  testWidgets('resetting a piece that was split again warns about its child',
      (tester) async {
    // Issue #143: -1 grows back over -2, so -2 goes with it — and only -2. The
    // root above the reset is not below it and must not be counted.
    final notifier = _chainNotifier();
    await _pumpPushed(
        tester, _activity(edited: true, id: -1, splitParentId: 111), notifier);

    await tester.tap(find.text('Reset track'));
    await tester.pumpAndSettle();

    expect(find.textContaining('The piece cut out of it'), findsOneWidget);
    expect(notifier.resets, isEmpty);
  });

  testWidgets('resetting the root of a chain counts the grandchild too',
      (tester) async {
    // The walk is transitive: 111 → -1 → -2 is two pieces, not one.
    final notifier = _chainNotifier();
    await _pumpPushed(tester, _activity(edited: true), notifier);

    await tester.tap(find.text('Reset to Strava'));
    await tester.pumpAndSettle();

    expect(find.textContaining('The 2 pieces cut out of it'), findsOneWidget);
  });

  testWidgets('resetting an activity that was never split does not warn',
      (tester) async {
    final notifier = _familyNotifier(0);
    await _pumpPushed(tester, _activity(edited: true), notifier);

    await tester.tap(find.text('Reset to Strava'));
    await tester.pumpAndSettle();

    expect(find.byType(AlertDialog), findsNothing);
    expect(notifier.resets, [111]);
  });

  // ── The name keeps its place in the AppBar on a phone (issue #407) ────────
  //
  // On an edited activity the labelled Reset button left the title 0 px at 320
  // and 18 px at 360 in Roboto, most of that spent on "Edit — ": open a track
  // you had edited and the AppBar did not say which. These tests run in the
  // test font, whose glyphs are all 1 em wide, which is harsher: there the title
  // got 0 px from 320 to 480 and the actions row overflowed by 75 px at 320 and
  // 35 px at 360. Measured, like the issue, on a pushed route (back button).

  const longName = 'Long afternoon ride around the lake and back';
  // Every phone and small-tablet width below the 720 px breakpoint, stated as an
  // invariant across the band rather than at one width next to the flip.
  const phoneWidths = [
    320.0, 340.0, 360.0, 375.0, 390.0, 412.0, 430.0, 480.0, 540.0, 600.0,
    680.0, 719.0,
  ];

  testWidgets('Reset leaves the title room on every phone width',
      (tester) async {
    // Both labels: "Reset to Strava" is the longer, "Reset track" the local one.
    for (final id in [111, -1]) {
      for (final width in phoneWidths) {
        await tester.pumpWidget(const SizedBox()); // a fresh navigator each time
        await _pumpPushed(
            tester,
            _activity(edited: true, id: id)..['name'] = longName,
            _RecordingNotifier(),
            size: Size(width, 900));

        expect(tester.takeException(), isNull,
            reason: 'the AppBar overflowed at $width px (id $id)');
        final title = find.textContaining(longName);
        final glyph = tester.widget<Text>(title).style!.fontSize!;
        // Two glyphs is a floor, not a target: past it the text is more than
        // an ellipsis, and the old layout gave it nothing at all.
        expect(tester.getSize(title).width, greaterThanOrEqualTo(2 * glyph),
            reason: 'the title was squeezed out at $width px (id $id)');
      }
    }
  });

  testWidgets('on a phone the title spends its room on the name',
      (tester) async {
    // Room for the title is not room for the name: at 320 px, once Reset moved
    // out, the title got 80 px in Roboto and "Edit — " took 49 of them (in the
    // test font: 55 px, all of it). So the name's share is the title's width
    // less whatever text sits in front of the name, measured at its natural
    // width in the same style: chrome counts against the title, not for it.
    final semantics = tester.ensureSemantics();

    for (final width in phoneWidths) {
      await tester.pumpWidget(const SizedBox());
      await _pumpPushed(tester, _activity(edited: true)..['name'] = longName,
          _RecordingNotifier(),
          size: Size(width, 900));

      final title = find.textContaining(longName);
      final para = tester.renderObject<RenderParagraph>(
          find.descendant(of: title, matching: find.byType(RichText)));
      final text = para.text.toPlainText();
      final before = TextPainter(
        text: TextSpan(
            text: text.substring(0, text.indexOf(longName)),
            style: para.text.style),
        textDirection: TextDirection.ltr,
        textScaler: para.textScaler,
      )..layout();
      final nameRoom = para.size.width - before.width;
      before.dispose();
      final glyph = tester.widget<Text>(title).style!.fontSize!;
      expect(nameRoom, greaterThanOrEqualTo(2 * glyph),
          reason: 'the name itself got no room at $width px');
      // What the eye loses, the ear keeps.
      expect(find.bySemanticsLabel('Edit — $longName'), findsOneWidget,
          reason: 'at $width px');
    }

    semantics.dispose();
  });

  testWidgets('from 720 px the title still reads "Edit — name"',
      (tester) async {
    for (final width in [720.0, 1200.0]) {
      await tester.pumpWidget(const SizedBox());
      await _pumpPushed(tester, _activity(edited: true), _RecordingNotifier(),
          size: Size(width, 900));
      expect(find.text('Edit — Test Ride'), findsOneWidget,
          reason: 'at $width px');
    }
  });

  testWidgets('on a phone Reset sits in the overflow menu, labelled',
      (tester) async {
    final semantics = tester.ensureSemantics();

    for (final (id, label) in [(111, 'Reset to Strava'), (-1, 'Reset track')]) {
      await tester.pumpWidget(const SizedBox());
      await _pumpPushed(tester, _activity(edited: true, id: id),
          _RecordingNotifier(),
          size: const Size(360, 800));

      // Not in the bar any more, but the menu that holds it is named for a
      // screen reader and a long-press, like every other AppBar icon: a button
      // whose tooltip is what TalkBack and VoiceOver read out.
      expect(find.text(label), findsNothing);
      expect(find.byTooltip('More options'), findsOneWidget);
      expect(
          tester.getSemantics(find.byTooltip('More options')),
          isSemantics(
              tooltip: 'More options', isButton: true, hasTapAction: true));

      await tester.tap(find.byTooltip('More options'));
      await tester.pumpAndSettle();
      expect(find.text(label), findsOneWidget);
      expect(find.bySemanticsLabel(RegExp(label)), findsAtLeastNWidgets(1));
    }

    semantics.dispose();
  });

  testWidgets('on a phone an unedited activity has no overflow menu',
      (tester) async {
    // The menu exists only to hold Reset; an empty one would be a dead end.
    await _pumpPushed(tester, _activity(), _RecordingNotifier(),
        size: const Size(360, 800));
    expect(find.byTooltip('More options'), findsNothing);
  });

  testWidgets('on a phone, resetting a split root from the menu still warns',
      (tester) async {
    final notifier = _familyNotifier(1);
    await _pumpPushed(tester, _activity(edited: true), notifier,
        size: const Size(360, 800));

    Future<void> chooseReset() async {
      await tester.tap(find.byTooltip('More options'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Reset to Strava'));
      await tester.pumpAndSettle();
    }

    await chooseReset();
    expect(find.textContaining('The piece cut out of it'), findsOneWidget);
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
    expect(notifier.resets, isEmpty);

    await chooseReset();
    await tester.tap(find.widgetWithText(FilledButton, 'Reset'));
    await tester.pumpAndSettle();
    expect(notifier.resets, [111]);
  });

  testWidgets('on a phone, resetting an unsplit activity from the menu does '
      'not warn', (tester) async {
    final notifier = _familyNotifier(0);
    await _pumpPushed(tester, _activity(edited: true), notifier,
        size: const Size(360, 800));

    await tester.tap(find.byTooltip('More options'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Reset to Strava'));
    await tester.pumpAndSettle();

    expect(find.byType(AlertDialog), findsNothing);
    expect(notifier.resets, [111]);
  });

  testWidgets('from 720 px Reset stays a labelled button in the AppBar',
      (tester) async {
    for (final width in [720.0, 1200.0]) {
      await tester.pumpWidget(const SizedBox());
      await _pumpPushed(tester, _activity(edited: true), _RecordingNotifier(),
          size: Size(width, 900));

      expect(find.widgetWithText(TextButton, 'Reset to Strava'), findsOneWidget,
          reason: 'at $width px');
      expect(find.byTooltip('More options'), findsNothing,
          reason: 'at $width px');
    }
  });

  // ── A write in flight owns the editor (review of #407) ────────────────────
  //
  // Save disables ⋮ only on the next rebuild, so a tap on Save and one on ⋮ in
  // the same frame both land. Nothing may then start a second write, and when
  // the save lands it has to close the editor, not whatever sits on top of it.
  // Save's spinner never settles, so these pump fixed frames, not to idle.

  Future<void> settleFrames(WidgetTester tester) async {
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 500));
  }

  /// Make an edit so Save is live, then tap Save and ⋮ with no frame between.
  Future<void> saveThenOpenMenu(WidgetTester tester) async {
    _controllerOf(tester).removeSelected(0);
    await tester.pump();
    await tester.tap(find.text('Save'));
    await tester.tap(find.byTooltip('More options'));
    await settleFrames(tester);
  }

  testWidgets('Reset chosen while a save is in flight sends nothing',
      (tester) async {
    final notifier = _SlowSaveNotifier();
    await _pumpPushed(tester, _activity(edited: true), notifier,
        size: const Size(360, 800));
    await saveThenOpenMenu(tester);
    expect(notifier.saves, 1);

    await tester.tap(find.text('Reset to Strava'));
    await settleFrames(tester);
    expect(notifier.resets, isEmpty,
        reason: 'a reset went out beside the save already in flight');

    notifier.saveDone.complete();
    await tester.pumpAndSettle();
    expect(notifier.resets, isEmpty);
  });

  testWidgets('Reset tapped in the same frame as Save sends nothing',
      (tester) async {
    // The wide bar's labelled button: no menu in between to re-check, so this
    // is _reset()'s own guard on its own.
    final notifier = _SlowSaveNotifier();
    await _pumpPushed(tester, _activity(edited: true), notifier);
    _controllerOf(tester).removeSelected(0);
    await tester.pump();
    await tester.tap(find.text('Save'));
    await tester.tap(find.text('Reset to Strava'));
    await settleFrames(tester);
    expect(notifier.saves, 1);
    expect(notifier.resets, isEmpty);

    notifier.saveDone.complete();
    await tester.pumpAndSettle();
    expect(notifier.resets, isEmpty);
  });

  testWidgets('a save that lands under the open menu closes the editor',
      (tester) async {
    final notifier = _SlowSaveNotifier();
    Object? popped = 'still open';
    await _pumpPushed(tester, _activity(edited: true), notifier,
        size: const Size(360, 800), onPopped: (r) => popped = r);
    await saveThenOpenMenu(tester);
    expect(find.text('Reset to Strava'), findsOneWidget); // the menu is up

    notifier.saveDone.complete();
    await tester.pumpAndSettle();

    // Popping the top route would close the menu instead, and handing it the
    // editor's bool result throws — which the save's catch reported as a
    // failed save, over a save that had succeeded.
    expect(tester.takeException(), isNull);
    expect(find.textContaining('Save failed'), findsNothing);
    expect(find.byType(ActivityEditorPage), findsNothing);
    expect(popped, isTrue);
  });

  testWidgets('a save that lands as the editor is leaving closes nothing else',
      (tester) async {
    // Back pressed while Save spins: the editor is on its way out but still
    // mounted when the save lands. Closing "the editor" then must not pop the
    // screen underneath in its place.
    final notifier = _SlowSaveNotifier();
    await _pumpPushed(tester, _activity(edited: true), notifier);
    _controllerOf(tester).removeSelected(0);
    await tester.pump();
    await tester.tap(find.text('Save'));
    await tester.pump();

    tester.state<NavigatorState>(find.byType(Navigator)).pop();
    await tester.pump(const Duration(milliseconds: 50)); // mid-transition
    expect(find.byType(ActivityEditorPage), findsOneWidget);

    notifier.saveDone.complete();
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.text('open editor'), findsOneWidget);
  });

  testWidgets('while a save is in flight the point menu offers no split or cut',
      (tester) async {
    // The map stays live while Save spins, so the point menu still opens. A
    // split or cut confirmed there could not go out beside the save, so it is
    // not offered at all rather than accepted and then dropped.
    final notifier = _SlowSaveNotifier();
    await _pumpPushed(tester, _longActivity(), notifier);
    _controllerOf(tester).removeSelected(1);
    await tester.pump();
    await tester.tap(find.text('Save'));
    await settleFrames(tester);

    await tester.longPress(find.byKey(const ValueKey('vertex_2')));
    await settleFrames(tester);
    for (final label in ['Split here', 'Cut & add transport']) {
      final item = tester.widget<PopupMenuItem<int>>(find.ancestor(
          of: find.text(label), matching: find.byType(PopupMenuItem<int>)));
      expect(item.enabled, isFalse, reason: '"$label" offered during a save');
    }
    await tester.tap(find.text('Split here'), warnIfMissed: false);
    await settleFrames(tester);
    expect(find.byType(AlertDialog), findsNothing);
    expect(notifier.splits, isEmpty);

    notifier.saveDone.complete();
    await tester.pumpAndSettle();
    expect(find.byType(ActivityEditorPage), findsNothing);
  });

  testWidgets('a split or cut confirmed once a write has started says why '
      'nothing happened', (tester) async {
    // The backstop behind the disabled entries. No real tap reaches Save
    // through the confirmation's barrier, so this calls Save's callback while
    // the dialog is up: whatever gets a write going there, the action the user
    // confirmed must not vanish without a word.
    for (final (entry, confirm) in [
      ('Split here', 'Split'),
      ('Cut & add transport', 'Cut'),
    ]) {
      final notifier = _SlowSaveNotifier();
      await tester.pumpWidget(const SizedBox());
      await _pumpPushed(tester, _longActivity(), notifier);
      _controllerOf(tester).removeSelected(1);
      await tester.pump();

      await _pointMenu(tester, 2, entry);
      tester
          .widget<TrackEditorSaveButton>(find.byType(TrackEditorSaveButton))
          .onPressed();
      await settleFrames(tester);
      await tester.tap(find.widgetWithText(FilledButton, confirm));
      await settleFrames(tester);

      expect(notifier.saves, 1);
      expect(notifier.splits, isEmpty, reason: '$confirm went out mid-save');
      expect(find.textContaining('Wait for the save to finish'), findsOneWidget,
          reason: '$confirm was dropped without a word');
    }
  });

  testWidgets('Reset from a menu opened on a phone still works once the '
      'window is widened past 720 px', (tester) async {
    // Landscape rotation, split screen, a browser resize: the bar swaps ⋮ for
    // the labelled button while the menu is still up, and the item must not
    // turn into a dead tap.
    final notifier = _familyNotifier(0);
    await _pumpPushed(tester, _activity(edited: true), notifier,
        size: const Size(700, 900));
    await tester.tap(find.byTooltip('More options'));
    await tester.pumpAndSettle();

    tester.view.physicalSize = const Size(900, 900);
    await tester.pumpAndSettle();
    expect(find.byTooltip('More options'), findsNothing);
    final item = find.descendant(
        of: find.byType(ListTile), matching: find.text('Reset to Strava'));
    expect(item, findsOneWidget);

    await tester.tap(item);
    await tester.pumpAndSettle();
    expect(notifier.resets, [111]);
    expect(find.byType(ActivityEditorPage), findsNothing);
  });

  testWidgets('an edit via the controller enables Save', (tester) async {
    await _pump(tester, _activity());
    _controllerOf(tester).removeSelected(0);
    await tester.pump();
    final save = tester.widget<TextButton>(
      find.ancestor(of: find.text('Save'), matching: find.byType(TextButton)),
    );
    expect(save.onPressed, isNotNull);
  });

  testWidgets('removing a point updates the rendered map polyline',
      (tester) async {
    await _pump(tester, _activity());
    PolylineLayer poly() =>
        tester.widget<PolylineLayer>(find.byType(PolylineLayer));
    expect(poly().polylines.first.points.length, 4);

    _controllerOf(tester).removeSelected(1);
    await tester.pump();

    expect(poly().polylines.first.points.length, 3,
        reason: 'the map polyline should drop the removed vertex');
  });

  // ── End-to-end gesture tests (issue #38) — drive the real widget tree ──────

  int polyCount(WidgetTester tester) =>
      tester.widget<PolylineLayer>(find.byType(PolylineLayer))
          .polylines
          .first
          .points
          .length;

  testWidgets('Add mode: tapping the map surface inserts a point',
      (tester) async {
    await _pump(tester, _activity());
    expect(polyCount(tester), 4);

    // Enable Add mode via the real toolbar chip.
    await tester.tap(find.text('Add points'));
    await tester.pump();

    // Tap the map surface away from the vertices (which sit at/east of centre)
    // so the tap lands on the tile layer, not a handle marker. flutter_map
    // defers the tap by its double-tap window, so pump past it.
    final mapCentre = tester.getCenter(find.byType(FlutterMap));
    await tester.tapAt(mapCentre + const Offset(-200, -120));
    await tester.pump(const Duration(milliseconds: 400));

    expect(polyCount(tester), 5,
        reason: 'a map-surface tap in Add mode should insert one vertex');
  });

  testWidgets('Delete via context menu removes the point and updates the map',
      (tester) async {
    await _pump(tester, _activity());
    expect(polyCount(tester), 4);

    // Long-press the first vertex handle (at map centre, so its menu has room)
    // to open its context menu.
    await tester.longPress(find.byKey(const ValueKey('vertex_0')));
    await tester.pumpAndSettle();
    expect(find.text('Delete point'), findsOneWidget);

    await tester.tap(find.text('Delete point'));
    await tester.pumpAndSettle();

    expect(polyCount(tester), 3,
        reason: 'deleting via the menu should drop the vertex from the map');
  });

  testWidgets('Dragging a vertex handle moves the point (issue #36)',
      (tester) async {
    await _pump(tester, _activity());
    final c = _controllerOf(tester);
    final before = c.points.first;

    await tester.drag(
      find.byKey(const ValueKey('vertex_0')),
      const Offset(0, -80), // drag north
    );
    await tester.pump();

    final after = c.points.first;
    expect(after.lat == before.lat && after.lng == before.lng, isFalse,
        reason: 'the dragged vertex should have committed a new position');
    expect(c.isDirty, isTrue);
    // The move is committed through moveVertex → the save payload reflects it.
    final payload = (c.toSavePayload()['points'] as List).first as Map;
    expect(payload['lat'], closeTo(after.lat, 1e-9));
  });

  // ── Edits compound with Split / Cut (issue #127) ───────────────────────────
  // The editor holds edits locally until Save, so a split that sent only an
  // index discarded them — and applied that index to a different point list
  // than the one on screen. Both actions now carry the current points.

  List<double> lngsOf(Map<String, dynamic>? payload) => [
        for (final p in (payload?['points'] as List? ?? [])) (p as Map)['lng'] as double,
      ];

  testWidgets('Cut & add transport carries the points left after a deletion',
      (tester) async {
    final notifier = _RecordingNotifier();
    await _pumpPushed(tester, _longActivity(), notifier);

    // Delete the vertex at index 1 (lon 2.01), then cut at index 2 of what's left.
    await _pointMenu(tester, 1, 'Delete point');
    await _pointMenu(tester, 2, 'Cut & add transport');
    await tester.tap(find.widgetWithText(FilledButton, 'Cut'));
    await tester.pumpAndSettle();

    expect(notifier.splits, hasLength(1));
    final call = notifier.splits.single;
    expect(call.dropBoundary, isTrue);
    expect(call.index, 2);
    // The cut carries the post-deletion track: 5 points, without lon 2.01.
    expect(lngsOf(call.payload), hasLength(5));
    expect(lngsOf(call.payload).any((l) => (l - 2.01).abs() < 1e-6), isFalse,
        reason: 'the deleted point must not come back with the cut');
  });

  testWidgets('Split here carries the edited points too', (tester) async {
    final notifier = _RecordingNotifier();
    await _pumpPushed(tester, _longActivity(), notifier);

    await _pointMenu(tester, 1, 'Delete point');
    await _pointMenu(tester, 2, 'Split here');
    await tester.tap(find.widgetWithText(FilledButton, 'Split'));
    await tester.pumpAndSettle();

    final call = notifier.splits.single;
    expect(call.dropBoundary, isFalse);
    expect(lngsOf(call.payload), hasLength(5));
    expect(lngsOf(call.payload).any((l) => (l - 2.01).abs() < 1e-6), isFalse);
  });

  testWidgets('the confirmation says pending edits are applied, only when dirty',
      (tester) async {
    final notifier = _RecordingNotifier();
    await _pumpPushed(tester, _longActivity(), notifier);

    // Clean editor: no note.
    await tester.longPress(find.byKey(const ValueKey('vertex_2')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Cut & add transport'));
    await tester.pumpAndSettle();
    expect(find.textContaining('unsaved point edits'), findsNothing);
    await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
    await tester.pumpAndSettle();

    // After an edit, the same confirmation warns that it will be applied.
    await _pointMenu(tester, 1, 'Delete point');
    await _pointMenu(tester, 2, 'Cut & add transport');
    expect(find.textContaining('unsaved point edits'), findsOneWidget);
  });

  // ── Optimistic-lock conflict (issue #31) ───────────────────────────────────
  //
  // A 409 means the activity changed elsewhere (another tab/device) since the
  // editor loaded it — a stale copy that must not be silently retried. The
  // editor tells the user plainly and closes rather than showing the raw
  // ApiException text.

  testWidgets('a stale-version conflict on save is explained and closes the editor',
      (tester) async {
    await _pumpPushed(tester, _activity(), _StaleVersionNotifier());

    _controllerOf(tester).removeSelected(0);
    await tester.pump();
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(find.textContaining('changed elsewhere'), findsOneWidget);
    expect(find.byType(ActivityEditorPage), findsNothing,
        reason: 'a stale write must close the editor, not leave it open on '
            'a copy that can only conflict again');
  });

  testWidgets('a stale-version conflict on split is explained and closes the editor',
      (tester) async {
    await _pumpPushed(tester, _longActivity(), _StaleVersionNotifier());

    await _pointMenu(tester, 2, 'Split here');
    await tester.tap(find.widgetWithText(FilledButton, 'Split'));
    await tester.pumpAndSettle();

    expect(find.textContaining('changed elsewhere'), findsOneWidget);
    expect(find.byType(ActivityEditorPage), findsNothing);
  });
}
