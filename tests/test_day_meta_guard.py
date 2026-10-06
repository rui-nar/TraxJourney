"""PATCH /day-meta must not let a caller delete day-meta for a day whose content
they cannot see (issue #387).

"The user cleared this day's notes" and "the trip-end prune dropped a day it
should not have" arrive as the same request — a day in ``delete``. #372
settled which days may go, but only client-side, and only a current client
asks. These tests pin the server's own rule: a drop is refused only when the
caller demonstrably could not have known what they were dropping.

The second test is the one that matters most — a guard that blocks clearing
notes on an ordinary day would be far worse than the hole it closes. See
day_meta_editor.dart's _persist: emptying a day REMOVES its key.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import models.db as db_module
from api.deps import get_current_user
from api.journal import router as journal_router
from api.projects import router as projects_router
from models.project_db import (
    DBActivity,
    DBMemory,
    DBProject,
    DBProjectItem,
    DBProjectMember,
)
from models.user import UserInfo


@pytest.fixture()
def env(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "get_session", lambda: Session(engine))
    import api.projects as projects_mod
    import api.journal as journal_mod
    monkeypatch.setattr(projects_mod, "get_session", lambda: Session(engine))
    monkeypatch.setattr(journal_mod, "get_session", lambda: Session(engine))
    monkeypatch.setattr(journal_mod, "_DATA_DIR", str(tmp_path))

    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        users = {
            who: UserInfo(display_name=who.capitalize(), email=f"{who}@e.com")
            for who in ("owner", "editor")
        }
        for u in users.values():
            sess.add(u)
        sess.commit()
        for u in users.values():
            sess.refresh(u)
        ids = {who: u.id for who, u in users.items()}

        proj = DBProject(user_info_id=ids["owner"], name="Trip")
        sess.add(proj)
        sess.commit()
        sess.refresh(proj)
        ids["project"] = proj.id
        sess.add(DBProjectMember(
            project_id=proj.id, user_info_id=ids["editor"], role="editor",
            invited_by=ids["owner"]))
        sess.commit()

    current = {"uid": ids["owner"]}
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(current["uid"])}
    app.include_router(projects_router)
    app.include_router(journal_router)
    return TestClient(app), engine, ids, lambda who: current.update(uid=ids[who])


def _patch(client, days: dict | None = None, delete: list | None = None):
    r = client.patch("/api/projects/Trip/day-meta",
                     json={"days": days or {}, "delete": delete or []})
    assert r.status_code == 200, r.text


def _stored(engine):
    """Read the stored row directly: the project payload normalises day-meta to
    the fields the client knows, which would hide what this guard preserves."""
    import json as _json
    with Session(engine) as sess:
        row = sess.exec(select(DBProject).where(DBProject.name == "Trip")).one()
        return _json.loads(row.day_meta_json) if row.day_meta_json else {}


def _companion_journal(client, act_as, ids, date: str):
    act_as("editor")
    r = client.post(f"/api/journal/?owner={ids['owner']}", json={
        "project_name": "Trip", "date": date,
        "geo_mode": "custom", "lat": 1.0, "lon": 2.0,
        "description": "editor's private note",
    })
    assert r.status_code == 201, r.text
    act_as("owner")


def test_a_day_pinned_only_by_another_members_journal_survives_a_drop(env):
    """The hole #387 closes: a stale client prunes past the trip end date and
    drops a day it has no way of knowing is still on screen for someone else."""
    client, engine, ids, act_as = env
    _patch(client, {"2026-07-04": {"note": "shared notes"},
                    "2026-07-05": {"note": "keep me"}})
    _companion_journal(client, act_as, ids, "2026-07-04")

    _patch(client, delete=["2026-07-04"])

    assert _stored(engine) == {
        "2026-07-04": {"note": "shared notes"},
        "2026-07-05": {"note": "keep me"},
    }


def test_clearing_the_notes_on_an_ordinary_day_still_removes_it(env):
    """day_meta_editor._persist removes the key when a day is emptied. A guard
    that blocked this would break the commonest edit in the app."""
    client, engine, ids, _ = env
    with Session(engine) as sess:
        sess.add(DBActivity(id=77, user_info_id=ids["owner"], name="Ride",
                            start_date="2026-07-04T06:00:00Z",
                            start_date_local="2026-07-04T08:00:00"))
        sess.add(DBProjectItem(project_id=ids["project"], position=0,
                               item_type="activity", activity_id=77))
        sess.commit()
    _patch(client, {"2026-07-04": {"note": "my notes"}})

    _patch(client, delete=["2026-07-04"])

    assert _stored(engine) == {}


def test_clearing_a_day_the_caller_can_see_via_their_own_journal_still_works(env):
    """Their own journal is visible to them, so they know what they are
    dropping — nothing to protect them from."""
    client, engine, ids, act_as = env
    r = client.post("/api/journal/", json={
        "project_name": "Trip", "date": "2026-07-04",
        "geo_mode": "custom", "lat": 1.0, "lon": 2.0,
        "description": "my own note",
    })
    assert r.status_code == 201, r.text
    _patch(client, {"2026-07-04": {"note": "my notes"}})

    _patch(client, delete=["2026-07-04"])

    assert _stored(engine) == {}


def test_an_empty_entry_on_an_invisibly_pinned_day_is_still_dropped(env):
    """Nothing to lose, so nothing to protect — the guard must not pin empty
    rows in place forever."""
    client, engine, ids, act_as = env
    _patch(client, {"2026-07-04": {}})
    _companion_journal(client, act_as, ids, "2026-07-04")

    _patch(client, delete=["2026-07-04"])

    assert _stored(engine) == {}


def test_a_day_with_no_content_at_all_still_prunes(env):
    """The ordinary trip-end prune from #358 must keep working."""
    client, engine, _, _ = env
    _patch(client, {"2026-07-04": {"note": "past the end date"},
                    "2026-07-01": {"note": "keep"}})

    _patch(client, delete=["2026-07-04"])

    assert _stored(engine) == {"2026-07-01": {"note": "keep"}}


