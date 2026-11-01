"""Tests for the Immich client module (issue #33, plan unit U2).

Immich is faked by patching the shared ``_http`` instance's get/post, or, for
the address policy, by stubbing name resolution and the connection under the
real guarded fetch. No network is used.

Covers: the album search request body (albumIds, window, type IMAGE) and its
paging across pages, nextPage as a string or a number, the 10,000-item limit,
the search page cap and its truncated flag, asset fields (local time,
trashed, archived), malformed items dropped one by one, album listing, typed
errors (403 naming the permissions), thumbnail and original URLs, and the
address policy on every call.
"""
from __future__ import annotations

import pytest
import requests

import src.immich.client as client_module
from src.immich.client import (
    ImmichAddressNotAllowed,
    ImmichBadAnswer,
    ImmichClient,
    ImmichRejected,
    ImmichTooManyAssets,
    ImmichUnreachable,
)
from src.utils import safe_fetch

SERVER = "https://immich.example.com"
AFTER = "2024-06-01T00:00:00.000Z"
BEFORE = "2024-06-10T23:59:59.999Z"


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, content=b""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}
        self.content = content
        self.closed = False

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json

    def iter_content(self, chunk_size=None):
        return iter([self.content])

    def close(self):
        self.closed = True


def _item(n, **extra):
    return {"id": f"a{n}", "fileCreatedAt": "2024-06-02T10:00:00.000Z", **extra}


def _page(items, next_page=None):
    return _FakeResponse(json_data={"assets": {"items": items, "nextPage": next_page}})


@pytest.fixture
def fake(monkeypatch):
    """Scripted search pages; records every request."""
    calls: list = []
    pages: list = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return pages.pop(0)

    def get(url, headers=None, timeout=None, stream=False):
        calls.append({"method": "GET", "url": url, "headers": headers, "stream": stream})
        return pages.pop(0)

    monkeypatch.setattr(client_module._http, "post", post)
    monkeypatch.setattr(client_module._http, "get", get)
    return calls, pages


def _client():
    return ImmichClient(SERVER + "/", "secret-key")


# ── Album photos ──────────────────────────────────────────────────────────────

def test_album_iterator_pins_the_request_and_follows_three_pages(fake):
    calls, pages = fake
    pages += [
        _page([_item(1), _item(2)], next_page="2"),
        _page([_item(3)], next_page=3),
        _page([_item(4)], next_page=None),
    ]
    got = [a.id for a in _client().iter_album_assets("album-1", AFTER, BEFORE)]
    assert got == ["a1", "a2", "a3", "a4"]
    assert [c["url"] for c in calls] == [SERVER + "/api/search/metadata"] * 3
    assert all(c["headers"]["x-api-key"] == "secret-key" for c in calls)
    base = {
        "albumIds": ["album-1"],
        "takenAfter": AFTER,
        "takenBefore": BEFORE,
        "type": "IMAGE",
        "withExif": True,
        "size": 1000,
    }
    assert [c["json"] for c in calls] == [{**base, "page": 1}, {**base, "page": 2}, {**base, "page": 3}]


def test_album_iterator_yields_exactly_the_limit(fake):
    _calls, pages = fake
    pages += [_page([_item(i) for i in range(1000)], next_page=str(p + 2)) for p in range(9)]
    pages.append(_page([_item(i) for i in range(1000)], next_page=None))
    assert sum(1 for _ in _client().iter_album_assets("al", AFTER, BEFORE)) == 10_000


def test_album_iterator_fails_instead_of_yielding_item_10001(fake):
    _calls, pages = fake
    pages += [_page([_item(i) for i in range(1000)], next_page=str(p + 2)) for p in range(10)]
    pages.append(_page([_item(10_000)], next_page=None))
    seen = []
    with pytest.raises(ImmichTooManyAssets):
        for asset in _client().iter_album_assets("al", AFTER, BEFORE):
            seen.append(asset)
    assert len(seen) == 10_000


def test_album_iterator_limit_is_a_parameter(fake):
    _calls, pages = fake
    pages.append(_page([_item(1), _item(2), _item(3)]))
    it = _client().iter_album_assets("al", AFTER, BEFORE, max_items=2)
    assert [next(it).id, next(it).id] == ["a1", "a2"]
    with pytest.raises(ImmichTooManyAssets):
        next(it)


