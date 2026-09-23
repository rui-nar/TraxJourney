/// App-wide Material 3 theme definitions.
///
/// Both light and dark variants are defined here.
/// [themeMode] is set to [ThemeMode.system] in main.dart so the OS preference
/// is respected automatically.
// TODO(settings): add a ThemeMode override in user settings so the user
// can force light/dark independent of the system preference.
library;

import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

// ── Brand seed color ───────────────────────────────────────────────────────────
const _seed = Color(0xFF0D6EFD); // TraxJourney brand blue

// ── Shared error tokens ────────────────────────────────────────────────────────
const _error = Color(0xFFEF4444);
const _errorLight = Color(0xFFDC2626);

// ── Dark palette ───────────────────────────────────────────────────────────────
const _darkBg = Color(0xFF0D1B2A);
const _darkCard = Color(0xFF1B2838);
const _darkInput = Color(0xFF0F2236);
const _darkBorder = Color(0xFF2D4A6A);
const _darkOnBg = Color(0xFFE2E8F0); // slate-200
const _darkOnCard = Color(0xFFCBD5E1); // slate-300
const _darkHint = Color(0xFF64748B); // slate-500

// ── Light palette ──────────────────────────────────────────────────────────────
const _lightBg = Color(0xFFF1F5F9); // slate-100
const _lightCard = Color(0xFFFFFFFF);
const _lightInput = Color(0xFFF8FAFC); // slate-50
const _lightBorder = Color(0xFFCBD5E1); // slate-300
const _lightOnBg = Color(0xFF1E293B); // slate-800
const _lightOnCard = Color(0xFF334155); // slate-700
const _lightHint = Color(0xFF94A3B8); // slate-400

// ── Dark theme ─────────────────────────────────────────────────────────────────
final ThemeData darkTheme = ThemeData(
  useMaterial3: true,
  brightness: Brightness.dark,
  fontFamily: GoogleFonts.inter().fontFamily,
  colorScheme: ColorScheme.fromSeed(
    seedColor: _seed,
    brightness: Brightness.dark,
    surface: _darkCard,
    onSurface: _darkOnCard,
    error: _error,
  ).copyWith(
    onSurfaceVariant: _darkHint,
  ),
  scaffoldBackgroundColor: _darkBg,
  appBarTheme: const AppBarTheme(
    backgroundColor: _darkCard,
    foregroundColor: _darkOnBg,
    elevation: 0,
    scrolledUnderElevation: 0,
    surfaceTintColor: Colors.transparent,
    shadowColor: _darkBorder,
    centerTitle: false,
    titleTextStyle: TextStyle(
        color: _darkOnBg, fontSize: 18, fontWeight: FontWeight.w600),
    shape: Border(bottom: BorderSide(color: _darkBorder, width: 1)),
  ),
  cardTheme: const CardThemeData(
    color: _darkCard,
    elevation: 0,
    shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.all(Radius.circular(12))),
    margin: EdgeInsets.zero,
  ),
  inputDecorationTheme: const InputDecorationTheme(
    filled: true,
    fillColor: _darkInput,
    border: OutlineInputBorder(
      borderRadius: BorderRadius.all(Radius.circular(8)),
      borderSide: BorderSide(color: _darkBorder),
    ),
    enabledBorder: OutlineInputBorder(
      borderRadius: BorderRadius.all(Radius.circular(8)),
      borderSide: BorderSide(color: _darkBorder),
    ),
    focusedBorder: OutlineInputBorder(
      borderRadius: BorderRadius.all(Radius.circular(8)),
      borderSide: BorderSide(color: _seed, width: 1.5),
    ),
    errorBorder: OutlineInputBorder(
      borderRadius: BorderRadius.all(Radius.circular(8)),
      borderSide: BorderSide(color: _error),
    ),
    labelStyle: TextStyle(color: _darkOnCard),
    hintStyle: TextStyle(color: _darkHint),
  ),
  elevatedButtonTheme: ElevatedButtonThemeData(
    style: ElevatedButton.styleFrom(
      backgroundColor: _seed,
      foregroundColor: Colors.white,
      minimumSize: const Size.fromHeight(44),
      shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(8))),
      textStyle:
          const TextStyle(fontWeight: FontWeight.w600, fontSize: 15),
    ),
  ),
  outlinedButtonTheme: OutlinedButtonThemeData(
    style: OutlinedButton.styleFrom(
      foregroundColor: _darkOnBg,
      side: const BorderSide(color: _darkBorder),
      minimumSize: const Size.fromHeight(44),
      shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(8))),
    ),
  ),
  tabBarTheme: const TabBarThemeData(
    labelColor: _seed,
    unselectedLabelColor: _darkHint,
    indicatorColor: _seed,
    dividerColor: _darkBorder,
  ),
  textTheme: const TextTheme(
    headlineMedium: TextStyle(
        color: _darkOnBg, fontSize: 22, fontWeight: FontWeight.w700),
    titleLarge: TextStyle(color: _darkOnBg, fontWeight: FontWeight.w600),
    bodyMedium: TextStyle(color: _darkOnCard),
    bodySmall: TextStyle(color: _darkHint),
  ),
  segmentedButtonTheme: SegmentedButtonThemeData(
    style: ButtonStyle(
      shape: const WidgetStatePropertyAll(
        RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(8))),
      ),
      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
      visualDensity: VisualDensity.compact,
      textStyle: const WidgetStatePropertyAll(
        TextStyle(fontSize: 13, fontWeight: FontWeight.w500),
      ),
    ),
  ),
  dividerTheme: const DividerThemeData(color: _darkBorder, thickness: 1),
  snackBarTheme: const SnackBarThemeData(
    backgroundColor: _darkCard,
    contentTextStyle: TextStyle(color: _darkOnBg),
  ),
);

