"""Journal entries in a .traxj file are imported as journal entries (issue #452).

The import had no branch for them: each journal item became a timeline item
with no journal row, which the loader then read as a flight segment at
(0, 0), and the entry's text was lost. An imported journal entry belongs to the
importer, like one they wrote.
"""

from __future__ import annotations

import json

from sqlmodel import Session, select

from models.project_db import DBJournalEntry, DBProject
from tests.test_import_replace import _doc, _import, _project, env  # noqa: F401


def test_an_imported_journal_entry_keeps_its_text_and_belongs_to_the_importer(env):
    client, engine, ids, act_as, _ = env

    r = _import(client, "Alps", _doc([], journal=["a quiet evening"]))

    assert r.status_code == 201, r.text
    project = _project(engine, ids["owner"])
    with Session(engine) as sess:
        (entry,) = sess.exec(select(DBJournalEntry).where(
            DBJournalEntry.project_id == project.id)).all()
    assert (entry.description, entry.user_info_id) == ("a quiet evening", ids["owner"])
    items = client.get("/api/projects/Alps").json()["items"]
    assert [i["item_type"] for i in items] == ["journal"]
    assert items[0]["journal"]["description"] == "a quiet evening"


def test_an_exported_journal_survives_a_copy(env):
    client, engine, ids, act_as, _ = env
    assert _import(client, "Alps", _doc([], journal=["a quiet evening"])).status_code == 201
    exported = client.get("/api/projects/Alps/export-traxj").content
    assert [i["item_type"] for i in json.loads(exported)["items"]] == ["journal"]

    r = _import(client, "Alps", exported, on_conflict="copy")

    assert r.status_code == 201, r.text
    items = client.get("/api/projects/Alps (2)").json()["items"]
    assert [(i["item_type"], i["journal"]["description"]) for i in items] == [
        ("journal", "a quiet evening")]
    with Session(engine) as sess:
        assert len(sess.exec(select(DBProject)).all()) == 2