def test_asset_fields_including_trashed_and_archived(fake):
    _calls, pages = fake
    pages.append(_page([
        _item(1, type="IMAGE", localDateTime="2024-06-02T17:23:00.000Z",
              originalMimeType="image/jpeg", originalFileName="IMG_1.jpg",
              exifInfo={"latitude": 43.6, "longitude": 1.44, "fileSizeInByte": 1234}),
        _item(2, isTrashed=True),
        _item(3, isArchived=True),
        _item(4, visibility="archive"),
    ]))
    a1, a2, a3, a4 = _client().iter_album_assets("al", AFTER, BEFORE)
    assert a1.local_date_time == "2024-06-02T17:23:00.000Z"  # never converted
    assert a1.file_created_at == "2024-06-02T10:00:00.000Z"
    assert (a1.type, a1.mime, a1.original_file_name, a1.file_size) == ("IMAGE", "image/jpeg", "IMG_1.jpg", 1234)
    assert (a1.lat, a1.lon) == (43.6, 1.44)
    assert (a1.is_trashed, a1.is_archived) == (False, False)
    assert (a2.is_trashed, a2.is_archived) == (True, False)
    assert a3.is_archived and a4.is_archived
    assert a2.local_date_time is None and a2.file_size is None


def test_asset_dimensions_come_from_exif_as_stored(fake):
    _calls, pages = fake
    pages.append(_page([
        _item(1, exifInfo={"exifImageWidth": 4080, "exifImageHeight": 3072}),
        _item(2, exifInfo={}),
        _item(3),
        _item(4, exifInfo={"exifImageWidth": "4080", "exifImageHeight": -3072}),
        _item(5, exifInfo={"exifImageWidth": 0, "exifImageHeight": True}),
        _item(6, exifInfo={"exifImageWidth": 40.5, "exifImageHeight": None}),
    ]))
    got = [(a.width, a.height) for a in _client().iter_album_assets("al", AFTER, BEFORE)]
    assert got == [(4080, 3072)] + [(None, None)] * 5


def test_malformed_items_are_dropped_one_by_one(fake):
    _calls, pages = fake
    pages.append(_page([1, {"id": 5, "fileCreatedAt": "x"}, {"id": "no-date"},
                        _item(1, exifInfo=5, localDateTime=7)]))
    got = list(_client().iter_album_assets("al", AFTER, BEFORE))
    assert [a.id for a in got] == ["a1"]
    assert got[0].local_date_time is None and got[0].lat is None


@pytest.mark.parametrize("answer", [
    {"assets": {"items": None}},
    {"assets": []},
    [1, 2],
    {"assets": {"items": [], "nextPage": "two"}},
    {"assets": {"items": [], "nextPage": "1"}},  # does not advance
])
def test_an_answer_of_the_wrong_shape_is_a_bad_answer(fake, answer):
    _calls, pages = fake
    pages.append(_FakeResponse(json_data=answer))
    with pytest.raises(ImmichBadAnswer):
        list(_client().iter_album_assets("al", AFTER, BEFORE))


def test_an_unreadable_answer_is_a_bad_answer(fake):
    _calls, pages = fake
    pages.append(_FakeResponse(json_data=ValueError("not json")))
    with pytest.raises(ImmichBadAnswer):
        _client().search_assets(AFTER, BEFORE)


# ── Search ────────────────────────────────────────────────────────────────────

def test_search_follows_pages_and_is_not_truncated(fake):
    calls, pages = fake
    pages += [_page([_item(1)], "2"), _page([_item(2)], "3"), _page([_item(3)], None)]
    assets, truncated = _client().search_assets(AFTER, BEFORE)
    assert [a.id for a in assets] == ["a1", "a2", "a3"]
    assert truncated is False
    assert calls[0]["json"] == {"takenAfter": AFTER, "takenBefore": BEFORE, "withExif": True,
                                "page": 1, "size": 1000}
    assert [c["json"]["page"] for c in calls] == [1, 2, 3]


def test_search_stops_at_its_page_cap_and_says_so(fake):
    calls, pages = fake
    pages += [_page([_item(p)], str(p + 2)) for p in range(10)]
    assets, truncated = _client().search_assets(AFTER, BEFORE)
    assert truncated is True
    assert len(assets) == 10 and len(calls) == 10
    assert pages == []  # the 11th page was never asked for


def test_search_exactly_at_the_cap_is_not_truncated(fake):
    _calls, pages = fake
    pages += [_page([_item(1)], "2"), _page([_item(2)], None)]
    assert _client().search_assets(AFTER, BEFORE, max_pages=2)[1] is False


# ── Albums ────────────────────────────────────────────────────────────────────

