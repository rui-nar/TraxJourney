import 'package:flutter/material.dart';

import 'add_speed_dial.dart';
import 'day_meta_editor.dart';
import 'encounter_dialog.dart';
import 'memory_dialog.dart';
import 'project_notifier.dart';

/// The owner-only "+" speed-dial shown on the project map in both edit and
/// view modes (never for viewers — those screens don't build it). It fans out
/// to three add shortcuts:
///   • Memory   — opens the standalone memory composer.
///   • Day info — opens the full day-metadata editor for the active day.
///   • Counter  — opens a counters-only day editor for the active day.
///
/// The two day-bound actions default to [ProjectNotifier.activeDayKey] (today
/// while the trip is active, otherwise its last day) and reuse the existing
/// day-meta launchers, picking the sheet on narrow layouts and the dialog on
/// wide ones to match the rest of the app.
Widget buildProjectAddFab(BuildContext context, ProjectNotifier notifier) {
  final useSheet = MediaQuery.sizeOf(context).width < 720;

  void openDay({required bool countersOnly}) {
    final dayKey = notifier.activeDayKey();
    if (dayKey == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('No days in this project yet.')),
      );
      return;
    }
    final keys = notifier.itemsFacet.orderedDayKeys();
    if (useSheet) {
      showDayMetaSheet(context, notifier, dayKey,
          orderedDateKeys: keys, countersOnly: countersOnly);
    } else {
      showDayMetaDialog(context, notifier, dayKey,
          orderedDateKeys: keys, countersOnly: countersOnly);
    }
  }

  return AddSpeedDial(
    actions: [
      SpeedDialAction(
        icon: Icons.photo_camera_outlined,
        label: 'Memory',
        onTap: () => showDialog<void>(
          context: context,
          useRootNavigator: true,
          builder: (_) => MemoryDialog(notifier: notifier),
        ),
      ),
      SpeedDialAction(
        icon: Icons.groups_outlined,
        label: 'Encounter',
        onTap: () => showDialog<void>(
          context: context,
          useRootNavigator: true,
          builder: (_) => EncounterDialog(
            notifier: notifier,
            initialDate: notifier.activeDayKey(),
          ),
        ),
      ),
      SpeedDialAction(
        icon: Icons.edit_calendar_outlined,
        label: 'Day info',
        onTap: () => openDay(countersOnly: false),
      ),
      SpeedDialAction(
        icon: Icons.add_chart_outlined,
        label: 'Counter',
        onTap: () => openDay(countersOnly: true),
      ),
    ],
  );
}