def test_the_journals_author_can_still_drop_their_own_day(env):
    """Symmetry check: the editor sees their own journal, so for them the day
    is not invisible and their own drop goes through."""
    client, engine, ids, act_as = env
    _patch(client, {"2026-07-04": {"note": "shared notes"}})
    _companion_journal(client, act_as, ids, "2026-07-04")

    act_as("editor")
    r = client.patch(f"/api/projects/Trip/day-meta?owner={ids['owner']}",
                     json={"delete": ["2026-07-04"]})
    assert r.status_code == 200, r.text

    act_as("owner")
    assert _stored(engine) == {}

def test_a_journal_left_behind_by_a_departed_member_pins_nothing(env):
    """remove_member deletes the membership, not the member's journal rows. An
    orphan like that is visible to nobody — the ex-member gets a 404 on the
    trip too — so pinning its day would make those notes unclearable by
    anyone, forever. Found in adversarial review of the first cut."""
    client, engine, ids, act_as = env
    _patch(client, {"2026-07-04": {"note": "shared notes"}})
    _companion_journal(client, act_as, ids, "2026-07-04")

    with Session(engine) as sess:
        member = sess.exec(
            select(DBProjectMember).where(
                DBProjectMember.project_id == ids["project"],
                DBProjectMember.user_info_id == ids["editor"],
            )
        ).one()
        sess.delete(member)
        sess.commit()

    _patch(client, delete=["2026-07-04"])

    assert _stored(engine) == {}


def test_a_gap_filled_day_row_is_not_treated_as_something_to_lose(env):
    """_fill_day_gaps writes an all-null dict for every gap day. It is truthy
    but holds nothing, so it must not pin a day in place."""
    client, engine, ids, act_as = env
    _patch(client, {"2026-07-04": {
        "difficulty": None, "sleeping": None, "weather": None,
        "journal": None, "tags": None, "counters": [],
    }})
    _companion_journal(client, act_as, ids, "2026-07-04")

    _patch(client, delete=["2026-07-04"])

    assert _stored(engine) == {}


def test_a_day_pinned_by_another_members_memory_is_still_droppable(env):
    """Memories and encounters are shared, not per-user, so the caller can see
    what pins the day and needs no protection from dropping it."""
    client, engine, ids, _ = env
    with Session(engine) as sess:
        sess.add(DBMemory(project_id=ids["project"], date="2026-07-04"))
        sess.commit()
    _patch(client, {"2026-07-04": {"note": "my notes"}})

    _patch(client, delete=["2026-07-04"])

    assert _stored(engine) == {}


