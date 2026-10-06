/// People section (issue #40): a searchable directory of people met on the trip,
/// with a per-person detail sheet listing every place + date you met them.
library;

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show Clipboard, ClipboardData;

import '../billing/upgrade_sheet.dart';
import '../core/countries.dart';
import '../core/design_tokens.dart';
import '../core/picked_file_bytes.dart';
import 'encounter_dialog.dart';
import 'group_form_dialog.dart';
import 'people_search.dart';
import 'person_form_dialog.dart';
import 'project_notifier.dart';
import 'social_links_field.dart' show socialNetworkLabel;

/// Full-screen People directory for the open project.
class PeopleScreen extends StatefulWidget {
  final ProjectNotifier notifier;
  const PeopleScreen({super.key, required this.notifier});

  @override
  State<PeopleScreen> createState() => _PeopleScreenState();
}

class _PeopleScreenState extends State<PeopleScreen> {
  final _search = TextEditingController();
  bool _showGroups = false;

  @override
  void initState() {
    super.initState();
    _search.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Encounters & groups')),
      floatingActionButton: FloatingActionButton.extended(
        backgroundColor: kAccent,
        foregroundColor: Colors.white,
        onPressed: _showGroups
            ? () => showGroupFormDialog(context, widget.notifier)
            : () => showPersonFormDialog(context, widget.notifier),
        icon: Icon(_showGroups ? Icons.group_add : Icons.person_add_alt_1),
        label: Text(_showGroups ? 'Add group' : 'Add person'),
      ),
      body: AnimatedBuilder(
        animation: widget.notifier,
        builder: (context, _) {
          return Column(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(12, 12, 12, 4),
                child: SegmentedButton<bool>(
                  showSelectedIcon: false,
                  segments: const [
                    ButtonSegment(
                        value: false,
                        icon: Icon(Icons.person_outline, size: 18),
                        label: Text('People')),
                    ButtonSegment(
                        value: true,
                        icon: Icon(Icons.groups_outlined, size: 18),
                        label: Text('Groups')),
                  ],
                  selected: {_showGroups},
                  onSelectionChanged: (s) =>
                      setState(() => _showGroups = s.first),
                ),
              ),
              Expanded(
                  child: _showGroups ? _groupsBody(theme) : _peopleBody(theme)),
            ],
          );
        },
      ),
    );
  }

  Widget _peopleBody(ThemeData theme) {
    final notesByPerson =
        encounterNotesByPerson(widget.notifier.itemsFacet.items, widget.notifier.itemsFacet.people);
    final counts =
        encounterCountByPerson(widget.notifier.itemsFacet.items, widget.notifier.itemsFacet.people);
    final filtered =
        filterPeople(widget.notifier.itemsFacet.people, _search.text, notesByPerson);
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 6),
          child: TextField(
            controller: _search,
            decoration: InputDecoration(
              prefixIcon: const Icon(Icons.search),
              hintText: 'Search people, details or encounter notes',
              isDense: true,
              border: const OutlineInputBorder(),
              suffixIcon: _search.text.isEmpty
                  ? null
                  : IconButton(
                      icon: const Icon(Icons.clear),
                      onPressed: () => _search.clear(),
                    ),
            ),
          ),
        ),
        Expanded(
          child: widget.notifier.itemsFacet.people.isEmpty
              ? _empty(theme, 'No people yet',
                  'Add someone you met, or log an encounter on a day.')
              : filtered.isEmpty
                  ? _empty(theme, 'No matches', 'Try a different search.')
                  : ListView.builder(
                      itemCount: filtered.length,
                      itemBuilder: (_, i) => _PersonTile(
                        notifier: widget.notifier,
                        person: filtered[i],
                        encounterCount: counts[filtered[i]['id']] ?? 0,
                      ),
                    ),
        ),
      ],
    );
  }

  Widget _groupsBody(ThemeData theme) {
    final groups = widget.notifier.itemsFacet.groups;
    if (groups.isEmpty) {
      return _empty(theme, 'No groups yet',
          'Group people you met — e.g. a hostel crew or a family.');
    }
    final counts = memberCountByGroup(widget.notifier.itemsFacet.people);
    final encCounts = encounterCountByGroup(widget.notifier.itemsFacet.items);
    return ListView.builder(
      itemCount: groups.length,
      itemBuilder: (_, i) => _GroupTile(
        notifier: widget.notifier,
        group: groups[i],
        memberCount: counts[groups[i]['id']] ?? 0,
        encounterCount: encCounts[groups[i]['id']] ?? 0,
      ),
    );
  }

  Widget _empty(ThemeData theme, String title, String subtitle) => Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.groups_outlined, size: 48,
                  color: theme.colorScheme.onSurfaceVariant),
              const SizedBox(height: 12),
              Text(title, style: theme.textTheme.titleMedium),
              const SizedBox(height: 4),
              Text(subtitle,
                  textAlign: TextAlign.center,
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            ],
          ),
        ),
      );
}

