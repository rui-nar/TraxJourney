/// Create / edit a group of people (issue #50): name, nationalities, socials
/// (reusing the person modal's shared field widgets) and a member list — pick
/// existing people or create one on the fly.
library;

import 'package:flutter/material.dart';

import '../core/design_tokens.dart';
import 'nationality_field.dart';
import 'people_search.dart';
import 'person_form_dialog.dart';
import 'project_notifier.dart';
import 'save_error_banner.dart';
import 'social_links_field.dart';

/// Show the create/edit group dialog. Returns the group id on success, or null.
Future<int?> showGroupFormDialog(
  BuildContext context,
  ProjectNotifier notifier, {
  Map<String, dynamic>? group,
}) {
  return showDialog<int?>(
    context: context,
    builder: (_) => _GroupFormDialog(notifier: notifier, group: group),
  );
}

class _GroupFormDialog extends StatefulWidget {
  final ProjectNotifier notifier;
  final Map<String, dynamic>? group;
  const _GroupFormDialog({required this.notifier, this.group});

  @override
  State<_GroupFormDialog> createState() => _GroupFormDialogState();
}

class _GroupFormDialogState extends State<_GroupFormDialog> {
  final _socialsKey = GlobalKey<SocialLinksFieldState>();
  final _nationalityKey = GlobalKey<NationalityFieldState>();

  late final TextEditingController _name =
      TextEditingController(text: widget.group?['name'] as String? ?? '');
  late final Set<int> _memberIds = _initialMembers();
  bool _saving = false;
  String? _saveError;

  // Set once createGroup succeeds, so that a retry after a setGroupMembers
  // failure (group created, but its member list didn't get set) updates the
  // already-created group instead of creating a second one.
  int? _savedGroupId;

  bool get _isEdit => widget.group != null;
  int? get _groupId => (widget.group?['id'] as num?)?.toInt() ?? _savedGroupId;

  Set<int> _initialMembers() {
    final gid = _groupId;
    if (gid == null) return {};
    return {
      for (final p in membersOfGroup(widget.notifier.itemsFacet.people, gid))
        (p['id'] as num).toInt(),
    };
  }

  List<Map<String, dynamic>> _initialSocials() {
    final raw = widget.group?['socials'];
    return raw is List ? raw.cast<Map<String, dynamic>>() : const [];
  }

