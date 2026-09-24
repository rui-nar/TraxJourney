"""docs/ENCRYPTION.md must list every field the client encrypts (issue #433).

The client's `encryptedFieldsByResource` (flutter_client/lib/src/crypto/
encryption_migration.dart) is the pinned list of end-to-end-encrypted fields;
its own Dart test holds the migration to it. This test holds the doc to it,
from server CI where no Flutter toolchain exists: it parses the constant out of
the Dart source with a regex and checks that every resource and every field
appears, in backticks, in the doc's "What is encrypted" table.

A field added to the constant without a doc row fails here; so does a row
deleted from the doc.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
DART_SOURCE = _ROOT / "flutter_client" / "lib" / "src" / "crypto" / "encryption_migration.dart"
DOC = _ROOT / "docs" / "ENCRYPTION.md"

# How the doc names each resource in the table's first column.
_DOC_LABELS = {
    "memory": "Memory",
    "journal": "Journal entry",
    "activity": "Activity",
}


def parse_encrypted_fields(dart_source: str) -> dict[str, set[str]]:
    """``{resource: {field, ...}}`` from the ``encryptedFieldsByResource`` literal."""
    m = re.search(
        r"const encryptedFieldsByResource\s*=\s*<String,\s*Set<String>>\{(.*?)\n\};",
        dart_source,
        re.DOTALL,
    )
    assert m, "encryptedFieldsByResource not found in encryption_migration.dart"
    body = m.group(1)
    result: dict[str, set[str]] = {}
    for resource, fields in re.findall(r"'(\w+)':\s*\{([^}]*)\}", body, re.DOTALL):
        result[resource] = set(re.findall(r"'(\w+)'", fields))
    return result


def encrypted_table(doc: str) -> str:
    """The Markdown table under '## What is encrypted', up to the next heading."""
    m = re.search(r"^## What is encrypted\n(.*?)(?=^## )", doc, re.DOTALL | re.MULTILINE)
    assert m, "'## What is encrypted' section not found in docs/ENCRYPTION.md"
    rows = [line for line in m.group(1).splitlines() if line.startswith("|")]
    assert rows, "no Markdown table under '## What is encrypted'"
    return "\n".join(rows)


@pytest.fixture(scope="module")
def encrypted_fields() -> dict[str, set[str]]:
    fields = parse_encrypted_fields(DART_SOURCE.read_text(encoding="utf-8"))
    # Guard the regex itself: a silent parse miss must not pass as "nothing to check".
    assert set(fields) == set(_DOC_LABELS), f"unexpected resources: {sorted(fields)}"
    assert all(fields.values()), f"a resource parsed with no fields: {fields}"
    return fields


@pytest.fixture(scope="module")
def table() -> str:
    return encrypted_table(DOC.read_text(encoding="utf-8"))


def test_every_resource_has_a_row(encrypted_fields, table):
    for resource in encrypted_fields:
        label = _DOC_LABELS[resource]
        assert re.search(rf"^\| {re.escape(label)} \|", table, re.MULTILINE), (
            f"docs/ENCRYPTION.md has no '{label}' row in the 'What is encrypted' table"
        )


def claimed_fields(table: str, resource: str) -> set[str]:
    """Fields named in backticks in the *encrypted-field cell* (column 2) of
    every row for *resource*. Only that cell counts, in both directions: a
    field mentioned in the "Database column" or "Encrypted where" cells is
    not a claim that it is encrypted."""
    claimed: set[str] = set()
    for line in table.splitlines():
        if line.startswith(f"| {_DOC_LABELS[resource]} |"):
            claimed |= set(re.findall(r"`(\w+)`", line.split("|")[2]))
    return claimed


def test_every_encrypted_field_is_in_the_doc(encrypted_fields, table):
    for resource, fields in encrypted_fields.items():
        missing = sorted(fields - claimed_fields(table, resource))
        assert not missing, (
            f"{resource}: encryptedFieldsByResource has {missing} but the "
            f"'{_DOC_LABELS[resource]}' rows of docs/ENCRYPTION.md do not name "
            "them in the encrypted-field column — update the doc (or the constant)"
        )


def test_the_doc_lists_no_field_the_client_does_not_encrypt(encrypted_fields, table):
    """A field named in a resource's encrypted-field cell must be one the
    client encrypts — the table may not promise more than the code does."""
    for resource, fields in encrypted_fields.items():
        extra = sorted(claimed_fields(table, resource) - fields)
        assert not extra, (
            f"{resource}: the doc claims {extra} is encrypted "
            "but encryptedFieldsByResource does not list it"
        )