// ── Light theme ────────────────────────────────────────────────────────────────
final ThemeData lightTheme = ThemeData(
  useMaterial3: true,
  brightness: Brightness.light,
  fontFamily: GoogleFonts.inter().fontFamily,
  colorScheme: ColorScheme.fromSeed(
    seedColor: _seed,
    brightness: Brightness.light,
    surface: _lightCard,
    onSurface: _lightOnCard,
    error: _errorLight,
  ).copyWith(
    onSurfaceVariant: _lightHint,
  ),
  scaffoldBackgroundColor: _lightBg,
  appBarTheme: const AppBarTheme(
    backgroundColor: _lightCard,
    foregroundColor: _lightOnBg,
    elevation: 0,
    scrolledUnderElevation: 0,
    surfaceTintColor: Colors.transparent,
    shadowColor: _lightBorder,
    centerTitle: false,
    titleTextStyle: TextStyle(
        color: _lightOnBg, fontSize: 18, fontWeight: FontWeight.w600),
    shape: Border(bottom: BorderSide(color: _lightBorder, width: 1)),
  ),
  cardTheme: const CardThemeData(
    color: _lightCard,
    elevation: 0,
    shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.all(Radius.circular(12))),
    margin: EdgeInsets.zero,
  ),
  inputDecorationTheme: const InputDecorationTheme(
    filled: true,
    fillColor: _lightInput,
    border: OutlineInputBorder(
      borderRadius: BorderRadius.all(Radius.circular(8)),
      borderSide: BorderSide(color: _lightBorder),
    ),
    enabledBorder: OutlineInputBorder(
      borderRadius: BorderRadius.all(Radius.circular(8)),
      borderSide: BorderSide(color: _lightBorder),
    ),
    focusedBorder: OutlineInputBorder(
      borderRadius: BorderRadius.all(Radius.circular(8)),
      borderSide: BorderSide(color: _seed, width: 1.5),
    ),
    errorBorder: OutlineInputBorder(
      borderRadius: BorderRadius.all(Radius.circular(8)),
      borderSide: BorderSide(color: _errorLight),
    ),
    labelStyle: TextStyle(color: _lightOnCard),
    hintStyle: TextStyle(color: _lightHint),
  ),
  elevatedButtonTheme: ElevatedButtonThemeData(
    style: ElevatedButton.styleFrom(
      backgroundColor: _seed,
      foregroundColor: Colors.white,
      minimumSize: const Size.fromHeight(44),
      shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(8))),
      textStyle:
          const TextStyle(fontWeight: FontWeight.w600, fontSize: 15),
    ),
  ),
  outlinedButtonTheme: OutlinedButtonThemeData(
    style: OutlinedButton.styleFrom(
      foregroundColor: _lightOnBg,
      side: const BorderSide(color: _lightBorder),
      minimumSize: const Size.fromHeight(44),
      shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(8))),
    ),
  ),
  tabBarTheme: const TabBarThemeData(
    labelColor: _seed,
    unselectedLabelColor: _lightHint,
    indicatorColor: _seed,
    dividerColor: _lightBorder,
  ),
  textTheme: const TextTheme(
    headlineMedium: TextStyle(
        color: _lightOnBg, fontSize: 22, fontWeight: FontWeight.w700),
    titleLarge: TextStyle(color: _lightOnBg, fontWeight: FontWeight.w600),
    bodyMedium: TextStyle(color: _lightOnCard),
    bodySmall: TextStyle(color: _lightHint),
  ),
  segmentedButtonTheme: SegmentedButtonThemeData(
    style: ButtonStyle(
      shape: const WidgetStatePropertyAll(
        RoundedRectangleBorder(borderRadius: BorderRadius.all(Radius.circular(8))),
      ),
      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
      visualDensity: VisualDensity.compact,
      textStyle: const WidgetStatePropertyAll(
        TextStyle(fontSize: 13, fontWeight: FontWeight.w500),
      ),
    ),
  ),
  dividerTheme: const DividerThemeData(color: _lightBorder, thickness: 1),
  snackBarTheme: const SnackBarThemeData(
    backgroundColor: _lightCard,
    contentTextStyle: TextStyle(color: _lightOnBg),
  ),
);
