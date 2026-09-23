"""The product name users and third parties read comes from ``src.brand`` (issue #151).

Emails (subjects and every template), the GPX ``creator`` and the API docs title
must say the current name and never the old one. Rendering the real templates
and calling the real send functions is the point: a hard-coded name in a
template or an f-string is exactly what a rename misses.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from src.brand import APP_NAME
from src.email.templates import (
    render_invite_email,
    render_poster_failed_email,
    render_poster_ready_email,
    render_verification_email,
)

_OLD_NAME = re.compile(r"view[ _-]?trip", re.IGNORECASE)


def test_app_name_is_traxjourney():
    """The one literal pin. Renaming the product changes this line on purpose."""
    assert APP_NAME == "TraxJourney"


# ── Emails ────────────────────────────────────────────────────────────────────

_BRANDED_RENDERS = {
    "invite": lambda: render_invite_email(
        project_name="Alps", owner_name="Ana", role="editor", join_url="https://x/join/t"),
    "verify": lambda: render_verification_email(
        display_name="Ana", verify_url="https://x/verify-email/t", expires_hours=24),
}
# These don't name the product today; they must still never name the old one.
_UNBRANDED_RENDERS = {
    "poster_ready": lambda: render_poster_ready_email(
        project_name="Alps", download_url="https://x/p"),
    "poster_failed": lambda: render_poster_failed_email(project_name="Alps"),
}


@pytest.mark.parametrize("name", sorted(_BRANDED_RENDERS))
def test_branded_email_bodies_name_the_product(name):
    text_body, html_body = _BRANDED_RENDERS[name]()

    for body in (text_body, html_body):
        assert APP_NAME in body
        assert not _OLD_NAME.search(body), body


@pytest.mark.parametrize("name", sorted({**_BRANDED_RENDERS, **_UNBRANDED_RENDERS}))
def test_no_email_body_names_the_old_product(name):
    text_body, html_body = {**_BRANDED_RENDERS, **_UNBRANDED_RENDERS}[name]()

    assert not _OLD_NAME.search(text_body), text_body
    assert not _OLD_NAME.search(html_body), html_body


class _FakeEmailService:
    def __init__(self):
        self.sent = []

    async def send(self, message) -> None:
        self.sent.append(message)


def test_invite_subject_names_the_product(monkeypatch):
    from api import members

    fake = _FakeEmailService()
    monkeypatch.setattr(members, "get_email_service", lambda: fake)

    asyncio.run(members.send_invite_email(
        "friend@example.com", "Alps", "Ana", "editor", "tok"))

    [msg] = fake.sent
    assert msg.subject == f"Ana invited you to Alps on {APP_NAME}"
    assert not _OLD_NAME.search(msg.subject)


def test_verification_subject_names_the_product(monkeypatch):
    from src.auth import email_verification

    fake = _FakeEmailService()
    monkeypatch.setattr(email_verification, "get_email_service", lambda: fake)

    asyncio.run(email_verification.send_verification_email(
        "ana@example.com", "Ana", "tok"))

    [msg] = fake.sent
    assert msg.subject == f"Confirm your {APP_NAME} email address"
    assert not _OLD_NAME.search(msg.subject)


# ── API docs ──────────────────────────────────────────────────────────────────

def test_openapi_title_and_description_name_the_product():
    import api.router as router_mod

    info = router_mod.app.openapi()["info"]

    assert info["title"] == f"{APP_NAME} API"
    assert APP_NAME in info["description"]
    assert not _OLD_NAME.search(info["title"] + info["description"])


def test_scalar_docs_title_names_the_product():
    import api.router as router_mod

    html = TestClient(router_mod.app).get("/scalar").text

    assert f"<title>{APP_NAME} API</title>" in html
    assert not _OLD_NAME.search(html)


# ── GPX export ────────────────────────────────────────────────────────────────

def test_project_gpx_export_creator_is_the_product(monkeypatch):
    from api.deps import get_current_user
    from api.project_transfer import router as project_transfer_router
    from models.project_db import DBProject
    from models.user import UserInfo

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="owner@e.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        sess.add(DBProject(user_info_id=user.id, name="Trip"))
        sess.commit()
        uid = user.id

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uid)}
    app.include_router(project_transfer_router)

    r = TestClient(app).get("/api/projects/Trip/export")

    assert r.status_code == 200, r.text
    assert ET.fromstring(r.content).attrib["creator"] == APP_NAME


# ── Outbound User-Agent ───────────────────────────────────────────────────────

def test_user_agent_names_the_product_version_and_real_repository(monkeypatch):
    import importlib

    import src.brand as brand

    monkeypatch.setenv("APP_VERSION", "v9.8.7")
    try:
        reloaded = importlib.reload(brand)
        assert reloaded.USER_AGENT == "TraxJourney/v9.8.7 (+https://github.com/rui-nar/TraxJourney)"
    finally:
        monkeypatch.undo()
        importlib.reload(brand)


def test_every_outbound_client_sends_the_shared_user_agent(monkeypatch):
    """Overpass, Transitous (MOTIS) and Nominatim all identify the same way."""
    import api.geo as geo
    import src.services.hafas_service as hafas
    import src.services.overpass_service as overpass
    from src.brand import USER_AGENT

    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    def _capture(url, params=None, headers=None, timeout=None):
        seen["nominatim"] = (headers or {}).get("User-Agent")
        return _Resp()

    monkeypatch.setattr(geo.requests, "get", _capture)
    geo._nominatim_search("lisbon")

    agents = {
        "overpass": overpass._HEADERS["User-Agent"],
        "motis": hafas._HEADERS["User-Agent"],
        "nominatim": seen["nominatim"],
    }
    for service, ua in agents.items():
        assert ua == USER_AGENT, service
        assert ua.startswith(f"{APP_NAME}/"), service
        assert "(+https://github.com/rui-nar/TraxJourney)" in ua, service
        assert not _OLD_NAME.search(ua), service
