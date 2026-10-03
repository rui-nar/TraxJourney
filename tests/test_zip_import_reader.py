"""Reading an uploaded trip ZIP (src/project/zip_import.py, #469).

The archive is untrusted. Each fault must give InvalidTripArchive and leave
no photo or thumbnail in the staging directory (its manifest.json may stay:
the caller removes the directory).
"""
from __future__ import annotations

import io
import json
import struct
import warnings
import zipfile
import zlib

import pytest
from PIL import Image

import src.project.zip_import as zip_import
from src.models.journal import JournalEntry
from src.models.memory import Memory
from src.models.project import Project, ProjectItem
from src.project.project_io import ProjectIO
from src.project.staged_photos import staged_total
from src.project.zip_import import InvalidTripArchive, read_trip_zip

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"
MISSING = "44444444-4444-4444-8444-444444444444"


def _jpeg(size=(640, 480), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _png_header_only(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b""))


def _project(memory_photos=(U1, U2, MISSING), journal_photos=(U3,)) -> Project:
    return Project(name="Alps", items=[
        ProjectItem(item_type="memory", memory=Memory(
            id=7, date="2025-06-01", name="Summit", photos=list(memory_photos))),
        ProjectItem(item_type="journal", journal=JournalEntry(
            id=3, date="2025-06-02", description="Rest day", photos=list(journal_photos))),
    ])


def _trip_bytes(project: Project) -> bytes:
    """The trip document as U1's export writes it: the full .traxj document,
    plus photo_refs on memories and journal entries."""
    data = ProjectIO.to_dict(project)
    data["activities"] = []
    for d in data["items"]:
        if d["item_type"] == "memory":
            m = d["memory"]
            m["photo_refs"] = [f"photos/{m['id']}/{u}.jpg" for u in m["photos"]]
        elif d["item_type"] == "journal":
            j = d["journal"]
            j["photo_refs"] = [f"journal/{j['id']}/{u}.jpg" for u in j["photos"]]
    return ProjectIO.dumps(data)


