"""The suite does not keep every test's engine alive (issue #540).

Each test builds its own in-memory engine; process-wide library caches held
on to them (see ``release_per_test_engines`` in conftest.py), and by the end
of the suite they held ~250 MB of the pytest process's RSS.
"""
from __future__ import annotations

import gc
import weakref

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from api.deps import get_current_user
from models.user import UserInfo
from tests.conftest import release_per_test_engines


def _use_an_engine_like_a_test_does():
    """Write through the ORM and serve a request whose dependency override
    closes over the engine — the two paths that cached it. Returns weak
    references to the engine and its dialect."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        sess.add(UserInfo(display_name="A", email="a@e.com"))
        sess.commit()

    app = FastAPI()

    @app.get("/me")
    def me(user=Depends(get_current_user)):
        return user

    app.dependency_overrides[get_current_user] = lambda: {"sub": str(engine.url)}
    assert TestClient(app).get("/me").status_code == 200

    engine.dispose()
    return weakref.ref(engine), weakref.ref(engine.dialect)


def test_a_tests_engine_is_kept_alive_until_the_caches_are_released():
    """The precondition the release exists for: without it, the ORM's
    compiled-statement cache still references the dialect. If this starts
    failing after a SQLAlchemy upgrade, the release may no longer be needed."""
    _engine, dialect = _use_an_engine_like_a_test_does()
    gc.collect()
    assert dialect() is not None


def test_a_tests_engine_is_freed_once_the_caches_are_released():
    engine, dialect = _use_an_engine_like_a_test_does()
    release_per_test_engines()
    gc.collect()
    assert engine() is None
    assert dialect() is None
