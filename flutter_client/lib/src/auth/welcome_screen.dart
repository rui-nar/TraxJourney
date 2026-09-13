/// Public-facing welcome / landing screen — implements welcome.html handoff.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:url_launcher/url_launcher.dart';

import '../core/app_version.dart';
import '../core/brand.dart';
import '../core/design_tokens.dart';

// ── Layout constants ──────────────────────────────────────────────────────────
const _kBreak = 900.0;
const _kNavH  = 64.0;
const _kShell = 1200.0;
const _kGH    = 'https://github.com/rui-nar/TraxJourney';

// ── Always-dark showcase palette ─────────────────────────────────────────────
const _dBg   = Color(0xFF0D1B2A);
const _dCard = Color(0xFF1B2838);
const _dBord = Color(0xFF2D4A6A);
const _dFg   = Color(0xFFE2E8F0);
const _dFgSub = Color(0xFF94A3B8);
const _dFgHint = Color(0xFF64748B);

// ── Brand gradients ───────────────────────────────────────────────────────────
const _gradBlue = LinearGradient(
  begin: Alignment.topLeft,
  end: Alignment.bottomRight,
  colors: [Color(0xFF0D6EFD), Color(0xFF1E40AF)],
);
const _gradRed = LinearGradient(
  begin: Alignment.topLeft,
  end: Alignment.bottomRight,
  colors: [Color(0xFFEF4444), Color(0xFFB91C1C)],
);
const _gradText = LinearGradient(
  begin: Alignment.topLeft,
  end: Alignment.bottomRight,
  colors: [Color(0xFF0D6EFD), Color(0xFF1E40AF), Color(0xFFDC2626)],
  stops: [0.0, 0.55, 1.2],
);
const _gradLogoText = LinearGradient(
  begin: Alignment.topLeft,
  end: Alignment.bottomRight,
  colors: [Color(0xFF0D6EFD), Color(0xFFDC2626)],
);

// ─────────────────────────────────────────────────────────────────────────────

class WelcomeScreen extends StatefulWidget {
  const WelcomeScreen({super.key});

  @override
  State<WelcomeScreen> createState() => _WelcomeScreenState();
}

class _WelcomeScreenState extends State<WelcomeScreen> {
  final _featKey = GlobalKey();
  final _howKey  = GlobalKey();
  final _hostKey = GlobalKey();

  void _scrollTo(GlobalKey key) {
    final ctx = key.currentContext;
    if (ctx != null) {
      Scrollable.ensureVisible(ctx,
          duration: const Duration(milliseconds: 600), curve: Curves.easeInOut);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Stack(
        children: [
          SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const SizedBox(height: _kNavH),
                const _HeroSection(),
                _FeaturesSection(sectionKey: _featKey),
                _HowItWorksSection(sectionKey: _howKey),
                const _ShowcaseSection(),
                _SelfHostSection(sectionKey: _hostKey),
                const _Footer(),
              ],
            ),
          ),
          Positioned(
            top: 0, left: 0, right: 0,
            child: _NavBar(
              onFeatures: () => _scrollTo(_featKey),
              onHow: () => _scrollTo(_howKey),
              onSelfHost: () => _scrollTo(_hostKey),
            ),
          ),
        ],
      ),
    );
  }
}

// ── Shell wrapper ─────────────────────────────────────────────────────────────

class _Shell extends StatelessWidget {
  final Widget child;
  final EdgeInsets padding;
  const _Shell({required this.child, this.padding = const EdgeInsets.symmetric(horizontal: 32)});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: _kShell),
        child: Padding(padding: padding, child: child),
      ),
    );
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

TextStyle _inter(double size, FontWeight weight, Color color,
    {double? height, double? spacing}) =>
    GoogleFonts.inter(fontSize: size, fontWeight: weight, color: color,
        height: height, letterSpacing: spacing);

TextStyle _mono(double size, FontWeight weight, Color color) =>
    GoogleFonts.jetBrainsMono(fontSize: size, fontWeight: weight, color: color);

Widget _gradientText(String text, TextStyle style, {TextAlign? textAlign}) => ShaderMask(
  blendMode: BlendMode.srcIn,
  shaderCallback: (b) => _gradText.createShader(b),
  child: Text(text, textAlign: textAlign, style: style.copyWith(color: Colors.white)),
);

Future<void> _openUrl(String url) async {
  final uri = Uri.parse(url);
  if (await canLaunchUrl(uri)) await launchUrl(uri);
}

// ── Logo ──────────────────────────────────────────────────────────────────────

class _Logo extends StatelessWidget {
  final double size;
  const _Logo({this.size = 28});

  @override
  Widget build(BuildContext context) =>
      SizedBox(width: size, height: size, child: CustomPaint(painter: _LogoPainter()));
}

class _LogoPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size sz) {
    final s = sz.width / 100;

    final hex = Path()
      ..moveTo(50 * s, 4 * s)
      ..lineTo(92 * s, 28 * s)
      ..lineTo(92 * s, 72 * s)
      ..lineTo(50 * s, 96 * s)
      ..lineTo(8 * s, 72 * s)
      ..lineTo(8 * s, 28 * s)
      ..close();

    canvas.drawPath(
      hex,
      Paint()
        ..shader = const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFF3B82F6), Color(0xFF1E40AF)],
        ).createShader(Rect.fromLTWH(0, 0, sz.width, sz.height)),
    );

    canvas.drawPath(
      Path()
        ..moveTo(22 * s, 74 * s)
        ..lineTo(36 * s, 62 * s)
        ..lineTo(50 * s, 68 * s)
        ..lineTo(62 * s, 48 * s)
        ..lineTo(78 * s, 40 * s),
      Paint()
        ..color = Colors.white
        ..strokeWidth = 3.2 * s
        ..style = PaintingStyle.stroke
        ..strokeJoin = StrokeJoin.miter
        ..strokeCap = StrokeCap.butt,
    );

    canvas.drawRect(
      Rect.fromLTWH(47 * s, 65 * s, 6 * s, 6 * s),
      Paint()..color = const Color(0xFFDC2626),
    );
  }

  @override
  bool shouldRepaint(covariant CustomPainter _) => false;
}

// ── Navigation bar ────────────────────────────────────────────────────────────