class _PersonTile extends StatelessWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic> person;
  final int encounterCount;
  const _PersonTile({
    required this.notifier,
    required this.person,
    required this.encounterCount,
  });

  @override
  Widget build(BuildContext context) {
    final subtitleParts = <String>[
      if (encounterCount > 0)
        '$encounterCount ${encounterCount == 1 ? 'encounter' : 'encounters'}',
      if ((person['email'] as String?)?.isNotEmpty ?? false) person['email'],
    ];
    return ListTile(
      leading: PersonAvatar(notifier: notifier, person: person, radius: 22),
      title: Text(personDisplayName(person)),
      subtitle: subtitleParts.isEmpty ? null : Text(subtitleParts.join('  •  ')),
      onTap: () => showPersonDetailSheet(context, notifier, person,
          onLocationTap: (lat, lon) {
            // Close the sheet, then close PeopleScreen itself with the picked
            // point so the caller (map/activity panel) can focus it (#72).
            Navigator.of(context).pop();
            Navigator.of(context).pop({'lat': lat, 'lon': lon});
          },
          onTripShown: () {
            // Same two pops: the trip overlay is drawn on the map behind this
            // full-screen route, so leaving it up looks like nothing happened
            // (#123). The map fits its own camera to the overlay.
            Navigator.of(context).pop();
            Navigator.of(context).pop();
          }),
    );
  }
}

/// Circular avatar: the person's photo thumbnail, or their initial on an accent
/// background when there's no photo.
class PersonAvatar extends StatelessWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic> person;
  final double radius;
  const PersonAvatar({
    super.key,
    required this.notifier,
    required this.person,
    this.radius = 22,
  });

  @override
  Widget build(BuildContext context) {
    final hasPhoto = (person['avatar_photo'] as String?)?.isNotEmpty ?? false;
    final label = personDisplayName(person);
    final initial = label.isNotEmpty ? label.characters.first.toUpperCase() : '?';
    if (hasPhoto) {
      final url =
          '${notifier.apiBaseUrl}/api/people/${person['id']}/avatar/thumb';
      return CircleAvatar(
        radius: radius,
        backgroundImage: NetworkImage(url, headers: notifier.photoAuthHeaders),
      );
    }
    return CircleAvatar(
      radius: radius,
      backgroundColor: kAccentSoft,
      child: Text(initial,
          style: TextStyle(color: kAccent, fontWeight: FontWeight.w600)),
    );
  }
}

/// Show the per-person detail sheet: info + every place/date you met them.
///
/// [onLocationTap], when given, is invoked with an encounter's coordinates
/// when its place icon is tapped (issue #72); the caller decides whether/how
/// to dismiss any sheet(s) — see call sites for the pop conventions used.
///
/// [onTripShown], when given, is invoked once one of the person's Polarsteps
/// trips has been loaded onto the map overlay (issue #123) and likewise owns
/// the dismissal: the sheet alone defaults to closing itself, which is not
/// enough when a full-screen route (PeopleScreen) still covers the map.
Future<void> showPersonDetailSheet(
  BuildContext context,
  ProjectNotifier notifier,
  Map<String, dynamic> person, {
  void Function(double lat, double lon)? onLocationTap,
  VoidCallback? onTripShown,
}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (_) => _PersonDetailSheet(
      notifier: notifier,
      person: person,
      onLocationTap: onLocationTap,
      onTripShown: onTripShown,
    ),
  );
}