def test_a_legacy_null_author_journal_is_the_owners_on_the_shared_path(env):
    """A NULL author is a pre-#106 row belonging to the project owner. The
    owner may drop that day; an editor coming through ?owner= may not."""
    client, engine, ids, act_as = env
    _patch(client, {"2026-07-04": {"note": "shared notes"}})
    r = client.post("/api/journal/", json={
        "project_name": "Trip", "date": "2026-07-04",
        "geo_mode": "custom", "lat": 1.0, "lon": 2.0,
        "description": "legacy",
    })
    assert r.status_code == 201, r.text
    with Session(engine) as sess:
        from models.project_db import DBJournalEntry
        entry = sess.exec(select(DBJournalEntry).where(
            DBJournalEntry.project_id == ids["project"])).one()
        entry.user_info_id = None
        sess.add(entry)
        sess.commit()

    act_as("editor")
    r = client.patch(f"/api/projects/Trip/day-meta?owner={ids['owner']}",
                     json={"delete": ["2026-07-04"]})
    assert r.status_code == 200, r.text
    assert _stored(engine) == {"2026-07-04": {"note": "shared notes"}}

    act_as("owner")
    _patch(client, delete=["2026-07-04"])
    assert _stored(engine) == {}


def test_a_preserved_day_keeps_its_counters_exactly_once(env):
    """The guard runs before _merge_day_meta_preserve_counters; a day it puts
    back must not come out with duplicated or dropped counters."""
    client, engine, ids, act_as = env
    _patch(client, {"2026-07-04": {"note": "shared", "counters": [{"name": "c", "value": 2}]}})
    _companion_journal(client, act_as, ids, "2026-07-04")

    _patch(client, delete=["2026-07-04"])

    assert _stored(engine) == {
        "2026-07-04": {"note": "shared", "counters": [{"name": "c", "value": 2}]}
    }


# ── A malformed stored blob must not 500 the write ───────────────────────────

@pytest.mark.parametrize("blob", ['"null"', "null", "[1, 2]", "not json", '"a string"'])
def test_a_malformed_day_meta_row_does_not_break_the_write(env, blob):
    """day_meta_json is expected to hold an object. A hand-edited row, a bad
    migration or an older bug can leave something else there, and reading it
    with a bare .items() turned every settings save for that trip into a 500 —
    which the client reads as "cannot save at all"."""
    client, engine, ids, _ = env
    with Session(engine) as sess:
        row = sess.exec(select(DBProject).where(DBProject.name == "Trip")).one()
        row.day_meta_json = blob
        sess.add(row)
        sess.commit()

    _patch(client, {"2026-07-04": {"note": "written over the rubble"}})

    assert _stored(engine) == {"2026-07-04": {"note": "written over the rubble"}}


def test_a_day_entry_that_is_not_an_object_does_not_break_counter_merging(env):
    """Same for one bad day inside an otherwise fine map."""
    client, engine, _, _ = env
    with Session(engine) as sess:
        row = sess.exec(select(DBProject).where(DBProject.name == "Trip")).one()
        row.day_meta_json = '{"2026-07-04": "rubble", "2026-07-05": {"note": "fine"}}'
        sess.add(row)
        sess.commit()

    _patch(client, {"2026-07-05": {"note": "fine"}})

    assert _stored(engine) == {"2026-07-05": {"note": "fine"}}


@pytest.mark.parametrize("blob", ['"null"', "null", "[1, 2]", "not json",
                                  '{"2026-07-04": "rubble"}'])
def test_a_malformed_day_meta_row_does_not_break_loading_the_project(env, blob):
    """The write-side guard is not enough on its own: if the loader still
    raised, one bad row would 500 every project GET and every importer for that
    trip — and the user would have no settings screen to save the repair from.
    """
    client, engine, _, _ = env
    with Session(engine) as sess:
        row = sess.exec(select(DBProject).where(DBProject.name == "Trip")).one()
        row.day_meta_json = blob
        sess.add(row)
        sess.commit()

    r = client.get("/api/projects/Trip")
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("blob", ['"null"', "null", "[]", "not json",
                                  '{"2026-07-04": "rubble"}'])
def test_a_malformed_day_meta_row_does_not_break_the_stats_endpoint(env, blob):
    """Making only the loader and the save path tolerant left this endpoint
    returning 500 for a trip with one bad row.

    Scope, stated precisely because an earlier version of this docstring
    overstated it: the only caller is ProjectStatsScreen._load, reached from
    the Statistics button, plus the public /share/{token}/stats route. Opening
    a project is GET /{name}, which has been tolerant since the loader fix, so
    a bad row never blocked opening or repairing the trip — it broke the
    Statistics screen and the public share stats."""
    client, engine, _, _ = env
    with Session(engine) as sess:
        row = sess.exec(select(DBProject).where(DBProject.name == "Trip")).one()
        row.day_meta_json = blob
        sess.add(row)
        sess.commit()

    r = client.get("/api/projects/Trip/stats")
    assert r.status_code == 200, r.text
