"""Strava rate limiting is per *application*, not per request (issue #130).

The limiter used to be constructed inside each ``StravaAPI``, and ``StravaAPI``
is built fresh for every incoming request — so every request got an empty
window and the limiter never limited anything. These tests pin the fix:

  1. all clients in the process share the same windows;
  2. both of Strava's quotas (100/15min and 1000/day) are enforced;
  3. a full window fails fast instead of parking a request thread;
  4. ``remaining_requests`` reflects real usage, so the callers that defer work
     on it (``_enrich_activities``) actually defer.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.api.strava_client import (
    _DAILY_LIMITER,
    _SHORT_TERM_LIMITER,
    RateLimiter,
    StravaAPI,
)
from src.config.settings import Config
from src.exceptions.errors import APIError, RateLimitError


class DummyConfig(Config):
    def __init__(self):
        super().__init__()
        self.set("strava.client_id", "id")
        self.set("strava.client_secret", "secret")


def _client() -> StravaAPI:
    client = StravaAPI(DummyConfig())
    client.token_data = {
        "access_token": "t", "refresh_token": "r", "expires_at": time.time() + 1000,
    }
    return client


class TestSharedAcrossInstances:
    def test_two_clients_share_one_window(self):
        """The regression itself: a second client (i.e. the next HTTP request)
        must not start with a fresh, empty quota."""
        first, second = _client(), _client()

        with patch("src.api.strava_client.requests.request") as mock_req:
            mock_req.return_value = MagicMock(status_code=200)
            mock_req.return_value.json.return_value = {}
            first.get_activities()
            first.get_activities()

        assert _SHORT_TERM_LIMITER.current_usage == 2
        assert second.remaining_requests == RateLimiter.MAX_REQUESTS - 2

    def test_remaining_requests_reflects_real_usage(self):
        """`_enrich_activities` defers work when this drops to <= 2. With a
        per-instance limiter it never dropped at all."""
        client = _client()
        assert client.remaining_requests == RateLimiter.MAX_REQUESTS

        _SHORT_TERM_LIMITER._timestamps.extend(
            [time.monotonic()] * (RateLimiter.MAX_REQUESTS - 1)
        )

        assert client.remaining_requests == 1

    def test_daily_quota_is_enforced_too(self):
        """Strava also caps 1000/day; the 15-min window alone lets 9600
        requests through in a day."""
        client = _client()
        _DAILY_LIMITER._timestamps.extend([time.monotonic()] * 999)

        assert client.remaining_requests == 1

    def test_remaining_requests_is_the_tightest_window(self):
        client = _client()
        _SHORT_TERM_LIMITER._timestamps.extend([time.monotonic()] * 95)
        _DAILY_LIMITER._timestamps.extend([time.monotonic()] * 500)

        # 5 left in the 15-min window vs 500 in the daily one.
        assert client.remaining_requests == 5


class TestFailFast:
    def test_full_window_raises_instead_of_blocking(self):
        """A shared limiter genuinely fills up. Parking a request thread for
        the rest of the window would turn a quota problem into an outage."""
        limiter = RateLimiter(max_requests=1, window_seconds=900, name="test")
        limiter.acquire()

        started = time.monotonic()
        with pytest.raises(RateLimitError):
            limiter.acquire(max_wait=0.2)

        assert time.monotonic() - started < 5  # failed fast, didn't wait 15 min

    def test_no_request_is_sent_when_throttled(self):
        """The point of a limiter: the call never reaches Strava. The 429 retry
        path only helps *after* Strava has already rejected us."""
        _SHORT_TERM_LIMITER._timestamps.extend(
            [time.monotonic()] * RateLimiter.MAX_REQUESTS
        )

        with patch("src.api.strava_client.requests.request") as mock_req:
            with patch.object(RateLimiter, "MAX_WAIT_SECONDS", 0.1):
                with pytest.raises(RateLimitError):
                    _client().get_activities()

        mock_req.assert_not_called()

    def test_rate_limit_error_is_an_api_error(self):
        """So the existing app-level handler maps it to a 502 with an
        actionable message — no new wiring, and callers catching APIError
        keep working."""
        assert issubclass(RateLimitError, APIError)

    def test_throttling_is_counted(self, metric):
        before = metric("traxjourney_strava_throttled_total", window="15min")
        _SHORT_TERM_LIMITER._timestamps.extend(
            [time.monotonic()] * RateLimiter.MAX_REQUESTS
        )

        with patch("src.api.strava_client.requests.request"):
            with patch.object(RateLimiter, "MAX_WAIT_SECONDS", 0.1):
                with pytest.raises(RateLimitError):
                    _client().get_activities()

        assert metric("traxjourney_strava_throttled_total", window="15min") - before == 1

    def test_waits_for_a_slot_that_frees_up_in_time(self):
        """Short waits are still honoured — a slot about to expire is worth
        waiting for rather than failing the user's import."""
        limiter = RateLimiter(max_requests=1, window_seconds=1, name="test")
        limiter.acquire()

        started = time.monotonic()
        limiter.acquire(max_wait=5)
        elapsed = time.monotonic() - started

        assert 0.5 < elapsed < 3