/// The tappable "place" leading icon for an encounter list tile (issue #72):
/// tappable only when [onLocationTap] is given and the encounter has both
/// coordinates; otherwise a plain (non-interactive) icon.
Widget _encounterPlaceIcon(
  Map<String, dynamic> encounter,
  void Function(double lat, double lon)? onLocationTap,
) {
  final lat = (encounter['lat'] as num?)?.toDouble();
  final lon = (encounter['lon'] as num?)?.toDouble();
  if (onLocationTap == null || lat == null || lon == null) {
    return const Icon(Icons.place_outlined, size: 18);
  }
  return InkWell(
    borderRadius: BorderRadius.circular(14),
    onTap: () => onLocationTap(lat, lon),
    child: const Icon(Icons.place_outlined, size: 18),
  );
}

/// Trailing edit + delete buttons for an encounter row (issue #175), shared by
/// the person and group detail sheets so an encounter can be changed where it
/// is listed, not only from the trip timeline.
///
/// Deliberately buttons rather than the timeline's swipe: a [Dismissible] inside
/// a scrollable modal sheet fights the sheet's own drag. Delete confirms instead
/// of offering undo, mirroring the sheets' existing person/group delete — an
/// undo snackbar is easy to miss when the sheet itself can be dismissed.
///
/// [onChanged] runs after a successful edit or delete so the host can refresh.
Widget _encounterActions(
  BuildContext context,
  ProjectNotifier notifier,
  Map<String, dynamic> encounter,
  VoidCallback onChanged,
) {
  final id = encounter['id']?.toString();
  if (id == null || id.isEmpty) return const SizedBox.shrink();

  Future<void> edit() async {
    await showDialog<void>(
      context: context,
      useRootNavigator: true,
      builder: (_) => EncounterDialog(notifier: notifier, editEntry: encounter),
    );
    onChanged();
  }

  Future<void> delete() async {
    final ok = await showDialog<bool>(
      context: context,
      useRootNavigator: true,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete encounter?'),
        content: const Text(
            'The encounter is removed from the trip. The person or group is '
            'kept. This cannot be undone.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: kAccent),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    await notifier.deleteEncounter(id);
    onChanged();
  }

  return Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      IconButton(
        key: Key('encounter-edit-$id'),
        tooltip: 'Edit encounter',
        icon: const Icon(Icons.edit_outlined, size: 18),
        visualDensity: VisualDensity.compact,
        onPressed: edit,
      ),
      IconButton(
        key: Key('encounter-delete-$id'),
        tooltip: 'Delete encounter',
        icon: Icon(Icons.delete_outline, size: 18, color: kAccent),
        visualDensity: VisualDensity.compact,
        onPressed: delete,
      ),
    ],
  );
}

/// The subtitle for an encounter tile in the person detail sheet: the note, and
/// — for an encounter inherited from the person's group (issue #124) — a line
/// naming the group so it reads as a group meeting, not a one-to-one one.
/// Returns null when there is nothing to show.
Widget? _encounterSubtitle(ThemeData theme, Map<String, dynamic> encounter) {
  final note = (encounter['description'] as String?) ?? '';
  final groupLabel = inheritedGroupLabel(encounter);
  if (groupLabel == null) {
    return note.isEmpty ? null : _EncounterDescription(text: note);
  }
  return Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    mainAxisSize: MainAxisSize.min,
    children: [
      Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.groups_outlined,
              size: 14, color: theme.colorScheme.onSurfaceVariant),
          const SizedBox(width: 4),
          Flexible(
            child: Text(
              groupLabel,
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
              overflow: TextOverflow.ellipsis,
            ),
          ),
        ],
      ),
      if (note.isNotEmpty) _EncounterDescription(text: note),
    ],
  );
}

