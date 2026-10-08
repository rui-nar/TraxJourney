"""OAuth2 helper for Strava authentication."""

import webbrowser
import requests
from urllib.parse import urlencode
from typing import Any, Dict, Optional

from src.config.settings import Config
from src.exceptions.errors import AuthenticationError, TokenError, ConfigurationError


class OAuth2Session:
    """Simple OAuth2 session manager for Strava."""

    AUTH_URL = "https://www.strava.com/oauth/authorize"
    TOKEN_URL = "https://www.strava.com/oauth/token"
    # The documented revocation endpoint; /oauth/deauthorize is legacy and
    # unsupported after 2027-06-01 (issue #440).
    REVOKE_URL = "https://www.strava.com/oauth/revoke"
    # (connect, read) seconds. Revocation sits on the disconnect and
    # account-deletion paths (issue #440), which must not hang on Strava.
    TOKEN_TIMEOUT: tuple = (5, 15)

    def __init__(self, config: Config):
        self.config = config
        self.client_id = config.get("strava.client_id")
        self.client_secret = config.get("strava.client_secret")
        self.redirect_uri = config.get("strava.redirect_uri")
        if not self.client_id or not self.client_secret:
            raise ConfigurationError("Strava client_id/secret not configured")

    def authorization_url(self, scope: str = "activity:read_all") -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "approval_prompt": "auto",
            "scope": scope,
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    def open_authorization(self, scope: str = "activity:read_all") -> None:
        url = self.authorization_url(scope)
        webbrowser.open(url)

    def exchange_code(self, code: str) -> Dict[str, Any]:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
        resp = requests.post(self.TOKEN_URL, data=data, timeout=self.TOKEN_TIMEOUT)
        if resp.status_code != 200:
            raise AuthenticationError(f"Failed to exchange code: {resp.text}")
        token_data = resp.json()
        return token_data

    def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        resp = requests.post(self.TOKEN_URL, data=data, timeout=self.TOKEN_TIMEOUT)
        if resp.status_code != 200:
            raise TokenError(f"Failed to refresh token: {resp.text}")
        return resp.json()

    def revoke(self, token: str, token_type_hint: str) -> None:
        """Revoke this application's access to the athlete's Strava account.

        ``POST /oauth/revoke`` as documented: HTTP Basic ``client_id:client_secret``,
        the token in the form body with ``token_type_hint`` (``"access_token"``
        or ``"refresh_token"``). Revoking either token revokes the other, an
        expired token is accepted, and Strava answers 200 whether or not it
        knew the token — so no refresh is needed first. The token travels in
        the body, never in the URL, so it cannot leak through a requests
        exception message or an access log.

        A 503 is documented as safe to retry and is retried once. Any other
        non-200 raises :class:`AuthenticationError` carrying only the status
        code — the body is deliberately left out of the message.
        """
        for attempt in range(2):
            resp = requests.post(
                self.REVOKE_URL,
                auth=(self.client_id, self.client_secret),
                data={"token": token, "token_type_hint": token_type_hint},
                timeout=self.TOKEN_TIMEOUT,
            )
            if resp.status_code == 200:
                return
            if resp.status_code != 503 or attempt == 1:
                raise AuthenticationError(
                    f"Failed to revoke at Strava: HTTP {resp.status_code}"
                )
