"""Share links must not hand out a photo's EXIF location or device data (#430).

A memory photo is stored exactly as uploaded, GPS coordinates, camera serial
and all, and `GET /api/share/{token}/photos/{memory_id}/{uuid}` used to serve
that file to anyone holding the link. The owner's original stays untouched
(their authenticated fetch still carries everything); what a share link gets
is a copy with all EXIF removed, orientation applied to the pixels first so
the photo is not shown sideways. The copy is derived on first serve and cached
next to the original, deleted with it, and not charged to the owner's quota.
"""
from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import ExifTags, Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import api.memories as mem_mod
import api.share as share_mod
import models.db as db_module
import src.admin.storage as storage_mod
from api.deps import get_current_user, get_optional_current_user
from api.memories import router as memories_router
from api.share import invalidate_share_cache, router as share_router
from models.billing import UserUsage
from models.project_db import DBMemory, DBProject, DBProjectItem
from models.user import UserInfo

RED = (255, 0, 0)
BLUE = (0, 0, 255)


def _private_exif() -> Image.Exif:
    """Orientation 6 plus everything the issue is about: GPS, device, serials."""
    exif = Image.Exif()
    exif[0x0112] = 6                       # Orientation: rotate 90° CW to display
    exif[0x010F] = "TestMaker"             # Make
    exif[0x0110] = "TestModel"             # Model
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[1] = "N"; gps[2] = (48.0, 51.0, 24.0)
    gps[3] = "E"; gps[4] = (2.0, 21.0, 3.0)
    ex = exif.get_ifd(ExifTags.IFD.Exif)
    ex[0x9003] = "2024:06:01 12:00:00"     # DateTimeOriginal
    ex[0xA431] = "BODYSERIAL123"           # BodySerialNumber
    ex[0xA435] = "LENSSERIAL456"           # LensSerialNumber
    ex[0x927C] = b"MAKERNOTEBYTES"         # MakerNote
    return exif


def _half_red_half_blue(size=(40, 20)) -> Image.Image:
    """Left half red, right half blue — after a 90° CW rotation red is on top."""
    img = Image.new("RGB", size)
    w, h = size
    for x in range(w):
        for y in range(h):
            img.putpixel((x, y), RED if x < w // 2 else BLUE)
    return img


def _encoded(fmt: str, exif: Image.Exif | None = None, **kwargs) -> bytes:
    buf = io.BytesIO()
    extra = {"exif": exif.tobytes()} if exif is not None else {}
    _half_red_half_blue().save(buf, fmt, **extra, **kwargs)
    return buf.getvalue()


def _gps_jpeg() -> bytes:
    return _encoded("JPEG", _private_exif(), quality=95)


def _close(px, ref, tol=40) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(px[:3], ref))


COM, APP0, APP1, APP2, APP13, APP14, SOS = 0xFE, 0xE0, 0xE1, 0xE2, 0xED, 0xEE, 0xDA


def _jpeg_markers(data: bytes) -> list[int]:
    """The marker bytes of every segment up to and including SOS."""
    assert data[:2] == b"\xff\xd8", "not a JPEG"
    i, markers = 2, []
    while i + 4 <= len(data):
        assert data[i] == 0xFF, f"lost sync at byte {i}"
        marker = data[i + 1]
        markers.append(marker)
        if marker == SOS:
            break
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return markers


def _scan_data(data: bytes) -> bytes:
    """Everything from the SOS marker on — the entropy-coded pixels."""
    return data[data.index(b"\xff\xda"):]


def _assert_no_metadata_segments(data: bytes) -> None:
    markers = _jpeg_markers(data)
    assert COM not in markers, "COM segment survived"
    assert APP1 not in markers, "APP1 (EXIF/XMP) segment survived"
    assert APP13 not in markers, "APP13 (IPTC) segment survived"


