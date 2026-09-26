"""The guarded fetch used for URLs a client chooses (src/utils/safe_fetch).

No test here touches the network: name resolution is replaced by a table and
the connection by a stub that records where it was asked to connect.
"""
from __future__ import annotations

import io
import json

import pytest
from PIL import Image
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from models.project_db import DBMemory, DBProject
from models.user import UserInfo
from src.utils import safe_fetch
from src.utils.safe_fetch import FetchRefused, address_allowed, fetch_bytes, open_url, vet


# ── Address rules ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("addr", [
    "8.8.8.8", "1.1.1.1", "2001:4860:4860::8888", "::ffff:8.8.8.8",
])
def test_public_addresses_are_allowed(addr):
    assert address_allowed(addr)


@pytest.mark.parametrize("addr", [
    "10.0.0.1", "172.16.0.1", "192.168.1.5", "100.64.0.1", "fc00::1",
    "127.0.0.1", "::1", "169.254.169.254", "fe80::1", "0.0.0.0", "::",
    "224.0.0.1", "ff0e::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
    "64:ff9b::a00:1", "2002:0a00:0001::1", "fec0::1",
])
def test_non_public_addresses_are_refused(addr):
    assert not address_allowed(addr)


@pytest.mark.parametrize("addr", ["10.0.0.1", "192.168.1.5", "fc00::1"])
def test_an_allowed_private_host_may_use_private_addresses(addr):
    assert address_allowed(addr, private_ok=True)


@pytest.mark.parametrize("addr", [
    "127.0.0.1", "::1", "169.254.169.254", "fe80::1", "0.0.0.0", "224.0.0.1",
    "::ffff:127.0.0.1",
])
def test_loopback_link_local_and_the_like_are_never_allowed(addr):
    assert not address_allowed(addr, private_ok=True)


# ── URL vetting ───────────────────────────────────────────────────────────────

@pytest.fixture
def dns(monkeypatch):
    """Resolve names from a table instead of the network."""
    table: dict = {}

    def resolve(host, port):
        if host not in table:
            raise OSError(f"unknown host {host}")
        return list(table[host])

    monkeypatch.setattr(safe_fetch, "_resolve", resolve)
    return table


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "example.com/x",
])
def test_only_http_and_https_are_allowed(dns, url):
    with pytest.raises(FetchRefused):
        vet(url)


def test_credentials_in_the_url_are_refused(dns):
    dns["example.com"] = ["93.184.216.34"]
    with pytest.raises(FetchRefused):
        vet("http://user:pass@example.com/x")


def test_a_name_resolving_to_a_private_address_is_refused(dns):
    dns["sneaky.example"] = ["10.1.2.3"]
    with pytest.raises(FetchRefused):
        vet("https://sneaky.example/photo.jpg")


def test_one_private_address_among_public_ones_is_enough_to_refuse(dns):
    dns["mixed.example"] = ["93.184.216.34", "127.0.0.1"]
    with pytest.raises(FetchRefused):
        vet("https://mixed.example/photo.jpg")


def test_the_vetted_address_and_the_request_target_are_returned(dns):
    dns["example.com"] = ["93.184.216.34"]
    assert vet("https://example.com:8443/a/b.jpg?x=1") == (
        "https", "example.com", 8443, "93.184.216.34", "/a/b.jpg?x=1",
    )


def test_an_allowed_host_may_resolve_privately_but_not_to_loopback(dns):
    dns["immich.lan"] = ["192.168.1.20"]
    dns["evil.lan"] = ["127.0.0.1"]
    assert vet("http://immich.lan/api", ["immich.lan"])[3] == "192.168.1.20"
    with pytest.raises(FetchRefused):
        vet("http://immich.lan/api")  # not allowed without the allowlist
    with pytest.raises(FetchRefused):
        vet("http://evil.lan/api", ["evil.lan"])


# ── Connections, redirects, caps ──────────────────────────────────────────────