class _NavBar extends StatelessWidget {
  final VoidCallback onFeatures;
  final VoidCallback onHow;
  final VoidCallback onSelfHost;
  const _NavBar({required this.onFeatures, required this.onHow, required this.onSelfHost});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isDark = theme.brightness == Brightness.dark;
    final wide = MediaQuery.sizeOf(context).width >= _kBreak;

    return Container(
      height: _kNavH,
      decoration: BoxDecoration(
        color: isDark
            ? theme.scaffoldBackgroundColor.withValues(alpha: 0.95)
            : Colors.white.withValues(alpha: 0.92),
        border: Border(bottom: BorderSide(color: theme.dividerColor)),
      ),
      child: _Shell(
        child: Row(
          children: [
            const _Logo(size: 28),
            const SizedBox(width: 10),
            _LogoWordmark(theme),
            if (wide) ...[
              const SizedBox(width: 32),
              _NavLink('Features', onFeatures, theme),
              _NavLink('How it works', onHow, theme),
              _NavLink('Self-host', onSelfHost, theme),
              _NavLink('GitHub', () => _openUrl(_kGH), theme),
            ],
            const Spacer(),
            TextButton(
              onPressed: () => context.go('/login'),
              style: TextButton.styleFrom(
                foregroundColor: theme.colorScheme.onSurface.withValues(alpha: 0.65),
              ),
              child: const Text('Sign in'),
            ),
            const SizedBox(width: 8),
            ElevatedButton(
              style: ElevatedButton.styleFrom(
                minimumSize: const Size(88, 36),
                padding: const EdgeInsets.symmetric(horizontal: 16),
              ),
              onPressed: () => context.go('/register'),
              child: const Text('Start free'),
            ),
          ],
        ),
      ),
    );
  }
}

class _LogoWordmark extends StatelessWidget {
  final ThemeData theme;
  const _LogoWordmark(this.theme);

  @override
  Widget build(BuildContext context) {
    final s = _inter(18, FontWeight.w800, theme.colorScheme.onSurface, spacing: -0.02);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Text('View', style: s),
        ShaderMask(
          blendMode: BlendMode.srcIn,
          shaderCallback: (b) => _gradLogoText.createShader(b),
          child: Text('Trip', style: s.copyWith(color: Colors.white)),
        ),
      ],
    );
  }
}

class _NavLink extends StatelessWidget {
  final String label;
  final VoidCallback onTap;
  final ThemeData theme;
  const _NavLink(this.label, this.onTap, this.theme);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(right: 24),
      child: TextButton(
        onPressed: onTap,
        style: TextButton.styleFrom(
          foregroundColor: theme.colorScheme.onSurface.withValues(alpha: 0.6),
          textStyle: _inter(14, FontWeight.w500, Colors.transparent),
        ),
        child: Text(label),
      ),
    );
  }
}

// ── Hero ──────────────────────────────────────────────────────────────────────

class _HeroSection extends StatelessWidget {
  const _HeroSection();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      color: theme.scaffoldBackgroundColor,
      child: _Shell(
        padding: const EdgeInsets.fromLTRB(32, 80, 32, 64),
        child: LayoutBuilder(
          builder: (_, c) => c.maxWidth >= _kBreak
              ? _HeroWide(theme)
              : _HeroNarrow(theme),
        ),
      ),
    );
  }
}

class _HeroWide extends StatelessWidget {
  final ThemeData theme;
  const _HeroWide(this.theme);

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        Expanded(flex: 11, child: _HeroText(theme, centered: false)),
        const SizedBox(width: 56),
        Expanded(flex: 10, child: _HeroVis(theme)),
      ],
    );
  }
}

class _HeroNarrow extends StatelessWidget {
  final ThemeData theme;
  const _HeroNarrow(this.theme);

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        _HeroVis(theme),
        const SizedBox(height: 48),
        _HeroText(theme, centered: true),
      ],
    );
  }
}

class _HeroText extends StatelessWidget {
  final ThemeData theme;
  final bool centered;
  const _HeroText(this.theme, {required this.centered});

  @override
  Widget build(BuildContext context) {
    final fg1 = theme.colorScheme.onSurface;
    final fg2 = theme.colorScheme.onSurfaceVariant;
    final align = centered ? TextAlign.center : TextAlign.left;
    final cross = centered ? CrossAxisAlignment.center : CrossAxisAlignment.start;

    return Column(
      crossAxisAlignment: cross,
      children: [
        // Eyebrow pill
        Container(
          padding: const EdgeInsets.fromLTRB(5, 5, 12, 5),
          decoration: BoxDecoration(
            color: theme.cardColor,
            border: Border.all(color: theme.dividerColor),
            borderRadius: BorderRadius.circular(999),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  gradient: _gradBlue,
                  borderRadius: BorderRadius.circular(999),
                ),
                // The release badge, not a diagnostic readout: it names the
                // build a visitor would download. The pair of versions lives in
                // this page's footer, where it fits (#275).
                child: Text(kClientVersion,
                    style: _mono(10, FontWeight.w700, Colors.white)
                        .copyWith(letterSpacing: 0.05)),
              ),
              const SizedBox(width: 8),
              Text('Self-hosted · open source · Docker',
                  style: _mono(12, FontWeight.w500, fg2)),
            ],
          ),
        ),
        const SizedBox(height: 20),

        // H1 — two lines
        Text('Your adventure,',
            textAlign: align,
            style: _inter(64, FontWeight.w800, fg1, height: 1.02, spacing: -0.035)),
        SizedBox(
          width: double.infinity,
          child: _gradientText(
            'stitched end-to-end.',
            _inter(64, FontWeight.w800, Colors.white, height: 1.02, spacing: -0.035),
            textAlign: centered ? TextAlign.center : TextAlign.start,
          ),
        ),
        const SizedBox(height: 20),

        // Lead
        Text(
          'Merge Strava rides, connecting flights and trains, and pinned memories '
          'into one continuous journey. Built for cyclists and long-distance travellers '
          'who want the whole arc on a single map.',
          textAlign: align,
          style: _inter(19, FontWeight.w400, fg2, height: 1.45),
        ),
        const SizedBox(height: 32),

