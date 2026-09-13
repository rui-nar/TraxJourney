"""Token storage using a local JSON file."""

import json
from pathlib import Path
from typing import Optional

from src.exceptions.errors import TokenError


class TokenStore:
    """Store and retrieve tokens in a local JSON file.

    Tokens are written to ``~/.config/traxjourney/tokens.json`` (created on
    first save).  This is simpler and more portable than ``keyring``, which
    can silently fall back to a non-persistent in-memory backend on some
    Windows / virtual-environment setups.
    """

    _TOKEN_DIR = Path.home() / ".config" / "traxjourney"
    _TOKEN_FILE = _TOKEN_DIR / "tokens.json"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _load_all(cls) -> dict:
        try:
            if cls._TOKEN_FILE.exists():
                return json.loads(cls._TOKEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    @classmethod
    def _save_all(cls, data: dict) -> None:
        cls._TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        cls._TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def save_token(user_id: str, token_data: dict) -> None:
        try:
            all_tokens = TokenStore._load_all()
            all_tokens[user_id] = token_data
            TokenStore._save_all(all_tokens)
        except Exception as e:
            raise TokenError(f"Unable to save token: {e}")

    @staticmethod
    def load_token(user_id: str) -> Optional[dict]:
        try:
            return TokenStore._load_all().get(user_id)
        except Exception as e:
            raise TokenError(f"Unable to load token: {e}")

    @staticmethod
    def delete_token(user_id: str) -> None:
        try:
            all_tokens = TokenStore._load_all()
            all_tokens.pop(user_id, None)
            TokenStore._save_all(all_tokens)
        except Exception as e:
            raise TokenError(f"Unable to delete token: {e}")
