/// Vertical day carousel (issue #199) — view-mode only.
///
/// A transparent, pill-shaped overlay docked to the right edge of the map.
/// Scrolls through the trip's days; the centered day becomes the active map
/// selection via `notifier.selectDays()`, so `MapPanel`'s highlight/
/// auto-zoom-to-selection behaviour (also added for view mode by this same
/// change — see map_panel.dart) applies. The reverse direction holds too
/// (issue #322): tapping an activity on the map scrolls the strip to that
/// activity's day. Idle-dims and retracts toward the screen edge to stay
/// unobtrusive.
library;

import 'dart:async' show Timer;
import 'dart:ui' show lerpDouble;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart' show kShadow2, monoStyle;
import 'map_panel.dart' show dayForSelection;
import 'project_notifier.dart';

/// Pure index → day-key mapping for [DayCarousel]'s `onSelectedItemChanged`,
/// extracted so the selection logic can be tested without driving a real
/// [ListWheelScrollView] scroll gesture.
@visibleForTesting
String? dayCarouselSelection(int index, List<String> days) =>
    (index >= 0 && index < days.length) ? days[index] : null;

/// Scale multiplier for a card [distanceInSlots] away from the centered
/// (highlighted) item — 1.0 at rest, growing to [DayCarousel.magnification]
/// at the center, linearly over one slot. Extracted for testing.
@visibleForTesting
double dayCarouselScale(double distanceInSlots, double magnification) {
  final t = (1 - distanceInSlots).clamp(0.0, 1.0);
  return lerpDouble(1.0, magnification, t)!;
}

/// Leftward pixel offset for a card [distanceInSlots] away from the centered
/// item — 0 at rest, growing to [maxOffset] at the center, linearly over one
/// slot. The staggered-stack look from issue #199 feedback: each row nudges
/// further left the closer it is to center, so the highlighted card bulges
/// past the pill's edge. Extracted for testing.
@visibleForTesting
double dayCarouselOffset(double distanceInSlots, double maxOffset) {
  final t = (1 - distanceInSlots).clamp(0.0, 1.0);
  return lerpDouble(0.0, maxOffset, t)!;
}

class DayCarousel extends StatefulWidget {
  final ProjectNotifier notifier;

  /// Scale factor applied to the centered card's text relative to a
  /// neighboring (unselected) card — issue #199 feedback: "2x bigger".
  final double magnification;

  /// How far left (px) the centered card bulges past the pill's edge —
  /// issue #199 feedback: a staggered/offset stack.
  final double maxOffset;

  const DayCarousel({
    super.key,
    required this.notifier,
    this.magnification = 2.0,
    this.maxOffset = 42.0,
  });

  @override
  State<DayCarousel> createState() => _DayCarouselState();
}

class _DayCarouselState extends State<DayCarousel> {
  static const _idleDelay = Duration(seconds: 3);
  static const _animDuration = Duration(milliseconds: 280);
  static const _selectDebounceDelay = Duration(milliseconds: 120);

  bool _revealed = true;
  Timer? _idleTimer;
  Timer? _selectDebounce;
  late FixedExtentScrollController _scrollController;

  // Last map selection the wheel reacted to, so it only follows a *new* one —
  // same convention as ActivityPanel's _prevSelectedActivityIdStr (issue #322).
  String? _prevSelectedActivityIdStr;
  String? _prevSelectedSegmentIdStr;

  // The index a follow-the-map-selection scroll is currently heading for, or
  // null when the wheel is at rest / being dragged; see _followSelection. The
  // generation counter goes with it so a superseded animation's own
  // completion can't clear it out from under the follow that replaced it
  // (same idiom as MapPanel's _hitTestGen).
  int? _followingIndex;
  int _followGen = 0;

  int _activeIndex(List<String> days) {
    final active = widget.notifier.selectionFacet.selectedDays.isNotEmpty
        ? widget.notifier.selectionFacet.selectedDays.first
        : widget.notifier.selectionFacet.selectedDay;
    if (active == null) return 0;
    final i = days.indexOf(active);
    return i >= 0 ? i : 0;
  }

  @override
  void initState() {
    super.initState();
    _scrollController = FixedExtentScrollController(
      initialItem: _activeIndex(widget.notifier.itemsFacet.orderedDayKeys()),
    );
    _prevSelectedActivityIdStr = widget.notifier.selectionFacet.selectedActivityId?.toString();
    _prevSelectedSegmentIdStr = widget.notifier.selectionFacet.selectedSegmentId?.toString();
    widget.notifier.addListener(_onNotifierChanged);
    _scheduleRetract();
  }

