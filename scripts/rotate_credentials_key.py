#!/usr/bin/env python
"""Re-encrypt every stored third-party credential under the current key.

Rotating ``CREDENTIALS_ENCRYPTION_KEY`` (src/auth/credentials_crypto.py):

1. move the old key into ``CREDENTIALS_ENCRYPTION_KEYS_RETIRED`` (comma-separated)
   and set a new ``CREDENTIALS_ENCRYPTION_KEY``; restart. Reads accept both,
   writes use the new one;
2. run this script, dry run first, then with ``--apply``;
3. once it reports nothing left under a retired key, drop that key from
   ``CREDENTIALS_ENCRYPTION_KEYS_RETIRED`` and restart.

Every column stored with ``EncryptedString`` is covered, found from the models
rather than listed here. A value written meanwhile by the app is not
overwritten: each update only applies if the row still holds the value read.
Values are never printed — only counts, and the table, column and row id of
any value no configured key can decrypt (exit status 1 if there are any).

DRY-RUN BY DEFAULT — reports what would change and changes nothing.

Usage, inside the API container, with the same environment as the server:
    docker compose run --rm --entrypoint python traxjourney scripts/rotate_credentials_key.py
    docker compose run --rm --entrypoint python traxjourney scripts/rotate_credentials_key.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlmodel  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

import models.billing  # noqa: E402,F401 — register every table with the metadata
import models.project_db  # noqa: E402,F401
import models.user  # noqa: E402,F401
from models.db_url import resolve_database_url  # noqa: E402
from src.auth.credentials_crypto import (  # noqa: E402
    CredentialDecryptError,
    EncryptedString,
    credentials_keyring,
    decrypt_credential,
    encrypt_credential,
    key_id_of,
)


def encrypted_columns() -> List[Tuple[str, str]]:
    """(table, column) of every column the models store encrypted."""
    return sorted(
        (table.name, column.name)
        for table in sqlmodel.SQLModel.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, EncryptedString)
    )


@dataclass
class Report:
    current: int = 0        # already under the current key
    rewritten: int = 0      # re-encrypted (or would be, on a dry run)
    plaintext: int = 0      # of those, found unencrypted
    changed_meanwhile: int = 0
    unreadable: List[Tuple[str, str, int]] = field(default_factory=list)


def rotate(engine: Engine, apply: bool) -> Report:
    current_id = credentials_keyring().current_id
    report = Report()
    for table, column in encrypted_columns():
        label = f"{table}.{column}"
        with engine.begin() as conn:
            rows = conn.execute(text(f"SELECT id, {column} FROM {table}")).fetchall()
            for row_id, value in rows:
                if not value:
                    continue
                kid = key_id_of(value)
                if kid == current_id:
                    report.current += 1
                    continue
                if kid is None:
                    plain = value
                    report.plaintext += 1
                else:
                    try:
                        plain = decrypt_credential(value, label)
                    except CredentialDecryptError:
                        report.unreadable.append((table, column, row_id))
                        continue
                report.rewritten += 1
                if not apply:
                    continue
                result = conn.execute(
                    text(f"UPDATE {table} SET {column} = :new "
                         f"WHERE id = :id AND {column} = :old"),
                    {"new": encrypt_credential(plain, label), "id": row_id, "old": value})
                if result.rowcount != 1:
                    report.changed_meanwhile += 1
    return report


def main(argv: Optional[List[str]] = None, engine: Optional[Engine] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the re-encrypted values (default: dry run)")
    args = parser.parse_args(argv)
    if engine is None:
        engine = create_engine(resolve_database_url())

    report = rotate(engine, args.apply)

    verb = "re-encrypted" if args.apply else "to re-encrypt"
    print(f"Current key id: {credentials_keyring().current_id}")
    print(f"Already under the current key: {report.current}")
    print(f"{verb.capitalize()}: {report.rewritten}"
          + (f" ({report.plaintext} found unencrypted)" if report.plaintext else ""))
    if report.changed_meanwhile:
        print(f"Changed by the app meanwhile, left as written: {report.changed_meanwhile}")
    for table, column, row_id in report.unreadable:
        print(f"Unreadable with the configured keys: {table}.{column} row {row_id}")
    if not args.apply and report.rewritten:
        print("Dry run: nothing written. Re-run with --apply.")
    return 1 if report.unreadable else 0


if __name__ == "__main__":
    sys.exit(main())
