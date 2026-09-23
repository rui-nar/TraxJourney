"""Best-effort revocation of a user's Strava authorisation (issue #440).

Shared by ``DELETE /api/strava/disconnect`` and account deletion so the two
never drift apart: both drop the user's local Strava data regardless of
what Strava answers, and both leave the app listed in the athlete's Strava
settings only if the revoke call itself failed — which is logged, without
the token, so an operator can see it.
"""
from __future__ import annotations

import os
import time

from models.user import StravaToken
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


def deauthorize_strava(token_row: StravaToken, cfg: Config | None = None) -> bool:
    """Revoke the app at Strava for the athlete behind ``token_row``.

    Refreshes the access token first when it has expired (Strava rejects an
    expired one). Never raises: a network failure, a timeout, a refresh that
    Strava refuses or a missing client config are logged as a warning and
    reported as ``False`` so the caller still removes the local data. The
    refreshed token is not persisted — the caller is about to delete the row.
    """
    if not token_row.access_token:
        return False
    try:
        oauth = OAuth2Session(cfg if cfg is not None else strava_config())
        access_token = token_row.access_token
        if token_row.expires_at < time.time():
            access_token = oauth.refresh_token(token_row.refresh_token)["access_token"]
        oauth.deauthorize(access_token)
    except Exception:
        # Anticipated — Strava down, token already revoked on Strava's side,
        # refresh token stale. The message never carries our token: it is sent
        # in a header, and the refresh error quotes only Strava's reply.
        _log.warning(
            "strava deauthorize failed for user=%s; local data is removed anyway",
            token_row.user_info_id, exc_info=True,
        )
        return False
    return True