        // CTAs
        Wrap(
          alignment: centered ? WrapAlignment.center : WrapAlignment.start,
          spacing: 12,
          runSpacing: 12,
          children: [
            ElevatedButton.icon(
              style: ElevatedButton.styleFrom(
                minimumSize: const Size(152, 48),
                padding: const EdgeInsets.symmetric(horizontal: 20),
                textStyle: _inter(15, FontWeight.w600, Colors.transparent),
              ),
              onPressed: () => context.go('/login'),
              icon: const Icon(Icons.rocket_launch_outlined, size: 18),
              label: const Text('Try the demo'),
            ),
            OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                foregroundColor: fg1,
                side: BorderSide(color: theme.dividerColor),
                minimumSize: const Size(180, 48),
                padding: const EdgeInsets.symmetric(horizontal: 20),
                textStyle: _inter(15, FontWeight.w600, Colors.transparent),
              ),
              onPressed: () => _openUrl(_kGH),
              icon: const Icon(Icons.code, size: 18),
              label: const Text('Self-host on Docker'),
            ),
          ],
        ),
        const SizedBox(height: 32),

        // Meta stats
        Container(
          padding: const EdgeInsets.only(top: 24),
          decoration: BoxDecoration(
            border: Border(top: BorderSide(color: theme.dividerColor)),
          ),
          child: Wrap(
            spacing: 24,
            runSpacing: 12,
            children: [
              _MetaStat('847', 'km', 'avg journey length', theme),
              _MetaStat('18', '', 'activities / project', theme),
              _MetaStat('0', '€', 'MIT · forever', theme),
            ],
          ),
        ),
      ],
    );
  }
}

class _MetaStat extends StatelessWidget {
  final String value;
  final String unit;
  final String label;
  final ThemeData theme;
  const _MetaStat(this.value, this.unit, this.label, this.theme);

  @override
  Widget build(BuildContext context) {
    final fg1 = theme.colorScheme.onSurface;
    final fg2 = theme.colorScheme.onSurfaceVariant;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.baseline,
          textBaseline: TextBaseline.alphabetic,
          children: [
            Text(value,
                style: _inter(18, FontWeight.w700, fg1)
                    .copyWith(fontFeatures: [const FontFeature.tabularFigures()])),
            if (unit.isNotEmpty) ...[
              const SizedBox(width: 2),
              Text(unit, style: _inter(14, FontWeight.w400, fg2)),
            ],
          ],
        ),
        Text(label, style: _mono(12, FontWeight.w500, fg2)),
      ],
    );
  }
}

// ── Hero visual — app window mock ─────────────────────────────────────────────

class _HeroVis extends StatelessWidget {
  final ThemeData theme;
  const _HeroVis(this.theme);

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: theme.cardColor,
        border: Border.all(color: theme.dividerColor),
        borderRadius: BorderRadius.circular(16),
        boxShadow: [
          BoxShadow(
            color: const Color(0xFF0D6EFD).withValues(alpha: 0.22),
            blurRadius: 60,
            spreadRadius: -8,
            offset: const Offset(0, 24),
          ),
          BoxShadow(
            color: const Color(0xFF0F2236).withValues(alpha: 0.06),
            blurRadius: 8,
            offset: const Offset(0, 2),
          ),
        ],
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          _WindowBar(theme),
          _MapView(theme),
          _ElevationStrip(theme),
        ],
      ),
    );
  }
}

class _WindowBar extends StatelessWidget {
  final ThemeData theme;
  const _WindowBar(this.theme);

  @override
  Widget build(BuildContext context) {
    final bg = theme.brightness == Brightness.dark
        ? const Color(0xFF142031)
        : const Color(0xFFEFF3F8);
    return Container(
      height: 32,
      color: bg,
      padding: const EdgeInsets.symmetric(horizontal: 12),
      child: Row(
        children: [
          _TrafficDot(const Color(0xFFEF4444)),
          const SizedBox(width: 6),
          _TrafficDot(const Color(0xFFF59E0B)),
          const SizedBox(width: 6),
          _TrafficDot(const Color(0xFF16A34A)),
          const SizedBox(width: 12),
          Text('traxjourney.com / camino-portugués',
              style: _mono(11, FontWeight.w500, theme.colorScheme.onSurfaceVariant)),
        ],
      ),
    );
  }
}

class _TrafficDot extends StatelessWidget {
  final Color color;
  const _TrafficDot(this.color);

  @override
  Widget build(BuildContext context) =>
      Container(width: 10, height: 10, decoration: BoxDecoration(color: color, shape: BoxShape.circle));
}

class _MapView extends StatelessWidget {
  final ThemeData theme;
  const _MapView(this.theme);

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 280,
      child: Stack(
        fit: StackFit.expand,
        children: [
          // Map canvas
          CustomPaint(painter: _HeroMapPainter()),

          // City labels
          Positioned(left: 24, bottom: 36,
              child: _MapLabel('Porto')),
          Positioned(left: 0.48 * MediaQuery.sizeOf(context).width, bottom: 64,
              child: _MapLabel('Barcelos')),
          Positioned(right: 8, top: 48,
              child: _MapLabel('Santiago')),

          // Memory card
          Positioned(
            left: 0.32 * MediaQuery.sizeOf(context).width,
            top: 40,
            child: _MemoryCard(theme),
          ),
        ],
      ),
    );
  }
}

class _MapLabel extends StatelessWidget {
  final String text;
  const _MapLabel(this.text);

  @override
  Widget build(BuildContext context) {
    return Text(
      text,
      style: const TextStyle(
        fontFamily: 'Inter',
        fontSize: 11,
        fontWeight: FontWeight.w600,
        color: Color(0xFF0F172A),
        letterSpacing: 0.06,
        shadows: [Shadow(blurRadius: 4, color: Colors.white)],
      ),
    );
  }
}

class _MemoryCard extends StatelessWidget {
  final ThemeData theme;
  const _MemoryCard(this.theme);

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 148,
      padding: const EdgeInsets.all(8),
      decoration: BoxDecoration(
        color: theme.cardColor,
        border: Border.all(color: theme.dividerColor),
        borderRadius: BorderRadius.circular(10),
        boxShadow: const [
          BoxShadow(
            color: Color(0x380F2236),
            blurRadius: 24,
            offset: Offset(0, 8),
          ),
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            height: 60,
            decoration: BoxDecoration(
              gradient: const LinearGradient(
                colors: [Color(0xFFF59E0B), Color(0xFFB45309)],
              ),
              borderRadius: BorderRadius.circular(6),
            ),
          ),
          const SizedBox(height: 6),
          Text('Ponte de Lima café',
              style: _inter(12, FontWeight.w600, theme.colorScheme.onSurface)),
          Text('day 2 · 14:12',
              style: _mono(10, FontWeight.w500, theme.colorScheme.onSurfaceVariant)),
        ],
      ),
    );
  }
}