class TestWindowMechanics:
    def test_expired_timestamps_free_their_slots(self):
        limiter = RateLimiter(max_requests=2, window_seconds=900, name="test")
        limiter._timestamps.extend([time.monotonic() - 901] * 2)

        assert limiter.current_usage == 0
        limiter.acquire(max_wait=0)  # a slot is free, no waiting needed
        assert limiter.current_usage == 1

    def test_concurrent_callers_never_exceed_the_limit(self):
        """The lock is released while waiting, so the window is re-checked
        under it before claiming — two threads must never take the last slot."""
        limiter = RateLimiter(max_requests=5, window_seconds=900, name="test")
        granted, refused = [], []

        def _worker():
            try:
                limiter.acquire(max_wait=0)
                granted.append(1)
            except RateLimitError:
                refused.append(1)

        threads = [threading.Thread(target=_worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(granted) == 5
        assert len(refused) == 15
        assert limiter.current_usage == 5

    def test_reset_clears_the_window(self):
        limiter = RateLimiter(max_requests=5, window_seconds=900, name="test")
        limiter.acquire()
        limiter.reset()
        assert limiter.current_usage == 0


class TestEnrichmentDefersInsteadOfDropping:
    """`_enrich_activities` returns the activities it couldn't enrich so a
    background job can retry them after the window resets. Now that the window
    can actually fill, a throttled call must land in that list — the generic
    `except Exception: pass` below it would otherwise drop the activity for
    good."""

    def _activities(self, make_activity, count):
        return [make_activity(id=i + 1) for i in range(count)]

    def test_throttled_activity_and_the_rest_are_deferred(self, make_activity):
        from api.activities import _enrich_activities

        activities = self._activities(make_activity, 4)
        client = MagicMock()
        client.remaining_requests = 50
        client.get_activity_streams.side_effect = RateLimitError("window full")

        pending = _enrich_activities(activities, client)

        assert [a.id for a in pending] == [1, 2, 3, 4]
        # Stopped at the first refusal — the rest would hit the same wall.
        assert client.get_activity_streams.call_count == 1

    def test_other_failures_still_skip_silently(self, make_activity):
        """A private activity or a transient network error is not a quota
        problem: keep going, and don't queue a retry."""
        from api.activities import _enrich_activities

        activities = self._activities(make_activity, 3)
        client = MagicMock()
        client.remaining_requests = 50
        client.get_activity_streams.side_effect = ValueError("private activity")

        assert _enrich_activities(activities, client) == []
        assert client.get_activity_streams.call_count == 3


class TestRateLimitMetrics:
    def test_usage_gauge_tracks_the_shared_window(self, metric):
        """#125 skipped this gauge because a per-instance limiter made it
        meaningless; sharing the limiter is what makes it real."""
        with patch("src.api.strava_client.requests.request") as mock_req:
            mock_req.return_value = MagicMock(status_code=200)
            mock_req.return_value.json.return_value = {}
            _client().get_activities()

        assert metric("traxjourney_strava_rate_limit_usage", window="15min") == 1
        assert metric("traxjourney_strava_rate_limit_usage", window="daily") == 1

    def test_capacity_gauges_report_stravas_quotas(self, metric):
        assert metric("traxjourney_strava_rate_limit_capacity", window="15min") == 100
        assert metric("traxjourney_strava_rate_limit_capacity", window="daily") == 1000
