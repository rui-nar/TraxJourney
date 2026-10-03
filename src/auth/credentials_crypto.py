"""Encryption at rest for the third-party credentials users connect.

Strava OAuth tokens, the Polarsteps ``remember_token`` cookie and the Immich
API key each give full access to the user's account on that service, so the
database and its nightly backups must not hold them in a usable form. They are
stored as AES-256-GCM ciphertext under a key that lives outside the database,
in the ``CREDENTIALS_ENCRYPTION_KEY`` environment variable.

The format of a stored value is ``tjenc:v1:<key id>:<base64url(nonce|ciphertext)>``:

- the nonce is 96 random bits, fresh for every value;
- the key id names which key encrypted it, so a key can be rotated: decryption
  accepts the current key and any listed in ``CREDENTIALS_ENCRYPTION_KEYS_RETIRED``,
  writes always use the current one, and ``scripts/rotate_credentials_key.py``
  re-encrypts what is left under a retired key;
- the associated data names the ``table.column`` the value belongs to, so a
  value copied into another credential column does not decrypt there.

The model layer applies this transparently (:class:`EncryptedString` on the
columns in ``models/user.py``): call sites read and assign plaintext.

Losing the key loses the credentials, not the accounts: a value that does not
decrypt reads as empty, the service shows as disconnected, and the user
reconnects it.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Optional

import sqlmodel
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.types import TypeDecorator

from src.utils.logging import get_logger

_log = get_logger(__name__)

KEY_ENV = "CREDENTIALS_ENCRYPTION_KEY"
RETIRED_KEYS_ENV = "CREDENTIALS_ENCRYPTION_KEYS_RETIRED"

PREFIX = "tjenc:v1:"
_NONCE_BYTES = 12
_KEY_ID_HEX = 8

_FIX = ("Generate one with `openssl rand -hex 32` and set it as an environment "
        "variable (see .env.example).")


class CredentialDecryptError(Exception):
    """A stored credential could not be decrypted with any configured key.

    The message names the column and key id, never the value.
    """


def _key_id(key: bytes) -> str:
    """A short, non-secret name for *key*: an HMAC of a fixed label under it,
    so the id says nothing about the key itself."""
    return hmac.new(key, b"traxjourney credentials key id", hashlib.sha256).hexdigest()[:_KEY_ID_HEX]


def _parse_key(raw: str, name: str) -> bytes:
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        raise RuntimeError(
            f"{name} is not a hexadecimal string. {_FIX}") from None
    if len(key) != 32:
        raise RuntimeError(
            f"{name} must be 32 bytes (64 hexadecimal characters), "
            f"got {len(key)} bytes. {_FIX}")
    return key


@dataclass(frozen=True)
class Keyring:
    """The current key (used for every write) and every key reads accept."""

    current_id: str
    keys: dict  # key id -> AESGCM


#: (current env value, retired env value) -> Keyring for the last pair parsed.
#: The env is read per call, as for JWT_SECRET, so a test can set it and a
#: restart is enough to rotate; parsing is done once per value.
_cached: Optional[tuple[tuple[str, str], Keyring]] = None


def credentials_keyring() -> Keyring:
    """The configured keys. Raises :class:`RuntimeError` when they are not
    usable — called at startup (api/router.py's lifespan) so a missing or
    malformed key stops the server instead of failing the first connect."""
    global _cached
    current = os.environ.get(KEY_ENV, "").strip()
    retired = os.environ.get(RETIRED_KEYS_ENV, "").strip()
    if _cached is not None and _cached[0] == (current, retired):
        return _cached[1]
    if not current:
        raise RuntimeError(
            f"{KEY_ENV} is not configured. It encrypts the Strava, Polarsteps "
            f"and Immich credentials users connect. {_FIX} Keep it safe: if it "
            "is lost, users have to reconnect those services.")
    key = _parse_key(current, KEY_ENV)
    current_id = _key_id(key)
    raw_keys = {current_id: key}
    for i, raw in enumerate(r.strip() for r in retired.split(",") if r.strip()):
        old = _parse_key(raw, f"{RETIRED_KEYS_ENV} entry {i + 1}")
        old_id = _key_id(old)
        # The same key listed twice (or the current key listed as retired) is
        # harmless; two different keys sharing an id would be ambiguous.
        if raw_keys.setdefault(old_id, old) != old:
            raise RuntimeError(f"{RETIRED_KEYS_ENV} entry {i + 1} has the same key "
                               f"id as another configured key. {_FIX}")
    keyring = Keyring(current_id=current_id,
                      keys={kid: AESGCM(k) for kid, k in raw_keys.items()})
    _cached = ((current, retired), keyring)
    return keyring


def _aad(column: str, key_id: str) -> bytes:
    return f"{PREFIX}{key_id}:{column}".encode()


def is_encrypted(value: Optional[str]) -> bool:
    return bool(value and value.startswith(PREFIX))


def key_id_of(value: str) -> Optional[str]:
    """The key id an encrypted value names, or None for anything else."""
    if not is_encrypted(value):
        return None
    return value[len(PREFIX):].split(":", 1)[0]


def encrypt_credential(plaintext: str, column: str) -> str:
    """Encrypt *plaintext* under the current key for ``table.column`` *column*."""
    keyring = credentials_keyring()
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = keyring.keys[keyring.current_id].encrypt(
        nonce, plaintext.encode("utf-8"), _aad(column, keyring.current_id))
    payload = base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")
    return f"{PREFIX}{keyring.current_id}:{payload}"


def decrypt_credential(stored: str, column: str) -> str:
    """Decrypt a value :func:`encrypt_credential` produced for *column*.

    Raises :class:`CredentialDecryptError` for a value that is not in the
    format, names a key that is not configured, or does not authenticate (the
    wrong key, or a value that belongs to another column).
    """
    if not is_encrypted(stored):
        raise CredentialDecryptError(f"{column}: value is not encrypted")
    key_id, _, payload = stored[len(PREFIX):].partition(":")
    cipher = credentials_keyring().keys.get(key_id)
    if cipher is None:
        raise CredentialDecryptError(
            f"{column}: encrypted with key {key_id!r}, which is neither "
            f"{KEY_ENV} nor listed in {RETIRED_KEYS_ENV}")
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        plain = cipher.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _aad(column, key_id))
    except (InvalidTag, ValueError, binascii.Error):
        raise CredentialDecryptError(
            f"{column}: value under key {key_id!r} does not authenticate") from None
    return plain.decode("utf-8")


class EncryptedString(TypeDecorator):
    """A string column stored encrypted (see the module docstring).

    The impl is the same ``AutoString`` the columns had before, so the schema
    and ``alembic check`` see no change. The empty string stays empty — it is
    the "not connected" default these columns already use and carries no
    secret. A stored value that does not decrypt reads as empty and is logged
    (column and key id only): that user's service shows as disconnected and can
    be reconnected, where raising would break every read of the row — account
    deletion included.
    """

    impl = sqlmodel.sql.sqltypes.AutoString
    cache_ok = True

    def __init__(self, column: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: ``table.column``, bound into each value's associated data.
        self.column = column

    def process_bind_param(self, value, dialect):
        if not value:
            return value
        return encrypt_credential(value, self.column)

    def process_result_value(self, value, dialect):
        if not value:
            return value
        try:
            return decrypt_credential(value, self.column)
        except CredentialDecryptError as exc:
            _log.warning("Stored credential unreadable, treated as not connected: %s", exc)
            return ""