def _assert_stripped_and_upright(data: bytes) -> None:
    img = Image.open(io.BytesIO(data))
    exif = img.getexif()
    assert dict(exif) == {}, f"EXIF survived: {dict(exif)}"
    assert dict(exif.get_ifd(ExifTags.IFD.GPSInfo)) == {}
    assert dict(exif.get_ifd(ExifTags.IFD.Exif)) == {}
    assert "exif" not in img.info and "xmp" not in img.info
    _assert_no_metadata_segments(data)
    # Orientation 6 on a 40x20 source: pixels rotated, so 20x40 with red on top.
    assert img.size == (20, 40)
    assert _close(img.getpixel((10, 5)), RED)
    assert _close(img.getpixel((10, 35)), BLUE)


def _assert_has_gps(data: bytes) -> None:
    exif = Image.open(io.BytesIO(data)).getexif()
    assert exif.get(0x0112) == 6
    assert dict(exif.get_ifd(ExifTags.IFD.GPSInfo))[2] == (48.0, 51.0, 24.0)


# ── fixture: owner + shared project, memories and share routers ──────────────

@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(mem_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(share_mod, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(engine)

    token = f"tok_{uuid.uuid4().hex}"
    with Session(engine) as sess:
        user = UserInfo(display_name="Owner", email="o@example.com")
        sess.add(user); sess.commit(); sess.refresh(user)
        project = DBProject(user_info_id=user.id, name="Trip", share_token=token)
        sess.add(project); sess.commit(); sess.refresh(project)
        mem = DBMemory(project_id=project.id, public_id="pub1", date="2024-06-01",
                       geo_mode="custom", photos_json="[]")
        sess.add(mem); sess.commit(); sess.refresh(mem)
        sess.add(DBProjectItem(project_id=project.id, position=0,
                               item_type="memory", memory_id=mem.id))
        sess.commit()
        user_id, memory_id = user.id, mem.id
    invalidate_share_cache(token)

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id), "email": "o@example.com"}
    app.dependency_overrides[get_optional_current_user] = lambda: None
    app.include_router(memories_router)
    app.include_router(share_router)
    client = TestClient(app)

    class Env:
        pass

    e = Env()
    e.client, e.engine, e.user_id, e.memory_id, e.token = client, engine, user_id, memory_id, token
    e.photo_dir = tmp_path / "users" / str(user_id) / "memories" / str(memory_id)
    e.share_url = lambda u, thumb=False: (
        f"/api/share/{token}/photos/{memory_id}/{u}" + ("/thumb" if thumb else ""))
    e.owner_url = lambda u, thumb=False: (
        f"/api/memories/{memory_id}/photos/{u}" + ("/thumb" if thumb else ""))
    yield e


def _upload(e, raw: bytes) -> str:
    resp = e.client.post(e.owner_url("").rstrip("/"),
                         files={"file": ("p.jpg", raw, "image/jpeg")})
    assert resp.status_code == 201, resp.text
    return resp.json()["uuid"]


def _usage(engine, user_id: int) -> int:
    with Session(engine) as sess:
        row = sess.exec(select(UserUsage).where(UserUsage.user_info_id == user_id)).first()
        return row.storage_bytes if row else 0


# ── the share link serves a stripped, upright copy; the owner keeps the original

