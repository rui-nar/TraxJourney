"""Strava API client for TraxJourney."""

import threading
import requests
import time
from collections import deque
from typing import Any, Dict, Optional

from src.config.settings import Config
from src.auth.oauth import OAuth2Session
from src.auth.token_store import TokenStore
from src.exceptions.errors import (
    APIError,
    AuthenticationError,
    RateLimitError,
    TokenError,
)
from src.utils.logging import get_logger
from src.utils.metrics import (
    STRAVA_RATE_LIMIT_CAPACITY,
    STRAVA_RATE_LIMIT_USAGE,
    STRAVA_THROTTLED,
    normalise_path,
    outcome_for_status,
    track_external,
)

_log = get_logger(__name__)


class RateLimiter:
    """Sliding-window rate limiter, defaulting to Strava's 100 req/15 min.

    Strava enforces 100 requests per 15 minutes *and* 1000 per day, per
    application — not per user, not per request. A limiter is therefore only
    meaningful if every caller in the process shares one; see
    :data:`_SHORT_TERM_LIMITER` / :data:`_DAILY_LIMITER` below.

    ``acquire`` waits at most ``max_wait`` seconds for a free slot and then
    raises, rather than blocking indefinitely. A shared limiter genuinely fills
    up, and a request thread parked for the remainder of a 15-minute window (or
    the rest of the day, for the daily quota) turns a quota problem into an
    outage. Callers that can defer work check :attr:`remaining` first and
    reschedule instead — see ``_enrich_activities`` in api/activities.py.
    """

    WINDOW_SECONDS: int = 900  # 15 minutes
    MAX_REQUESTS: int = 100
    MAX_WAIT_SECONDS: float = 60.0
    _POLL_SECONDS: float = 0.5

    def __init__(
        self,
        max_requests: int = MAX_REQUESTS,
        window_seconds: int = WINDOW_SECONDS,
        name: str = "15min",
    ) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self.name = name
        self._timestamps: deque = deque()
        self._lock: threading.Lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Drop timestamps that have fallen out of the window. Caller holds the lock."""
        cutoff = now - self._window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def acquire(self, max_wait: Optional[float] = None) -> None:
        """Claim a request slot, waiting up to *max_wait* seconds for one.

        Raises :class:`RateLimitError` if no slot comes free in time. The lock
        is released while waiting and the window re-checked on each pass, so a
        slot freed by another thread is picked up promptly and two threads can
        never both claim the last one.
        """
        limit = self.MAX_WAIT_SECONDS if max_wait is None else max_wait
        deadline = time.monotonic() + limit

        while True:
            with self._lock:
                now = time.monotonic()
                self._prune(now)
                if len(self._timestamps) < self._max_requests:
                    self._timestamps.append(now)
                    return
                wait_for = self._timestamps[0] + self._window_seconds - now

            if time.monotonic() + wait_for > deadline:
                raise RateLimitError(
                    f"Strava {self.name} rate limit reached "
                    f"({self._max_requests} requests); a slot frees up in "
                    f"{wait_for:.0f}s. Please try again later."
                )
            time.sleep(min(wait_for, self._POLL_SECONDS))

    @property
    def current_usage(self) -> int:
        """Number of requests made in the current window."""
        with self._lock:
            self._prune(time.monotonic())
            return len(self._timestamps)

    @property
    def remaining(self) -> int:
        """Slots left in the current window."""
        return self._max_requests - self.current_usage

    def reset(self) -> None:
        """Forget every recorded request. For tests — the shared limiters are
        process-wide, so without this one test's calls leak into the next."""
        with self._lock:
            self._timestamps.clear()


# Strava's quotas are per *application*, so these are shared by every StravaAPI
# instance in the process (issue #130 — they used to be per-instance, and
# StravaAPI is constructed per request, so the window was always empty and the
# limiter never actually limited anything).
#
# Process-wide is as far as this design goes: a second uvicorn/gunicorn worker
# would get its own counters and the app could exceed the quota by a factor of
# the worker count. entrypoint.sh runs a single worker; going wider means moving
# this state somewhere shared (Redis, or the DB).
_SHORT_TERM_LIMITER = RateLimiter(
    max_requests=RateLimiter.MAX_REQUESTS,
    window_seconds=RateLimiter.WINDOW_SECONDS,
    name="15min",
)
_DAILY_LIMITER = RateLimiter(max_requests=1000, window_seconds=86400, name="daily")