def _zip(entries, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # "Duplicate name"
        with zipfile.ZipFile(buf, "w", compression) as zf:
            for name, data in entries:
                zf.writestr(zipfile.ZipInfo(name), data, compress_type=compression)
    return buf.getvalue()


def _export(project=None, photos=None, extra=()) -> bytes:
    """An export of *project*: its trip file, then *photos* (entry name ->
    bytes; by default one JPEG per photo the archive carries), then *extra*."""
    project = project or _project()
    if photos is None:
        photos = {f"photos/7/{U1}.jpg": _jpeg(), f"photos/7/{U2}.jpg": _jpeg((300, 900)),
                  f"journal/3/{U3}.jpg": _jpeg((50, 50))}
    return _zip([("Alps.traxj", _trip_bytes(project)), *photos.items(), *extra])


def _read(data: bytes, staging):
    return read_trip_zip(io.BytesIO(data), staging, importer=42, trip_name="Alps")


def _staged_files(staging):
    return sorted(p.relative_to(staging).as_posix() for p in staging.rglob("*.jpg"))


def _refused(data: bytes, staging, match=None) -> InvalidTripArchive:
    with pytest.raises(InvalidTripArchive, match=match) as exc:
        _read(data, staging)
    assert _staged_files(staging) == []
    return exc.value


# ── The end-of-central-directory record, forged ──────────────────────────────

_EOCD = struct.Struct("<4s4H2LH")


def _with_end_record(data: bytes, *, entries=None, cd_size=None) -> bytes:
    pos = data.rfind(b"PK\x05\x06")
    fields = list(_EOCD.unpack_from(data, pos))
    if entries is not None:
        fields[3] = fields[4] = entries
    if cd_size is not None:
        fields[5] = cd_size
    return data[:pos] + _EOCD.pack(*fields) + data[pos + _EOCD.size:]


def _as_zip64(data: bytes, *, entries=None, cd_size=None) -> bytes:
    """*data* with a ZIP64 end record (and its locator) in front of the end
    record, which then defers to it, as a writer does past 65,535 entries."""
    pos = data.rfind(b"PK\x05\x06")
    _, _, _, _, count, real_size, cd_offset, _ = _EOCD.unpack_from(data, pos)
    entries = count if entries is None else entries
    record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0,
                         entries, entries, real_size if cd_size is None else cd_size, cd_offset)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, pos, 1)
    end = _EOCD.pack(b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    return data[:pos] + record + locator + end


def _forbid_zipfile(monkeypatch):
    """Fail the test if the archive is opened with ZipFile from here on."""
    def boom(*a, **k):
        pytest.fail("ZipFile was constructed before the end record was checked")
    monkeypatch.setattr(zipfile, "ZipFile", boom)


# ── A valid export ────────────────────────────────────────────────────────────

def test_an_export_round_trips_into_the_trip_and_its_staged_photos(tmp_path):
    staging = tmp_path / "import-x"
    photos = {f"photos/7/{U1}.jpg": _jpeg(), f"photos/7/{U2}.jpg": _jpeg((300, 900)),
              f"journal/3/{U3}.jpg": _jpeg((50, 50))}
    thumb_in_archive = (f"photos/7/{U1}_thumb.jpg", b"ignored")
    project, staged = _read(_export(photos=photos, extra=[thumb_in_archive]), staging)

    assert project.name == "Alps"
    mem = project.items[0].memory
    assert mem.id == 7 and mem.photos == [U1, U2, MISSING]
    assert project.items[1].journal.photos == [U3]

    assert set(staged) == {("memories", 7), ("journal", 3)}
    assert set(staged[("memories", 7)]) == {U1, U2}  # MISSING is not in the archive
    for (kind, item_id), by_name in staged.items():
        archive_folder = {"memories": "photos", "journal": "journal"}[kind]
        for name, photo in by_name.items():
            assert photo.full == staging / kind / str(item_id) / f"{name}.jpg"
            assert photo.thumb == staging / kind / str(item_id) / f"{name}_thumb.jpg"
            assert photo.full.read_bytes() == photos[f"{archive_folder}/{item_id}/{name}.jpg"]
            with Image.open(photo.thumb) as t:  # regenerated, not the archive's
                assert t.format == "JPEG" and max(t.size) <= 400
            assert photo.bytes == photo.full.stat().st_size + photo.thumb.stat().st_size
    assert staged_total(staged) == sum(
        p.bytes for by_name in staged.values() for p in by_name.values())
    assert _staged_files(staging) == sorted([
        f"journal/3/{U3}.jpg", f"journal/3/{U3}_thumb.jpg",
        f"memories/7/{U1}.jpg", f"memories/7/{U1}_thumb.jpg",
        f"memories/7/{U2}.jpg", f"memories/7/{U2}_thumb.jpg",
    ])


def test_the_manifest_is_written(tmp_path):
    staging = tmp_path / "import-x"
    _read(_export(), staging)
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["importer"] == 42 and manifest["trip_name"] == "Alps"
    assert manifest["created"]


def test_the_manifest_is_written_even_when_the_archive_is_refused(tmp_path):
    staging = tmp_path / "import-x"
    _refused(b"not a zip", staging)
    assert (staging / "manifest.json").exists()


def test_a_zip64_archive_is_read(tmp_path):
    project, staged = _read(_as_zip64(_export()), tmp_path / "s")
    assert project.name == "Alps" and set(staged) == {("memories", 7), ("journal", 3)}


def test_a_stored_archive_is_read(tmp_path):
    data = _zip([("Alps.traxj", _trip_bytes(_project())), (f"photos/7/{U1}.jpg", _jpeg())],
                compression=zipfile.ZIP_STORED)
    _, staged = _read(data, tmp_path / "s")
    assert set(staged[("memories", 7)]) == {U1}


def test_a_photo_listed_twice_is_staged_once(tmp_path):
    data = _export(project=_project(memory_photos=(U1, U1), journal_photos=()),
                   photos={f"photos/7/{U1}.jpg": _jpeg()})
    _, staged = _read(data, tmp_path / "s")
    assert staged == {("memories", 7): {U1: staged[("memories", 7)][U1]}}


def test_the_trip_file_limit_is_the_traxj_upload_limit():
    from api.project_transfer import MAX_IMPORT_BYTES
    assert zip_import.MAX_TRIP_FILE_BYTES == MAX_IMPORT_BYTES


# ── Entry names ───────────────────────────────────────────────────────────────

def test_traversal_and_absolute_names_are_ignored_and_never_written(tmp_path):
    staging = tmp_path / "deep" / "import-x"
    project = _project(memory_photos=(U1,), journal_photos=())
    data = _export(project=project, photos={f"photos/7/{U1}.jpg": _jpeg()}, extra=[
        ("../evil.jpg", _jpeg()),
        ("../../evil.jpg", _jpeg()),
        ("/tmp/evil.jpg", _jpeg()),
        (f"/photos/7/{U2}.jpg", _jpeg()),
        ("photos/7/../../evil.jpg", _jpeg()),
        (f"photos/7/{U1}/../../../evil.jpg", _jpeg()),
        ("photos\\7\\evil.jpg", _jpeg()),
    ])
    _, staged = _read(data, staging)

    assert staged == {("memories", 7): {U1: staged[("memories", 7)][U1]}}
    assert [p for p in tmp_path.rglob("*") if "evil" in p.name] == []
    assert _staged_files(staging) == [f"memories/7/{U1}.jpg", f"memories/7/{U1}_thumb.jpg"]


def test_a_photo_under_another_items_folder_is_not_taken(tmp_path):
    """Photos are found by the id the trip file gives the item, nothing else."""
    data = _export(photos={f"photos/8/{U1}.jpg": _jpeg(), f"journal/7/{U1}.jpg": _jpeg(),
                           f"photos/3/{U3}.jpg": _jpeg()})
    _, staged = _read(data, tmp_path / "s")
    assert staged == {}


def test_duplicate_entry_names_are_refused(tmp_path):
    data = _export(extra=[(f"photos/7/{U1}.jpg", _jpeg((10, 10)))])
    _refused(data, tmp_path / "s", match="same name")


def test_a_duplicated_trip_file_is_refused(tmp_path):
    trip = _trip_bytes(_project())
    _refused(_zip([("Alps.traxj", trip), ("Alps.traxj", trip)]), tmp_path / "s", match="same name")


# ── Entry count and central directory, before ZipFile ────────────────────────

def test_too_many_entries_are_refused_from_the_end_record(tmp_path, monkeypatch):
    data = _with_end_record(_export(), entries=zip_import.MAX_ENTRIES + 1)
    _forbid_zipfile(monkeypatch)
    _refused(data, tmp_path / "s", match="too many files")


def test_too_many_entries_in_a_zip64_end_record_are_refused(tmp_path, monkeypatch):
    data = _as_zip64(_export(), entries=10 ** 9)
    _forbid_zipfile(monkeypatch)
    _refused(data, tmp_path / "s", match="too many files")


def test_an_oversize_central_directory_is_refused_from_the_end_record(tmp_path, monkeypatch):
    data = _with_end_record(_export(), cd_size=zip_import.MAX_CENTRAL_DIRECTORY_BYTES + 1)
    _forbid_zipfile(monkeypatch)
    _refused(data, tmp_path / "s", match="table of contents")


def test_an_oversize_zip64_central_directory_is_refused(tmp_path, monkeypatch):
    data = _as_zip64(_export(), cd_size=2 ** 40)
    _forbid_zipfile(monkeypatch)
    _refused(data, tmp_path / "s")


def test_a_directory_with_more_entries_than_its_end_record_claims_is_refused(tmp_path, monkeypatch):
    """ZipFile reads entries until the directory's size is consumed, whatever
    count the end record claims, so the real list is counted again."""
    monkeypatch.setattr(zip_import, "MAX_ENTRIES", 5)
    data = _with_end_record(_export(extra=[(f"x{i}", b"") for i in range(5)]), entries=1)
    _refused(data, tmp_path / "s", match="too many files")


def test_an_archive_at_the_entry_limit_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(zip_import, "MAX_ENTRIES", 4)
    project, _ = _read(_export(), tmp_path / "s")
    assert project.name == "Alps"


# ── Sizes ─────────────────────────────────────────────────────────────────────

def test_a_photo_that_inflates_past_the_photo_limit_is_refused(tmp_path):
    bomb = _jpeg() + b"\x00" * (25 * 1024 * 1024)  # deflates to a few tens of KB
    data = _export(photos={f"photos/7/{U1}.jpg": _jpeg(), f"photos/7/{U2}.jpg": bomb})
    assert len(data) < 1024 * 1024
    _refused(data, tmp_path / "s", match=f"photo photos/7/{U2}.jpg .* too large")


def test_a_photo_whose_declared_size_lies_is_refused(tmp_path):
    """The central directory says 10 bytes; the entry inflates to far more."""
    data = bytearray(_export(photos={f"photos/7/{U1}.jpg": _jpeg() + b"\x00" * 100_000}))
    name = f"photos/7/{U1}.jpg".encode()
    cd = data.rfind(b"PK\x01\x02")
    while data[cd + 46: cd + 46 + len(name)] != name:
        cd = data.rfind(b"PK\x01\x02", 0, cd)
    struct.pack_into("<L", data, cd + 24, 10)
    _refused(bytes(data), tmp_path / "s", match="damaged or unreadable")


def test_the_staged_total_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(zip_import, "MAX_STAGED_PHOTO_BYTES", len(_jpeg()) + 1)
    _refused(_export(), tmp_path / "s", match="too large together")


def test_an_oversize_trip_file_is_refused(tmp_path):
    trip = _trip_bytes(_project())
    padded = trip[:-1] + b" " * (zip_import.MAX_TRIP_FILE_BYTES - len(trip) + 1) + trip[-1:]
    assert len(padded) == zip_import.MAX_TRIP_FILE_BYTES + 1
    _refused(_zip([("Alps.traxj", padded)]), tmp_path / "s", match="trip file .* too large")


def test_a_trip_file_at_its_limit_is_read(tmp_path, monkeypatch):
    trip = _trip_bytes(_project())
    monkeypatch.setattr(zip_import, "MAX_TRIP_FILE_BYTES", len(trip))
    project, _ = _read(_zip([("Alps.traxj", trip)]), tmp_path / "s")
    assert project.name == "Alps"


# ── The trip file ─────────────────────────────────────────────────────────────

def test_an_archive_without_a_trip_file_is_refused(tmp_path):
    _refused(_zip([(f"photos/7/{U1}.jpg", _jpeg())]), tmp_path / "s", match="no .* trip file")


def test_a_trip_file_in_the_one_top_level_folder_is_taken(tmp_path):
    """Once refused; accepted since the owner's 2026-10-03 envelope decision
    (#469 integrated review, round 1): it is a re-zipped export."""
    project, staged = _read(_zip([("Alps/Alps.traxj", _trip_bytes(_project()))]), tmp_path / "s")
    assert project.name == "Alps" and staged == {}


def test_a_trip_file_two_folders_down_is_not_taken(tmp_path):
    _refused(_zip([("a/b/x.traxj", _trip_bytes(_project()))]), tmp_path / "s",
             match="no .* trip file")


# ── An export unzipped and zipped again ───────────────────────────────────────

def _rezipped(folder="Alps", extra=()) -> bytes:
    """An export as Finder's "Compress" makes it from the unzipped folder:
    folder entries, every file under *folder*/, and __MACOSX/ AppleDouble
    files beside them."""
    photos = {f"photos/7/{U1}.jpg": _jpeg(), f"photos/7/{U2}.jpg": _jpeg((300, 900)),
              f"journal/3/{U3}.jpg": _jpeg((50, 50))}
    entries = [(f"{folder}/", b""), (f"{folder}/Alps.traxj", _trip_bytes(_project())),
               (f"{folder}/photos/", b""), (f"{folder}/photos/7/", b""),
               (f"{folder}/journal/", b""), (f"{folder}/journal/3/", b"")]
    entries += [(f"{folder}/{n}", d) for n, d in photos.items()]
    entries += [("__MACOSX/", b""), (f"__MACOSX/{folder}/", b""),
                (f"__MACOSX/{folder}/._Alps.traxj", b"\x00\x05\x16\x07"),
                (f"__MACOSX/{folder}/photos/7/._{U1}.jpg", b"\x00\x05\x16\x07"),
                (f"{folder}/._Alps.traxj", b"\x00\x05\x16\x07")]
    return _zip([*entries, *extra])


def test_a_rezipped_export_round_trips_with_its_photos(tmp_path):
    staging = tmp_path / "s"
    project, staged = _read(_rezipped(), staging)
    assert project.name == "Alps"
    assert {k: set(v) for k, v in staged.items()} == {
        ("memories", 7): {U1, U2}, ("journal", 3): {U3}}
    assert _staged_files(staging) == sorted([
        f"journal/3/{U3}.jpg", f"journal/3/{U3}_thumb.jpg",
        f"memories/7/{U1}.jpg", f"memories/7/{U1}_thumb.jpg",
        f"memories/7/{U2}.jpg", f"memories/7/{U2}_thumb.jpg",
    ])


def test_an_archive_with_two_top_level_folders_is_refused(tmp_path):
    trip = _trip_bytes(_project())
    _refused(_zip([("Alps/Alps.traxj", trip), (f"Other/photos/7/{U1}.jpg", _jpeg())]),
             tmp_path / "s", match="no .* trip file")


def test_a_file_beside_the_folder_is_refused(tmp_path):
    _refused(_zip([("Alps/Alps.traxj", _trip_bytes(_project())), ("readme.txt", b"hi")]),
             tmp_path / "s", match="no .* trip file")


@pytest.mark.parametrize("folder", ["..", ".", ""], ids=["dotdot", "dot", "absolute"])
def test_a_dot_or_absolute_folder_is_never_a_root(tmp_path, folder):
    _refused(_zip([(f"{folder}/Alps.traxj", _trip_bytes(_project()))]), tmp_path / "s",
             match="no .* trip file")


def test_traversal_names_under_the_folder_are_ignored_and_never_written(tmp_path):
    staging = tmp_path / "deep" / "import-x"
    data = _rezipped(extra=[
        ("Alps/../evil.jpg", _jpeg()),
        ("Alps/../../evil.jpg", _jpeg()),
        ("Alps/photos/7/../../../evil.jpg", _jpeg()),
        (f"Alps/photos/7/{U1}/../../../../evil.jpg", _jpeg()),
        ("Alps/photos\\7\\evil.jpg", _jpeg()),
    ])
    _, staged = _read(data, staging)
    assert {k: set(v) for k, v in staged.items()} == {
        ("memories", 7): {U1, U2}, ("journal", 3): {U3}}
    assert [p for p in tmp_path.rglob("*") if "evil" in p.name] == []


def test_a_root_level_export_ignores_photos_under_a_folder(tmp_path):
    """With the trip file at the top level, the root is the top level."""
    data = _export(photos={f"Alps/photos/7/{U1}.jpg": _jpeg()})
    _, staged = _read(data, tmp_path / "s")
    assert staged == {}


def test_an_archive_with_two_trip_files_is_refused(tmp_path):
    trip = _trip_bytes(_project())
    _refused(_zip([("Alps.traxj", trip), ("Other.traxj", trip)]), tmp_path / "s",
             match="more than one trip file")


def test_an_unreadable_trip_file_is_refused(tmp_path):
    _refused(_zip([("Alps.traxj", b"{not json")]), tmp_path / "s", match="isn't a valid")


# ── Photos ────────────────────────────────────────────────────────────────────

def test_a_non_image_jpg_is_refused_and_earlier_photos_are_removed(tmp_path):
    staging = tmp_path / "s"
    data = _export(photos={f"photos/7/{U1}.jpg": _jpeg(), f"photos/7/{U2}.jpg": b"<html>no</html>"})
    exc = _refused(data, staging)
    assert f"photos/7/{U2}.jpg" in str(exc) and "not a readable image" in str(exc)
    assert (staging / "manifest.json").exists()


def test_an_image_over_the_pixel_limit_is_refused(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        exc = _refused(_export(photos={f"journal/3/{U3}.jpg": _png_header_only(12_000, 10_000)}),
                       tmp_path / "s")
    assert "megapixels" in str(exc)


# ── Corrupt archives ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("data", [
    b"",
    b"PK\x03\x04 not really a zip",
    bytes(range(256)) * 64,
], ids=["empty", "local-header-only", "noise"])
def test_something_that_is_not_a_zip_is_refused(tmp_path, data):
    _refused(data, tmp_path / "s", match="not a ZIP archive")


