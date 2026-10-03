"""encrypt the stored third-party credentials

The Strava access and refresh tokens, the Polarsteps ``remember_token`` and the
Immich API key were stored as plain text. The models now store them encrypted
(src/auth/credentials_crypto.py); this data migration encrypts the values
already there under ``CREDENTIALS_ENCRYPTION_KEY``.

Idempotent: values already encrypted, and empty ones, are left alone. The key
is only needed when there is something to encrypt, so a fresh database (CI's
``alembic upgrade head && alembic check``) migrates without one; the server
itself refuses to start without it.

Downgrade decrypts back to plain text, and refuses — leaving the data as it is —
if a value does not decrypt with the configured keys.

Unlike most migrations here this imports app code: the format, key ids and
key handling must be exactly those the models read, and the module keeps
decrypting every format it ever wrote.

Revision ID: e3a91c5d7f20
Revises: b7e4d2c9a1f0
Create Date: 2026-10-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from src.auth.credentials_crypto import (
    CredentialDecryptError,
    decrypt_credential,
    encrypt_credential,
    is_encrypted,
)


# revision identifiers, used by Alembic.
revision: str = 'e3a91c5d7f20'
down_revision: Union[str, Sequence[str], None] = 'b7e4d2c9a1f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen here rather than read from the models: a column encrypted later gets
# its own migration.
_COLUMNS = (
    ("stravatoken", "access_token"),
    ("stravatoken", "refresh_token"),
    ("polarstepstoken", "remember_token"),
    ("immichtoken", "api_key"),
)


def _rewrite(convert) -> None:
    bind = op.get_bind()
    for table, column in _COLUMNS:
        rows = bind.execute(sa.text(f"SELECT id, {column} FROM {table}")).fetchall()
        for row_id, value in rows:
            new = convert(value, f"{table}.{column}", row_id)
            if new is not None:
                bind.execute(
                    sa.text(f"UPDATE {table} SET {column} = :v WHERE id = :id"),
                    {"v": new, "id": row_id})


def _encrypt(value, column, _row_id):
    if not value or is_encrypted(value):
        return None
    return encrypt_credential(value, column)


def _decrypt(value, column, row_id):
    if not is_encrypted(value):
        return None
    try:
        return decrypt_credential(value, column)
    except CredentialDecryptError as exc:
        raise RuntimeError(
            f"Cannot downgrade: row {row_id} of {exc}. Set the key it was "
            "encrypted with in CREDENTIALS_ENCRYPTION_KEY or "
            "CREDENTIALS_ENCRYPTION_KEYS_RETIRED.") from None


def upgrade() -> None:
    """Encrypt every plain-text credential."""
    _rewrite(_encrypt)


def downgrade() -> None:
    """Decrypt every credential back to plain text."""
    _rewrite(_decrypt)