class TestSharedPhotoIsStripped:
    def test_share_full_photo_has_no_location_or_device_exif_and_is_upright(self, env):
        photo = _upload(env, _gps_jpeg())
        resp = env.client.get(env.share_url(photo))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert "public" in resp.headers.get("cache-control", "")
        _assert_stripped_and_upright(resp.content)

    def test_owner_fetch_still_returns_the_untouched_original(self, env):
        raw = _gps_jpeg()
        photo = _upload(env, raw)
        env.client.get(env.share_url(photo))  # deriving the copy must not touch the original
        resp = env.client.get(env.owner_url(photo))
        assert resp.status_code == 200
        assert resp.content == raw
        _assert_has_gps(resp.content)

    def test_share_thumbnail_carries_no_exif(self, env):
        photo = _upload(env, _gps_jpeg())
        resp = env.client.get(env.share_url(photo, thumb=True))
        assert resp.status_code == 200
        exif = Image.open(io.BytesIO(resp.content)).getexif()
        assert dict(exif) == {}
        assert dict(exif.get_ifd(ExifTags.IFD.GPSInfo)) == {}

    def test_share_thumbnail_fallback_to_full_res_is_stripped_too(self, env):
        photo = _upload(env, _gps_jpeg())
        (env.photo_dir / f"{photo}_thumb.jpg").unlink()
        resp = env.client.get(env.share_url(photo, thumb=True))
        assert resp.status_code == 200
        _assert_stripped_and_upright(resp.content)

    def test_no_memories_token_still_gets_no_photo(self, env):
        photo = _upload(env, _gps_jpeg())
        with Session(env.engine) as sess:
            proj = sess.exec(select(DBProject)).first()
            proj.share_token_no_memories = "tok_nomem"
            sess.add(proj); sess.commit()
        invalidate_share_cache("tok_nomem")
        resp = env.client.get(f"/api/share/tok_nomem/photos/{env.memory_id}/{photo}")
        assert resp.status_code == 404

    def test_png_and_webp_uploads_are_stripped_as_well(self, env):
        for fmt in ("PNG", "WEBP"):
            photo = _upload(env, _encoded(fmt, _private_exif()))
            resp = env.client.get(env.share_url(photo))
            assert resp.status_code == 200, fmt
            _assert_stripped_and_upright(resp.content)

    def test_share_thumbnail_carries_no_comment_segment(self, env):
        """Pillow carries a JPEG comment across the thumbnail re-encode."""
        photo = _upload(env, _encoded("JPEG", _private_exif(), quality=95,
                                      comment=b"taken at home"))
        resp = env.client.get(env.share_url(photo, thumb=True))
        assert resp.status_code == 200
        assert b"taken at home" not in resp.content
        _assert_no_metadata_segments(resp.content)

    def test_pre_existing_thumbnail_is_served_without_metadata_and_without_re_encoding(self, env):
        """Thumbnails written before #430 may hold a comment (and, if a
        deployment ever wrote them differently, EXIF): served losslessly
        stripped, the pixels bit-for-bit those on disk, ICC kept."""
        from PIL import ImageCms

        photo = _upload(env, _gps_jpeg())
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        buf = io.BytesIO()
        _half_red_half_blue().save(buf, "JPEG", exif=_private_exif().tobytes(),
                                   comment=b"taken at home", icc_profile=icc)
        on_disk = buf.getvalue()
        (env.photo_dir / f"{photo}_thumb.jpg").write_bytes(on_disk)
        assert {COM, APP1} <= set(_jpeg_markers(on_disk))

        resp = env.client.get(env.share_url(photo, thumb=True))
        assert resp.status_code == 200
        served = resp.content
        _assert_no_metadata_segments(served)
        assert _scan_data(served) == _scan_data(on_disk)
        assert Image.open(io.BytesIO(served)).info.get("icc_profile") == icc
        assert dict(Image.open(io.BytesIO(served)).getexif()) == {}
        assert (env.photo_dir / f"{photo}_thumb.jpg").read_bytes() == on_disk

    def test_undecodable_original_is_a_404_and_warns_once_without_the_path(self, env, caplog):
        import logging

        photo = _upload(env, _gps_jpeg())
        (env.photo_dir / f"{photo}.jpg").write_bytes(b"not an image at all")
        with caplog.at_level(logging.WARNING, logger="src.utils.photo_privacy"):
            for _ in range(2):
                assert env.client.get(env.share_url(photo)).status_code == 404
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert photo in message, "the photo uuid is what an operator needs"
        assert str(env.photo_dir) not in message and "users" not in message, \
            "the owner's user id lives in the path, and the path is not logged"
        assert not (env.photo_dir / f"{photo}_share.jpg").exists()


# ── the app fetches the same copy, signed in, without a share link (R2-1) ────

