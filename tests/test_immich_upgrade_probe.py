"""Tests for scripts/immich_upgrade_probe.py (issue #33, plan unit U1).

Immich is faked by patching ``api.immich._http``. Covers: nothing is written
to the database or the data directory, and the database connection refuses
writes; a 403 is flagged with the key permission it needs; the contact sheet
lists the 3 nearest candidates per trip photo; the stored key never reaches
the output; the album listing mode; the EXIF check on Polarsteps photos; and
a search that ignores ``albumIds`` falls back to the album's own list.
"""
from __future__ import annotations

import hashlib
import io
import random
import re
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest
from PIL import Image
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

import api.immich as immich_module
from models.project_db import DBJournalEntry, DBMemory, DBProject
from models.user import ImmichToken, UserInfo

import scripts.immich_upgrade_probe as probe

API_KEY = "very-secret-immich-key"
ALBUM = "album-1"


def _image(seed: int, size=(64, 48)) -> Image.Image:
    rnd = random.Random(seed)
    w, h = size
    return Image.frombytes("RGB", size, bytes(rnd.randrange(256) for _ in range(w * h * 3)))


def _jpeg(img: Image.Image, exif=None) -> bytes:
    buf = io.BytesIO()
    if exif is not None:
        img.save(buf, "JPEG", quality=95, exif=exif)
    else:
        img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self._content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size=None):
        return iter([self._content] if self._content else [])

    def close(self):
        pass


# Album: five images over the two trip days, one video, plus one more day out.
_ASSETS = [
    ("a1", "IMG_1.jpg", "image/jpeg", "2024-05-01T08:00:00.000Z", 1),
    ("a2", "IMG_2.HEIC", "image/heic", "2024-05-01T09:00:00.000Z", 2),
    ("a3", "IMG_3.DNG", "image/x-adobe-dng", "2024-05-01T10:00:00.000Z", 3),
    ("a4", "IMG_4.jpg", "image/jpeg", "2024-05-02T08:00:00.000Z", 4),
    ("a5", "IMG_5.jpg", "image/jpeg", "2024-04-30T23:00:00.000Z", 5),
]


def _asset(aid, name, mime, local, _seed, type_="IMAGE"):
    return {"id": aid, "type": type_, "originalFileName": name, "originalMimeType": mime,
            "fileCreatedAt": local,
            "localDateTime": local, "isTrashed": False,
            "exifInfo": {"exifImageWidth": 4000, "exifImageHeight": 3000}}