// Map painter — replicates the SVG route from welcome.html
class _HeroMapPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size sz) {
    final w = sz.width;
    final h = sz.height;

    // Map background
    final bgRect = Rect.fromLTWH(0, 0, w, h);
    canvas.drawRect(
      bgRect,
      Paint()
        ..shader = const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFFD6E3EE), Color(0xFFB8CCE0)],
        ).createShader(bgRect),
    );

    // Grid overlay
    final gp = Paint()
      ..color = Colors.white.withValues(alpha: 0.35)
      ..strokeWidth = 1;
    for (double x = 0; x < w; x += 40) { canvas.drawLine(Offset(x, 0), Offset(x, h), gp); }
    for (double y = 0; y < h; y += 40) { canvas.drawLine(Offset(0, y), Offset(w, y), gp); }

    // Subtle terrain blobs
    canvas.drawCircle(Offset(w * 0.25, h * 0.65),
        w * 0.22, Paint()..color = const Color(0xFF66BB6A).withValues(alpha: 0.18));
    canvas.drawCircle(Offset(w * 0.70, h * 0.35),
        w * 0.20, Paint()..color = const Color(0xFF4FC3F7).withValues(alpha: 0.18));

    final routePaint = Paint()
      ..color = const Color(0xFFFC4C02)
      ..strokeWidth = 3.5
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;

    // Route 1: Porto → Barcelos
    // SVG: M 60 280 Q 130 250 200 265 T 330 230  (viewBox 600×360)
    final r1 = Path()
      ..moveTo(w * 0.10, h * 0.78)
      ..quadraticBezierTo(w * 0.22, h * 0.69, w * 0.33, h * 0.74)
      ..quadraticBezierTo(w * 0.44, h * 0.78, w * 0.55, h * 0.64);
    canvas.drawPath(r1, routePaint);

    // Connector (dashed): Barcelos → Braga
    _dashedLine(canvas, Offset(w * 0.55, h * 0.64), Offset(w * 0.68, h * 0.46),
        const Color(0xFF94A3B8), 2);

    // Route 2: Braga → Santiago
    final r2 = Path()
      ..moveTo(w * 0.68, h * 0.46)
      ..quadraticBezierTo(w * 0.77, h * 0.40, w * 0.85, h * 0.50)
      ..quadraticBezierTo(w * 0.91, h * 0.58, w * 0.925, h * 0.33);
    canvas.drawPath(r2, routePaint);

    // Start dot (Porto) — cobalt
    _dot(canvas, Offset(w * 0.10, h * 0.78), const Color(0xFF0D6EFD), 6);
    // End dot (Santiago) — crimson
    _dot(canvas, Offset(w * 0.925, h * 0.33), const Color(0xFFDC2626), 6);

    // Memory / stop pins
    _pin(canvas, Offset(w * 0.32, h * 0.72));
    _pin(canvas, Offset(w * 0.68, h * 0.46));
  }

  void _dot(Canvas canvas, Offset c, Color color, double r) {
    canvas.drawCircle(c, r, Paint()..color = color);
    canvas.drawCircle(c, r,
        Paint()
          ..color = Colors.white
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2);
  }

  void _pin(Canvas canvas, Offset c) {
    canvas.drawCircle(c, 6, Paint()..color = const Color(0xFFDC2626));
    canvas.drawCircle(c, 6,
        Paint()
          ..color = Colors.white
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2);
  }

  void _dashedLine(Canvas canvas, Offset a, Offset b, Color color, double sw) {
    final dir = b - a;
    final len = dir.distance;
    final n = dir / len;
    const dash = 5.0, gap = 5.0;
    double d = 0;
    bool draw = true;
    final p = Paint()..color = color..strokeWidth = sw..strokeCap = StrokeCap.butt;
    while (d < len) {
      final seg = (draw ? dash : gap).clamp(0.0, len - d);
      if (draw) canvas.drawLine(a + n * d, a + n * (d + seg), p);
      d += seg;
      draw = !draw;
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter _) => false;
}

class _ElevationStrip extends StatelessWidget {
  final ThemeData theme;
  const _ElevationStrip(this.theme);

  @override
  Widget build(BuildContext context) {
    final fg1 = theme.colorScheme.onSurface;
    final fg2 = theme.colorScheme.onSurfaceVariant;
    return Container(
      height: 72,
      color: theme.cardColor,
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Text('Elevation', style: _inter(11, FontWeight.w600, fg1)),
              const Spacer(),
              Text('456 km · ↑ 5 218 m', style: _mono(11, FontWeight.w500, fg2)),
            ],
          ),
          const SizedBox(height: 4),
          Expanded(child: CustomPaint(painter: _ElevPainter())),
        ],
      ),
    );
  }
}

class _ElevPainter extends CustomPainter {
  static const _pts = [0.80, 0.60, 0.65, 0.35, 0.45, 0.20, 0.40, 0.30, 0.55, 0.45, 0.70];

  @override
  void paint(Canvas canvas, Size sz) {
    final w = sz.width;
    final h = sz.height;
    final segW = w / (_pts.length - 1);

    final line = Path();
    for (int i = 0; i < _pts.length; i++) {
      final x = i * segW;
      final y = h * (1 - _pts[i] * 0.85);
      if (i == 0) {
        line.moveTo(x, y);
      } else {
        final px = (i - 1) * segW;
        final py = h * (1 - _pts[i - 1] * 0.85);
        line.cubicTo(px + segW * 0.4, py, x - segW * 0.4, y, x, y);
      }
    }

    final fill = Path.from(line)
      ..lineTo(w, h)
      ..lineTo(0, h)
      ..close();

    canvas.drawPath(
      fill,
      Paint()
        ..shader = LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [
            const Color(0xFF0D6EFD).withValues(alpha: 0.3),
            const Color(0xFF0D6EFD).withValues(alpha: 0.0),
          ],
        ).createShader(Rect.fromLTWH(0, 0, w, h)),
    );
    canvas.drawPath(
      line,
      Paint()
        ..color = const Color(0xFF0D6EFD)
        ..strokeWidth = 1.8
        ..style = PaintingStyle.stroke
        ..strokeCap = StrokeCap.round
        ..strokeJoin = StrokeJoin.round,
    );
  }

  @override
  bool shouldRepaint(covariant CustomPainter _) => false;
}