class TestShareableCopyForTheApp:
    def test_owner_gets_the_stripped_copy_privately_cacheable(self, env):
        photo = _upload(env, _gps_jpeg())
        resp = env.client.get(env.owner_url(photo) + "/shareable")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        cache = resp.headers.get("cache-control", "")
        assert "private" in cache and "public" not in cache
        _assert_stripped_and_upright(resp.content)

    def test_it_is_the_one_copy_share_links_use(self, env, monkeypatch):
        from src.utils import photo_privacy

        calls = []
        real = photo_privacy.strip_private_metadata
        monkeypatch.setattr(photo_privacy, "strip_private_metadata",
                            lambda raw: calls.append(1) or real(raw))
        photo = _upload(env, _gps_jpeg())
        mine = env.client.get(env.owner_url(photo) + "/shareable").content
        theirs = env.client.get(env.share_url(photo)).content
        assert mine == theirs == (env.photo_dir / f"{photo}_share.jpg").read_bytes()
        assert calls == [1]

    def test_no_share_token_is_needed_or_created(self, env):
        with Session(env.engine) as sess:
            proj = sess.exec(select(DBProject)).first()
            proj.share_token = None
            sess.add(proj); sess.commit()
        photo = _upload(env, _gps_jpeg())
        assert env.client.get(env.owner_url(photo) + "/shareable").status_code == 200
        with Session(env.engine) as sess:
            assert sess.exec(select(DBProject)).first().share_token is None

    def test_deleted_and_undecodable_are_404_like_the_share_route(self, env):
        photo = _upload(env, _gps_jpeg())
        (env.photo_dir / f"{photo}.jpg").write_bytes(b"not an image at all")
        assert env.client.get(env.owner_url(photo) + "/shareable").status_code == 404
        (env.photo_dir / f"{photo}.jpg").unlink()
        assert env.client.get(env.owner_url(photo) + "/shareable").status_code == 404

    def test_an_outsider_is_refused(self, env):
        photo = _upload(env, _gps_jpeg())
        with Session(env.engine) as sess:
            other = UserInfo(display_name="Other", email="x@example.com")
            sess.add(other); sess.commit(); sess.refresh(other)
            other_id = other.id
        app = FastAPI()
        app.dependency_overrides[get_current_user] = lambda: {"sub": str(other_id), "email": "x@example.com"}
        app.include_router(memories_router)
        resp = TestClient(app).get(env.owner_url(photo) + "/shareable")
        assert resp.status_code in (403, 404)
        assert resp.headers.get("content-type", "").startswith("application/json")


# ── the copy is cached next to the original, deleted with it, and not charged

