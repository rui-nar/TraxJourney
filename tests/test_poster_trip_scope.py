"""A poster only draws memories of the trip it is made for.

The poster request carries the memories to pin (their ids, positions, text and
photo names): the client sends them because it holds the decrypted text of an
encrypted trip. The ids are the client's word, though, and the photos are read
from disk by id. So only memories of the requested trip are drawn, with only
the photos that memory holds, read from the trip owner's folder, whoever asks.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import api.memories as memories_mod
import api.project_shared as project_shared_mod
import models.db as db_module
import src.admin.storage as storage_mod
import src.poster.poster_job_runner as job_runner_mod
import src.poster.poster_renderer as renderer_mod
from api.deps import get_current_user
from models.project_db import DBMemory
from models.user import UserInfo


@pytest.fixture
def env(monkeypatch, tmp_path):
    import api.router as router

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    for mod in (project_shared_mod, storage_mod, memories_mod):
        monkeypatch.setattr(mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(job_runner_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(renderer_mod, "render_basemap",
                        lambda bounds, w, h, **_kw: Image.new("RGB", (w, h), (120, 140, 160)))
    for var in ("BILLING_ENABLED", "BILLING_ENFORCE_QUOTAS", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        users = {n: UserInfo(display_name=n, email=f"{n}@e.com") for n in ("alice", "carol")}
        for u in users.values():
            sess.add(u)
        sess.commit()
        ids = {n: u.id for n, u in users.items()}

    current = {"uid": ids["alice"]}
    router.app.dependency_overrides[get_current_user] = (
        lambda: {"sub": str(current["uid"]), "email": "x@e.com"})

    def act_as(who: str) -> None:
        current["uid"] = ids[who]

    # What reaches a card, and which photo files get read for it.
    drawn: list = []
    read: list = []
    real_assemble = renderer_mod.assemble_card_content
    real_resolver = renderer_mod._photo_resolver

    def assemble(config, memory, *a, **kw):
        drawn.append(memory["id"])
        return real_assemble(config, memory, *a, **kw)

    def resolver(*a, **kw):
        resolve = real_resolver(*a, **kw)

        def recorded(name):
            path = resolve(name)
            if path is not None:
                read.append(Path(path))
            return path
        return recorded

    monkeypatch.setattr(renderer_mod, "assemble_card_content", assemble)
    monkeypatch.setattr(renderer_mod, "_photo_resolver", resolver)

    try:
        yield (TestClient(router.app, raise_server_exceptions=False),
               engine, ids, act_as, tmp_path, drawn, read)
    finally:
        router.app.dependency_overrides.pop(get_current_user, None)


def _jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 200, 10)).save(path, "JPEG")


def _trip_with_memory(client, engine, data, owner_id: int, trip: str) -> tuple[int, str]:
    assert client.post("/api/projects/", json={"name": trip}).status_code == 201
    r = client.post("/api/memories/", json={
        "project_name": trip, "name": f"{trip} memory", "date": "2024-06-01",
        "geo_mode": "custom", "lat": 48.85, "lon": 2.35})
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    name = str(uuid.uuid4())
    with Session(engine) as sess:
        row = sess.get(DBMemory, mid)
        row.photos_json = json.dumps([name])
        sess.add(row)
        sess.commit()
    folder = data / "users" / str(owner_id) / "memories" / str(mid)
    _jpeg(folder / f"{name}.jpg")
    _jpeg(folder / f"{name}_thumb.jpg")
    return mid, name


def _body(*memories) -> dict:
    return {
        "bounds": {"north": 48.9, "south": 48.8, "east": 2.4, "west": 2.3},
        "orientation": "landscape", "paper_size": "A4",
        "config": {"hero_photo": True, "all_photos": True, "memory_text": True},
        "memories": [
            {"id": mid, "lat": 48.85, "lon": 2.35, "date": "2024-06-01",
             "name": "n", "description": "d", "photo_uuids": photos}
            for mid, photos in memories
        ],
    }


@pytest.fixture
def trips(env):
    """Alice's trips A (Carol is a companion) and B, one memory with a photo each."""
    client, engine, ids, act_as, data, drawn, read = env
    a = _trip_with_memory(client, engine, data, ids["alice"], "A")
    b = _trip_with_memory(client, engine, data, ids["alice"], "B")
    token = client.post("/api/projects/A/members/invite").json()["token"]
    act_as("carol")
    assert client.post(f"/api/invites/{token}/accept").status_code == 200
    return env, a, b


def _poster(client, trip: str, body: dict, owner_q: str = ""):
    r = client.post(f"/api/projects/{trip}/poster{owner_q}", json=body)
    assert r.status_code == 201, r.text
    status = client.get(f"/api/projects/{trip}/poster/{r.json()['job_id']}{owner_q}").json()
    assert status["status"] == "done", status


def test_a_companions_poster_draws_only_memories_of_that_trip(trips):
    (client, engine, ids, act_as, data, drawn, read), (ma, pa), (mb, pb) = trips

    _poster(client, "A", _body((ma, [pa]), (mb, [pb])), f"?owner={ids['alice']}")

    assert drawn == [ma]
    assert all(pb not in p.name for p in read), read


def test_a_companions_poster_reads_the_trips_photos_from_the_owners_folder(trips):
    (client, engine, ids, act_as, data, drawn, read), (ma, pa), (mb, pb) = trips

    _poster(client, "A", _body((ma, [pa])), f"?owner={ids['alice']}")

    owner_folder = data / "users" / str(ids["alice"]) / "memories" / str(ma)
    assert read and all(p.parent == owner_folder for p in read), read
    assert any(pa in p.name for p in read)


def test_a_poster_reads_only_photos_the_memory_holds(trips):
    (client, engine, ids, act_as, data, drawn, read), (ma, pa), (mb, pb) = trips
    # A file in the memory's folder that the memory does not list.
    stray = str(uuid.uuid4())
    _jpeg(data / "users" / str(ids["alice"]) / "memories" / str(ma) / f"{stray}_thumb.jpg")

    _poster(client, "A", _body((ma, [pa, stray])), f"?owner={ids['alice']}")

    assert all(stray not in p.name for p in read), read


def test_a_companions_preview_draws_only_memories_of_that_trip(trips):
    (client, engine, ids, act_as, data, drawn, read), (ma, pa), (mb, pb) = trips

    r = client.post(f"/api/projects/A/poster/preview?owner={ids['alice']}",
                    json=_body((ma, [pa]), (mb, [pb])))

    assert r.status_code == 200, r.text
    assert drawn == [ma]


def test_the_owners_poster_still_draws_their_memories_with_photos(trips):
    (client, engine, ids, act_as, data, drawn, read), (ma, pa), (mb, pb) = trips
    act_as("alice")

    _poster(client, "B", _body((mb, [pb])))

    assert drawn == [mb]
    assert any(pb in p.name for p in read)


# ── A share link serves only its own trip's photos ──────────────────────────

def test_a_share_link_serves_photos_of_its_own_trip_only(trips, monkeypatch):
    import api.share as share_mod

    (client, engine, ids, act_as, data, drawn, read), (ma, pa), (mb, pb) = trips
    monkeypatch.setattr(share_mod, "_DATA_DIR", str(data))
    act_as("alice")
    token = client.post("/api/projects/A/share").json()["share_token"]

    for suffix in ("", "/thumb"):
        assert client.get(f"/api/share/{token}/photos/{ma}/{pa}{suffix}").status_code == 200
        # The owner's other trip, not shared by this link.
        r = client.get(f"/api/share/{token}/photos/{mb}/{pb}{suffix}")
        assert r.status_code == 404, (suffix, r.status_code)