_ALL_LIMITERS = (_SHORT_TERM_LIMITER, _DAILY_LIMITER)


def reset_rate_limiters() -> None:
    """Clear both shared windows. For tests."""
    for limiter in _ALL_LIMITERS:
        limiter.reset()


# Now that usage is process-wide it is worth reporting: near-100 in the 15-min
# window means imports are about to start deferring (issue #125 skipped this
# gauge precisely because the per-instance limiter made it meaningless).
STRAVA_RATE_LIMIT_USAGE.labels("15min").set_function(
    lambda: _SHORT_TERM_LIMITER.current_usage
)
STRAVA_RATE_LIMIT_USAGE.labels("daily").set_function(
    lambda: _DAILY_LIMITER.current_usage
)
STRAVA_RATE_LIMIT_CAPACITY.labels("15min").set(RateLimiter.MAX_REQUESTS)
STRAVA_RATE_LIMIT_CAPACITY.labels("daily").set(1000)


class StravaAPI:
    """Client for interacting with the Strava API."""

    BASE_URL = "https://www.strava.com/api/v3"
    MAX_RETRIES: int = 3
    # (connect, read) seconds. Without this, requests waits forever: a stalled
    # Strava connection parked a worker thread indefinitely and, before the
    # refresh moved off the request path, surfaced as the client's own 30 s
    # TimeoutException with no server-side error at all (issue #148). Every other
    # outbound client here sets one (hafas 12 s, polarsteps 20 s, overpass 45 s).
    # The read budget is generous because a streams response is multi-MB.
    TIMEOUT: tuple = (5, 60)

    def __init__(self, config: Config, user_id: str = "default"):
        self.config = config
        self.user_id = user_id
        self.oauth = OAuth2Session(config)
        self.token_data = TokenStore.load_token(user_id) or {}
        # Shared, not per-instance: Strava's quota belongs to the application,
        # and this object is built fresh for every request (issue #130).
        self._rate_limiters = _ALL_LIMITERS

    def _ensure_token(self) -> None:
        """Ensure access token is valid, refresh if needed."""
        if not self.token_data:
            raise AuthenticationError("No token data available. Please authenticate with Strava.")

        # simple expiration check
        if self.token_data.get("expires_at", 0) < time.time():
            try:
                self.token_data = self.oauth.refresh_token(self.token_data.get("refresh_token"))
                TokenStore.save_token(self.user_id, self.token_data)
            except TokenError as e:
                # Token refresh failed - clear the invalid token
                self.clear_token()
                raise AuthenticationError(
                    "Token refresh failed. Please re-authenticate with Strava. "
                    f"Error: {str(e)}"
                )

    def clear_token(self) -> None:
        """Clear stored token data."""
        self.token_data = {}
        try:
            TokenStore.delete_token(self.user_id)
        except Exception:
            # Anticipated — the token may already be gone (e.g. a previous
            # clear_token() call, or it was never persisted).
            _log.warning("clear_token: could not delete stored token for user=%s", self.user_id, exc_info=True)

    def set_token(self, token_data: Dict[str, Any]) -> None:
        """Store initial token data."""
        self.token_data = token_data
        TokenStore.save_token(self.user_id, token_data)

    def request(self, method: str, path: str, max_retries: int = MAX_RETRIES, **kwargs) -> Dict[str, Any]:
        """Make an authenticated request with rate limiting and retry logic.

        Retry behaviour:
          - HTTP 401: attempt one token refresh then retry; raise AuthenticationError on failure
          - HTTP 429: wait Retry-After (or 60s) then retry
          - HTTP 5xx: exponential backoff (1s, 2s, 4s) then retry
          - HTTP 4xx (not 401/429): raise APIError immediately, no retry

        Raises :class:`RateLimitError` (an ``APIError``, so it surfaces as a
        502) if the application's own quota window is full and stays full — no
        call is made in that case, which is the point: the 429 path above is
        only reached once Strava has already rejected us.

        Every branch here can take minutes end to end (a 60 s limiter wait per
        attempt, plus a >=60 s sleep per 429), which is why no caller may sit on
        this inside a request/response cycle — see :func:`api.activities._refresh_activity_job`
        for the background-job pattern that issue #148 moved the re-fetch to.
        """
        self._ensure_token()
        headers = {"Authorization": f"Bearer {self.token_data['access_token']}"}
        url = f"{self.BASE_URL}{path}"
        # Popped once, outside the retry loop, so every attempt gets the same
        # budget (popping inside would silently fall back to the default on retry).
        timeout = kwargs.pop("timeout", self.TIMEOUT)

        _refreshed = False
        last_error: Optional[str] = None
        # Templated so an activity ID never becomes a metric label value.
        endpoint = normalise_path(path)
        for attempt in range(max_retries):
            self._acquire_slot()
            # Inside the retry loop: each attempt is a real call against
            # Strava's quota and is counted as one.
            with track_external("strava", endpoint) as call:
                resp = requests.request(
                    method, url, headers=headers, timeout=timeout, **kwargs,
                )
                call.outcome = outcome_for_status(resp.status_code)

            if resp.status_code < 400:
                return resp.json()

            if resp.status_code == 401:
                if not _refreshed:
                    # Token may have been revoked or expired early — try refresh once
                    _refreshed = True
                    try:
                        self.token_data = self.oauth.refresh_token(
                            self.token_data.get("refresh_token")
                        )
                        TokenStore.save_token(self.user_id, self.token_data)
                        headers = {"Authorization": f"Bearer {self.token_data['access_token']}"}
                        continue   # retry with new token
                    except Exception:
                        # Anticipated — the refresh token itself may be
                        # revoked/expired; falls through to clear_token() and
                        # AuthenticationError below either way.
                        _log.warning("token refresh after 401 failed for user=%s", self.user_id, exc_info=True)
                self.clear_token()
                raise AuthenticationError(
                    "Strava access token is invalid or expired. "
                    "Please re-authenticate via Add track → From Strava…"
                )

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                time.sleep(max(retry_after, 60))
                last_error = "Rate limited (429)"
                continue

            if resp.status_code >= 500:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                last_error = f"Server error {resp.status_code}: {resp.text}"
                continue

            # Other 4xx: not retryable
            raise APIError(f"Strava API error {resp.status_code}: {resp.text}")

        raise APIError(f"Request failed after {max_retries} attempts. Last error: {last_error}")

    def get_activities(self, **params) -> Dict[str, Any]:
        """Fetch list of activities."""
        return self.request("GET", "/athlete/activities", params=params)

    def get_activity(self, activity_id: int) -> Dict[str, Any]:
        """Fetch full metadata for a single activity."""
        return self.request("GET", f"/activities/{activity_id}")

    def get_activity_streams(self, activity_id: int) -> Dict[str, Any]:
        """Fetch GPS streams (latlng, altitude, time, distance) for a single activity."""
        return self.request(
            "GET",
            f"/activities/{activity_id}/streams",
            params={"keys": "latlng,altitude,time,distance", "key_by_type": "true"},
        )

    def _acquire_slot(self) -> None:
        """Claim a slot in every quota window before calling Strava.

        Both windows must have room. Failing here means no HTTP call is made at
        all — which is the whole point of a limiter, versus discovering the
        quota is gone from a 429.
        """
        for limiter in self._rate_limiters:
            try:
                limiter.acquire()
            except RateLimitError:
                STRAVA_THROTTLED.labels(limiter.name).inc()
                raise

    @property
    def remaining_requests(self) -> int:
        """Requests still available before the tightest quota window is full.

        Callers use this to decide whether to keep fetching or defer the rest
        (``_enrich_activities`` in api/activities.py). It only became a real
        signal once the limiters were shared process-wide — per instance it
        reported a near-full quota on every request (issue #130).
        """
        return min(limiter.remaining for limiter in self._rate_limiters)