def test_list_albums_owned_and_shared_each_once(fake):
    calls, pages = fake
    pages.append(_FakeResponse(json_data=[
        {"id": "own", "albumName": "Japan", "assetCount": 37, "startDate": "2024-03-01T08:00:00.000Z",
         "endDate": "2024-03-10T20:00:00.000Z", "albumThumbnailAssetId": "t1", "shared": False},
        {"id": "both", "albumName": "Family", "assetCount": 5, "shared": True},
        {"albumName": "no id"},
    ]))
    pages.append(_FakeResponse(json_data=[
        {"id": "both", "albumName": "Family", "assetCount": 5, "shared": True},
        {"id": "theirs", "albumName": "Friend's trip", "assetCount": "many"},
    ]))
    albums = _client().list_albums()
    assert [c["url"] for c in calls] == [SERVER + "/api/albums", SERVER + "/api/albums?shared=true"]
    assert [(a.id, a.shared) for a in albums] == [("own", False), ("both", True), ("theirs", True)]
    own = albums[0]
    assert (own.name, own.asset_count, own.thumbnail_asset_id) == ("Japan", 37, "t1")
    assert (own.start_date, own.end_date) == ("2024-03-01T08:00:00.000Z", "2024-03-10T20:00:00.000Z")
    assert albums[2].asset_count is None and albums[2].thumbnail_asset_id is None


def test_list_albums_of_the_wrong_shape_is_a_bad_answer(fake):
    _calls, pages = fake
    pages.append(_FakeResponse(json_data={"albums": []}))
    with pytest.raises(ImmichBadAnswer):
        _client().list_albums()


# ── Errors ────────────────────────────────────────────────────────────────────

def test_a_403_names_the_permissions(fake):
    _calls, pages = fake
    pages.append(_FakeResponse(status_code=403))
    with pytest.raises(ImmichRejected) as info:
        _client().list_albums()
    assert info.value.status == 403
    for permission in ("album.read", "asset.read", "asset.view", "asset.download"):
        assert permission in str(info.value)


def test_other_statuses_are_rejected_with_their_status(fake):
    _calls, pages = fake
    pages.append(_FakeResponse(status_code=500))
    with pytest.raises(ImmichRejected) as info:
        _client().search_assets(AFTER, BEFORE)
    assert info.value.status == 500
    assert "album.read" not in str(info.value)


def test_a_network_failure_is_unreachable(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(client_module._http, "post", boom)
    monkeypatch.setattr(client_module._http, "get", boom)
    with pytest.raises(ImmichUnreachable):
        _client().search_assets(AFTER, BEFORE)
    with pytest.raises(ImmichUnreachable):
        _client().open_original("a1")


# ── Asset bytes ───────────────────────────────────────────────────────────────

def test_thumbnail_bytes(fake):
    calls, pages = fake
    pages.append(_FakeResponse(headers={"content-type": "image/webp"}, content=b"RIFFwebp"))
    assert _client().thumbnail_bytes("a1", "thumbnail") == b"RIFFwebp"
    assert calls == [{"method": "GET", "url": SERVER + "/api/assets/a1/thumbnail?size=thumbnail",
                      "headers": {"x-api-key": "secret-key"}, "stream": False}]


def test_thumbnail_size_is_checked():
    with pytest.raises(ValueError):
        _client().thumbnail_bytes("a1", "huge&x=1")


def test_open_original_and_fullsize_stream(fake):
    calls, pages = fake
    pages += [_FakeResponse(content=b"orig"), _FakeResponse(content=b"full")]
    assert _client().open_original("a/1").content == b"orig"
    assert _client().open_fullsize("a1").content == b"full"
    assert [(c["url"], c["stream"]) for c in calls] == [
        (SERVER + "/api/assets/a%2F1/original", True),
        (SERVER + "/api/assets/a1/thumbnail?size=fullsize", True),
    ]


def test_open_original_closes_and_raises_on_404(fake):
    _calls, pages = fake
    resp = _FakeResponse(status_code=404)
    pages.append(resp)
    with pytest.raises(ImmichRejected) as info:
        _client().open_original("gone")
    assert info.value.status == 404
    assert resp.closed


# ── Address policy ────────────────────────────────────────────────────────────
# The real guarded fetch, over a stubbed resolver: a stored server whose name
# now resolves to a private address is refused by every call, before any
# connection is opened.

@pytest.fixture
def private_server(monkeypatch):
    opened: list = []
    monkeypatch.setattr(safe_fetch, "_resolve", lambda host, port: ["10.1.2.3"])
    monkeypatch.setattr(safe_fetch, "_open_once", lambda *a, **k: opened.append(a))
    monkeypatch.delenv("IMMICH_ALLOWED_HOSTS", raising=False)
    return opened


@pytest.mark.parametrize("call", [
    lambda c: c.list_albums(),
    lambda c: list(c.iter_album_assets("al", AFTER, BEFORE)),
    lambda c: c.search_assets(AFTER, BEFORE),
    lambda c: c.thumbnail_bytes("a1", "thumbnail"),
    lambda c: c.open_original("a1"),
    lambda c: c.open_fullsize("a1"),
], ids=["list_albums", "iter_album_assets", "search_assets", "thumbnail_bytes",
        "open_original", "open_fullsize"])
def test_every_call_applies_the_address_policy(private_server, call):
    with pytest.raises(ImmichAddressNotAllowed):
        call(_client())
    assert private_server == []
