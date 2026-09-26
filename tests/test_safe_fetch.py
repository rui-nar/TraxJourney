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

    def open_once(method, scheme, host, port, ip, path, headers, body, timeout, watchdog=None):
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


def test_each_read_waits_at_most_the_idle_timeout(dns, wire):
    calls, script = wire
    dns["cdn.example"] = ["93.184.216.34"]
    script += [_Raw(200, b"a"), _Raw(200, b"b")]
    open_url("https://cdn.example/1", max_bytes=10, total_timeout=600, idle_timeout=20).read_all()
    open_url("https://cdn.example/2", max_bytes=10, total_timeout=5, idle_timeout=20).read_all()
    assert calls[0]["timeout"] == 20
    assert calls[1]["timeout"] <= 5


def test_an_internationalised_host_name_is_used_in_its_ascii_form(dns):
    dns["xn--bcher-kva.example"] = ["93.184.216.34"]
    assert vet("https://b\u00fccher.example/p.jpg")[1] == "xn--bcher-kva.example"


# ── The real connection setup (no connection is made) ────────────────────────

import threading  # noqa: E402


class _FakeSock:
    """A connected TCP socket stand-in; its duplicates share one shutdown flag."""

    def __init__(self, shut=None):
        self.shut = shut or threading.Event()
        self.closed = False
        self.dups: list = []

    def settimeout(self, t):
        pass

    def setsockopt(self, *a):
        pass

    def dup(self):
        d = _FakeSock(self.shut)
        self.dups.append(d)
        return d

    def shutdown(self, how):
        self.shut.set()

    def close(self):
        self.closed = True


class _ConnRecorder:
    made: list = []

    def __init__(self, host, **kwargs):
        self.host, self.kwargs, self.sock, self.closed = host, kwargs, None, False
        _ConnRecorder.made.append(self)

    def connect(self):
        self.sock = self._new_conn()

    def request(self, method, path, **kwargs):
        self.request_kwargs = kwargs

    def getresponse(self):
        return _Raw(200, b"")

    def close(self):
        self.closed = True


@pytest.fixture
def conns(monkeypatch):
    import urllib3.connection
    _ConnRecorder.made = []
    socks: list = []

    def create_connection(address, timeout=None):
        sock = _FakeSock()
        sock.address, sock.connect_timeout = address, timeout
        socks.append(sock)
        return sock

    monkeypatch.setattr(urllib3.connection, "HTTPSConnection", _ConnRecorder)
    monkeypatch.setattr(urllib3.connection, "HTTPConnection", _ConnRecorder)
    monkeypatch.setattr(safe_fetch.socket, "create_connection", create_connection)
    return _ConnRecorder.made, socks


def test_https_connects_to_the_vetted_address_and_checks_the_certificate_for_the_name(conns):
    made, socks = conns
    safe_fetch._open_once("GET", "https", "cdn.example", 443, "93.184.216.34", "/p.jpg",
                          {"x": "1"}, None, 30.0)
    conn = made[0]
    assert socks[0].address == ("93.184.216.34", 443)
    assert conn.sock is socks[0]  # TLS runs over the socket we connected
    assert conn.kwargs["server_hostname"] == "cdn.example"
    assert conn.kwargs["assert_hostname"] == "cdn.example"
    assert conn.kwargs["cert_reqs"] == "CERT_REQUIRED"
    assert conn.request_kwargs["headers"]["Host"] == "cdn.example"


def test_the_tcp_connect_waits_at_most_ten_seconds(conns):
    _, socks = conns
    safe_fetch._open_once("GET", "http", "cdn.example", 80, "93.184.216.34", "/", {}, None, 60.0)
    assert socks[0].connect_timeout == 10.0


def test_an_ipv6_literal_goes_in_brackets_in_the_host_header(conns):
    made, _ = conns
    safe_fetch._open_once("GET", "http", "2606:4700::1111", 8080, "2606:4700::1111", "/",
                          {}, None, 5.0)
    assert made[0].request_kwargs["headers"]["Host"] == "[2606:4700::1111]:8080"


class _FailingConn(_ConnRecorder):
    def request(self, method, path, **kwargs):
        raise ConnectionResetError("reset")


def test_a_failed_request_closes_the_connection_and_the_socket(conns, monkeypatch):
    import urllib3.connection
    made, socks = conns
    monkeypatch.setattr(urllib3.connection, "HTTPConnection", _FailingConn)
    with pytest.raises(ConnectionResetError):
        safe_fetch._open_once("GET", "http", "cdn.example", 80, "93.184.216.34", "/", {}, None, 5.0)
    assert made[-1].closed and socks[0].closed


# ── The watchdog: a hard limit on the exchange once connected ─────────────────

class _HandshakeStall(_ConnRecorder):
    """TLS handshake that never completes until the connection is shut."""

    def connect(self):
        self.sock = self._new_conn()
        if not self.sock.shut.wait(10):
            raise AssertionError("the watchdog never shut the connection")
        raise ConnectionResetError("shut during the handshake")


class _HeaderStall(_ConnRecorder):
    """Connects, then never finishes sending its response headers."""

    def getresponse(self):
        if not self.sock.shut.wait(10):
            raise AssertionError("the watchdog never shut the connection")
        raise ConnectionResetError("socket shut down")


class _Stuck(_Raw):
    """A body whose single read never returns until the connection is shut."""

    def __init__(self, sock):
        super().__init__(200, b"")
        self._sock = sock

    def read1(self, n):
        if not self._sock.shut.wait(10):
            raise AssertionError("the watchdog never shut the connection")
        raise ConnectionResetError("socket shut down")