class _Raw:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.headers = dict(headers or {})
        self._body = body

    def read1(self, n):
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    def release_conn(self):
        pass

    def close(self):
        pass


@pytest.fixture
def wire(monkeypatch):
    """Stub the connection: record each (ip, host, path) and serve scripted responses."""
    calls: list = []
    script: list = []

    def open_once(method, scheme, host, port, ip, path, headers, body, timeout):
        calls.append({"method": method, "ip": ip, "host": host, "port": port, "path": path,
                      "headers": dict(headers), "body": body, "timeout": timeout})
        return script.pop(0)

    monkeypatch.setattr(safe_fetch, "_open_once", open_once)
    return calls, script


def test_the_connection_goes_to_the_vetted_address(dns, wire):
    calls, script = wire
    dns["cdn.example"] = ["93.184.216.34"]
    script.append(_Raw(200, b"jpegbytes"))
    assert fetch_bytes("https://cdn.example/p.jpg", max_bytes=1000, total_timeout=5) == b"jpegbytes"
    assert [{k: c[k] for k in ("method", "ip", "host", "port", "path")} for c in calls] == [
        {"method": "GET", "ip": "93.184.216.34", "host": "cdn.example", "port": 443, "path": "/p.jpg"}]


def test_a_redirect_to_a_private_address_is_refused_before_connecting(dns, wire):
    calls, script = wire
    dns["cdn.example"] = ["93.184.216.34"]
    dns["internal.example"] = ["10.0.0.5"]
    script.append(_Raw(302, headers={"location": "http://internal.example/secret"}))
    with pytest.raises(FetchRefused):
        fetch_bytes("https://cdn.example/p.jpg", max_bytes=1000, total_timeout=5)
    assert [c["host"] for c in calls] == ["cdn.example"]  # never reached the second hop


def test_a_relative_redirect_to_a_public_host_is_followed(dns, wire):
    calls, script = wire
    dns["cdn.example"] = ["93.184.216.34"]
    script += [_Raw(301, headers={"location": "/moved.jpg"}), _Raw(200, b"ok")]
    assert fetch_bytes("https://cdn.example/p.jpg", max_bytes=1000, total_timeout=5) == b"ok"
    assert [c["path"] for c in calls] == ["/p.jpg", "/moved.jpg"]


def test_too_many_redirects_are_refused(dns, wire):
    calls, script = wire
    dns["loop.example"] = ["93.184.216.34"]
    script += [_Raw(302, headers={"location": "/again"}) for _ in range(10)]
    with pytest.raises(FetchRefused):
        open_url("https://loop.example/", max_bytes=1000, total_timeout=5, max_redirects=3)
    assert len(calls) == 4


def test_a_body_over_the_cap_is_refused(dns, wire):
    _, script = wire
    dns["big.example"] = ["93.184.216.34"]
    script.append(_Raw(200, b"x" * 5000))
    with pytest.raises(FetchRefused):
        fetch_bytes("https://big.example/p.jpg", max_bytes=1000, total_timeout=5)


def test_a_non_2xx_status_is_refused(dns, wire):
    _, script = wire
    dns["cdn.example"] = ["93.184.216.34"]
    script.append(_Raw(404, b"nope"))
    with pytest.raises(FetchRefused):
        fetch_bytes("https://cdn.example/missing.jpg", max_bytes=1000, total_timeout=5)


# ── The photo-from-URL download ───────────────────────────────────────────────

@pytest.fixture
def memory_db(monkeypatch, tmp_path):
    import api.memories as memories_mod
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(memories_mod, "_DATA_DIR", str(tmp_path))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        user = UserInfo(display_name="Alice", email="alice@example.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        project = DBProject(user_info_id=user.id, name="Trip")
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        memory = DBMemory(project_id=project.id, date="2025-06-01", photos_json="[]")
        sess.add(memory)
        sess.commit()
        sess.refresh(memory)
        return engine, user.id, project.id, memory.id


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "JPEG")
    return buf.getvalue()


