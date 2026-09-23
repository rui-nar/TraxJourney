/// The brand splash shown while the app restores a persisted session.
///
/// Always dark, whatever the active theme — it is a brand moment, not a themed
/// surface. Two layouts, matching the design's mobile and large-screen boards:
/// stacked and centred below [kSplashWideBreakpoint], mark-beside-wordmark
/// above it.
///
/// The pre-Flutter boot chrome is painted to match, so nothing jumps when the
/// engine takes over — see web/index.html and the Android launch_background /
/// values-v31 styles.
library;

import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:provider/provider.dart';

import '../auth/auth_notifier.dart';
import 'app_version.dart';
import 'brand.dart';
import 'brand_mark.dart';

const _wordmark = kAppName;
const _tagline = 'Merge · Visualise · Export';

/// The app's own one-liner, kept in step with web/manifest.json.
const _blurb =
    'Merge Strava rides, segments and memories into one continuous journey.';

/// Width at which the wide layout fits: mark + divider + a 380 px text column.
const double kSplashWideBreakpoint = 760;

// ── Palette ──────────────────────────────────────────────────────────────────
const _fieldNear = Color(0xFF132238);
const _fieldMid = Color(0xFF0B1420);
const _fieldFar = Color(0xFF080F18);

/// Flat stand-in for the field gradient, used by the boot chrome that cannot
/// paint one (Android's launch window colour).
const kSplashBackground = _fieldMid;

const _gridLine = Color(0x0BFFFFFF);
const _wordmarkColour = Color(0xFFE2E8F0);
const _taglineColour = Color(0xFF64748B);
const _blurbColour = Color(0xFF94A3B8);
const _dividerColour = Color(0xFF2D4A6A);
const _trackColour = Color(0xFF1E3350);
const _footerColour = Color(0xFF3A526E);
const _progressStart = Color(0xFF3B82F6);
const _progressEnd = Color(0xFFEF4444);

// ── Field ────────────────────────────────────────────────────────────────────

/// The two boards' fields, transcribed from the design's CSS:
///   stacked: radial-gradient(ellipse 120% 80% at 50% 18%, …)
///   wide:    radial-gradient(ellipse 110% 95% at 50% 12%, …)
final _stackedField = _cssRadial(width: 1.20, height: 0.80, atY: 0.18);
final _wideField = _cssRadial(width: 1.10, height: 0.95, atY: 0.12);

/// A CSS `radial-gradient(ellipse W H at 50% atY)`.
///
/// [width] and [height] are the ellipse's extents as fractions of the box, and
/// they differ — so this cannot be a plain RadialGradient, whose single radius
/// is always a fraction of the shortest side. The transform stretches that
/// circle back out to the two extents the design asks for; without it the glow
/// falls off within the top third of a tall screen instead of filling it.
RadialGradient _cssRadial({
  required double width,
  required double height,
  required double atY,
}) =>
    RadialGradient(
      center: Alignment(0, atY * 2 - 1),
      colors: const [_fieldNear, _fieldMid, _fieldFar],
      stops: const [0.0, 0.55, 1.0],
      transform: _EllipseExtents(width: width, height: height, atY: atY),
    );

@immutable
class _EllipseExtents extends GradientTransform {
  const _EllipseExtents({
    required this.width,
    required this.height,
    required this.atY,
  });

  final double width;
  final double height;
  final double atY;

  @override
  Matrix4 transform(Rect bounds, {TextDirection? textDirection}) {
    // RadialGradient's default radius of 0.5 measures against the shortest side.
    final unit = bounds.shortestSide / 2;
    final cx = bounds.center.dx;
    final cy = bounds.top + bounds.height * atY;
    return Matrix4.identity()
      ..translateByDouble(cx, cy, 0, 1)
      ..scaleByDouble(bounds.width * width / unit, bounds.height * height / unit, 1, 1)
      ..translateByDouble(-cx, -cy, 0, 1);
  }

  @override
  bool operator ==(Object other) =>
      other is _EllipseExtents &&
      other.width == width &&
      other.height == height &&
      other.atY == atY;

  @override
  int get hashCode => Object.hash(width, height, atY);
}

/// Holds the splash over [child] until the persisted session has been
/// restored, so the first thing on screen is the brand rather than a welcome
/// screen that is about to redirect.
class SplashGate extends StatelessWidget {
  const SplashGate({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    // Deliberately not `isLoading`: that is also true during an interactive
    // sign-in, which would throw the splash back over the login form.
    final restoring = context.watch<AuthNotifier>().isRestoring;
    return Stack(
      children: [
        child,
        if (restoring) const Positioned.fill(child: SplashScreen()),
      ],
    );
  }
}

class SplashScreen extends StatelessWidget {
  const SplashScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final wide = constraints.maxWidth >= kSplashWideBreakpoint;
        return DecoratedBox(
          decoration: BoxDecoration(
            gradient: wide ? _wideField : _stackedField,
          ),
          // The splash is mounted from MaterialApp.builder, above the Navigator
          // and outside any Material — so the DefaultTextStyle in scope is
          // MaterialApp's deliberate eyesore fallback, which carries a yellow
          // double underline. The type styles below set colour and metrics but
          // not decoration, so without this every label inherits it (#177).
          child: DefaultTextStyle.merge(
            style: const TextStyle(decoration: TextDecoration.none),
            child: Stack(
              fit: StackFit.expand,
              children: [
                const CustomPaint(painter: _GridPainter()),
                wide ? const _WideLayout() : const _StackedLayout(),
              ],
            ),
          ),
        );
      },
    );
  }
}