class _BodyStall(_ConnRecorder):
    def getresponse(self):
        return _Stuck(self.sock)


@pytest.mark.parametrize("conn_class", [_HandshakeStall, _HeaderStall, _BodyStall],
                         ids=["tls-handshake", "headers", "body"])
def test_the_watchdog_ends_an_exchange_that_outlasts_the_deadline(dns, conns, monkeypatch, conn_class):
    import time as _time
    import urllib3.connection
    monkeypatch.setattr(urllib3.connection, "HTTPSConnection", conn_class)
    dns["slow.example"] = ["93.184.216.34"]
    started = _time.monotonic()
    with pytest.raises(FetchRefused, match="longer than allowed"):
        fetch_bytes("https://slow.example/p.jpg", max_bytes=1000, total_timeout=0.3)
    assert _time.monotonic() - started < 5


class _FiredDog:
    fired = True

    def stop(self):
        pass


def test_nothing_is_handed_out_once_the_deadline_has_passed():
    # A shut TLS socket can still return raw bytes it had buffered.
    resp = safe_fetch.SafeResponse(status=200, headers={}, url="https://x.example/",
                                   _raw=_Raw(200, b"ciphertext"), _max_bytes=1000,
                                   _watchdog=_FiredDog())
    with pytest.raises(FetchRefused, match="longer than allowed"):
        resp.read_all()


def test_an_end_of_file_after_the_deadline_is_a_timeout_not_a_short_body():
    resp = safe_fetch.SafeResponse(status=200, headers={}, url="https://x.example/",
                                   _raw=_Raw(200, b""), _max_bytes=1000, _watchdog=_FiredDog())
    with pytest.raises(FetchRefused, match="longer than allowed"):
        resp.read_all()


def test_stopping_the_watchdog_cancels_the_timer_and_closes_the_connection():
    import time as _time
    dog = safe_fetch._Watchdog(_time.monotonic() + 60)
    sock, conn = _FakeSock(), _ConnRecorder("93.184.216.34")
    dog.watch(sock, conn)
    dog.stop()
    assert conn.closed and sock.dups[0].closed
    assert not dog._timer.is_alive() or dog._timer.finished.is_set()


def test_closing_a_response_stops_its_watchdog():
    stopped = []

    class Dog:
        fired = False

        def stop(self):
            stopped.append(True)

    safe_fetch.SafeResponse(status=200, headers={}, url="https://x.example/",
                            _raw=_Raw(200, b"ok"), _max_bytes=1000, _watchdog=Dog()).close()
    assert stopped == [True]


def test_a_redirect_stops_the_watchdog_of_its_hop(dns, wire, monkeypatch):
    _, script = wire
    dns["cdn.example"] = ["93.184.216.34"]
    stops = []
    real = safe_fetch._Watchdog.stop

    def counting_stop(self):
        stops.append(self)
        real(self)

    monkeypatch.setattr(safe_fetch._Watchdog, "stop", counting_stop)
    script += [_Raw(302, headers={"location": "/b"}), _Raw(200, b"ok")]
    fetch_bytes("https://cdn.example/a", max_bytes=1000, total_timeout=5)
    assert len(stops) == 2 and stops[0] is not stops[1]  # the first hop's, then the response's


# ── Origins ───────────────────────────────────────────────────────────────────

def test_an_upgrade_to_https_on_the_same_host_keeps_headers_and_body(dns, wire):
    calls, script = wire
    dns["immich.example"] = ["93.184.216.34"]
    script += [_Raw(308, headers={"location": "https://immich.example/search"}), _Raw(200, b"{}")]
    open_url("http://immich.example/search", method="POST", body=b"{}",
             headers={"x-api-key": "SECRET"}, max_bytes=1000, total_timeout=5).read_all()
    assert calls[1]["headers"] == {"x-api-key": "SECRET"} and calls[1]["body"] == b"{}"


@pytest.mark.parametrize("a, b, same", [
    (("http", "a.example", 80), ("https", "a.example", 443), True),
    (("https", "a.example", 443), ("https", "a.example", 443), True),
    (("http", "a.example", 80), ("https", "b.example", 443), False),
    (("http", "a.example", 8080), ("https", "a.example", 443), False),
    (("http", "a.example", 80), ("https", "a.example", 8443), False),
    (("https", "a.example", 443), ("http", "a.example", 80), False),
    (("https", "a.example", 443), ("https", "a.example", 8443), False),
])
def test_what_counts_as_the_same_origin(a, b, same):
    assert safe_fetch._same_origin(a, b) is same


def test_an_upgrade_to_https_on_another_host_does_not_keep_headers(dns, wire):
    calls, script = wire
    dns["immich.example"] = ["93.184.216.34"]
    dns["evil.example"] = ["93.184.216.35"]
    script += [_Raw(301, headers={"location": "https://evil.example/x"}), _Raw(200, b"ok")]
    open_url("http://immich.example/a", headers={"x-api-key": "SECRET"},
             max_bytes=1000, total_timeout=5).read_all()
    assert calls[1]["host"] == "evil.example" and calls[1]["headers"] == {}


def test_redirect_refusals_are_not_reported_as_a_refused_destination(dns, wire):
    _, script = wire
    dns["cdn.example"] = ["93.184.216.34"]
    script.append(_Raw(302, headers={"location": "http://cdn.example/p.jpg"}))
    with pytest.raises(FetchRefused) as exc:
        fetch_bytes("https://cdn.example/p.jpg", max_bytes=1000, total_timeout=5)
    assert not isinstance(exc.value, safe_fetch.DestinationRefused)