/// An encounter's note, clamped to 2 lines with a "Read more" that opens the
/// full text in a dialog — but only when it actually overflows 2 lines
/// (issue #73). Shared by the person and group detail sheets.
class _EncounterDescription extends StatelessWidget {
  final String text;
  const _EncounterDescription({required this.text});

  void _showFullText(BuildContext context) {
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        content: SingleChildScrollView(child: Text(text)),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Close'),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final style = DefaultTextStyle.of(context).style;
    return LayoutBuilder(
      builder: (context, constraints) {
        final overflowing = (TextPainter(
          text: TextSpan(text: text, style: style),
          maxLines: 2,
          textDirection: Directionality.of(context),
        )..layout(maxWidth: constraints.maxWidth))
            .didExceedMaxLines;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(text, maxLines: 2, overflow: TextOverflow.ellipsis, style: style),
            if (overflowing)
              Padding(
                padding: const EdgeInsets.only(top: 2),
                child: TextButton(
                  style: TextButton.styleFrom(
                    padding: EdgeInsets.zero,
                    minimumSize: Size.zero,
                    tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    alignment: Alignment.centerLeft,
                  ),
                  onPressed: () => _showFullText(context),
                  child: const Text('Read more'),
                ),
              ),
          ],
        );
      },
    );
  }
}

class _PersonDetailSheet extends StatefulWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic> person;
  final void Function(double lat, double lon)? onLocationTap;
  final VoidCallback? onTripShown;
  const _PersonDetailSheet({
    required this.notifier,
    required this.person,
    this.onLocationTap,
    this.onTripShown,
  });

  @override
  State<_PersonDetailSheet> createState() => _PersonDetailSheetState();
}

class _PersonDetailSheetState extends State<_PersonDetailSheet> {
  Map<String, dynamic>? _full;
  bool _loading = true;
  List<Map<String, dynamic>>? _psTrips;
  bool _psLoading = false;

  int get _personId => (widget.person['id'] as num).toInt();