def test_a_truncated_archive_is_refused(tmp_path):
    data = _export()
    _refused(data[: len(data) // 2], tmp_path / "s")


def test_an_archive_with_a_bad_central_directory_is_refused(tmp_path):
    data = bytearray(_export())
    cd = data.find(b"PK\x01\x02")
    data[cd:cd + 4] = b"XXXX"
    _refused(bytes(data), tmp_path / "s", match="not a ZIP archive")


def test_a_damaged_photo_is_refused(tmp_path):
    photo = _jpeg((900, 900))
    data = bytearray(_zip([("Alps.traxj", _trip_bytes(_project())), (f"photos/7/{U1}.jpg", photo)],
                          compression=zipfile.ZIP_STORED))
    at = data.find(photo) + len(photo) // 2
    data[at:at + 16] = bytes(b ^ 0xFF for b in data[at:at + 16])  # CRC no longer matches
    _refused(bytes(data), tmp_path / "s", match="damaged or unreadable")


def test_a_directory_name_flagged_utf8_that_isnt_is_refused(tmp_path):
    """ZipFile() decodes a name with flag 0x800 as UTF-8 (IR1-1)."""
    data = _export(extra=[("\u00e9.txt", b"x")])  # non-ASCII: zipfile sets 0x800
    assert data.count("\u00e9".encode()) == 2  # local header and directory
    _refused(data.replace("\u00e9".encode(), b"\xff\xfe"), tmp_path / "s", match="not a ZIP archive")


def test_a_local_name_flagged_utf8_that_isnt_is_refused(tmp_path):
    """Opening an entry decodes its local header name by that header's flag (IR1-1)."""
    name = f"photos/7/{U1}.jpg".encode()
    data = bytearray(_export(photos={f"photos/7/{U1}.jpg": _jpeg()}))
    lh = data.find(b"PK\x03\x04")
    while data[lh + 30: lh + 30 + len(name)] != name:
        lh = data.find(b"PK\x03\x04", lh + 4)
    struct.pack_into("<H", data, lh + 6, struct.unpack_from("<H", data, lh + 6)[0] | 0x800)
    data[lh + 30 + len(name) - 1] = 0xFF
    _refused(bytes(data), tmp_path / "s", match=f"photo photos/7/{U1}.jpg .* damaged or unreadable")


def _zstd_supported() -> bool:
    try:
        zipfile.ZipFile(io.BytesIO(), "w").writestr(
            zipfile.ZipInfo("x"), b"x", compress_type=zipfile.ZIP_ZSTANDARD)
    except (AttributeError, RuntimeError, NotImplementedError, ImportError):
        return False
    return True


_OTHER_METHODS = [
    pytest.param(zipfile.ZIP_BZIP2, "bzip2", id="bzip2"),
    pytest.param(zipfile.ZIP_LZMA, "lzma", id="lzma"),
    pytest.param(getattr(zipfile, "ZIP_ZSTANDARD", 93), "zstd", id="zstd",
                 marks=pytest.mark.skipif(not _zstd_supported(),
                                          reason="no Zstandard support in this zipfile")),
]


def _with_photo_in(compression) -> bytes:
    """An export whose trip file is deflated and whose photo uses *compression*."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("Alps.traxj"), _trip_bytes(_project()),
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr(zipfile.ZipInfo(f"photos/7/{U1}.jpg"), _jpeg((900, 900), color=(10, 200, 90)),
                    compress_type=compression)
    return buf.getvalue()


@pytest.mark.parametrize("compression,method", _OTHER_METHODS)
def test_a_photo_with_corrupt_compressed_data_is_refused(tmp_path, compression, method):
    """Corrupt bzip2, LZMA or Zstandard data each raise an error of their own
    decompressor (IR1-1, IR2-1); the method is refused before the entry is
    opened (owner decision, 2026-10-03)."""
    data = bytearray(_with_photo_in(compression))
    name = f"photos/7/{U1}.jpg".encode()
    lh = data.find(b"PK\x03\x04")
    while data[lh + 30: lh + 30 + len(name)] != name:
        lh = data.find(b"PK\x03\x04", lh + 4)
    size = struct.unpack_from("<L", data, lh + 18)[0]
    start = lh + 30 + len(name) + struct.unpack_from("<H", data, lh + 28)[0]
    for at in range(start + 8, start + size - 8, 7):
        data[at] ^= 0x5A  # garble the stream throughout, past its header
    _refused(bytes(data), tmp_path / "s",
             match=rf"unsupported compression method \({method}\)\. Re-create it as a standard ZIP")


@pytest.mark.parametrize("compression,method", _OTHER_METHODS)
def test_an_intact_photo_in_another_method_is_refused(tmp_path, compression, method):
    _refused(_with_photo_in(compression), tmp_path / "s",
             match=rf"unsupported compression method \({method}\)")


def test_a_trip_file_in_another_method_is_refused_before_it_is_opened(tmp_path, monkeypatch):
    data = _zip([("Alps.traxj", _trip_bytes(_project()))], compression=zipfile.ZIP_BZIP2)
    def no_open(*a, **k):
        pytest.fail("the entry was opened")
    monkeypatch.setattr(zipfile.ZipFile, "open", no_open)
    _refused(data, tmp_path / "s", match=r"unsupported compression method \(bzip2\)")


def test_an_unread_entry_in_another_method_does_not_block_the_import(tmp_path):
    """Only the entries read are checked: every other entry is ignored."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Alps.traxj", _trip_bytes(_project(memory_photos=(U1,), journal_photos=())))
        zf.writestr(f"photos/7/{U1}.jpg", _jpeg())
        zf.writestr(zipfile.ZipInfo("notes.txt"), b"hello" * 100, compress_type=zipfile.ZIP_BZIP2)
    _, staged = _read(buf.getvalue(), tmp_path / "s")
    assert set(staged[("memories", 7)]) == {U1}


def test_a_disk_error_staging_a_photo_is_not_blamed_on_the_archive(tmp_path, monkeypatch):
    """OSError is an archive fault only around the zipfile calls (IR1-1)."""
    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(zip_import, "write_photo_files", disk_full)
    with pytest.raises(OSError) as exc:
        _read(_export(), tmp_path / "s")
    assert not isinstance(exc.value, InvalidTripArchive)
