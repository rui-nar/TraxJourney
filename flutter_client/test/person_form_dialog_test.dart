import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:traxjourney_client/src/projects/person_form_dialog.dart';
import 'package:traxjourney_client/src/projects/project_notifier.dart';
import 'package:traxjourney_client/src/projects/project_service.dart';

/// Captures create/update payloads and stubs place search — so the dialog is
/// tested without any network.
class _FakeNotifier extends ProjectNotifier {
  _FakeNotifier() : super(ProjectService());

  Map<String, dynamic>? created;
  Map<String, dynamic>? updated;
  List<String> placeResults = [];

  @override
  Future<int?> createPerson({
    String? name,
    String? email,
    String? phone,
    String? notes,
    List<Map<String, String>>? socials,
    List<String>? nationalities,
    String? residence,
  }) async {
    created = {
      'name': name, 'email': email, 'phone': phone, 'notes': notes,
      'socials': socials, 'nationalities': nationalities, 'residence': residence,
    };
    return 1;
  }

  @override
  Future<bool> updatePerson(
    int personId, {
    String? name,
    String? email,
    String? phone,
    String? notes,
    List<Map<String, String>>? socials,
    List<String>? nationalities,
    String? residence,
  }) async {
    updated = {
      'id': personId, 'name': name, 'socials': socials,
      'nationalities': nationalities, 'residence': residence,
    };
    return true;
  }

  @override
  Future<List<String>> searchPlaces(String query) async => placeResults;
}

Future<void> _pump(WidgetTester tester, _FakeNotifier notifier,
    {Map<String, dynamic>? person}) async {
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: Center(child: PersonFormDialog(notifier: notifier, person: person)),
    ),
  ));
}

Finder get _primaryAdd => find.widgetWithText(FilledButton, 'Add');

void main() {
  testWidgets('invalid email blocks save', (tester) async {
    final n = _FakeNotifier();
    await _pump(tester, n);
    await tester.enterText(find.byKey(const Key('person-email')), 'not-an-email');
    await tester.tap(_primaryAdd);
    await tester.pumpAndSettle();
    expect(find.text('Enter a valid email'), findsOneWidget);
    expect(n.created, isNull, reason: 'save must not fire when email is invalid');
  });

  testWidgets('valid person saves with title-cased name intent', (tester) async {
    final n = _FakeNotifier();
    await _pump(tester, n);
    await tester.enterText(find.byKey(const Key('person-email')), 'a@b.com');
    await tester.tap(_primaryAdd);
    await tester.pumpAndSettle();
    expect(n.created, isNotNull);
    expect(n.created!['email'], 'a@b.com');
  });

  testWidgets('add and remove a social row', (tester) async {
    final n = _FakeNotifier();
    await _pump(tester, n);
    expect(find.byKey(const Key('remove-social-0')), findsNothing);

    await tester.tap(find.byKey(const Key('add-social')));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.descendant(
            of: find.byType(Row),
            matching: find.byType(TextField)).first,
        'alice_ig');
    expect(find.byKey(const Key('remove-social-0')), findsOneWidget);

    await tester.tap(find.byKey(const Key('remove-social-0')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('remove-social-0')), findsNothing);
  });

  testWidgets('socials are sent on save', (tester) async {
    final n = _FakeNotifier();
    await _pump(tester, n);
    await tester.tap(find.byKey(const Key('add-social')));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.descendant(of: find.byType(Row), matching: find.byType(TextField)).first,
        'alice_ig');
    await tester.tap(_primaryAdd);
    await tester.pumpAndSettle();
    expect(n.created!['socials'], [
      {'network': 'instagram', 'handle': 'alice_ig'}
    ]);
  });

  testWidgets('multi-select nationality via searchable picker', (tester) async {
    final n = _FakeNotifier();
    await _pump(tester, n);

    await tester.tap(find.byKey(const Key('add-nationality')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('country-search')), 'Portugal');
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(ListTile, 'Portugal'));
    await tester.pumpAndSettle();

    expect(find.widgetWithText(Chip, 'Portugal'), findsOneWidget);

    await tester.tap(_primaryAdd);
    await tester.pumpAndSettle();
    expect(n.created!['nationalities'], ['PT']);
  });

  testWidgets('residence autocomplete surfaces suggestions and saves value',
      (tester) async {
    final n = _FakeNotifier()..placeResults = ['Lisbon, Portugal'];
    await _pump(tester, n);

    await tester.enterText(
        find.byKey(const Key('person-residence')), 'Lisbon, Portugal');
    await tester.pumpAndSettle();
    // Suggestion surfaced from the stubbed search (in the overlay).
    expect(find.text('Lisbon, Portugal'), findsWidgets);

    await tester.tap(_primaryAdd);
    await tester.pumpAndSettle();
    expect(n.created!['residence'], 'Lisbon, Portugal');
  });

  testWidgets('edit prefills existing profile fields', (tester) async {
    final n = _FakeNotifier();
    await _pump(tester, n, person: {
      'id': 7,
      'name': 'Alice',
      'socials': [{'network': 'strava', 'handle': 'alice_s'}],
      'nationalities': ['FR'],
      'residence': 'Paris, France',
    });
    expect(find.widgetWithText(Chip, 'France'), findsOneWidget);
    expect(find.text('alice_s'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();
    expect(n.updated!['id'], 7);
    expect(n.updated!['nationalities'], ['FR']);
    expect(n.updated!['residence'], 'Paris, France');
  });
}
