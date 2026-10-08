"""Jinja-rendered pages served by the API itself (issue #584).

Same set-up as ``src.email.templates``: plain template files under
``templates/``, autoescaped because user-controlled strings (a display
name) land in the markup.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from src.brand import APP_NAME

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=lambda name: name is not None and name.endswith(".html.jinja2"),
)
_env.globals["app_name"] = APP_NAME

#: Longest display name the confirmation page shows before cutting it.
_NAME_MAX = 40


def render_strava_return_confirm(
    *, display_name: str, continue_url: str, cancel_url: str
) -> str:
    """Render the page that asks before an app connect returns to the app.

    ``display_name`` is the account owner's free choice, so it is untrusted:
    it is escaped, cut to 40 characters and shown only after the fixed
    warning (docs/STRAVA_CONNECT_FOLLOWUPS_PLAN.md D1). An empty or blank
    name gets the neutral wording. ``cancel_url`` must be ``#cancelled``,
    the in-page anchor the template's ``:target`` rule names.
    """
    name = display_name if display_name.strip() else None
    if name is not None and len(name) > _NAME_MAX:
        name = name[:_NAME_MAX] + "…"
    return _env.get_template("strava_return_confirm.html.jinja2").render(
        name=name, continue_url=continue_url, cancel_url=cancel_url)