// ── Features section ──────────────────────────────────────────────────────────

class _FeaturesSection extends StatelessWidget {
  final GlobalKey sectionKey;
  const _FeaturesSection({required this.sectionKey});

  static const _feats = [
    (false, Icons.sync_rounded,      'One-click Strava sync',
     'OAuth in 30 seconds. Pulls rides, runs, hikes with filters by date and type. '
     'Tokens refresh automatically; activities already added are flagged so you don\'t duplicate.'),
    (true,  Icons.location_on_outlined, 'Pinned memories',
     'Drop a photo, note, or video on any point of your route. Memories live on '
     'the timeline and the map — click a pin to jump, click the map to scroll the story.'),
    (false, Icons.flight_rounded,    'Transport gaps, solved',
     'Add a flight, train, bus, or ferry between rides. Great-circle arcs and SLERP '
     'interpolation render a plausible path on the map — never a straight line through mountains.'),
    (false, Icons.show_chart_rounded, 'Hover-synced elevation',
     'LTTB-downsampled profile across every activity. Hover the chart → a red dot walks '
     'the map. Click the map → the chart cursor snaps. 56× faster than raw data.'),
    (false, Icons.download_rounded,  'One GPX, everywhere',
     'Export the stitched journey as a single GPX — ready for Garmin, Komoot, RideWithGPS, '
     'or printing. Transport segments become interpolated trackpoints.'),
    (true,  Icons.lock_outlined,     'Your data, your box',
     'Self-host in Docker on a Synology NAS or any Linux box. Bcrypt passwords, JWT auth, '
     'SQLite out of the box. No analytics, no third-party calls besides Strava.'),
  ];

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final fg1 = theme.colorScheme.onSurface;
    final fg2 = theme.colorScheme.onSurfaceVariant;

    return Container(
      key: sectionKey,
      color: theme.scaffoldBackgroundColor,
      child: Column(
        children: [
          Divider(height: 1, color: theme.dividerColor),
          _Shell(
            padding: const EdgeInsets.fromLTRB(32, 96, 32, 96),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Features',
                    style: _inter(12, FontWeight.w600,
                        theme.colorScheme.primary).copyWith(letterSpacing: 0.12)),
                const SizedBox(height: 10),
                Text(
                  'One map. Every kilometre.\nIncluding the ones you didn\'t pedal.',
                  style: _inter(40, FontWeight.w800, fg1, height: 1.1, spacing: -0.025),
                ),
                const SizedBox(height: 12),
                Text(
                  'Most trip trackers drop out the moment you board a train. $kAppName fills the '
                  'gaps — great-circle flight arcs, SLERP-smoothed ferry paths, and dashed '
                  'transport connectors — so your journey line is unbroken from door to door.',
                  style: _inter(17, FontWeight.w400, fg2, height: 1.5),
                ),
                const SizedBox(height: 48),
                LayoutBuilder(
                  builder: (_, c) {
                    final wide = c.maxWidth >= _kBreak;
                    if (wide) {
                      return Column(
                        children: [
                          _FeatRow(_feats.sublist(0, 3), theme),
                          const SizedBox(height: 20),
                          _FeatRow(_feats.sublist(3, 6), theme),
                        ],
                      );
                    }
                    return Column(
                      children: [
                        for (int i = 0; i < _feats.length; i++) ...[
                          if (i > 0) const SizedBox(height: 16),
                          _FeatCard(_feats[i], theme),
                        ],
                      ],
                    );
                  },
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _FeatRow extends StatelessWidget {
  final List<(bool, IconData, String, String)> feats;
  final ThemeData theme;
  const _FeatRow(this.feats, this.theme);

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (int i = 0; i < feats.length; i++) ...[
          if (i > 0) const SizedBox(width: 20),
          Expanded(child: _FeatCard(feats[i], theme)),
        ],
      ],
    );
  }
}

class _FeatCard extends StatelessWidget {
  final (bool, IconData, String, String) feat;
  final ThemeData theme;
  const _FeatCard(this.feat, this.theme);

  @override
  Widget build(BuildContext context) {
    final (isRed, icon, title, body) = feat;
    final fg1 = theme.colorScheme.onSurface;
    final fg2 = theme.colorScheme.onSurfaceVariant;

    return Container(
      padding: const EdgeInsets.all(24),
      decoration: BoxDecoration(
        color: theme.cardColor,
        border: Border.all(color: theme.dividerColor),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Metallic gradient icon box
          Container(
            width: 40,
            height: 40,
            decoration: BoxDecoration(
              gradient: isRed ? _gradRed : _gradBlue,
              borderRadius: BorderRadius.circular(8),
              boxShadow: [
                BoxShadow(
                  color: (isRed ? const Color(0xFFEF4444) : const Color(0xFF0D6EFD))
                      .withValues(alpha: 0.45),
                  blurRadius: 20,
                  offset: const Offset(0, 8),
                ),
              ],
            ),
            child: Icon(icon, size: 20, color: Colors.white),
          ),
          const SizedBox(height: 16),
          Text(title, style: _inter(17, FontWeight.w700, fg1)),
          const SizedBox(height: 6),
          Text(body, style: _inter(14, FontWeight.w400, fg2, height: 1.55)),
        ],
      ),
    );
  }
}

// ── How it works ──────────────────────────────────────────────────────────────

class _HowItWorksSection extends StatelessWidget {
  final GlobalKey sectionKey;
  const _HowItWorksSection({required this.sectionKey});

  static const _steps = [
    ('01', 'Connect Strava',
     'OAuth once. $kAppName pulls your activity feed with type and date filters. '
     'Your tokens live in your own database — we never see them.'),
    ('02', 'Stitch & annotate',
     'Drag rides into order, add a connecting flight or train between gaps, '
     'drop memory pins on the moments that mattered.'),
    ('03', 'Share or export',
     'Public share link for friends, or one-click GPX for your device. View mode '
     'flips to satellite basemap for the full glossy walkthrough.'),
  ];

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final fg1 = theme.colorScheme.onSurface;