// ── Layouts ──────────────────────────────────────────────────────────────────

/// Mobile board: everything centred, progress and version pinned to the foot.
class _StackedLayout extends StatelessWidget {
  const _StackedLayout();

  @override
  Widget build(BuildContext context) {
    return Stack(
      children: [
        Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const BrandMark(size: 168),
              const SizedBox(height: 34),
              Text(_wordmark, style: _wordmarkStyle(28, -0.28)),
              const SizedBox(height: 10),
              Text(_tagline.toUpperCase(), style: _taglineStyle(12, 1.2)),
            ],
          ),
        ),
        Positioned(
          left: 0,
          right: 0,
          bottom: 52,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const _ProgressBar(width: 132),
              const SizedBox(height: 16),
              // Both versions, so a screenshot of the splash is enough to
              // report a bug against (#275). The server version usually lands
              // while the splash is still up — the fetch starts in the same
              // frame — but reads "unknown" if it never does.
              VersionText(
                  style: _footerStyle(),
                  textAlign: TextAlign.center,
                  omitUnknownServer: true),
            ],
          ),
        ),
      ],
    );
  }
}

/// Large board: mark and wordmark side by side, version and progress split
/// across the foot.
class _WideLayout extends StatelessWidget {
  const _WideLayout();

  @override
  Widget build(BuildContext context) {
    return Stack(
      children: [
        Center(
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              const BrandMark(size: 228),
              const SizedBox(width: 56),
              Container(width: 1, height: 184, color: _dividerColour),
              const SizedBox(width: 56),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 380),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(_wordmark, style: _wordmarkStyle(52, -1.04)),
                    const SizedBox(height: 16),
                    Text(_tagline.toUpperCase(), style: _taglineStyle(13, 1.56)),
                    const SizedBox(height: 20),
                    Text(
                      _blurb,
                      style: GoogleFonts.inter(
                        fontSize: 15,
                        fontWeight: FontWeight.w400,
                        height: 1.6,
                        color: _blurbColour,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        Positioned(
          left: 40,
          bottom: 32,
          child: VersionText(
              style: _footerStyle(size: 12), omitUnknownServer: true),
        ),
        Positioned(
          right: 40,
          bottom: 32,
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              const _ProgressBar(width: 120),
              const SizedBox(width: 10),
              Text(
                'Loading',
                style: GoogleFonts.inter(
                  fontSize: 11,
                  fontWeight: FontWeight.w500,
                  height: 1,
                  color: _taglineColour,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

// ── Pieces ───────────────────────────────────────────────────────────────────

/// The design's 62 %-wide gradient fill, shuttled across its track. The wait is
/// a session restore of unknown length, so an indeterminate sweep is honest
/// where the board's static bar would not be.
class _ProgressBar extends StatefulWidget {
  const _ProgressBar({required this.width});

  final double width;

  @override
  State<_ProgressBar> createState() => _ProgressBarState();
}

class _ProgressBarState extends State<_ProgressBar>
    with SingleTickerProviderStateMixin {
  static const _fillFraction = 0.62;

  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1400),
  )..repeat();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final fillWidth = widget.width * _fillFraction;
    return ClipRRect(
      borderRadius: BorderRadius.circular(2),
      child: SizedBox(
        width: widget.width,
        height: 2,
        child: ColoredBox(
          color: _trackColour,
          child: AnimatedBuilder(
            animation: _controller,
            builder: (context, child) {
              // Travels from fully off the left edge to fully off the right.
              final start = -fillWidth +
                  (widget.width + fillWidth) * _controller.value;
              return Stack(
                children: [Positioned(left: start, child: child!)],
              );
            },
            child: Container(
              width: fillWidth,
              height: 2,
              decoration: const BoxDecoration(
                gradient: LinearGradient(
                  colors: [_progressStart, _progressEnd],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// The 40 px hairline grid the design lays over the field.
class _GridPainter extends CustomPainter {
  const _GridPainter();

  static const _cell = 40.0;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = _gridLine
      ..strokeWidth = 1;
    for (double x = 0; x <= size.width; x += _cell) {
      canvas.drawLine(Offset(x, 0), Offset(x, size.height), paint);
    }
    for (double y = 0; y <= size.height; y += _cell) {
      canvas.drawLine(Offset(0, y), Offset(size.width, y), paint);
    }
  }

  @override
  bool shouldRepaint(_GridPainter oldDelegate) => false;
}

// ── Type ─────────────────────────────────────────────────────────────────────

TextStyle _wordmarkStyle(double size, double letterSpacing) => GoogleFonts.inter(
      fontSize: size,
      fontWeight: FontWeight.w700,
      height: 1.1,
      letterSpacing: letterSpacing,
      color: _wordmarkColour,
    );

TextStyle _taglineStyle(double size, double letterSpacing) => GoogleFonts.inter(
      fontSize: size,
      fontWeight: FontWeight.w500,
      height: 1.4,
      letterSpacing: letterSpacing,
      color: _taglineColour,
    );

TextStyle _footerStyle({double size = 11}) => GoogleFonts.inter(
      fontSize: size,
      fontWeight: FontWeight.w400,
      height: 1.4,
      letterSpacing: 0.44,
      color: _footerColour,
    );
