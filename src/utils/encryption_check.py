"""Shared server-side check for client-side E2EE ciphertext envelopes (issue #26).

The server never holds the key material and never decrypts field content — it
only needs a cheap structural test to tell ciphertext apart from plaintext so
it can (a) skip fields it can't safely read/derive from (GPS decode, JSON
parse, third-party translation) and (b) avoid clobbering an already-encrypted
row on a background write (Strava/Polarsteps sync, activity enrichment).

Originally lived as a private helper in api/memories.py (issue #26/#27);
promoted here so api/memories.py, api/share.py (issue #28), and the
activity-encryption guards (issue #29) all share one implementation.
"""
from __future__ import annotations

import base64
import binascii
from typing import Optional


def is_encrypted_envelope(value: Optional[str]) -> bool:
    """True if *value* looks like a client-side E2EE ciphertext envelope
    (`v1.<b64 wrapped DEK>.<b64 ciphertext>`, see `EncryptedField` in
    flutter_client/lib/src/crypto/e2ee_crypto.dart) rather than plaintext.

    Cheap structural check only — the server cannot and does not decrypt.
    """
    if not value:
        return False
    parts = value.split(".")
    return len(parts) == 3 and parts[0] == "v1"


# Byte lengths `EncryptedField.isWellFormed` checks: XChaCha20-Poly1305's
# 24-byte nonce and 16-byte MAC around a 32-byte DEK for the wrapped key, and
# at least a nonce and a MAC for the ciphertext (an empty plaintext).
_NONCE_LEN = 24
_MAC_LEN = 16
_WRAPPED_DEK_LEN = _NONCE_LEN + 32 + _MAC_LEN
_MIN_CIPHERTEXT_LEN = _NONCE_LEN + _MAC_LEN


def _standard_b64_len(part: str) -> Optional[int]:
    """Decoded length of *part* if it is standard base64 with padding, the
    form `EncryptedField.encode` writes (`+`, `/`, `=`), else None."""
    if not part or len(part) % 4:
        return None
    try:
        return len(base64.b64decode(part, validate=True))
    except (binascii.Error, ValueError):
        return None


def is_well_formed_envelope(value: Optional[str]) -> bool:
    """True if *value* could be an envelope `EncryptedField.encode` produced.

    The strict counterpart of :func:`is_encrypted_envelope`, mirroring
    `EncryptedField.isWellFormed` in e2ee_crypto.dart: version `v1`, then a
    wrapped DEK of exactly nonce + key + MAC bytes and a ciphertext of at least
    nonce + MAC bytes, both standard base64 with padding. Text a user typed,
    such as "v1.2.3", passes the loose check but not this one. Used where the
    server stores a client-computed value it can never read back, so that only
    real ciphertext gets there.
    """
    if not is_encrypted_envelope(value):
        return False
    _, wrapped_dek, ciphertext = value.split(".")
    return (_standard_b64_len(wrapped_dek) == _WRAPPED_DEK_LEN
            and (_standard_b64_len(ciphertext) or 0) >= _MIN_CIPHERTEXT_LEN)