    return Container(
      key: sectionKey,
      color: theme.scaffoldBackgroundColor,
      child: Column(
        children: [
          Divider(height: 1, color: theme.dividerColor),
          _Shell(
            padding: const EdgeInsets.fromLTRB(32, 96, 32, 96),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('How it works',
                    style: _inter(12, FontWeight.w600,
                        theme.colorScheme.primary).copyWith(letterSpacing: 0.12)),
                const SizedBox(height: 10),
                Text('From Strava dump to shareable journey in three steps.',
                    style: _inter(40, FontWeight.w800, fg1, height: 1.1, spacing: -0.025)),
                const SizedBox(height: 48),
                LayoutBuilder(builder: (_, c) {
                  final wide = c.maxWidth >= _kBreak;
                  if (wide) {
                    return Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        for (int i = 0; i < _steps.length; i++) ...[
                          if (i > 0) ...[
                            const SizedBox(width: 4),
                            Padding(
                              padding: const EdgeInsets.only(top: 24),
                              child: Text('→',
                                  style: _inter(20, FontWeight.w400, theme.dividerColor)),
                            ),
                            const SizedBox(width: 4),
                          ],
                          Expanded(child: _StepCard(_steps[i], theme)),
                        ],
                      ],
                    );
                  }
                  return Column(
                    children: [
                      for (int i = 0; i < _steps.length; i++) ...[
                        if (i > 0) const SizedBox(height: 16),
                        _StepCard(_steps[i], theme),
                      ],
                    ],
                  );
                }),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _StepCard extends StatelessWidget {
  final (String, String, String) step;
  final ThemeData theme;
  const _StepCard(this.step, this.theme);

  @override
  Widget build(BuildContext context) {
    final (num, title, body) = step;
    final fg1 = theme.colorScheme.onSurface;
    final fg2 = theme.colorScheme.onSurfaceVariant;

    return Container(
      padding: const EdgeInsets.all(28),
      decoration: BoxDecoration(
        color: theme.cardColor,
        border: Border.all(color: theme.dividerColor),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ShaderMask(
            blendMode: BlendMode.srcIn,
            shaderCallback: (b) => _gradBlue.createShader(b),
            child: Text(num,
                style: _mono(48, FontWeight.w800, Colors.white)
                    .copyWith(letterSpacing: -0.04, height: 1)),
          ),
          const SizedBox(height: 10),
          Text(title, style: _inter(16, FontWeight.w700, fg1)),
          const SizedBox(height: 6),
          Text(body, style: _inter(13.5, FontWeight.w400, fg2, height: 1.55)),
        ],
      ),
    );
  }
}

// ── Showcase (always dark) ────────────────────────────────────────────────────

class _ShowcaseSection extends StatelessWidget {
  const _ShowcaseSection();

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [_dBg, _dCard],
        ),
      ),
      child: Column(
        children: [
          Container(height: 1, color: Colors.transparent),
          Stack(
            children: [
              // Grid overlay
              Positioned.fill(child: CustomPaint(painter: _GridPainter())),
              _Shell(
                padding: const EdgeInsets.fromLTRB(32, 96, 32, 96),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Live project',
                        style: _inter(12, FontWeight.w600, const Color(0xFF60A5FA))
                            .copyWith(letterSpacing: 0.12)),
                    const SizedBox(height: 10),
                    Text('Camino Portugués · 18 activities · 4 connecting segments.',
                        style: _inter(40, FontWeight.w800, const Color(0xFFF1F5F9),
                            height: 1.1, spacing: -0.025)),
                    const SizedBox(height: 12),
                    Text(
                      'A real trip stitched in $kAppName: eight rides from Porto to Santiago, '
                      'train to Braga, ferry across the Minho, and 12 photo memories pinned '
                      'along the way.',
                      style: _inter(17, FontWeight.w400, _dFgSub, height: 1.5),
                    ),
                    const SizedBox(height: 56),
                    LayoutBuilder(builder: (_, c) {
                      final wide = c.maxWidth >= _kBreak;
                      if (wide) {
                        return Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: const [
                            Expanded(child: _ShowStats()),
                            SizedBox(width: 40),
                            Expanded(child: _ShowPanel()),
                          ],
                        );
                      }
                      return const Column(
                        children: [_ShowStats(), SizedBox(height: 24), _ShowPanel()],
                      );
                    }),
                  ],
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _GridPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size sz) {
    final p = Paint()
      ..color = Colors.white.withValues(alpha: 0.03)
      ..strokeWidth = 1;
    for (double x = 0; x < sz.width; x += 40) {
      canvas.drawLine(Offset(x, 0), Offset(x, sz.height), p);
    }
    for (double y = 0; y < sz.height; y += 40) {
      canvas.drawLine(Offset(0, y), Offset(sz.width, y), p);
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter _) => false;
}

class _ShowStats extends StatelessWidget {
  const _ShowStats();

  static const _stats = [
    ('Total distance',  '847.3', 'km',  '18 activities merged'),
    ('Elevation gain',  '12 480', 'm',  'across coastal + inland'),
    ('Moving time',     '47:22', 'h',   '11 riding days'),
    ('Memories pinned', '27', '',       'photos · notes · waypoints'),
  ];

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 16,
      runSpacing: 16,
      children: [
        for (final s in _stats)
          SizedBox(
            width: 200,
            child: _ShowStat(s.$1, s.$2, s.$3, s.$4),
          ),
      ],
    );
  }
}

class _ShowStat extends StatelessWidget {
  final String key_;
  final String val;
  final String unit;
  final String desc;
  const _ShowStat(this.key_, this.val, this.unit, this.desc);

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.04),
        border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(key_,
              style: _inter(11, FontWeight.w600, _dFgSub).copyWith(letterSpacing: 0.1)),
          const SizedBox(height: 6),
          Row(
            crossAxisAlignment: CrossAxisAlignment.baseline,
            textBaseline: TextBaseline.alphabetic,
            children: [
              Text(val,
                  style: _inter(32, FontWeight.w700, _dFg, spacing: -0.02)
                      .copyWith(fontFeatures: [const FontFeature.tabularFigures()])),
              if (unit.isNotEmpty) ...[
                const SizedBox(width: 4),
                Text(unit, style: _inter(16, FontWeight.w500, _dFgHint)),
              ],
            ],
          ),
          const SizedBox(height: 8),
          Text(desc, style: _mono(12, FontWeight.w500, const Color(0xFF22C55E))),
        ],
      ),
    );
  }
}