class TestShareCopyLifecycle:
    def test_copy_is_derived_once_and_cached_next_to_the_original(self, env, monkeypatch):
        from src.utils import photo_privacy

        calls = []
        real = photo_privacy.strip_private_metadata

        def counting(raw):
            calls.append(len(raw))
            return real(raw)

        monkeypatch.setattr(photo_privacy, "strip_private_metadata", counting)
        photo = _upload(env, _gps_jpeg())
        copy = env.photo_dir / f"{photo}_share.jpg"
        assert not copy.exists()
        first = env.client.get(env.share_url(photo)).content
        assert copy.exists()
        assert calls == [len(_gps_jpeg())]
        second = env.client.get(env.share_url(photo)).content
        assert calls == [len(_gps_jpeg())], "second serve must come from the cache"
        assert first == second == copy.read_bytes()
        assert {p.name for p in env.photo_dir.iterdir()} == {
            f"{photo}.jpg", f"{photo}_thumb.jpg", f"{photo}_share.jpg"}

    def test_deleting_the_photo_removes_the_copy(self, env):
        photo = _upload(env, _gps_jpeg())
        env.client.get(env.share_url(photo))
        assert (env.photo_dir / f"{photo}_share.jpg").exists()
        resp = env.client.delete(env.owner_url(photo))
        assert resp.status_code == 204
        assert list(env.photo_dir.iterdir()) == []

    def test_replacing_the_photo_removes_the_old_copy(self, env):
        photo = _upload(env, _gps_jpeg())
        env.client.get(env.share_url(photo))
        resp = env.client.put(env.owner_url(photo) + "/replace",
                              files={"file": ("p.jpg", _gps_jpeg(), "image/jpeg")})
        assert resp.status_code == 200
        new = resp.json()["uuid"]
        names = {p.name for p in env.photo_dir.iterdir()}
        assert names == {f"{new}.jpg", f"{new}_thumb.jpg"}

    def test_deleting_the_memory_removes_the_copy(self, env):
        photo = _upload(env, _gps_jpeg())
        env.client.get(env.share_url(photo))
        resp = env.client.delete(f"/api/memories/{env.memory_id}")
        assert resp.status_code == 204
        assert not env.photo_dir.exists()

    def test_copy_is_not_charged_to_the_owner_and_reconcile_agrees(self, env):
        from src.billing.usage import reconcile_usage

        photo = _upload(env, _gps_jpeg())
        after_upload = _usage(env.engine, env.user_id)
        assert after_upload > 0
        env.client.get(env.share_url(photo))
        assert (env.photo_dir / f"{photo}_share.jpg").stat().st_size > 0
        assert _usage(env.engine, env.user_id) == after_upload
        assert reconcile_usage(env.user_id) == after_upload
        env.client.delete(env.owner_url(photo))
        assert _usage(env.engine, env.user_id) == 0

    def test_photo_deleted_during_first_serve_leaves_no_orphan_copy(self, env, monkeypatch):
        """A DELETE that lands while the copy is being derived: the strip
        finishes and renames into place after the unlink — the copy must not
        stay behind, and the viewer gets a 404, never the original."""
        from src.utils import photo_privacy

        photo = _upload(env, _gps_jpeg())
        real = photo_privacy.strip_private_metadata

        def strip_while_deleted(raw):
            out = real(raw)
            mem_mod._delete_photo_files(str(env.user_id), env.memory_id, [photo])
            return out

        monkeypatch.setattr(photo_privacy, "strip_private_metadata", strip_while_deleted)
        assert env.client.get(env.share_url(photo)).status_code == 404
        assert list(env.photo_dir.iterdir()) == []

    def test_deleting_the_memory_removes_the_directory_whatever_is_left_in_it(self, env):
        photo = _upload(env, _gps_jpeg())
        env.client.get(env.share_url(photo))
        (env.photo_dir / f"{photo}.deadbeef.tmp_share.jpg").write_bytes(b"crashed mid-write")
        resp = env.client.delete(f"/api/memories/{env.memory_id}")
        assert resp.status_code == 204
        assert not env.photo_dir.exists()

    def test_deleting_a_photo_does_not_create_its_directory(self, env):
        with Session(env.engine) as sess:
            mem = sess.get(DBMemory, env.memory_id)
            mem.photos_json = json.dumps(["ghost"])
            sess.add(mem); sess.commit()
        assert not env.photo_dir.exists()
        resp = env.client.delete(env.owner_url("ghost"))
        assert resp.status_code == 204
        assert not env.photo_dir.exists()

    def test_the_temp_file_is_named_so_the_storage_walk_skips_it(self, env, monkeypatch):
        from src.admin.storage import dir_size
        from src.utils import photo_privacy

        renamed = []
        real_replace = photo_privacy.os.replace

        def spy(src, dst):
            renamed.append(Path(src).name)
            return real_replace(src, dst)

        monkeypatch.setattr(photo_privacy.os, "replace", spy)
        photo = _upload(env, _gps_jpeg())
        env.client.get(env.share_url(photo))
        assert len(renamed) == 1
        assert photo_privacy.is_share_copy(renamed[0])
        (env.photo_dir / renamed[0]).write_bytes(b"x" * 1000)
        counted = dir_size(env.photo_dir)
        assert counted == sum(
            p.stat().st_size for p in (env.photo_dir / f"{photo}.jpg",
                                       env.photo_dir / f"{photo}_thumb.jpg"))

    def test_stale_temp_files_are_cleaned_up(self, env):
        import os
        import time

        photo = _upload(env, _gps_jpeg())
        stale = env.photo_dir / f"{photo}.deadbeef.tmp_share.jpg"
        stale.write_bytes(b"crashed mid-write")
        old = time.time() - 3600
        os.utime(stale, (old, old))
        fresh = env.photo_dir / f"{photo}.cafebabe.tmp_share.jpg"
        fresh.write_bytes(b"another first serve, still writing")

        assert env.client.get(env.share_url(photo)).status_code == 200
        assert not stale.exists(), "a crash residue is removed on the next derive"
        assert fresh.exists(), "an in-flight derivation is left alone"

        assert env.client.delete(env.owner_url(photo)).status_code == 204
        assert list(env.photo_dir.iterdir()) == []

    def test_copy_is_never_double_served_from_a_stale_name(self, env):
        """A stripped copy is keyed by the photo's uuid — the replace path mints
        a new uuid, so a share fetch after a replace derives from the new bytes."""
        photo = _upload(env, _gps_jpeg())
        env.client.get(env.share_url(photo))
        plain = _encoded("JPEG", None, quality=95)  # 40x20, no orientation
        resp = env.client.put(env.owner_url(photo) + "/replace",
                              files={"file": ("p.jpg", plain, "image/jpeg")})
        new = resp.json()["uuid"]
        img = Image.open(io.BytesIO(env.client.get(env.share_url(new)).content))
        assert img.size == (40, 20)


