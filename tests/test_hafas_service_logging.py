"""Logging tests for src/services/hafas_service.py (issue #205, Unit C).

Before issue #205 this module had no logger at all — every train-routing
failure was a complete blackout. These tests confirm it still logs a WARNING
for expected upstream flakiness (nothing nearby, train not on the board) and an
ERROR (via ``.exception()``) for anything unexpected (a raw network failure),
while raising ``HafasError`` unchanged so callers' fallback behaviour is
untouched.

Rewritten for the MOTIS/Transitous backend (issue #277). The four HAFAS
provider backends these cases used to exercise — ``db``, ``obb``, ``dsb``,
``vr`` — are gone along with the dead APIs behind them; the failure *modes*
they stood for are the same and are covered here against the new one.
"""
import logging
from unittest.mock import patch

import pytest

from src.services.hafas_service import HafasError, get_train_route
import src.services.hafas_service as svc


# Arbitrary coordinates — content doesn't matter, only that the lookup fails.
_LAT1, _LON1 = 52.5200, 13.4050  # Berlin
_LAT2, _LON2 = 48.8566, 2.3522   # Paris


@pytest.fixture(autouse=True)
def _clear_stop_cache():
    svc._stop_cache.clear()
    yield
    svc._stop_cache.clear()


@pytest.fixture(autouse=True)
def _no_real_backoff():
    with patch("src.services.hafas_service.time.sleep"):
        yield


def _lookup(train="ICE 123", date="2026-06-01"):
    return get_train_route(
        train_number=train, date=date,
        start_lat=_LAT1, start_lon=_LON1,
        end_lat=_LAT2, end_lon=_LON2,
    )


class TestUnusableInput:
    def test_missing_date_logs_warning_and_raises(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.services.hafas_service"):
            with pytest.raises(HafasError, match="No usable service date"):
                _lookup(date="")
        assert any(r.levelno == logging.WARNING for r in caplog.records)


class TestNoNearbyStops:
    def test_logs_warning_and_raises(self, caplog):
        """Reverse-geocode finding nothing is expected upstream flakiness —
        WARNING, not an exception traceback."""
        with patch("src.services.hafas_service._nearest_stop", return_value=None):
            with caplog.at_level(logging.WARNING, logger="src.services.hafas_service"):
                with pytest.raises(HafasError, match="Could not locate a station"):
                    _lookup()
        assert any(r.levelno == logging.WARNING for r in caplog.records)


class TestTrainNotOnTheBoard:
    def test_logs_warning_and_raises(self, caplog):
        with patch("src.services.hafas_service._nearest_stop",
                   return_value={"id": "x", "name": "Berlin Hbf", "tz": "Europe/Berlin"}), \
             patch("src.services.hafas_service._iter_trip_ids", return_value=iter([])):
            with caplog.at_level(logging.WARNING, logger="src.services.hafas_service"):
                with pytest.raises(HafasError, match="not found departing"):
                    _lookup()
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert "Berlin Hbf" in caplog.text


class TestUnexpectedNetworkFailure:
    def test_logs_exception_and_raises(self, caplog):
        """A raw network error is unexpected — must use .exception() (ERROR
        level + traceback), not a plain WARNING."""
        with patch("src.services.hafas_service.requests.get",
                   side_effect=ConnectionError("boom")):
            with caplog.at_level(logging.WARNING, logger="src.services.hafas_service"):
                with pytest.raises(HafasError, match="Train route lookup failed"):
                    _lookup()
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "expected an ERROR-level .exception() log"
        assert error_records[0].exc_info is not None

    def test_wraps_outbound_call_in_track_external(self, metric):
        """The failing call is still counted via track_external("hafas", ...)."""
        before = metric("traxjourney_external_requests_total", service="hafas",
                        endpoint="motis/reverse-geocode", outcome="exception")
        with patch("src.services.hafas_service.requests.get",
                   side_effect=ConnectionError("boom")):
            with pytest.raises(HafasError):
                _lookup()
        after = metric("traxjourney_external_requests_total", service="hafas",
                       endpoint="motis/reverse-geocode", outcome="exception")
        assert after - before == 1

    def test_retryable_status_is_recorded_distinctly(self, metric):
        """A 503 that gets retried is counted as retryable_error, not success."""
        class _Resp:
            status_code = 503

            def raise_for_status(self):
                raise AssertionError("should not be reached for a retryable status")

        before = metric("traxjourney_external_requests_total", service="hafas",
                        endpoint="motis/reverse-geocode", outcome="retryable_error")
        with patch("src.services.hafas_service.requests.get",
                   return_value=_Resp()):
            with pytest.raises(HafasError, match="unavailable after 3 attempts"):
                _lookup()
        after = metric("traxjourney_external_requests_total", service="hafas",
                       endpoint="motis/reverse-geocode", outcome="retryable_error")
        assert after - before == 3