class _ShowPanel extends StatelessWidget {
  const _ShowPanel();

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: _dCard,
        border: Border.all(color: _dBord),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        children: [
          // Panel header
          Container(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
            decoration: const BoxDecoration(
              border: Border(bottom: BorderSide(color: _dBord)),
            ),
            child: Row(
              children: [
                Container(
                  width: 8, height: 8,
                  decoration: BoxDecoration(
                    color: const Color(0xFF22C55E),
                    shape: BoxShape.circle,
                    boxShadow: [BoxShadow(
                      color: const Color(0xFF22C55E).withValues(alpha: 0.35),
                      blurRadius: 0, spreadRadius: 4,
                    )],
                  ),
                ),
                const SizedBox(width: 8),
                Text('Porto → Santiago · live',
                    style: _inter(13, FontWeight.w600, _dFg)),
              ],
            ),
          ),
          // Rows
          Padding(
            padding: const EdgeInsets.all(8),
            child: Column(
              children: const [
                _PanelRow(false, Icons.directions_bike_rounded, kColorRide, 'Porto → Barcelos', '54.2 km', true),
                SizedBox(height: 4),
                _PanelRow(false, Icons.train_rounded, Color(0xFF94A3B8), 'Barcelos → Braga · train', '23.1 km', false),
                SizedBox(height: 4),
                _PanelRow(false, Icons.directions_bike_rounded, kColorRide, 'Braga → Ponte de Lima', '38.7 km', false),
                SizedBox(height: 4),
                _PanelRow(true, Icons.location_on_outlined, Color(0xFFEF4444), 'Café do Peregrino · photo', 'day 2', false),
                SizedBox(height: 4),
                _PanelRow(false, Icons.hiking_rounded, kColorHike, 'Santiago approach', '12.4 km', false),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _PanelRow extends StatelessWidget {
  final bool photo;
  final IconData icon;
  final Color color;
  final String name;
  final String meta;
  final bool selected;
  const _PanelRow(this.photo, this.icon, this.color, this.name, this.meta, this.selected);

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
      decoration: BoxDecoration(
        color: selected ? const Color(0xFF60A5FA).withValues(alpha: 0.10) : Colors.transparent,
        border: Border.all(
          color: selected ? const Color(0xFF3B82F6) : Colors.transparent,
        ),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        children: [
          Container(
            width: 28, height: 28,
            decoration: BoxDecoration(
              color: color.withValues(alpha: photo ? 0.15 : 0.20),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Icon(icon, size: 16, color: color),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(name, style: _inter(13, FontWeight.w500, _dFg)),
          ),
          Text(meta, style: _mono(11, FontWeight.w500, _dFgHint)),
        ],
      ),
    );
  }
}

// ── Self-host / pricing section ───────────────────────────────────────────────

class _SelfHostSection extends StatelessWidget {
  final GlobalKey sectionKey;
  const _SelfHostSection({required this.sectionKey});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final fg1 = theme.colorScheme.onSurface;
    final fg2 = theme.colorScheme.onSurfaceVariant;

    return Container(
      key: sectionKey,
      color: theme.scaffoldBackgroundColor,
      child: Column(
        children: [
          Divider(height: 1, color: theme.dividerColor),
          _Shell(
            padding: const EdgeInsets.fromLTRB(32, 96, 32, 96),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Deploy',
                    style: _inter(12, FontWeight.w600,
                        theme.colorScheme.primary).copyWith(letterSpacing: 0.12)),
                const SizedBox(height: 10),
                Text('Run it yourself, or let us.',
                    style: _inter(40, FontWeight.w800, fg1,
                        height: 1.1, spacing: -0.025)),
                const SizedBox(height: 12),
                Text(
                  '$kAppName ships as a single Docker image and a Flutter client. '
                  'Self-host in five minutes, or join the hosted beta and we\'ll '
                  'keep the box warm.',
                  style: _inter(17, FontWeight.w400, fg2, height: 1.5),
                ),
                const SizedBox(height: 48),
                LayoutBuilder(builder: (_, c) {
                  final wide = c.maxWidth >= 640;
                  if (wide) {
                    return Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Expanded(child: _TierCard(false, theme)),
                        const SizedBox(width: 20),
                        Expanded(child: _TierCard(true, theme)),
                      ],
                    );
                  }
                  return Column(children: [
                    _TierCard(false, theme),
                    const SizedBox(height: 20),
                    _TierCard(true, theme),
                  ]);
                }),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _TierCard extends StatelessWidget {
  final bool featured;
  final ThemeData theme;
  const _TierCard(this.featured, this.theme);

  @override
  Widget build(BuildContext context) {
    final fg1 = theme.colorScheme.onSurface;
    final fg2 = theme.colorScheme.onSurfaceVariant;
    final primary = theme.colorScheme.primary;

    final freeFeatures = [
      'Docker + docker-compose',
      'SQLite or Postgres',
      'Google + local auth',
      'Unlimited projects, users',
      'Community support',
    ];
    final cloudFeatures = [
      'Everything in self-hosted',
      'Daily backups',
      'Share links with custom domains',
      'Background Strava sync',
      'Priority support',
    ];

    return Stack(
      clipBehavior: Clip.none,
      children: [
        Container(
          padding: const EdgeInsets.all(32),
          decoration: BoxDecoration(
            color: theme.cardColor,
            border: Border.all(
              color: featured ? primary : theme.dividerColor,
              width: featured ? 1.5 : 1,
            ),
            borderRadius: BorderRadius.circular(16),
            boxShadow: featured
                ? [BoxShadow(
                    color: primary.withValues(alpha: 0.25),
                    blurRadius: 60,
                    spreadRadius: -4,
                    offset: const Offset(0, 24),
                  )]
                : null,
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (featured) const SizedBox(height: 8),
              Text(featured ? 'Cloud' : 'Self-hosted',
                  style: _inter(20, FontWeight.w700, fg1)),
              const SizedBox(height: 12),
              Row(
                crossAxisAlignment: CrossAxisAlignment.baseline,
                textBaseline: TextBaseline.alphabetic,
                children: [
                  Text(featured ? '€4' : 'Free',
                      style: _inter(40, FontWeight.w800, fg1, spacing: -0.03)),
                  const SizedBox(width: 6),
                  Text(featured ? '/ month' : '· MIT',
                      style: _inter(14, FontWeight.w500, fg2)),
                ],
              ),
              const SizedBox(height: 4),
              Text(
                featured
                    ? 'We run the Docker, you ride the bike.'
                    : 'For makers with their own hardware.',
                style: _inter(13, FontWeight.w400, fg2),
              ),
              const SizedBox(height: 24),
              for (final f in (featured ? cloudFeatures : freeFeatures)) ...[
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: Row(
                    children: [
                      Icon(Icons.check_rounded, size: 18, color: primary),
                      const SizedBox(width: 8),
                      Text(f, style: _inter(14, FontWeight.w400, fg2)),
                    ],
                  ),
                ),
              ],
              const SizedBox(height: 24),
              SizedBox(
                width: double.infinity,
                child: featured
                    ? ElevatedButton.icon(
                        style: ElevatedButton.styleFrom(
                          minimumSize: const Size.fromHeight(44),
                        ),
                        onPressed: () => context.go('/register'),
                        icon: const Icon(Icons.rocket_launch_outlined, size: 16),
                        label: const Text('Join the beta'),
                      )
                    : OutlinedButton.icon(
                        style: OutlinedButton.styleFrom(
                          foregroundColor: fg1,
                          side: BorderSide(color: theme.dividerColor),
                          minimumSize: const Size.fromHeight(44),
                        ),
                        onPressed: () => _openUrl(_kGH),
                        icon: const Icon(Icons.download_rounded, size: 16),
                        label: const Text('Clone on GitHub'),
                      ),
              ),
            ],
          ),
        ),
        if (featured)
          Positioned(
            top: -10,
            left: 24,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
              decoration: BoxDecoration(
                gradient: _gradBlue,
                borderRadius: BorderRadius.circular(999),
                boxShadow: [
                  BoxShadow(
                    color: const Color(0xFF0D6EFD).withValues(alpha: 0.45),
                    blurRadius: 20,
                    offset: const Offset(0, 8),
                  ),
                ],
              ),
              child: Text('Hosted beta',
                  style: _inter(10, FontWeight.w700, Colors.white)
                      .copyWith(letterSpacing: 0.1)),
            ),
          ),
      ],
    );
  }
}

// ── Footer ────────────────────────────────────────────────────────────────────

class _Footer extends StatelessWidget {
  const _Footer();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Container(
      color: theme.cardColor,
      child: Column(
        children: [
          Divider(height: 1, color: theme.dividerColor),
          _Shell(
            padding: const EdgeInsets.fromLTRB(32, 48, 32, 32),
            child: LayoutBuilder(builder: (_, c) {
              final wide = c.maxWidth >= _kBreak;
              if (wide) {
                return Column(
                  children: [
                    _FootGrid(theme),
                    const SizedBox(height: 40),
                    _FootBottom(theme),
                  ],
                );
              }
              return Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _FootBrand(theme),
                  const SizedBox(height: 32),
                  _FootLinks('Product', ['Features', 'How it works', 'Self-host', 'Changelog'], theme),
                  const SizedBox(height: 24),
                  _FootLinks('Docs', ['Quick start', 'Architecture', 'API reference', 'Docker'], theme),
                  const SizedBox(height: 24),
                  _FootLinks('Community', ['GitHub', 'Strava club', 'Discussions', 'License · MIT'], theme),
                  const SizedBox(height: 32),
                  _FootBottom(theme),
                ],
              );
            }),
          ),
        ],
      ),
    );
  }
}

