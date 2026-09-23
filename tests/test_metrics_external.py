"""Third-party call counters — Strava, Polarsteps, Google Translate, SMTP
(issue #125).

Built on the existing mocking harnesses for each client, so these assert on the
same code paths the behavioural tests already drive.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from src.api.polarsteps_client import PolarstepsClient
from src.api.strava_client import StravaAPI
from src.config.settings import Config
from src.exceptions.errors import APIError, AuthenticationError

_COUNTER = "traxjourney_external_requests_total"


class DummyConfig(Config):
    def __init__(self):
        super().__init__()
        self.set("strava.client_id", "id")
        self.set("strava.client_secret", "secret")


def _strava_client() -> StravaAPI:
    client = StravaAPI(DummyConfig())
    client.token_data = {
        "access_token": "t", "refresh_token": "r", "expires_at": time.time() + 1000,
    }
    return client


class TestStravaCounters:
    @patch("src.api.strava_client.requests.request")
    def test_successful_call_is_counted(self, mock_req, metric):
        before = metric(_COUNTER, service="strava",
                        endpoint="/athlete/activities", outcome="success")
        mock_req.return_value = MagicMock(status_code=200)
        mock_req.return_value.json.return_value = []

        _strava_client().get_activities()

        assert metric(_COUNTER, service="strava",
                      endpoint="/athlete/activities", outcome="success") - before == 1

    @patch("src.api.strava_client.requests.request")
    def test_activity_ids_are_templated_out_of_the_label(self, mock_req, metric):
        """Every activity ever imported would otherwise mint its own time
        series — the fastest way to take a Prometheus server down."""
        mock_req.return_value = MagicMock(status_code=200)
        mock_req.return_value.json.return_value = {}
        before = metric(_COUNTER, service="strava",
                        endpoint="/activities/{id}", outcome="success")

        client = _strava_client()
        client.get_activity(111)
        client.get_activity(222)

        assert metric(_COUNTER, service="strava",
                      endpoint="/activities/{id}", outcome="success") - before == 2
        assert metric(_COUNTER, service="strava",
                      endpoint="/activities/111", outcome="success") == 0

    @patch("src.api.strava_client.time.sleep")
    @patch("src.api.strava_client.requests.request")
    def test_every_retry_attempt_is_counted(self, mock_req, _sleep, metric):
        """Each attempt is a real call against Strava's quota, so the counter
        has to move once per attempt — not once per logical request."""
        before = metric(_COUNTER, service="strava",
                        endpoint="/test", outcome="server_error")
        mock_req.return_value = MagicMock(status_code=500, text="oops")

        with pytest.raises(APIError):
            _strava_client().request("GET", "/test", max_retries=3)

        assert metric(_COUNTER, service="strava",
                      endpoint="/test", outcome="server_error") - before == 3

    @patch("src.api.strava_client.time.sleep")
    @patch("src.api.strava_client.requests.request")
    def test_rate_limited_call_has_its_own_outcome(self, mock_req, _sleep, metric):
        """429s are the signal that the quota is actually being hit — they must
        not be lumped in with ordinary client errors."""
        before = metric(_COUNTER, service="strava",
                        endpoint="/test", outcome="rate_limited")
        limited = MagicMock(status_code=429, headers={"Retry-After": "1"})
        ok = MagicMock(status_code=200)
        ok.json.return_value = {"ok": True}
        mock_req.side_effect = [limited, ok]

        _strava_client().request("GET", "/test", max_retries=3)

        assert metric(_COUNTER, service="strava",
                      endpoint="/test", outcome="rate_limited") - before == 1

    @patch("src.api.strava_client.requests.request")
    def test_auth_failure_is_recorded_as_auth_error_not_exception(self, mock_req, metric):
        """The client classifies the 401 and then raises; the classification
        must survive the raise, or every expired token reads as a crash."""
        before = metric(_COUNTER, service="strava",
                        endpoint="/test", outcome="auth_error")
        mock_req.return_value = MagicMock(status_code=401)

        client = _strava_client()
        with patch("src.api.strava_client.TokenStore.delete_token"), \
                patch.object(client.oauth, "refresh_token", side_effect=RuntimeError):
            with pytest.raises(AuthenticationError):
                client.request("GET", "/test", max_retries=1)

        assert metric(_COUNTER, service="strava",
                      endpoint="/test", outcome="auth_error") - before == 1

    @patch("src.api.strava_client.requests.request")
    def test_transport_failure_is_recorded_as_an_exception(self, mock_req, metric):
        before = metric(_COUNTER, service="strava",
                        endpoint="/test", outcome="exception")
        mock_req.side_effect = ConnectionError("no route to host")

        with pytest.raises(ConnectionError):
            _strava_client().request("GET", "/test", max_retries=1)

        assert metric(_COUNTER, service="strava",
                      endpoint="/test", outcome="exception") - before == 1


class TestPolarstepsCounters:
    def test_successful_call_is_counted_with_a_templated_endpoint(self, metric):
        before = metric(_COUNTER, service="polarsteps",
                        endpoint="/users/{id}", outcome="success")
        client = PolarstepsClient("12345|hash")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": 12345}

        with patch.object(client._session, "get", return_value=resp):
            client.get_me()

        assert metric(_COUNTER, service="polarsteps",
                      endpoint="/users/{id}", outcome="success") - before == 1

    def test_expired_token_is_counted_as_auth_error(self, metric):
        before = metric(_COUNTER, service="polarsteps",
                        endpoint="/users/{id}", outcome="auth_error")
        client = PolarstepsClient("12345|hash")

        with patch.object(client._session, "get", return_value=MagicMock(status_code=401)):
            with pytest.raises(PermissionError):
                client.get_me()

        assert metric(_COUNTER, service="polarsteps",
                      endpoint="/users/{id}", outcome="auth_error") - before == 1


class TestTranslateCounters:
    @pytest.mark.anyio
    async def test_successful_translation_is_counted(self, metric):
        import api.translations as translations

        before = metric(_COUNTER, service="google_translate",
                        endpoint="/translate", outcome="success")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"translations": [{"translatedText": "bonjour"}]}}
        resp.raise_for_status.return_value = None

        async def _post(*_args, **_kwargs):
            return resp

        with patch("httpx.AsyncClient.post", _post):
            assert await translations.translate_text("hello", "fr") == "bonjour"

        assert metric(_COUNTER, service="google_translate",
                      endpoint="/translate", outcome="success") - before == 1

    @pytest.mark.anyio
    async def test_upstream_error_is_classified_by_status(self, metric):
        import api.translations as translations

        before = metric(_COUNTER, service="google_translate",
                        endpoint="/translate", outcome="client_error")
        resp = MagicMock(status_code=400)
        resp.raise_for_status.side_effect = RuntimeError("bad request")

        async def _post(*_args, **_kwargs):
            return resp

        with patch("httpx.AsyncClient.post", _post):
            with pytest.raises(RuntimeError):
                await translations.translate_text("hello", "fr")

        assert metric(_COUNTER, service="google_translate",
                      endpoint="/translate", outcome="client_error") - before == 1


class TestSmtpCounters:
    @pytest.fixture
    def service(self, monkeypatch):
        from src.email.service import SmtpEmailService

        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        return SmtpEmailService()

    def _message(self):
        from src.email.service import EmailMessage

        return EmailMessage("to@example.com", "Subject", "text", "<p>html</p>")

    @pytest.mark.anyio
    async def test_successful_send_is_counted(self, service, metric):
        before = metric(_COUNTER, service="smtp", endpoint="/send", outcome="success")

        async def _ok(*_args, **_kwargs):
            return None

        with patch("aiosmtplib.send", _ok):
            await service.send(self._message())

        assert metric(_COUNTER, service="smtp",
                      endpoint="/send", outcome="success") - before == 1

    @pytest.mark.anyio
    async def test_failed_send_is_counted_as_an_exception(self, service, metric):
        """A provider outage has to be visible as something other than silence
        — invite emails failing is otherwise only discoverable by complaint."""
        before = metric(_COUNTER, service="smtp", endpoint="/send", outcome="exception")

        async def _boom(*_args, **_kwargs):
            raise ConnectionRefusedError("relay down")

        with patch("aiosmtplib.send", _boom):
            with pytest.raises(ConnectionRefusedError):
                await service.send(self._message())

        assert metric(_COUNTER, service="smtp",
                      endpoint="/send", outcome="exception") - before == 1