  /// Issue #322: a tap on the map selects an activity/segment; the wheel
  /// follows it so the strip shows the day that activity belongs to.
  void _onNotifierChanged() {
    final actId = widget.notifier.selectionFacet.selectedActivityId?.toString();
    final segId = widget.notifier.selectionFacet.selectedSegmentId?.toString();
    final changed = actId != _prevSelectedActivityIdStr ||
        segId != _prevSelectedSegmentIdStr;
    _prevSelectedActivityIdStr = actId;
    _prevSelectedSegmentIdStr = segId;
    // Nothing selected any more — a second tap on the same activity
    // deselects it, and the day selection this carousel itself commits
    // clears it too. Leave the wheel where it is rather than jumping it
    // somewhere arbitrary.
    if (!changed || (actId == null && segId == null)) return;
    // A map selection is newer intent than a day commit still sitting in the
    // scroll debounce, and selectDays() would clear it — drop the older one.
    _selectDebounce?.cancel();
    final day = dayForSelection(
      widget.notifier.itemsFacet.items,
      widget.notifier.itemsFacet.activities,
      activityId: widget.notifier.selectionFacet.selectedActivityId,
      segmentId: segId,
    );
    // Unscheduled, or a day the strip doesn't list: same, leave the wheel be.
    if (day == null) return;
    final index = widget.notifier.itemsFacet.orderedDayKeys().indexOf(day);
    if (index < 0 || !_scrollController.hasClients) return;
    // Where the wheel will end up, not where it happens to be right now: a
    // follow already in flight has not reached its own target yet.
    if ((_followingIndex ?? _scrollController.selectedItem) == index) return;
    _followSelection(index);
  }

  /// Scrolls the wheel to [index] *without* committing a day selection of its
  /// own. `animateToItem` crosses every intermediate index and each crossing
  /// fires [_onSelectedItemChanged]; letting that commit `selectDays()` would
  /// clear the very activity selection that moved the wheel (selectActivity
  /// and selectDays are mutually exclusive on ProjectNotifier) and notify
  /// again — so a non-null [_followingIndex] suppresses the commit for the
  /// animation's duration, which is what keeps map selection and wheel from
  /// ping-ponging.
  Future<void> _followSelection(int index) async {
    final gen = ++_followGen;
    _followingIndex = index;
    try {
      await _scrollController.animateToItem(index,
          duration: _animDuration, curve: Curves.easeInOut);
    } finally {
      // A second map tap mid-animation supersedes this one: starting its
      // animation completes *this* future early, so only the newest follow
      // may clear the target.
      if (gen == _followGen) _followingIndex = null;
    }
  }

  @override
  void didUpdateWidget(covariant DayCarousel oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Defensive: if a future caller ever swaps in a different notifier
    // without remounting this widget's State (today's callers always
    // remount — see ViewScreen's ChangeNotifierProvider), re-center the
    // wheel on the new notifier's active day instead of leaving the old
    // project's scroll position/index behind.
    if (!identical(widget.notifier, oldWidget.notifier)) {
      oldWidget.notifier.removeListener(_onNotifierChanged);
      widget.notifier.addListener(_onNotifierChanged);
      _prevSelectedActivityIdStr =
          widget.notifier.selectionFacet.selectedActivityId?.toString();
      _prevSelectedSegmentIdStr = widget.notifier.selectionFacet.selectedSegmentId?.toString();
      if (_scrollController.hasClients) {
        _scrollController
            .jumpToItem(_activeIndex(widget.notifier.itemsFacet.orderedDayKeys()));
      }
    }
  }

  @override
  void dispose() {
    widget.notifier.removeListener(_onNotifierChanged);
    _idleTimer?.cancel();
    _selectDebounce?.cancel();
    _scrollController.dispose();
    super.dispose();
  }

  void _reveal() {
    _idleTimer?.cancel();
    if (!_revealed) setState(() => _revealed = true);
    _scheduleRetract();
  }

  void _scheduleRetract() {
    _idleTimer?.cancel();
    _idleTimer = Timer(_idleDelay, () {
      if (mounted) setState(() => _revealed = false);
    });
  }

