"""Unit tests for the transactional email service (issue #113).

No network, no DB — ConsoleEmailService never touches a socket, and
get_email_service()'s backend selection is exercised via monkeypatched env
vars. SmtpEmailService's actual `send()` isn't tested here (that would mean
either hitting a real relay or mocking aiosmtplib internals, which the issue
explicitly steers away from); its construction-from-env and the fact that
selecting it requires SMTP_HOST are covered instead.
"""
from __future__ import annotations

import logging

import pytest

import src.email.service as email_service
from src.email.service import (
    ConsoleEmailService,
    EmailMessage,
    SmtpEmailService,
    get_email_service,
)
from src.email.templates import (
    render_invite_email,
    render_poster_failed_email,
    render_poster_ready_email,
)


@pytest.fixture(autouse=True)
def _reset_service_singleton():
    """get_email_service() caches its choice at module scope — reset it
    around every test so one test's env vars can't leak into another's."""
    email_service._service = None
    yield
    email_service._service = None


class TestConsoleEmailService:
    @pytest.mark.anyio
    async def test_send_logs_recipient_and_subject_but_not_body_by_default(
        self, caplog, monkeypatch,
    ):
        monkeypatch.delenv("EMAIL_CONSOLE_SHOW_BODY", raising=False)
        svc = ConsoleEmailService()
        with caplog.at_level(logging.INFO):
            await svc.send(EmailMessage(
                to="a@b.com", subject="Hi",
                text_body="body text http://x/verify/tok123",
                html_body="<p>body</p>"))

        assert "a@b.com" in caplog.text
        assert "Hi" in caplog.text
        assert "body text" not in caplog.text
        assert "http://x/verify/tok123" not in caplog.text

    @pytest.mark.anyio
    async def test_send_logs_body_when_show_body_opt_in_is_set(
        self, caplog, monkeypatch,
    ):
        monkeypatch.setenv("EMAIL_CONSOLE_SHOW_BODY", "1")
        svc = ConsoleEmailService()
        with caplog.at_level(logging.INFO):
            await svc.send(EmailMessage(
                to="a@b.com", subject="Hi",
                text_body="body text http://x/verify/tok123",
                html_body="<p>body</p>"))

        assert "a@b.com" in caplog.text
        assert "Hi" in caplog.text
        assert "body text" in caplog.text
        assert "http://x/verify/tok123" in caplog.text


class TestGetEmailService:
    def test_defaults_to_console_when_smtp_host_unset(self, monkeypatch):
        monkeypatch.delenv("SMTP_HOST", raising=False)

        assert isinstance(get_email_service(), ConsoleEmailService)

    def test_picks_smtp_when_smtp_host_set(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")

        assert isinstance(get_email_service(), SmtpEmailService)

    def test_is_a_singleton_within_a_process(self, monkeypatch):
        monkeypatch.delenv("SMTP_HOST", raising=False)

        assert get_email_service() is get_email_service()

    def test_selecting_console_backend_warns_that_contents_are_not_logged(
        self, monkeypatch, caplog,
    ):
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.delenv("EMAIL_CONSOLE_SHOW_BODY", raising=False)

        with caplog.at_level(logging.WARNING):
            get_email_service()

        assert "SMTP_HOST is not set" in caplog.text
        assert "not logged" in caplog.text
        assert "WILL be logged" not in caplog.text

    def test_selecting_console_backend_with_show_body_warns_louder(
        self, monkeypatch, caplog,
    ):
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.setenv("EMAIL_CONSOLE_SHOW_BODY", "1")

        with caplog.at_level(logging.WARNING):
            get_email_service()

        assert "WILL be logged" in caplog.text
        assert "never" in caplog.text


class TestSmtpEmailServiceConstruction:
    def test_reads_config_from_env(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_PORT", "2525")
        monkeypatch.setenv("SMTP_USERNAME", "user")
        monkeypatch.setenv("SMTP_PASSWORD", "pw")
        monkeypatch.setenv("MAIL_FROM", "invites@example.com")

        svc = SmtpEmailService()

        assert svc._host == "smtp.example.com"
        assert svc._port == 2525
        assert svc._username == "user"
        assert svc._password == "pw"
        assert svc._mail_from == "invites@example.com"

    def test_mail_from_falls_back_to_username(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_USERNAME", "user@example.com")
        monkeypatch.delenv("MAIL_FROM", raising=False)

        assert SmtpEmailService()._mail_from == "user@example.com"

    def test_port_defaults_to_587(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.delenv("SMTP_PORT", raising=False)

        assert SmtpEmailService()._port == 587

    def test_missing_smtp_host_raises(self, monkeypatch):
        monkeypatch.delenv("SMTP_HOST", raising=False)

        with pytest.raises(KeyError):
            SmtpEmailService()


class TestRenderInviteEmail:
    def test_text_body_is_not_escaped(self):
        text, _ = render_invite_email(
            project_name="Trip & Co", owner_name="Ana <ana@x.com>",
            role="editor", join_url="http://x/join/abc")

        assert "Trip & Co" in text
        assert "Ana <ana@x.com>" in text
        assert "http://x/join/abc" in text
        assert "editor" in text

    def test_html_body_is_escaped(self):
        _, html = render_invite_email(
            project_name="Trip & Co", owner_name="Ana <ana@x.com>",
            role="viewer", join_url="http://x/join/abc")

        assert "Trip &amp; Co" in html
        assert "Ana &lt;ana@x.com&gt;" in html
        assert "<script" not in html
        # The link itself is not escaped — it must remain a working href.
        assert 'href="http://x/join/abc"' in html
        assert "viewer" in html


class TestRenderPosterReadyEmail:
    def test_text_body_carries_project_name_and_download_url(self):
        text, _ = render_poster_ready_email(
            project_name="Trip & Co", download_url="http://x/poster/abc")

        assert "Trip & Co" in text
        assert "http://x/poster/abc" in text

    def test_html_body_is_escaped_and_links_to_the_download_url(self):
        _, html = render_poster_ready_email(
            project_name="Trip & Co", download_url="http://x/poster/abc")

        assert "Trip &amp; Co" in html
        assert 'href="http://x/poster/abc"' in html


class TestRenderPosterFailedEmail:
    def test_text_body_carries_project_name_and_is_generic(self):
        text, _ = render_poster_failed_email(project_name="Trip & Co")

        assert "Trip & Co" in text
        # Generic copy, not an echo of any internal error_message.
        assert "went wrong" in text

    def test_html_body_is_escaped(self):
        _, html = render_poster_failed_email(project_name="Trip & Co")

        assert "Trip &amp; Co" in html
