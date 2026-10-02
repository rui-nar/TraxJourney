"""Shared fixtures for all test modules."""

import os
from datetime import datetime
import pytest

# Sign with an explicit key. The app refuses to start without one (issue #156),
# and the constant it used to fall back to is 23 bytes — under the 32-byte
# minimum for HS256, which is what produced the InsecureKeyLengthWarning noise
# across the suite. setdefault so a caller can still pin their own.
os.environ.setdefault("JWT_SECRET", "0123456789abcdef" * 4)

from prometheus_client import REGISTRY

from src.models.activity import Activity


@pytest.fixture(autouse=True)
def _reset_strava_rate_limiters():
    """Clear the process-wide Strava quota windows around every test.

    The limiters are shared by design (issue #130 — Strava's quota is per
    application), which means one test's mocked calls otherwise count against
    the next one's, and a suite making 100+ calls would start really sleeping.
    """
    from src.api.strava_client import reset_rate_limiters

    reset_rate_limiters()
    yield
    reset_rate_limiters()


@pytest.fixture(autouse=True)
def _reset_project_payload_cache():
    """Empty the per-project payload cache around every test.

    ``/meta`` and the geo endpoints are served from a process-wide dict keyed by
    (user id, project name, variant) — see api.geo. Every test builds a fresh
    in-memory database whose ids restart at 1, and most of them call their
    project "Trip", so without this one test's cached payload is a HIT in the
    next one and the assertion reads the previous test's data (issue #178).
    """
    from api.geo import _geo_cache, _geo_gen

    _geo_cache.clear()
    _geo_gen.clear()
    yield
    _geo_cache.clear()
    _geo_gen.clear()


def release_per_test_engines():
    """Drop the library caches that keep each test's engine alive (issue #540).

    Most tests build their own in-memory engine, and two process-wide caches
    keep it reachable after the test ends:

    - every SQLModel mapper keeps an LRU of compiled INSERT/UPDATE/DELETE
      statements (100 per mapper) keyed by the *dialect*, and each engine has
      its own dialect, holding every column type's per-dialect memo;
    - FastAPI keeps LRUs (4096 entries) of every dependency callable it has
      classified, which include the tests' ``dependency_overrides`` lambdas —
      closures over the test's engine, its pool and so its whole in-memory
      database.

    The app has one engine and a fixed set of dependencies, so neither cache
    grows in production. Across the suite they held ~250 MB by the end.
    """
    from fastapi.dependencies import models as fastapi_dependency_models
    from sqlmodel.main import default_registry

    for mapper in default_registry.mappers:
        mapper._compiled_cache.clear()
    for name in ("_is_gen_callable_cached", "_is_async_gen_callable_cached",
                 "_is_coroutine_callable_cached"):
        cache = getattr(fastapi_dependency_models, name, None)
        if cache is not None:
            cache.cache_clear()


@pytest.fixture(autouse=True)
def _release_per_test_engines():
    yield
    release_per_test_engines()


@pytest.fixture
def metric():
    """Read a Prometheus sample by name + labels, 0.0 if the series is absent.

    The default registry is process-wide and counters only ever increase, so
    tests must assert on the *delta* around the code under test — never on an
    absolute value, which depends on whatever else ran earlier in the session.
    """
    def _read(name: str, **labels) -> float:
        return REGISTRY.get_sample_value(name, labels or None) or 0.0
    return _read


@pytest.fixture
def make_activity():
    """Factory fixture — call with keyword overrides to customise any field."""
    def _make(**kwargs):
        defaults = dict(
            id=1,
            name="Morning Run",
            type="Run",
            distance=10000.0,
            moving_time=3600,
            elapsed_time=3660,
            total_elevation_gain=100.0,
            start_date=datetime(2024, 6, 15, 7, 0),
            start_date_local=datetime(2024, 6, 15, 9, 0),
            timezone="Europe/Paris",
            achievement_count=2,
            kudos_count=5,
            comment_count=1,
            athlete_count=1,
            photo_count=0,
            trainer=False,
            commute=False,
            manual=False,
            private=False,
            flagged=False,
            average_speed=2.78,   # m/s ≈ 10 km/h
            max_speed=3.5,
            pr_count=0,
            total_photo_count=0,
            has_kudoed=False,
        )
        defaults.update(kwargs)
        return Activity(**defaults)
    return _make
