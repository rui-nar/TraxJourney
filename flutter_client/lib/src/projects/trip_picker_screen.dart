/// Which trip a .gpx opened from another app goes into (issue #368).
///
/// The file is held by [incomingGpx]. Choosing a trip records it there and
/// opens that trip, whose screen opens the GPX import dialog with the file
/// already loaded. A file refused on arrival (too large, unreadable) is
/// explained here instead, since this is where the hand-off lands.
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../core/project_ref.dart';
import 'incoming_gpx.dart';
import 'projects_notifier.dart';

class TripPickerScreen extends StatelessWidget {
  const TripPickerScreen({super.key, this.incoming});

  /// The holder to read; the app's [incomingGpx] unless a test passes one.
  final IncomingGpx? incoming;

  @override
  Widget build(BuildContext context) {
    final holder = incoming ?? incomingGpx;
    return ListenableBuilder(
      listenable: holder,
      builder: (context, _) => Scaffold(
        appBar: AppBar(
          title: const Text('Import a GPX file'),
          leading: IconButton(
            icon: const Icon(Icons.close),
            tooltip: 'Cancel',
            onPressed: () {
              holder.dismiss();
              context.go('/projects');
            },
          ),
        ),
        body: _body(context, holder),
      ),
    );
  }

  Widget _body(BuildContext context, IncomingGpx holder) {
    final theme = Theme.of(context);
    final file = holder.file;
    if (file == null) {
      return _Message(
        holder.refusal ?? 'There is no file waiting to be imported.',
        onBack: () {
          holder.dismiss();
          context.go('/projects');
        },
      );
    }
    final projects = context.watch<ProjectsNotifier>();
    // A viewer can't add activities, so their trips aren't offered.
    final trips =
        projects.projects.where((p) => !p.ref.isViewer).toList();
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text('Which trip should ${file.name} go into?',
            key: const ValueKey('trip_picker_prompt'),
            style: theme.textTheme.bodyLarge),
        const SizedBox(height: 12),
        if (projects.isLoading && trips.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 24),
            child: Center(child: CircularProgressIndicator()),
          )
        else if (projects.error != null && trips.isEmpty)
          Text(projects.error!,
              style: TextStyle(color: theme.colorScheme.error))
        else if (trips.isEmpty)
          Text('You have no trip to add it to yet. Create one first, '
              'then open the file again.',
              style: theme.textTheme.bodyMedium)
        else
          for (final trip in trips)
            ListTile(
              key: ValueKey('trip_picker_${trip['name']}_${trip.ref.ownerId}'),
              leading:
                  Icon(Icons.map_outlined, color: theme.colorScheme.primary),
              title: Text(trip['name'] as String? ?? 'Untitled'),
              subtitle: trip.ownerName != null && trip.ownerName!.isNotEmpty
                  ? Text('Shared by ${trip.ownerName}')
                  : null,
              onTap: () => _open(context, holder, trip.ref),
            ),
      ],
    );
  }

  void _open(BuildContext context, IncomingGpx holder, ProjectRef trip) {
    holder.chooseTrip(trip);
    context.go(
        trip.withOwner('/app?project=${Uri.encodeComponent(trip.name)}'));
  }
}

class _Message extends StatelessWidget {
  const _Message(this.text, {required this.onBack});

  final String text;
  final VoidCallback onBack;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(text,
                  key: const ValueKey('trip_picker_message'),
                  textAlign: TextAlign.center),
              const SizedBox(height: 16),
              OutlinedButton(
                onPressed: onBack,
                child: const Text('Back to trips'),
              ),
            ],
          ),
        ),
      );
}
