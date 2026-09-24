"""Best-effort revocation of a user's Strava authorisation (issue #440).

Shared by ``DELETE /api/strava/disconnect`` and account deletion so the two
never drift apart: both drop the user's local Strava data regardless of
what Strava answers, and both leave the app listed in the athlete's Strava
settings only if the revoke call itself failed — which is logged, without
the token, so an operator can see it.

Takes plain token fields rather than the ORM row so callers can close their
DB session before the network round trip and open a fresh one afterwards.
"""
from __future__ import annotations

import os

from src.auth.oauth import OAuth2Session
from src.config.settings import Config
from src.utils.logging import get_logger

_log = get_logger(__name__)


def strava_config() -> Config:
    """``config/config.json`` with the env overrides — same sourcing as
    ``api/strava.py``, built lazily so importing this module has no side effect."""
    cfg = Config("config/config.json")
    if os.environ.get("STRAVA_CLIENT_ID"):
        cfg.set("strava.client_id", os.environ["STRAVA_CLIENT_ID"])
    if os.environ.get("STRAVA_CLIENT_SECRET"):
        cfg.set("strava.client_secret", os.environ["STRAVA_CLIENT_SECRET"])
    return cfg


def deauthorize_strava(
    user_info_id: int,
    access_token: str,
    refresh_token: str,
    cfg: Config | None = None,
) -> bool:
    """Revoke the app at Strava for the athlete behind these tokens.

    Revokes the refresh token (which takes the access tokens with it), or the
    access token when no refresh token is stored. No refresh step: Strava's
    revoke accepts an expired token, so nothing is rotated and nothing can be
    left behind. Never raises: a network failure, a timeout, a rejected call
    or a missing client config are logged as a warning and reported as
    ``False`` so the caller still removes the local data.
    """
    token, hint = (
        (refresh_token, "refresh_token") if refresh_token
        else (access_token, "access_token")
    )
    if not token:
        return False
    try:
        OAuth2Session(cfg if cfg is not None else strava_config()).revoke(token, hint)
    except Exception:
        # Anticipated — Strava down, client config missing. The message never
        # carries a token: it is sent in the request body, and the revoke
        # error quotes only the HTTP status.
        _log.warning(
            "strava revoke failed for user=%s; local data is removed anyway",
            user_info_id, exc_info=True,
        )
        return False
    return True