  // ListWheelScrollView fires onSelectedItemChanged for every index the
  // wheel crosses, including mid-fling — not just once scrolling settles.
  // Committing selectDays() (a notifyListeners(), rebuilding the map +
  // elevation chart) on each crossing would jank a fast fling, so debounce
  // to the index the wheel is actually resting on.
  void _onSelectedItemChanged(int index, List<String> days) {
    _reveal();
    _selectDebounce?.cancel();
    // Programmatic scroll following a map selection — see _followSelection.
    if (_followingIndex != null) return;
    _selectDebounce = Timer(_selectDebounceDelay, () {
      final key = dayCarouselSelection(index, days);
      if (key != null) widget.notifier.selectDays({key});
    });
  }

  @override
  Widget build(BuildContext context) {
    final days = widget.notifier.itemsFacet.orderedDayKeys();
    if (days.isEmpty) return const SizedBox.shrink();

    final cs = Theme.of(context).colorScheme;
    // Compact strip (no distance/climb) below this width — issue #199
    // responsive requirement.
    final compact = MediaQuery.sizeOf(context).width < 720;
    // 130 (not the tighter 110 a bare number would fit in) — the highlighted
    // card's "Day N" + distance/climb stats at 2x scale need the headroom.
    final itemExtent = compact ? 52.0 : 130.0;
    // Pill/stadium shape (issue #199 feedback): radius = half the pill's
    // width, so the right edge is one continuous curve; left edge stays
    // square. The scrollable layer below is reserved extra width beyond the
    // pill so the centered card can bulge past its left edge without being
    // clipped — see the staggered-stack offset (issue #199 feedback).
    final pillWidth = compact ? 64.0 : 120.0;
    final radius = pillWidth / 2;
    final pillRadius = BorderRadius.only(
      topRight: Radius.circular(radius),
      bottomRight: Radius.circular(radius),
    );
    final totalWidth = pillWidth + widget.maxOffset;
    const verticalInset = EdgeInsets.symmetric(vertical: 24);

    return MouseRegion(
      onEnter: (_) => _reveal(),
      onExit: (_) => _scheduleRetract(),
      child: GestureDetector(
        // Only intercept taps while retracted, so a tap reveals the strip
        // instead of falling through to whatever's behind it; while revealed,
        // leave gesture handling to the ListWheelScrollView beneath.
        onTap: _revealed ? null : _reveal,
        behavior: HitTestBehavior.opaque,
        child: AnimatedSlide(
          duration: _animDuration,
          curve: Curves.easeInOut,
          offset: _revealed ? Offset.zero : const Offset(0.6, 0),
          child: AnimatedOpacity(
            duration: _animDuration,
            opacity: _revealed ? 1.0 : 0.4,
            child: SizedBox(
              width: totalWidth,
              child: Stack(
                children: [
                  // Background pill — purely decorative. The scrollable
                  // layer below is deliberately NOT clipped to this shape,
                  // so the centered card can bulge left past its edge.
                  // Positioned (not a bare Stack child) so it gets a real
                  // height under Stack's loose constraints — an unsized
                  // Container would otherwise collapse to zero height.
                  Positioned(
                    top: 0,
                    bottom: 0,
                    right: 0,
                    width: pillWidth,
                    child: Container(
                      margin: verticalInset,
                      decoration: BoxDecoration(
                        // Transparent overlay (issue #199 feedback), not a
                        // solid card.
                        color: cs.surface.withValues(alpha: 0.4),
                        borderRadius: pillRadius,
                        boxShadow: kShadow2(Theme.of(context).brightness),
                      ),
                    ),
                  ),
                  Positioned(
                    top: 0,
                    bottom: 0,
                    right: 0,
                    width: totalWidth,
                    child: Padding(
                      padding: verticalInset,
                      // Only the right/top/bottom corners clip to the pill's
                      // curve — pillRadius is BorderRadius.only(topRight,
                      // bottomRight), so this box's extra width on the left
                      // (totalWidth vs. the pill's own pillWidth) stays
                      // square/unclipped, letting the centered card bulge
                      // past the pill's left edge without spilling past its
                      // rounded right silhouette.
                      child: ClipRRect(
                        borderRadius: pillRadius,
                        // Fades the top/bottom 30% to transparent (issue
                        // #199 feedback) so the strip recedes into the map
                        // at its edges.
                        child: ShaderMask(
                          blendMode: BlendMode.dstIn,
                          shaderCallback: (rect) => const LinearGradient(
                            begin: Alignment.topCenter,
                            end: Alignment.bottomCenter,
                            colors: [
                              Colors.transparent, Colors.white,
                              Colors.white, Colors.transparent,
                            ],
                            stops: [0.0, 0.3, 0.7, 1.0],
                          ).createShader(rect),
                          child: ListWheelScrollView.useDelegate(
                          controller: _scrollController,
                          itemExtent: itemExtent,
                          diameterRatio: 100,
                          perspective: 0.0005,
                          physics: const FixedExtentScrollPhysics(),
                          onSelectedItemChanged: (i) =>
                              _onSelectedItemChanged(i, days),
                          childDelegate: ListWheelChildBuilderDelegate(
                          childCount: days.length,
                          builder: (context, index) {
                            // dayTripNumbering()/dayStats() iterate the
                            // day/items lists — computed once per index here
                            // (called again only when this index re-enters
                            // the visible window), NOT inside the
                            // AnimatedBuilder below, which reruns every
                            // scroll frame. Only the cheap scale/offset/
                            // highlighted check needs to track the live
                            // offset that often.
                            final dateKey = days[index];
                            final n = dayTripNumbering(
                                dateKey, days, widget.notifier.itemsFacet.tripStart);
                            final stats = widget.notifier.itemsFacet.dayStats(dateKey);
                            return AnimatedBuilder(
                              // Rebuilds this card every scroll tick so its
                              // scale/offset/"Day N" state tracks the live
                              // offset — the hand-rolled replacement for
                              // ListWheelScrollView's built-in magnifier
                              // (issue #199: a true ~2x center card without
                              // clipping into its fixed-height slot, plus the
                              // staggered leftward bulge).
                              animation: _scrollController,
                              builder: (context, _) {
                                final offset = _scrollController.hasClients
                                    ? _scrollController.offset
                                    : _activeIndex(days) * itemExtent;
                                final distance =
                                    (index * itemExtent - offset).abs() /
                                        itemExtent;
                                final scale = dayCarouselScale(
                                    distance, widget.magnification);
                                final dx = dayCarouselOffset(
                                    distance, widget.maxOffset);
                                return Align(
                                  alignment: Alignment.centerRight,
                                  child: Transform.translate(
                                    offset: Offset(-dx, 0),
                                    child: SizedBox(
                                      width: pillWidth,
                                      child: _DayCard(
                                        dayNumber: n.dayNumber,
                                        distanceKm: stats.distanceKm,
                                        elevationM: stats.elevationM,
                                        compact: compact,
                                        scale: scale,
                                        highlighted: distance < 0.5,
                                      ),
                                    ),
                                  ),
                                );
                              },
                            );
                          },
                        ),
                      ),
                    ),
                  ),
                ),
                ),
              ],
            ),
          ),
        ),
      ),
      ),
    );
  }
}

