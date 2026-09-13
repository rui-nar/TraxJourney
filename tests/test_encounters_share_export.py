"""Share-exclusion + .traxj export/import round-trip for people/encounters (#40, phase 4)."""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from api.deps import get_optional_current_user
from api.share import invalidate_share_cache, router as share_router
from models.project_db import (
    DBEncounter, DBMemory, DBPerson, DBPersonGroup, DBProject, DBProjectItem,
)
from models.user import UserInfo
from src.models.encounter import Encounter
from src.models.person import Person
from src.models.person_group import PersonGroup
from src.models.project import Project, ProjectItem
from src.project.project_io import ProjectIO
from src.project.project_repo import ProjectRepo


# ── Share exclusion ────────────────────────────────────────────────────────────

def _seed_shared(engine):
    with Session(engine) as sess:
        u = UserInfo(display_name="Owner", email="o@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        proj = DBProject(user_info_id=u.id, name="Trip",
                         share_token="tok_full")
        sess.add(proj); sess.commit(); sess.refresh(proj)

        mem = DBMemory(project_id=proj.id, public_id="pub1", name="M", date="2024-06-01")
        sess.add(mem); sess.commit(); sess.refresh(mem)
        grp = DBPersonGroup(project_id=proj.id, name="Crew")
        sess.add(grp); sess.commit(); sess.refresh(grp)
        person = DBPerson(project_id=proj.id, name="Alice", email="alice@x.com",
                          group_id=grp.id)
        sess.add(person); sess.commit(); sess.refresh(person)
        enc = DBEncounter(project_id=proj.id, person_id=person.id, date="2024-06-01")
        sess.add(enc); sess.commit(); sess.refresh(enc)

        sess.add(DBProjectItem(project_id=proj.id, position=0,
                               item_type="memory", memory_id=mem.id))
        sess.add(DBProjectItem(project_id=proj.id, position=1,
                               item_type="encounter", encounter_id=enc.id))
        sess.commit()


@pytest.fixture
def share_client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    _seed_shared(engine)
    invalidate_share_cache("tok_full")

    app = FastAPI()
    app.dependency_overrides[get_optional_current_user] = lambda: None
    app.include_router(share_router)
    return TestClient(app)


def test_shared_view_excludes_people_and_encounters(share_client):
    result = share_client.get("/api/share/tok_full").json()
    # People + groups directories are stripped entirely.
    assert "people" not in result
    assert "groups" not in result
    # Encounter items are stripped; the memory item survives.
    types = [it["item_type"] for it in result["items"]]
    assert "encounter" not in types
    assert "memory" in types


def test_shared_meta_excludes_people_and_encounters(share_client):
    result = share_client.get("/api/share/tok_full/meta").json()
    assert "people" not in result
    assert "groups" not in result
    assert "encounter" not in [it["item_type"] for it in result["items"]]


# ── Export / import round-trip ─────────────────────────────────────────────────

def test_traxj_roundtrip_preserves_people_and_encounters(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        u = UserInfo(display_name="U", email="u@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        uid = u.id

    # Build a project with a person and an encounter referencing them.
    person = Person(id=1, name="Alice", email="a@x.com", polarsteps="alice")
    encounter = Encounter(id=1, person_id=1, date="2024-06-01", description="met")
    project = Project(
        name="Roundtrip",
        people=[person],
        items=[ProjectItem(item_type="encounter", encounter=encounter)],
    )

    repo = ProjectRepo()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "Roundtrip" + ProjectIO.EXTENSION)
        ProjectIO.save(project, path)
        # The exported file carries the people array + the encounter item.
        with Session(engine) as sess:
            repo.ingest_project(sess, uid, path)
        # ingest renames to *.migrated; reload from DB.
        with Session(engine) as sess:
            loaded = repo.get_project(sess, uid, "Roundtrip")

    assert loaded is not None
    assert len(loaded.people) == 1
    assert loaded.people[0].name == "Alice"
    assert loaded.people[0].polarsteps == "alice"
    enc_items = [it for it in loaded.items if it.item_type == "encounter"]
    assert len(enc_items) == 1
    # The encounter is re-linked to the imported person's new id.
    assert enc_items[0].encounter.person_id == loaded.people[0].id
    assert enc_items[0].encounter.description == "met"


def test_traxj_roundtrip_preserves_groups_and_membership(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        u = UserInfo(display_name="U", email="u@e.com")
        sess.add(u); sess.commit(); sess.refresh(u)
        uid = u.id

    group = PersonGroup(id=7, name="Crew", nationalities=["FR"],
                        socials=[{"network": "instagram", "handle": "crew"}])
    person = Person(id=1, name="Alice", group_id=7)
    project = Project(name="Roundtrip", people=[person], groups=[group])

    repo = ProjectRepo()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "Roundtrip" + ProjectIO.EXTENSION)
        ProjectIO.save(project, path)
        with Session(engine) as sess:
            repo.ingest_project(sess, uid, path)
        with Session(engine) as sess:
            loaded = repo.get_project(sess, uid, "Roundtrip")

    assert loaded is not None
    assert len(loaded.groups) == 1
    g = loaded.groups[0]
    assert g.name == "Crew" and g.nationalities == ["FR"]
    assert g.socials == [{"network": "instagram", "handle": "crew"}]
    # The member is re-linked to the imported group's new id.
    assert len(loaded.people) == 1
    assert loaded.people[0].group_id == g.id
