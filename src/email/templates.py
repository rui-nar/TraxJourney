"""Jinja-rendered email bodies (issue #113).

Templates live as plain files under ``templates/`` — adding a new email
(verification, notifications, ...) is a new pair of files here plus a small
render function, no changes to the service layer.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from src.brand import APP_NAME

# HTML templates must autoescape (user-controlled strings like project/owner
# names land in markup); the text counterpart must NOT — escaping would
# corrupt a plain-text body (e.g. "&" becoming "&amp;").
_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=lambda name: name is not None and name.endswith(".html.jinja2"),
)
# Every template can name the product without each render function passing it.
_env.globals["app_name"] = APP_NAME


def render_invite_email(
    *, project_name: str, owner_name: str, role: str, join_url: str
) -> tuple[str, str]:
    """Render the travel-companion invite email. Returns (text_body, html_body)."""
    ctx = {
        "project_name": project_name,
        "owner_name": owner_name,
        "role": role,
        "join_url": join_url,
    }
    text_body = _env.get_template("invite.txt.jinja2").render(ctx)
    html_body = _env.get_template("invite.html.jinja2").render(ctx)
    return text_body, html_body


def render_verification_email(
    *, display_name: str, verify_url: str, expires_hours: int
) -> tuple[str, str]:
    """Render the email-verification message (issue #110). Returns (text, html)."""
    ctx = {
        "display_name": display_name,
        "verify_url": verify_url,
        "expires_hours": expires_hours,
    }
    text_body = _env.get_template("verify.txt.jinja2").render(ctx)
    html_body = _env.get_template("verify.html.jinja2").render(ctx)
    return text_body, html_body


def render_poster_ready_email(
    *, project_name: str, download_url: str
) -> tuple[str, str]:
    """Render the "your poster is ready" email (issue #14). Returns (text, html)."""
    ctx = {"project_name": project_name, "download_url": download_url}
    text_body = _env.get_template("poster_ready.txt.jinja2").render(ctx)
    html_body = _env.get_template("poster_ready.html.jinja2").render(ctx)
    return text_body, html_body


def render_poster_failed_email(*, project_name: str) -> tuple[str, str]:
    """Render the "poster generation failed" email (issue #14). Returns (text, html).

    Deliberately takes no ``error_message`` — the job's internal error can
    carry implementation detail (e.g. a Mapbox error derived from a
    stack trace) that must not be forwarded to the user verbatim; the copy is
    a generic "try again" rather than echoing it.
    """
    ctx = {"project_name": project_name}
    text_body = _env.get_template("poster_failed.txt.jinja2").render(ctx)
    html_body = _env.get_template("poster_failed.html.jinja2").render(ctx)
    return text_body, html_body