def _photos(engine, memory_id):
    with Session(engine) as sess:
        return json.loads(sess.get(DBMemory, memory_id).photos_json or "[]")


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.7", "::1",
                                     "::ffff:127.0.0.1"])
def test_a_photo_url_on_a_non_public_address_is_never_fetched(memory_db, dns, monkeypatch, address):
    import api.memories as memories_mod
    engine, user_id, project_id, memory_id = memory_db
    dns["photos.example"] = [address]
    opened = []
    monkeypatch.setattr(safe_fetch, "_open_once", lambda *a, **k: opened.append(a) or _Raw(200, _jpeg()))
    # The old code fetched with requests.get; it must not be used either.
    monkeypatch.setattr("requests.get", lambda *a, **k: opened.append(a) or pytest.fail("requests.get used"))

    memories_mod._download_photo_from_url(memory_id, "http://photos.example/p.jpg", str(user_id), project_id)

    assert opened == []
    assert _photos(engine, memory_id) == []


def test_a_photo_url_on_a_public_address_is_stored(memory_db, dns, wire):
    import api.memories as memories_mod
    engine, user_id, project_id, memory_id = memory_db
    calls, script = wire
    dns["photos.example"] = ["93.184.216.34"]
    script.append(_Raw(200, _jpeg()))

    memories_mod._download_photo_from_url(memory_id, "https://photos.example/p.jpg", str(user_id), project_id)

    assert calls[0]["ip"] == "93.184.216.34" and calls[0]["host"] == "photos.example"
    assert len(_photos(engine, memory_id)) == 1


def test_a_journal_photo_url_on_a_non_public_address_is_never_fetched(dns, monkeypatch):
    import api.journal as journal_mod
    dns["photos.example"] = ["127.0.0.1"]
    opened = []
    monkeypatch.setattr(safe_fetch, "_open_once", lambda *a, **k: opened.append(a))
    monkeypatch.setattr("requests.get", lambda *a, **k: opened.append(a) or pytest.fail("requests.get used"))
    stored = []
    monkeypatch.setattr(journal_mod, "_save_photo_files", lambda *a, **k: stored.append(a))

    journal_mod._download_photo_from_url(1, "http://photos.example/p.jpg", "1", 1)

    assert opened == [] and stored == []


# ── Redirects across origins, timing, names ──────────────────────────────────

def test_a_redirect_to_another_host_does_not_carry_the_callers_headers(dns, wire):
    calls, script = wire
    dns["immich.example"] = ["93.184.216.34"]
    dns["elsewhere.example"] = ["93.184.216.35"]
    script += [_Raw(302, headers={"location": "https://elsewhere.example/x"}), _Raw(200, b"ok")]
    resp = open_url("https://immich.example/a", headers={"x-api-key": "SECRET"},
                    max_bytes=1000, total_timeout=5)
    resp.read_all()
    assert calls[0]["headers"] == {"x-api-key": "SECRET"}
    assert calls[1]["host"] == "elsewhere.example" and calls[1]["headers"] == {}


def test_a_redirect_within_the_same_origin_keeps_the_headers(dns, wire):
    calls, script = wire
    dns["immich.example"] = ["93.184.216.34"]
    script += [_Raw(302, headers={"location": "/b"}), _Raw(200, b"ok")]
    open_url("https://immich.example/a", headers={"x-api-key": "SECRET"},
             max_bytes=1000, total_timeout=5).read_all()
    assert calls[1]["headers"] == {"x-api-key": "SECRET"}


def test_a_redirect_from_https_to_http_is_refused(dns, wire):
    calls, script = wire
    dns["cdn.example"] = ["93.184.216.34"]
    script.append(_Raw(302, headers={"location": "http://cdn.example/p.jpg"}))
    with pytest.raises(FetchRefused):
        fetch_bytes("https://cdn.example/p.jpg", max_bytes=1000, total_timeout=5)
    assert len(calls) == 1


