import 'package:flutter_test/flutter_test.dart';

import 'package:traxjourney_client/src/projects/track_edit_model.dart';
import 'package:traxjourney_client/src/projects/track_editor_controller.dart';

TrackEditorController _controller() => TrackEditorController(
      TrackEditModel.fromPoints(const [
        EditPoint(48.0, 2.00, 100),
        EditPoint(48.0, 2.01, 110),
        EditPoint(48.0, 2.02, 120),
        EditPoint(48.0, 2.03, 130),
      ]),
    );

void main() {
  group('tool switching', () {
    test('defaults to trim and clears selection on change', () {
      final c = _controller();
      expect(c.tool, EditTool.trim);
      c.tool = EditTool.remove;
      c.selectVertex(2);
      expect(c.selectedIndex, 2);
      c.tool = EditTool.split;
      expect(c.selectedIndex, isNull); // cleared on tool change
    });

    test('notifies listeners on tool change', () {
      final c = _controller();
      var fired = 0;
      c.addListener(() => fired++);
      c.tool = EditTool.add;
      expect(fired, 1);
    });
  });

  group('trim', () {
    test('trimEnd defaults to the last index', () {
      final c = _controller();
      expect(c.trimStart, 0);
      expect(c.trimEnd, 3);
    });

    test('handles are clamped to a valid window', () {
      final c = _controller();
      c.setTrimEnd(2);
      c.setTrimStart(5); // clamped to <= trimEnd
      expect(c.trimStart, 2);
      c.setTrimEnd(0); // clamped to >= trimStart
      expect(c.trimEnd, 2);
    });

    test('applyTrim drops points outside the window', () {
      final c = _controller();
      c.setTrimStart(1);
      c.setTrimEnd(2);
      c.applyTrim();
      expect(c.points.length, 2);
      expect(c.points.first.lng, closeTo(2.01, 1e-9));
      expect(c.points.last.lng, closeTo(2.02, 1e-9));
      expect(c.isDirty, isTrue);
    });

    test('trimFrom keeps index..end (context-menu "keep from here")', () {
      final c = _controller();
      c.trimFrom(2);
      expect(c.points.length, 2);
      expect(c.points.first.lng, closeTo(2.02, 1e-9));
      expect(c.points.last.lng, closeTo(2.03, 1e-9));
    });

    test('trimTo keeps 0..index (context-menu "keep up to here")', () {
      final c = _controller();
      c.trimTo(1);
      expect(c.points.length, 2);
      expect(c.points.first.lng, closeTo(2.00, 1e-9));
      expect(c.points.last.lng, closeTo(2.01, 1e-9));
    });
  });

  group('add / remove', () {
    test('addPointAfter inserts into the segment', () {
      final c = _controller();
      c.addPointAfter(0, const EditPoint(48.5, 2.005));
      expect(c.points.length, 5);
      expect(c.points[1], const EditPoint(48.5, 2.005));
    });

    test('removeSelected removes the selected vertex and clears selection', () {
      final c = _controller();
      c.selectVertex(1);
      c.removeSelected();
      expect(c.points.length, 3);
      expect(c.selectedIndex, isNull);
    });

    test('removeSelected is a no-op with no selection', () {
      final c = _controller();
      c.removeSelected();
      expect(c.points.length, 4);
    });
  });

  group('split preview', () {
    test('canSplitAt only accepts interior indices', () {
      final c = _controller();
      expect(c.canSplitAt(0), isFalse);
      expect(c.canSplitAt(1), isTrue);
      expect(c.canSplitAt(2), isTrue);
      expect(c.canSplitAt(3), isFalse);
    });

    test('previewSplit shares the boundary and does not mutate', () {
      final c = _controller();
      final r = c.previewSplit(2);
      expect(r.head.length, 3);
      expect(r.tail.length, 2);
      expect(r.head.last, r.tail.first);
      expect(c.isDirty, isFalse);
    });
  });

  group('cut-for-transport preview (#104)', () {
    test('canCutForTransport needs one more tail point than canSplitAt', () {
      // 4-point track: canSplitAt allows index 1 and 2, but dropping the
      // boundary point needs an extra tail point, so only index 1 qualifies.
      final c = _controller();
      expect(c.canCutForTransport(0), isFalse);
      expect(c.canCutForTransport(1), isTrue);
      expect(c.canCutForTransport(2), isFalse);
      expect(c.canCutForTransport(3), isFalse);
    });

    test('canCutForTransport accepts interior indices on a longer track', () {
      final c = TrackEditorController(TrackEditModel.fromPoints(const [
        EditPoint(48.0, 2.0),
        EditPoint(48.0, 2.1),
        EditPoint(48.0, 2.2),
        EditPoint(48.0, 2.3),
        EditPoint(48.0, 2.4),
      ]));
      expect(c.canCutForTransport(0), isFalse);
      expect(c.canCutForTransport(1), isTrue);
      expect(c.canCutForTransport(2), isTrue);
      expect(c.canCutForTransport(3), isFalse); // only 1 point left after boundary
      expect(c.canCutForTransport(4), isFalse);
    });
  });

  group('save gating', () {
    test('canSave is false when pristine', () {
      expect(_controller().canSave, isFalse);
    });

    test('canSave becomes true after a valid edit', () {
      final c = _controller();
      c.removeSelected(0);
      expect(c.canSave, isTrue);
    });

    test('canSave is false when an edit leaves fewer than two points', () {
      final c = TrackEditorController(TrackEditModel.fromPoints(const [
        EditPoint(48.0, 2.0),
        EditPoint(48.0, 2.1),
      ]));
      c.removeSelected(0); // now 1 point → invalid
      expect(c.canSave, isFalse);
    });

    test('toSavePayload reflects the current points', () {
      final c = _controller();
      c.removeSelected(0);
      final points = c.toSavePayload()['points'] as List;
      expect(points.length, 3);
    });
  });
}