  String? get _psHandle {
    final h = (_full ?? widget.person)['polarsteps'] as String?;
    return (h != null && h.trim().isNotEmpty) ? h.trim() : null;
  }

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final full = await widget.notifier.fetchPerson(_personId);
    if (!mounted) return;
    setState(() {
      _full = full;
      _loading = false;
    });
  }

  Future<void> _loadPsTrips() async {
    setState(() => _psLoading = true);
    final trips = await widget.notifier.fetchPersonPolarstepsTrips(_personId);
    if (!mounted) return;
    setState(() {
      _psTrips = trips ?? [];
      _psLoading = false;
    });
    if (trips == null) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(widget.notifier.error ?? 'Could not load Polarsteps trips'),
      ));
    }
  }

  Future<void> _showTrip(Map<String, dynamic> trip) async {
    final navigator = Navigator.of(context);
    final name = personDisplayName(_full ?? widget.person);
    final label = '$name · ${trip['name'] ?? 'Trip'}';
    final ok = await widget.notifier.showPersonPolarstepsTrip(
        _personId, (trip['id'] as num).toInt(), label);
    if (!mounted) return;
    if (ok) {
      final onTripShown = widget.onTripShown;
      if (onTripShown != null) {
        onTripShown();
      } else {
        navigator.pop(); // close sheet so the overlay is visible on the map
      }
    } else {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(widget.notifier.error ?? 'Could not load that trip'),
      ));
    }
  }

  Future<void> _edit() async {
    final navigator = Navigator.of(context);
    await showPersonFormDialog(context, widget.notifier,
        person: _full ?? widget.person);
    navigator.pop();
  }

  Future<void> _pickAvatar() async {
    final f = await FilePicker.pickFile(type: FileType.image);
    if (f == null) return;
    final bytes = await f.readAsBytesOrNull();
    if (bytes == null) return;
    final ok = await widget.notifier.uploadPersonAvatar(
        _personId, bytes, f.name);
    if (!ok && mounted) {
      // Refused on a plan limit (issue #121) — offer the upgrade rather than
      // leaving the avatar silently unchanged.
      await maybeShowUpgradeSheet(context, widget.notifier.takeQuotaError());
    }
    await _load();
  }

  Future<void> _delete() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete person?'),
        content: const Text(
            'This also removes all encounters with them. This cannot be undone.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: kAccent),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    final navigator = Navigator.of(context);
    await widget.notifier.deletePerson(_personId);
    navigator.pop();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final person = _full ?? widget.person;
    final encounters =
        (_full?['encounters'] as List?)?.cast<Map<String, dynamic>>() ?? const [];
    return Padding(
      padding: EdgeInsets.only(
        left: 16, right: 16, top: 4,
        bottom: MediaQuery.of(context).viewInsets.bottom + 16,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Stack(
                children: [
                  PersonAvatar(
                      notifier: widget.notifier, person: person, radius: 28),
                  Positioned(
                    right: -6,
                    bottom: -6,
                    child: IconButton(
                      tooltip: 'Change photo',
                      iconSize: 18,
                      visualDensity: VisualDensity.compact,
                      icon: const Icon(Icons.photo_camera_outlined),
                      onPressed: _pickAvatar,
                    ),
                  ),
                ],
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Text(personDisplayName(person),
                    style: theme.textTheme.titleLarge),
              ),
              IconButton(
                  icon: const Icon(Icons.edit_outlined), onPressed: _edit),
              IconButton(
                  icon: Icon(Icons.delete_outline, color: kAccent),
                  onPressed: _delete),
            ],
          ),
          const SizedBox(height: 8),
          _detailRow(Icons.email_outlined, person['email']),
          _detailRow(Icons.phone_outlined, person['phone'], copyable: true),
          _detailRow(Icons.travel_explore_outlined, person['polarsteps']),
          _detailRow(Icons.notes_outlined, person['notes']),
          const SizedBox(height: 12),
          Text('Places & dates met', style: theme.textTheme.titleSmall),
          const SizedBox(height: 4),
          if (_loading)
            const Padding(
                padding: EdgeInsets.all(16),
                child: Center(child: CircularProgressIndicator()))
          else if (encounters.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Text('No encounters logged yet.',
                  style: theme.textTheme.bodySmall),
            )
          else
            Flexible(
              child: ListView(
                shrinkWrap: true,
                children: [
                  for (final e in encounters)
                    ListTile(
                      dense: true,
                      leading: _encounterPlaceIcon(e, widget.onLocationTap),
                      title: Text(e['date']?.toString() ?? ''),
                      subtitle: _encounterSubtitle(theme, e),
                      // Inherited group encounters are editable too — they are
                      // real encounters, and the "With <group>" subtitle above
                      // already flags that a change applies to the whole group.
                      trailing:
                          _encounterActions(context, widget.notifier, e, _load),
                    ),
                ],
              ),
            ),
          if (_psHandle != null) ...[
            const Divider(height: 20),
            Row(
              children: [
                const Icon(Icons.travel_explore_outlined, size: 18),
                const SizedBox(width: 8),
                Text('Polarsteps trips', style: theme.textTheme.titleSmall),
                const Spacer(),
                if (_psTrips == null)
                  TextButton(
                    onPressed: _psLoading ? null : _loadPsTrips,
                    child: _psLoading
                        ? const SizedBox(
                            width: 14, height: 14,
                            child: CircularProgressIndicator(strokeWidth: 2))
                        : const Text('Show trips'),
                  ),
              ],
            ),
            if (_psTrips != null && _psTrips!.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: Text('No trips visible to you.',
                    style: theme.textTheme.bodySmall),
              ),
            if (_psTrips != null && _psTrips!.isNotEmpty)
              Flexible(
                child: ListView(
                  shrinkWrap: true,
                  children: [
                    for (final t in _psTrips!)
                      ListTile(
                        dense: true,
                        leading: const Icon(Icons.map_outlined, size: 18),
                        title: Text(t['name']?.toString() ?? 'Trip'),
                        subtitle: Text([
                          if (t['start_date'] != null) t['start_date'],
                          if (t['steps_count'] != null)
                            '${t['steps_count']} steps',
                        ].join('  •  ')),
                        trailing: const Icon(Icons.arrow_forward, size: 16),
                        onTap: () => _showTrip(t),
                      ),
                  ],
                ),
              ),
          ],
        ],
      ),
    );
  }

  Widget _detailRow(IconData icon, Object? value, {bool copyable = false}) {
    final text = value?.toString().trim();
    if (text == null || text.isEmpty) return const SizedBox.shrink();
    final row = Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, size: 18, color: Theme.of(context).colorScheme.onSurfaceVariant),
        const SizedBox(width: 10),
        Expanded(child: Text(text)),
      ],
    );
    if (!copyable) {
      return Padding(padding: const EdgeInsets.symmetric(vertical: 3), child: row);
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: InkWell(
        onTap: () {
          Clipboard.setData(ClipboardData(text: text));
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text('Phone number copied')),
          );
        },
        child: row,
      ),
    );
  }
}

