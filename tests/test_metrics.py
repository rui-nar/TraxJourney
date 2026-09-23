"""Unit tests for the metric catalogue helpers (issue #125)."""

import logging

import pytest
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
    JobExecutionEvent,
    JobSubmissionEvent,
)

from src.utils.metrics import (
    _operation_of,
    normalise_path,
    outcome_for_status,
    record_job_event,
    track_external,
)


class TestNormalisePath:
    """Numeric path segments must never reach a label value — one Strava
    activity ID per time series would blow up Prometheus' cardinality."""

    @pytest.mark.parametrize("raw,expected", [
        ("/activities/12345", "/activities/{id}"),
        ("/activities/12345/streams", "/activities/{id}/streams"),
        ("/users/1/trips/2", "/users/{id}/trips/{id}"),
        ("/athlete/activities", "/athlete/activities"),
        ("/", "/"),
    ])
    def test_numeric_segments_are_templated(self, raw, expected):
        assert normalise_path(raw) == expected

    def test_leaves_alphanumeric_segments_alone(self):
        """Only whole numeric segments are IDs; a slug that merely contains
        digits is a fixed route name and stays readable."""
        assert normalise_path("/v3/oauth2/token") == "/v3/oauth2/token"


class TestOutcomeForStatus:
    @pytest.mark.parametrize("status,expected", [
        (200, "success"), (204, "success"), (301, "success"),
        (400, "client_error"), (404, "client_error"),
        (401, "auth_error"), (403, "auth_error"),
        (429, "rate_limited"),
        (500, "server_error"), (503, "server_error"),
    ])
    def test_maps_onto_the_closed_set(self, status, expected):
        assert outcome_for_status(status) == expected


class TestTrackExternal:
    def test_records_success_and_duration(self, metric):
        before = metric("traxjourney_external_requests_total",
                        service="demo", endpoint="/ping", outcome="success")
        before_count = metric("traxjourney_external_request_duration_seconds_count",
                              service="demo", endpoint="/ping")

        with track_external("demo", "/ping"):
            pass

        assert metric("traxjourney_external_requests_total",
                      service="demo", endpoint="/ping", outcome="success") - before == 1
        assert metric("traxjourney_external_request_duration_seconds_count",
                      service="demo", endpoint="/ping") - before_count == 1

    def test_caller_set_outcome_wins(self, metric):
        before = metric("traxjourney_external_requests_total",
                        service="demo", endpoint="/ping", outcome="rate_limited")
        with track_external("demo", "/ping") as call:
            call.outcome = "rate_limited"
        assert metric("traxjourney_external_requests_total",
                      service="demo", endpoint="/ping", outcome="rate_limited") - before == 1

    def test_raising_block_records_exception_and_reraises(self, metric):
        before = metric("traxjourney_external_requests_total",
                        service="demo", endpoint="/boom", outcome="exception")
        with pytest.raises(ValueError):
            with track_external("demo", "/boom"):
                raise ValueError("upstream exploded")
        assert metric("traxjourney_external_requests_total",
                      service="demo", endpoint="/boom", outcome="exception") - before == 1

    def test_caller_set_outcome_survives_a_raise(self, metric):
        """The Strava client classifies a 401 itself and *then* raises. The
        classification must win over the generic "exception" fallback,
        otherwise every upstream auth failure is recorded as a client crash."""
        before = metric("traxjourney_external_requests_total",
                        service="demo", endpoint="/auth", outcome="auth_error")
        with pytest.raises(RuntimeError):
            with track_external("demo", "/auth") as call:
                call.outcome = "auth_error"
                raise RuntimeError("401")
        assert metric("traxjourney_external_requests_total",
                      service="demo", endpoint="/auth", outcome="auth_error") - before == 1