class FakeImmich:
    """Routes the probe's calls; records them; answers 403 to the paths listed."""

    def __init__(self, forbidden=(), ignore_album_ids=False):
        self.forbidden = set(forbidden)
        self.ignore_album_ids = ignore_album_ids
        self.calls = []
        self.assets = [_asset(*a) for a in _ASSETS]
        self.assets.append(_asset("v1", "clip.mov", "video/quicktime",
                                  "2024-05-01T12:00:00.000Z", 9, type_="VIDEO"))
        self.images = {a[0]: _jpeg(_image(a[4])) for a in _ASSETS}

    def _forbidden(self, path):
        return any(path.startswith(f) or path.endswith(f) for f in self.forbidden)

    def get(self, url, headers=None, timeout=None, stream=False):
        assert headers == {"x-api-key": API_KEY}
        u = urlparse(url)
        self.calls.append(("GET", u.path + (f"?{u.query}" if u.query else "")))
        if self._forbidden(u.path):
            return _FakeResponse(403, {"message": "Missing required permission"})
        if u.path == "/api/server/about":
            return _FakeResponse(200, {"version": "v1.135.3"})
        if u.path == "/api/albums":
            return _FakeResponse(200, [{"id": ALBUM, "albumName": "Japan 2024", "assetCount": 6}])
        if u.path == f"/api/albums/{ALBUM}":
            return _FakeResponse(200, {"id": ALBUM, "albumName": "Japan 2024",
                                       "assetCount": len(self.assets), "assets": self.assets})
        m = re.fullmatch(r"/api/assets/([^/]+)/(thumbnail|original)", u.path)
        if m:
            raw = self.images.get(m.group(1))
            if raw is None:
                return _FakeResponse(404)
            return _FakeResponse(200, headers={"content-type": "image/jpeg"}, content=raw)
        return _FakeResponse(404)

    def post(self, url, headers=None, json=None, timeout=None):
        assert headers == {"x-api-key": API_KEY}
        path = urlparse(url).path
        self.calls.append(("POST", path, json))
        if self._forbidden(path):
            return _FakeResponse(403, {"message": "Missing required permission"})
        assert path == "/api/search/metadata"
        images = [a for a in self.assets if a["type"] == "IMAGE"]
        if self.ignore_album_ids:
            images = images + [_asset("x9", "other.jpg", "image/jpeg",
                                      "2024-05-01T08:00:00.000Z", 99)]
        if json.get("page") is None:
            return _FakeResponse(200, {"assets": {"total": 3, "count": 3,
                                                  "items": images[:3], "nextPage": "2"}})
        return _FakeResponse(200, {"assets": {"total": len(images) - 3, "count": len(images) - 3,
                                              "items": images[3:], "nextPage": None}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A file database and data dir: a trip of 3 photos over two days (two in
    a Polarsteps memory, one in the owner's journal), owner connected to Immich."""
    db = tmp_path / "traxjourney.db"
    data = tmp_path / "data"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    names = [str(uuid.uuid4()) for _ in range(3)]
    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="o@e.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        project = DBProject(user_info_id=user.id, name="Japan")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        sess.add(ImmichToken(user_info_id=user.id, server_url="https://immich.example.com",
                             api_key=API_KEY))
        mem = DBMemory(project_id=project.id, date="2024-05-01", polarsteps_step_id=7,
                       photos_json=f'["{names[0]}", "{names[1]}"]')
        journal = DBJournalEntry(project_id=project.id, user_info_id=user.id,
                                 date="2024-05-02", photos_json=f'["{names[2]}"]')
        sess.add(mem)
        sess.add(journal)
        sess.commit()
        uid, pid, mid, jid = user.id, project.id, mem.id, journal.id
    engine.dispose()

    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "2024:05:01 10:00:00"
    # Trip photos look like album photos a1, a2 and a4.
    for name, seed, kind, row, with_exif in (
            (names[0], 1, "memories", mid, True), (names[1], 2, "memories", mid, False),
            (names[2], 4, "journal", jid, False)):
        folder = data / "users" / str(uid) / kind / str(row)
        folder.mkdir(parents=True, exist_ok=True)
        img = _image(seed)
        (folder / f"{name}.jpg").write_bytes(_jpeg(img, exif if with_exif else None))
        (folder / f"{name}_thumb.jpg").write_bytes(_jpeg(img))

    fake = FakeImmich()
    monkeypatch.setattr(immich_module, "_http", fake)
    return {"db": db, "data": data, "pid": pid, "fake": fake, "out": tmp_path / "sheet.html",
            "names": names}


def _run(env, capsys, *extra, album=True):
    argv = ["--db", str(env["db"]), "--data-dir", str(env["data"]),
            "--project-id", str(env["pid"]), "--out", str(env["out"])]
    if album:
        argv += ["--album-id", ALBUM]
    code = probe.main(argv + list(extra))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_nothing_is_written_to_the_db_or_the_data_dir(env, capsys):
    db_before = hashlib.sha256(env["db"].read_bytes()).hexdigest()
    files_before = sorted(p.name for p in env["db"].parent.iterdir())
    data_before = _tree(env["data"])

    code, _ = _run(env, capsys)

    assert code == 0
    assert hashlib.sha256(env["db"].read_bytes()).hexdigest() == db_before
    # Only the contact sheet is new next to the database: no journal left.
    assert sorted(p.name for p in env["db"].parent.iterdir()) == sorted(files_before + ["sheet.html"])
    assert _tree(env["data"]) == data_before


def test_db_connection_refuses_writes(env):
    engine = probe.read_only_engine(env["db"])
    try:
        with engine.connect() as conn:
            with pytest.raises(Exception) as exc_info:
                conn.execute(text("DELETE FROM immichtoken"))
            assert isinstance(exc_info.value.orig, sqlite3.OperationalError)
    finally:
        engine.dispose()


def test_out_into_the_data_dir_is_refused(env, capsys):
    env["out"] = env["data"] / "sheet.html"
    code, output = _run(env, capsys)
    assert code == 2
    assert "data directory" in output
    assert not env["out"].exists()


def test_report_flags_a_403(env, capsys):
    env["fake"].forbidden = {"/original"}
    code, output = _run(env, capsys)
    assert code == 0
    assert re.search(r"HTTP 403: GET /api/assets/a1/original \(key permission: asset.download\)",
                     output)
    assert "no call answered 401 or 403" not in output


def test_report_flags_a_refused_search_and_falls_back_to_the_album(env, capsys):
    env["fake"].forbidden = {"/api/search/metadata"}
    code, output = _run(env, capsys)
    assert code == 0
    assert "HTTP 403: POST /api/search/metadata (key permission: asset.read)" in output
    assert "against the album's asset list" in output


def test_html_lists_three_candidates_per_photo(env, capsys):
    code, output = _run(env, capsys)
    assert code == 0
    sheet = env["out"].read_text(encoding="utf-8")
    rows = re.findall(r"<tr data-photo='([^']+)'>(.*?)</tr>", sheet, re.S)
    assert sorted(name for name, _ in rows) == sorted(env["names"])
    for _name, body in rows:
        assert body.count("class='candidate'") == 3
    # The trip photo made from album photo a1 ranks a1 first, at distance 0.
    first = dict(rows)[env["names"][0]]
    assert re.search(r"class='candidate'><img [^>]+>a1<br>score 0 ", first)


def test_report_answers_the_open_questions(env, capsys):
    code, output = _run(env, capsys)
    assert code == 0
    assert "version v1.135.3" in output
    # OD1: the search's paging and its fidelity to the album.
    assert "nextPage='2' (str)" in output
    assert "items outside the album: 0 (albumIds honoured)" in output
    assert "against search/metadata" in output
    assert "present on 5/5 assets" in output
    assert "localDateTime=2024-05-01T08:00:00.000Z" in output
    # OD3: one of each format probed at both sizes.
    for kind, aid in (("jpeg", "a1"), ("heic", "a2"), ("raw", "a3")):
        assert f"{kind}: asset {aid}" in output
    assert "size=fullsize: image/jpeg" in output
    # OD4: one of the two Polarsteps photos keeps its capture time.
    assert "1/2 have DateTimeOriginal" in output
    assert "2024:05:01 10:00:00" in output


def test_search_ignoring_album_ids_falls_back_to_the_album(env, capsys):
    env["fake"].ignore_album_ids = True
    code, output = _run(env, capsys)
    assert code == 0
    assert "albumIds is NOT honoured" in output
    assert "against the album's asset list" in output


def test_stored_key_never_reaches_the_output(env, capsys):
    env["fake"].forbidden = {"/original"}
    code, output = _run(env, capsys)
    assert code == 0
    assert API_KEY not in output
    assert API_KEY not in env["out"].read_text(encoding="utf-8")


def test_without_an_album_lists_albums(env, capsys):
    code, output = _run(env, capsys, album=False)
    assert code == 0
    assert f"{ALBUM}  Japan 2024  (6 assets)" in output
    assert not env["out"].exists()
    assert not any(c[1].startswith("/api/assets") for c in env["fake"].calls)


def test_limit_days_bounds_the_calibration(env, capsys):
    code, _ = _run(env, capsys, "--limit-days", "1")
    assert code == 0
    sheet = env["out"].read_text(encoding="utf-8")
    assert sorted(re.findall(r"<tr data-photo='([^']+)'>", sheet)) == sorted(env["names"][:2])


def test_hashes_are_near_for_one_image_and_far_for_another():
    img, other = _image(1), _image(2)
    assert probe.hamming(probe.ahash(img), probe.ahash(img.resize((128, 96)))) <= 4
    assert probe.hamming(probe.dhash(img), probe.dhash(other)) > 16