// ── Groups (issue #50) ────────────────────────────────────────────────────────

class _GroupTile extends StatelessWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic> group;
  final int memberCount;
  final int encounterCount;
  const _GroupTile({
    required this.notifier,
    required this.group,
    required this.memberCount,
    required this.encounterCount,
  });

  @override
  Widget build(BuildContext context) {
    final subtitleParts = [
      '$memberCount ${memberCount == 1 ? 'member' : 'members'}',
      if (encounterCount > 0)
        '$encounterCount ${encounterCount == 1 ? 'encounter' : 'encounters'}',
    ];
    return ListTile(
      leading: CircleAvatar(
        radius: 22,
        backgroundColor: kAccentSoft,
        child: const Icon(Icons.groups, color: kAccent),
      ),
      title: Text(groupDisplayName(group)),
      subtitle: Text(subtitleParts.join('  •  ')),
      onTap: () => showGroupDetailSheet(context, notifier, group,
          onLocationTap: (lat, lon) {
            // Close the sheet, then close PeopleScreen itself with the picked
            // point so the caller (map/activity panel) can focus it (#72).
            Navigator.of(context).pop();
            Navigator.of(context).pop({'lat': lat, 'lon': lon});
          },
          onTripShown: () {
            // A member's trip, picked from their sheet — see the person tile.
            Navigator.of(context).pop();
            Navigator.of(context).pop();
          }),
    );
  }
}

/// Per-group detail sheet: info + members (tap a member → their sheet).
///
/// [onLocationTap] and [onTripShown] — see [showPersonDetailSheet]; both are
/// forwarded to a member's own sheet, opened from the members list.
Future<void> showGroupDetailSheet(
  BuildContext context,
  ProjectNotifier notifier,
  Map<String, dynamic> group, {
  void Function(double lat, double lon)? onLocationTap,
  VoidCallback? onTripShown,
}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (_) => _GroupDetailSheet(
      notifier: notifier,
      group: group,
      onLocationTap: onLocationTap,
      onTripShown: onTripShown,
    ),
  );
}