class _FootGrid extends StatelessWidget {
  final ThemeData theme;
  const _FootGrid(this.theme);

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(flex: 2, child: _FootBrand(theme)),
        const SizedBox(width: 32),
        Expanded(child: _FootLinks('Product',
            ['Features', 'How it works', 'Self-host', 'Changelog'], theme)),
        Expanded(child: _FootLinks('Docs',
            ['Quick start', 'Architecture', 'API reference', 'Docker'], theme)),
        Expanded(child: _FootLinks('Community',
            ['GitHub', 'Strava club', 'Discussions', 'License · MIT'], theme)),
      ],
    );
  }
}

class _FootBrand extends StatelessWidget {
  final ThemeData theme;
  const _FootBrand(this.theme);

  @override
  Widget build(BuildContext context) {
    final fg2 = theme.colorScheme.onSurfaceVariant;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(children: [
          const _Logo(size: 26),
          const SizedBox(width: 8),
          _LogoWordmark(theme),
        ]),
        const SizedBox(height: 14),
        Text(
          'Open-source multi-sport journey builder.\nMerge · Remember · Export.',
          style: _inter(13, FontWeight.w400, fg2),
        ),
      ],
    );
  }
}

class _FootLinks extends StatelessWidget {
  final String heading;
  final List<String> links;
  final ThemeData theme;
  const _FootLinks(this.heading, this.links, this.theme);

  @override
  Widget build(BuildContext context) {
    final fg2 = theme.colorScheme.onSurfaceVariant;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(heading,
            style: _inter(12, FontWeight.w600, fg2).copyWith(letterSpacing: 0.1)),
        const SizedBox(height: 14),
        for (final l in links)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: GestureDetector(
              onTap: l == 'GitHub' ? () => _openUrl(_kGH) : null,
              child: MouseRegion(
                cursor: SystemMouseCursors.click,
                child: Text(l,
                    style: _inter(14, FontWeight.w400, fg2)
                        .copyWith(decoration: TextDecoration.none)),
              ),
            ),
          ),
      ],
    );
  }
}

class _FootBottom extends StatelessWidget {
  final ThemeData theme;
  const _FootBottom(this.theme);

  @override
  Widget build(BuildContext context) {
    final fg2 = theme.colorScheme.onSurfaceVariant;
    // Wrap, not Row + Spacer: this bar is also the phone footer, where the two
    // lines never fitted side by side and silently overflowed — and naming both
    // versions here (#275) makes the left one longer still. Laid out as one
    // row when it fits, stacked when it does not.
    return Wrap(
      alignment: WrapAlignment.spaceBetween,
      spacing: 16,
      runSpacing: 8,
      children: [
        VersionText(
            prefix: '© ${DateTime.now().year} $kAppName · ',
            style: _mono(12, FontWeight.w500, fg2)),
        Text('Built on Flutter + FastAPI · Designed in slate',
            style: _mono(12, FontWeight.w500, fg2)),
      ],
    );
  }
}