class _DayCard extends StatelessWidget {
  final int dayNumber;
  final double distanceKm;
  final double elevationM;
  final bool compact;
  final double scale;
  final bool highlighted;

  const _DayCard({
    required this.dayNumber,
    required this.distanceKm,
    required this.elevationM,
    required this.compact,
    required this.scale,
    required this.highlighted,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    // "Day N" on the highlighted card, in every layout — the leftward bulge
    // (dayCarouselOffset) gives it room even in the compact strip; issue
    // #199 feedback. Neighbors keep the bare number (space-constrained).
    final label = highlighted ? 'Day $dayNumber' : '$dayNumber';
    final baseFontSize = compact ? 13.0 : 15.0;
    final showStats = highlighted && !compact;
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            label,
            // "Day N" can be wider than this card's own SizedBox (pillWidth)
            // — translating the card left (the bulge) repositions it but
            // doesn't widen its layout box, so without this the text would
            // wrap to a second line and blow out the fixed-height slot
            // (itemExtent). softWrap:false + visible keeps it one line,
            // overflowing horizontally into the reserved bulge space
            // instead — the outer ClipRRect is sized to allow exactly that.
            softWrap: false,
            overflow: TextOverflow.visible,
            style: monoStyle(
              // Same font used by the day-meta editor's hero + the map's
              // selection-stats overlay (both JetBrains Mono via monoStyle).
              fontSize: baseFontSize * scale,
              fontWeight: FontWeight.w600,
              color: cs.onSurface,
              letterSpacing: -0.5,
            ),
          ),
          if (showStats) ...[
            const SizedBox(height: 2),
            Text('${distanceKm.round()} km',
                style: monoStyle(fontSize: 10, color: cs.onSurfaceVariant)),
            Text('${elevationM.round()} m',
                style: monoStyle(fontSize: 10, color: cs.onSurfaceVariant)),
          ],
        ],
      ),
    );
  }
}