class _GroupDetailSheet extends StatelessWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic> group;
  final void Function(double lat, double lon)? onLocationTap;
  final VoidCallback? onTripShown;
  const _GroupDetailSheet({
    required this.notifier,
    required this.group,
    this.onLocationTap,
    this.onTripShown,
  });

  int get _groupId => (group['id'] as num).toInt();

  Future<void> _edit(BuildContext context) async {
    final navigator = Navigator.of(context);
    await showGroupFormDialog(context, notifier, group: group);
    navigator.pop();
  }

  Future<void> _delete(BuildContext context) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete group?'),
        content: const Text(
            'The group is removed; its members are kept (just ungrouped).'),
        actions: [
          TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: kAccent),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true || !context.mounted) return;
    final navigator = Navigator.of(context);
    await notifier.deleteGroup(_groupId);
    navigator.pop();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final nationalities =
        (group['nationalities'] as List?)?.cast<String>() ?? const [];
    final socials =
        (group['socials'] as List?)?.cast<Map<String, dynamic>>() ?? const [];
    final members = membersOfGroup(notifier.itemsFacet.people, _groupId);
    return Padding(
      padding: EdgeInsets.only(
        left: 16, right: 16, top: 4,
        bottom: MediaQuery.of(context).viewInsets.bottom + 16,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              CircleAvatar(
                radius: 24,
                backgroundColor: kAccentSoft,
                child: const Icon(Icons.groups, color: kAccent),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Text(groupDisplayName(group),
                    style: theme.textTheme.titleLarge),
              ),
              IconButton(
                  icon: const Icon(Icons.edit_outlined),
                  onPressed: () => _edit(context)),
              IconButton(
                  icon: Icon(Icons.delete_outline, color: kAccent),
                  onPressed: () => _delete(context)),
            ],
          ),
          const SizedBox(height: 8),
          if (nationalities.isNotEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Wrap(
                spacing: 6,
                runSpacing: 4,
                children: [
                  for (final c in nationalities)
                    Chip(
                      label: Text(countryName(c)),
                      visualDensity: VisualDensity.compact,
                    ),
                ],
              ),
            ),
          for (final s in socials)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 2),
              child: Row(
                children: [
                  const Icon(Icons.link, size: 18),
                  const SizedBox(width: 10),
                  Text('${socialNetworkLabel('${s['network']}')}: '
                      '${s['handle'] ?? ''}'),
                ],
              ),
            ),
          const SizedBox(height: 12),
          Text('Members', style: theme.textTheme.titleSmall),
          const SizedBox(height: 4),
          if (members.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child:
                  Text('No members yet.', style: theme.textTheme.bodySmall),
            )
          else
            Flexible(
              child: ListView(
                shrinkWrap: true,
                children: [
                  for (final p in members)
                    ListTile(
                      dense: true,
                      leading:
                          PersonAvatar(notifier: notifier, person: p, radius: 16),
                      title: Text(personDisplayName(p)),
                      onTap: () {
                        Navigator.of(context).pop();
                        showPersonDetailSheet(context, notifier, p,
                            onLocationTap: onLocationTap,
                            onTripShown: onTripShown);
                      },
                    ),
                ],
              ),
            ),
          const SizedBox(height: 12),
          Text('Encounters', style: theme.textTheme.titleSmall),
          const SizedBox(height: 4),
          // Listens, unlike the rest of this sheet: editing or deleting an
          // encounter in place (issue #175) has to repaint this list, and the
          // rows come straight off notifier.items.
          ListenableBuilder(
            listenable: notifier,
            builder: (context, _) {
              final encounters = encountersForGroup(notifier.itemsFacet.items, _groupId);
              if (encounters.isEmpty) {
                return Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text('No encounters logged yet.',
                      style: theme.textTheme.bodySmall),
                );
              }
              return Flexible(
                child: ListView(
                  shrinkWrap: true,
                  children: [
                    for (final e in encounters)
                      ListTile(
                        dense: true,
                        leading: _encounterPlaceIcon(e, onLocationTap),
                        title: Text(e['date']?.toString() ?? ''),
                        subtitle: (e['description'] as String?)?.isNotEmpty ?? false
                            ? _EncounterDescription(text: e['description'] as String)
                            : null,
                        // The ListenableBuilder above handles the refresh.
                        trailing:
                            _encounterActions(context, notifier, e, () {}),
                      ),
                  ],
                ),
              );
            },
          ),
        ],
      ),
    );
  }
}