# ── the pure unit: strip_private_metadata ────────────────────────────────────

class TestStripPrivateMetadata:
    def test_jpeg_loses_every_exif_tag_and_is_transposed(self):
        from src.utils.photo_privacy import strip_private_metadata

        _assert_stripped_and_upright(strip_private_metadata(_gps_jpeg()))

    @pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
    def test_other_formats_come_out_as_stripped_jpeg(self, fmt):
        from src.utils.photo_privacy import strip_private_metadata

        out = strip_private_metadata(_encoded(fmt, _private_exif()))
        assert Image.open(io.BytesIO(out)).format == "JPEG"
        _assert_stripped_and_upright(out)

    def test_png_text_chunks_do_not_survive(self):
        from PIL import PngImagePlugin
        from src.utils.photo_privacy import strip_private_metadata

        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "taken at home")
        buf = io.BytesIO()
        _half_red_half_blue().save(buf, "PNG", pnginfo=info)
        out = Image.open(io.BytesIO(strip_private_metadata(buf.getvalue())))
        assert "taken at home" not in str(out.info)

    def test_no_orientation_means_no_rotation(self):
        from src.utils.photo_privacy import strip_private_metadata

        out = Image.open(io.BytesIO(strip_private_metadata(_encoded("JPEG", None))))
        assert out.size == (40, 20)
        assert _close(out.getpixel((5, 10)), RED)

    def test_jpeg_copy_reuses_the_source_quantisation_so_it_stays_about_as_big(self):
        from src.utils.photo_privacy import strip_private_metadata

        img = Image.new("RGB", (600, 400))
        px = img.load()
        for x in range(600):
            for y in range(400):
                px[x, y] = ((x * 3) % 256, (y * 2) % 256, (x * y) % 256)
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=70)
        raw = buf.getvalue()
        out = strip_private_metadata(raw)
        assert len(out) < len(raw) * 1.2

    def test_grayscale_jpeg_is_handled(self):
        from src.utils.photo_privacy import strip_private_metadata

        buf = io.BytesIO(); _half_red_half_blue().convert("L").save(buf, "JPEG")
        out = Image.open(io.BytesIO(strip_private_metadata(buf.getvalue())))
        assert out.size == (40, 20)

    def test_icc_profile_is_kept(self):
        """Colour management is not private data — dropping it shifts colours."""
        from PIL import ImageCms
        from src.utils.photo_privacy import strip_private_metadata

        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        buf = io.BytesIO(); _half_red_half_blue().save(buf, "JPEG", icc_profile=icc)
        out = Image.open(io.BytesIO(strip_private_metadata(buf.getvalue())))
        assert out.info.get("icc_profile") == icc