class TestTrackExternalLogging:
    """track_external is the single choke point for external-call logging
    (issue #205) — every caller gets an INFO/WARNING line for free."""

    def test_success_logs_at_info(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.utils.metrics"):
            with track_external("demo", "/ping"):
                pass
        info_lines = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("demo" in r.message and "/ping" in r.message for r in info_lines)

    def test_failure_logs_at_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.utils.metrics"):
            with pytest.raises(ValueError):
                with track_external("demo", "/boom"):
                    raise ValueError("upstream exploded")
        assert "demo" in caplog.text and "/boom" in caplog.text

    def test_caller_set_outcome_logs_at_warning(self, caplog):
        """A caller-classified failure (e.g. auth_error) still logs as a
        failure, not a success, even though no exception was raised."""
        with caplog.at_level(logging.WARNING, logger="src.utils.metrics"):
            with track_external("demo", "/auth") as call:
                call.outcome = "auth_error"
        assert "auth_error" in caplog.text and "/auth" in caplog.text


class TestRecordJobEvent:
    """APScheduler carries no duration on its execution events, so
    record_job_event pairs SUBMITTED with EXECUTED/ERROR to measure it."""

    def _submitted(self, job_id):
        return JobSubmissionEvent(EVENT_JOB_SUBMITTED, job_id, "default", [])

    def _executed(self, job_id, exception=None):
        code = EVENT_JOB_ERROR if exception else EVENT_JOB_EXECUTED
        return JobExecutionEvent(code, job_id, "default", None, exception=exception)

    def test_successful_run_counts_and_stamps_last_success(self, metric):
        before = metric("traxjourney_job_runs_total", job="demo_ok", result="success")
        record_job_event(self._submitted("demo_ok"))
        record_job_event(self._executed("demo_ok"))

        assert metric("traxjourney_job_runs_total", job="demo_ok", result="success") - before == 1
        assert metric("traxjourney_job_duration_seconds_count", job="demo_ok") == 1
        assert metric("traxjourney_job_last_success_timestamp_seconds", job="demo_ok") > 0

    def test_failed_run_counts_as_error_and_leaves_last_success_alone(self, metric):
        record_job_event(self._submitted("demo_err"))
        record_job_event(self._executed("demo_err"))
        stamped = metric("traxjourney_job_last_success_timestamp_seconds", job="demo_err")

        record_job_event(self._submitted("demo_err"))
        record_job_event(self._executed("demo_err", exception=RuntimeError("boom")))

        assert metric("traxjourney_job_runs_total", job="demo_err", result="error") == 1
        assert metric("traxjourney_job_last_success_timestamp_seconds", job="demo_err") == stamped

    def test_missed_run_is_recorded(self, metric):
        """A missed run only ever produced a log line before — invisible after
        the fact, which is exactly when it matters."""
        before = metric("traxjourney_job_runs_total", job="demo_missed", result="missed")
        record_job_event(JobExecutionEvent(EVENT_JOB_MISSED, "demo_missed", "default", None))
        assert metric("traxjourney_job_runs_total", job="demo_missed", result="missed") - before == 1

    def test_execution_without_submission_still_counts(self, metric):
        """Duration is unknown if the submit event was missed (e.g. the
        listener was attached mid-flight); the run must still be counted."""
        before = metric("traxjourney_job_runs_total", job="demo_orphan", result="success")
        record_job_event(self._executed("demo_orphan"))
        assert metric("traxjourney_job_runs_total", job="demo_orphan", result="success") - before == 1
        assert metric("traxjourney_job_duration_seconds_count", job="demo_orphan") == 0


class TestOperationOf:
    """SQL statements are folded to a bare keyword — never used as a label
    value themselves (unbounded cardinality, and they carry user content)."""

    @pytest.mark.parametrize("statement,expected", [
        ("SELECT * FROM project", "SELECT"),
        ("  insert into memory values (1)", "INSERT"),
        ("UPDATE project SET name='x'", "UPDATE"),
        ("DELETE FROM memory WHERE id=1", "DELETE"),
        ("PRAGMA journal_mode=WAL", "PRAGMA"),
        ("BEGIN", "OTHER"),
        ("", "OTHER"),
    ])
    def test_maps_statement_to_operation(self, statement, expected):
        assert _operation_of(statement) == expected