  List<String> _initialNationalities() {
    final raw = widget.group?['nationalities'];
    return raw is List ? [for (final c in raw) '$c'] : const [];
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  Future<void> _editMembers() async {
    final result = await showDialog<Set<int>>(
      context: context,
      builder: (_) =>
          _MembersPicker(notifier: widget.notifier, selected: {..._memberIds}),
    );
    if (result != null && mounted) {
      setState(() {
        _memberIds
          ..clear()
          ..addAll(result);
      });
    }
  }

  Future<void> _save() async {
    setState(() {
      _saveError = null;
      _saving = true;
    });
    final navigator = Navigator.of(context);
    final name = _name.text.trim().isEmpty ? null : _name.text.trim();
    final socials = _socialsKey.currentState?.value ?? const [];
    final nationalities = _nationalityKey.currentState?.value ?? const [];

    try {
      int? id = _groupId;
      if (id != null) {
        final ok = await widget.notifier.updateGroup(id,
            name: name, nationalities: nationalities, socials: socials);
        if (!ok) {
          setState(() =>
              _saveError = widget.notifier.error ?? 'Failed to save group');
          return;
        }
      } else {
        id = await widget.notifier.createGroup(
            name: name, nationalities: nationalities, socials: socials);
        if (id == null) {
          setState(() =>
              _saveError = widget.notifier.error ?? 'Failed to save group');
          return;
        }
        _savedGroupId = id;
      }

      final membersOk =
          await widget.notifier.setGroupMembers(id, _memberIds.toList());
      if (!membersOk) {
        setState(() => _saveError = 'Group saved, but the member list may '
            'not have updated (${widget.notifier.error ?? 'unknown error'}). '
            'Try again.');
        return;
      }

      if (!mounted) return;
      navigator.pop(id);
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return AlertDialog(
      title: Text(_isEdit ? 'Edit group' : 'Add group'),
      content: SizedBox(
        width: 380,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 6),
                child: TextField(
                  controller: _name,
                  textCapitalization: TextCapitalization.words,
                  decoration: const InputDecoration(
                    labelText: 'Group name',
                    border: OutlineInputBorder(),
                    isDense: true,
                  ),
                ),
              ),
              const SizedBox(height: 12),
              // Members
              Row(
                children: [
                  Text('Members', style: theme.textTheme.labelLarge),
                  const Spacer(),
                  TextButton.icon(
                    key: const Key('edit-members'),
                    onPressed: _editMembers,
                    icon: const Icon(Icons.group_add, size: 18),
                    label: Text(_memberIds.isEmpty ? 'Add' : 'Edit'),
                  ),
                ],
              ),
              if (_memberIds.isNotEmpty)
                Wrap(
                  spacing: 6,
                  runSpacing: 4,
                  children: [
                    for (final id in _memberIds)
                      Chip(
                        label: Text(_memberName(id)),
                        onDeleted: () => setState(() => _memberIds.remove(id)),
                      ),
                  ],
                ),
              const SizedBox(height: 12),
              SocialLinksField(key: _socialsKey, initial: _initialSocials()),
              const SizedBox(height: 12),
              NationalityField(
                  key: _nationalityKey, initial: _initialNationalities()),
              if (_saveError != null) ...[
                const SizedBox(height: 12),
                SaveErrorBanner(message: _saveError!),
              ],
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: _saving ? null : () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(
          style: FilledButton.styleFrom(backgroundColor: kAccent),
          onPressed: _saving ? null : _save,
          child: _saving
              ? const SizedBox(
                  width: 16, height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
              : Text(_isEdit ? 'Save' : 'Add'),
        ),
      ],
    );
  }

  String _memberName(int id) {
    for (final p in widget.notifier.itemsFacet.people) {
      if (p['id'] == id) return personDisplayName(p);
    }
    return 'Unknown';
  }
}

/// Multi-select of existing people (+ create one on the fly). Pops the selected
/// id set, or null on cancel.
class _MembersPicker extends StatefulWidget {
  final ProjectNotifier notifier;
  final Set<int> selected;
  const _MembersPicker({required this.notifier, required this.selected});

  @override
  State<_MembersPicker> createState() => _MembersPickerState();
}

class _MembersPickerState extends State<_MembersPicker> {
  late final Set<int> _selected = {...widget.selected};

  Future<void> _createPerson() async {
    final newId = await showPersonFormDialog(context, widget.notifier);
    if (newId != null && mounted) setState(() => _selected.add(newId));
  }

  @override
  Widget build(BuildContext context) {
    final people = widget.notifier.itemsFacet.people;
    return AlertDialog(
      title: const Text('Members'),
      content: SizedBox(
        width: 340,
        height: 420,
        child: Column(
          children: [
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                key: const Key('new-member-person'),
                onPressed: _createPerson,
                icon: const Icon(Icons.person_add_alt_1, size: 18),
                label: const Text('New person'),
              ),
            ),
            Expanded(
              child: people.isEmpty
                  ? const Center(child: Text('No people yet — add one.'))
                  : ListView.builder(
                      itemCount: people.length,
                      itemBuilder: (_, i) {
                        final p = people[i];
                        final id = (p['id'] as num).toInt();
                        return CheckboxListTile(
                          dense: true,
                          value: _selected.contains(id),
                          title: Text(personDisplayName(p)),
                          onChanged: (v) => setState(() =>
                              v == true ? _selected.add(id) : _selected.remove(id)),
                        );
                      },
                    ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(_selected),
          child: const Text('Done'),
        ),
      ],
    );
  }
}