class TestStripJpegMetadataSegments:
    """The lossless walk used on thumbnails."""

    def _jpeg(self, **save_kwargs) -> bytes:
        buf = io.BytesIO()
        _half_red_half_blue().save(buf, "JPEG", **save_kwargs)
        return buf.getvalue()

    def test_drops_com_app1_and_app13_and_keeps_the_pixels(self):
        from src.utils.photo_privacy import strip_jpeg_metadata_segments

        raw = self._jpeg(exif=_private_exif().tobytes(), comment=b"taken at home")
        # Splice in an IPTC-style APP13 segment after APP0, as editors do.
        iptc = b"\xff\xed" + (2 + 20).to_bytes(2, "big") + b"Photoshop 3.0\0" + b"\x00" * 6
        app0_end = 2 + 2 + int.from_bytes(raw[4:6], "big")
        raw = raw[:app0_end] + iptc + raw[app0_end:]
        assert {COM, APP1, APP13} <= set(_jpeg_markers(raw))

        out = strip_jpeg_metadata_segments(raw)
        markers = _jpeg_markers(out)
        assert not {COM, APP1, APP13} & set(markers)
        assert markers[0] == APP0
        assert _scan_data(out) == _scan_data(raw)
        assert Image.open(io.BytesIO(out)).tobytes() == Image.open(io.BytesIO(raw)).tobytes()
        assert b"taken at home" not in out and b"BODYSERIAL123" not in out

    def test_keeps_icc_and_adobe_segments(self):
        from PIL import ImageCms
        from src.utils.photo_privacy import strip_jpeg_metadata_segments

        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        raw = self._jpeg(icc_profile=icc)
        out = strip_jpeg_metadata_segments(raw)
        assert APP2 in _jpeg_markers(out)
        assert Image.open(io.BytesIO(out)).info.get("icc_profile") == icc

        buf = io.BytesIO()
        _half_red_half_blue().convert("CMYK").save(buf, "JPEG")
        cmyk = buf.getvalue()
        assert APP14 in _jpeg_markers(cmyk), "Pillow writes an Adobe marker for CMYK"
        out = strip_jpeg_metadata_segments(cmyk)
        assert APP14 in _jpeg_markers(out)
        assert Image.open(io.BytesIO(out)).tobytes() == Image.open(io.BytesIO(cmyk)).tobytes()

    def test_non_jpeg_bytes_pass_through_unchanged(self):
        from src.utils.photo_privacy import strip_jpeg_metadata_segments

        png = _encoded("PNG")
        assert strip_jpeg_metadata_segments(png) == png
        assert strip_jpeg_metadata_segments(b"") == b""

    def test_a_walk_that_never_reaches_the_scan_is_logged(self, caplog):
        """The guard for R2-3: a non-canonical JPEG is passed through from
        where the walk stopped, and says so — without any user data."""
        import logging
        from src.utils.photo_privacy import strip_jpeg_metadata_segments

        raw = self._jpeg(comment=b"taken at home")
        app0_end = 2 + 2 + int.from_bytes(raw[4:6], "big")
        # A stray byte where the next marker's 0xFF should be.
        malformed = raw[:app0_end] + b"\x00" + raw[app0_end:]
        with caplog.at_level(logging.WARNING, logger="src.utils.photo_privacy"):
            out = strip_jpeg_metadata_segments(malformed)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "start of scan" in warnings[0].getMessage()
        assert out == malformed[:app0_end] + malformed[app0_end:]

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="src.utils.photo_privacy"):
            strip_jpeg_metadata_segments(raw)
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


class TestStorageWalkIgnoresShareCopies:
    def test_dir_size_skips_share_copies_and_their_temp_files(self, tmp_path):
        from src.admin.storage import dir_size

        d = tmp_path / "users" / "1" / "memories" / "7"
        d.mkdir(parents=True)
        (d / "a.jpg").write_bytes(b"x" * 100)
        (d / "a_thumb.jpg").write_bytes(b"x" * 10)
        (d / "a_share.jpg").write_bytes(b"x" * 90)
        (d / "a.0123abcd.tmp_share.jpg").write_bytes(b"x" * 70)
        assert dir_size(tmp_path) == 110