def test_a_request_body_is_not_sent_to_another_origin(dns, wire):
    calls, script = wire
    dns["immich.example"] = ["93.184.216.34"]
    dns["elsewhere.example"] = ["93.184.216.35"]
    script.append(_Raw(307, headers={"location": "https://elsewhere.example/search"}))
    with pytest.raises(FetchRefused):
        open_url("https://immich.example/search", method="POST", body=b"{}",
                 max_bytes=1000, total_timeout=5)
    assert len(calls) == 1


class _Trickle(_Raw):
    """Sends one byte per read, each read taking `step` seconds of fake time."""

    def __init__(self, clock, step, size):
        super().__init__(200, b"x" * size)
        self._clock, self._step = clock, step

    def read1(self, n):
        self._clock[0] += self._step
        return super().read1(1)


def test_a_trickled_body_is_cut_off_at_the_total_time(dns, wire, monkeypatch):
    _, script = wire
    clock = [1000.0]
    monkeypatch.setattr(safe_fetch.time, "monotonic", lambda: clock[0])
    dns["slow.example"] = ["93.184.216.34"]
    script.append(_Trickle(clock, 0.25, 100))
    with pytest.raises(FetchRefused):
        fetch_bytes("https://slow.example/p.jpg", max_bytes=1000, total_timeout=1)
    assert clock[0] <= 1000.0 + 1.25  # one read past the deadline at most


def test_each_read_waits_at_most_the_idle_timeout(dns, wire):
    calls, script = wire
    dns["cdn.example"] = ["93.184.216.34"]
    script += [_Raw(200, b"a"), _Raw(200, b"b")]
    open_url("https://cdn.example/1", max_bytes=10, total_timeout=600, idle_timeout=20).read_all()
    open_url("https://cdn.example/2", max_bytes=10, total_timeout=5, idle_timeout=20).read_all()
    assert calls[0]["timeout"].read_timeout == 20
    assert calls[1]["timeout"].read_timeout <= 5


def test_an_internationalised_host_name_is_used_in_its_ascii_form(dns):
    dns["xn--bcher-kva.example"] = ["93.184.216.34"]
    assert vet("https://b\u00fccher.example/p.jpg")[1] == "xn--bcher-kva.example"


# ── The real connection setup (no connection is made) ────────────────────────

class _PoolRecorder:
    made: list = []

    def __init__(self, host, **kwargs):
        self.host, self.kwargs = host, kwargs
        _PoolRecorder.made.append(self)

    def urlopen(self, method, path, **kwargs):
        self.urlopen_kwargs = kwargs
        return _Raw(200, b"")


@pytest.fixture
def pools(monkeypatch):
    import urllib3
    _PoolRecorder.made = []
    monkeypatch.setattr(urllib3, "HTTPSConnectionPool", _PoolRecorder)
    monkeypatch.setattr(urllib3, "HTTPConnectionPool", _PoolRecorder)
    return _PoolRecorder.made


def test_https_connects_to_the_vetted_address_and_checks_the_certificate_for_the_name(pools):
    safe_fetch._open_once("GET", "https", "cdn.example", 443, "93.184.216.34", "/p.jpg",
                          {"x": "1"}, None, None)
    pool = pools[0]
    assert pool.host == "93.184.216.34"
    assert pool.kwargs["server_hostname"] == "cdn.example"
    assert pool.kwargs["assert_hostname"] == "cdn.example"
    assert pool.kwargs["cert_reqs"] == "CERT_REQUIRED"
    assert pool.urlopen_kwargs["headers"]["Host"] == "cdn.example"
    assert pool.urlopen_kwargs["redirect"] is False


def test_an_ipv6_literal_goes_in_brackets_in_the_host_header(pools):
    safe_fetch._open_once("GET", "http", "2606:4700::1111", 8080, "2606:4700::1111", "/",
                          {}, None, None)
    assert pools[0].urlopen_kwargs["headers"]["Host"] == "[2606:4700::1111]:8080"
