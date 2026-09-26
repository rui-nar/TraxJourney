"""Fetch a URL chosen by a client, but only from a public address.

A request whose destination the client picks (a photo "from a URL", a user's
Immich server) must not reach what only the server can reach: the host, other
containers, the LAN, or the cloud metadata service. So every fetch here:

- accepts only http and https;
- resolves the host once and refuses it if ANY address it resolves to is not
  public (IPv4 and IPv6, including IPv4 embedded in IPv6: mapped, 6to4,
  Teredo and NAT64), unless the caller explicitly allows a named private host
  — and even then never loopback, link-local (169.254.169.254 included),
  multicast, reserved or unspecified addresses;
- connects to the address it vetted, not to whatever the name resolves to a
  moment later, keeping the original Host header and TLS name;
- follows redirects itself, vetting every hop the same way, and never
  carries the caller's headers (an API key, say) or body to another origin,
  nor from https down to http;
- caps the bytes read, the wait for each read (`idle_timeout`) and the
  whole exchange (`total_timeout`): at the deadline a watchdog shuts the
  connection's socket down, whatever it is doing (trickled headers, a
  compressed body that never decodes, an endless chunked trailer).
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import urllib3
import urllib3.connection

try:  # requests depends on certifi; fall back to the system store without it
    import certifi
    _CA_CERTS: Optional[str] = certifi.where()
except ImportError:  # pragma: no cover
    _CA_CERTS = None

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_CHUNK = 64 * 1024


class FetchRefused(Exception):
    """The URL, one of its redirects, or its response broke the policy."""


class DestinationRefused(FetchRefused):
    """The destination itself is not allowed: its scheme, its address, or
    where a redirect tried to take the request. Other refusals (size, time,
    an unresolvable name) are about the fetch, not the destination."""


def _embedded_v4(addr: ipaddress.IPv6Address) -> list:
    """IPv4 addresses an IPv6 address carries, which also have to be public."""
    out = []
    if addr.ipv4_mapped:
        out.append(addr.ipv4_mapped)
    if addr.sixtofour:
        out.append(addr.sixtofour)
    if addr.teredo:
        out.extend(addr.teredo)
    if addr in _NAT64:
        out.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return out


def _never_allowed(addr) -> bool:
    return (addr.is_loopback or addr.is_link_local or addr.is_multicast
            or addr.is_unspecified or addr.is_reserved
            or (isinstance(addr, ipaddress.IPv6Address) and addr.is_site_local))


def address_allowed(addr, *, private_ok: bool = False) -> bool:
    """True if a fetch may connect to `addr`.

    Public addresses always may. With `private_ok` (an operator-allowed host),
    so may private ones, but loopback, link-local, multicast, reserved and
    unspecified never do.
    """
    addr = ipaddress.ip_address(addr)
    candidates = [addr]
    if isinstance(addr, ipaddress.IPv6Address):
        candidates += _embedded_v4(addr)
    for a in candidates:
        if _never_allowed(a):
            return False
        if a.is_global:
            continue
        if private_ok and a.is_private:
            continue
        return False
    return True


def _resolve(host: str, port: int) -> list:
    """Every address `host` resolves to. Patched in tests."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0].split("%", 1)[0] for info in infos]


def _ascii_host(host: str) -> str:
    """The host as it goes on the wire: lower case, IDNA A-label."""
    host = host.strip().lower().rstrip(".")
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        raise FetchRefused("the host name is not valid") from None


def vet(url: str, allowed_private_hosts: Iterable[str] = ()) -> Tuple[str, str, int, str, str]:
    """Check `url` against the policy and pick the address to connect to.

    Returns (scheme, host, port, ip, path_and_query). Raises FetchRefused.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise FetchRefused(f"not a valid URL: {exc}") from None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise DestinationRefused("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise DestinationRefused("URLs with credentials are not allowed")
    if not parts.hostname:
        raise FetchRefused("the URL has no host")
    host = _ascii_host(parts.hostname)
    port = port or (443 if scheme == "https" else 80)
    allowed = set()
    for h in allowed_private_hosts:
        if h and h.strip():
            try:
                allowed.add(_ascii_host(h))
            except FetchRefused:
                pass
    private_ok = host in allowed
    try:
        addresses = _resolve(host, port)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise FetchRefused(f"the host could not be resolved: {exc}") from None
    if not addresses:
        raise FetchRefused("the host could not be resolved")
    for a in addresses:
        try:
            ok = address_allowed(a, private_ok=private_ok)
        except ValueError:
            ok = False
        if not ok:
            raise DestinationRefused("the host resolves to an address that is not allowed")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, addresses[0], path


class _Watchdog:
    """Shuts the current connection's socket down at the deadline.

    Per-read timeouts cannot bound a whole exchange: a read that keeps
    receiving a byte now and then never times out, and a single urllib3
    read can loop over many socket reads (a compressed body that yields
    nothing, a chunked trailer that never ends). Shutting the socket down
    from here makes whatever read is in progress fail at once.
    """

    def __init__(self, deadline: float):
        self.conn = None
        self.fired = False
        self._lock = threading.Lock()
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, conn) -> None:
        """Watch `conn` (already connected); shut it at once if already late."""
        with self._lock:
            self.conn = conn
            late = self.fired
        if late:
            self._shut(conn)

    def _fire(self) -> None:
        with self._lock:
            self.fired = True
            conn = self.conn
        if conn is not None:
            self._shut(conn)

    @staticmethod
    def _shut(conn) -> None:
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self) -> None:
        self._timer.cancel()
        with self._lock:
            conn, self.conn = self.conn, None
        if conn is not None:
            conn.close()


@dataclass
class SafeResponse:
    """A vetted response. Read it with `iter_bytes` or `read_all`, then close."""
    status: int
    headers: Mapping[str, str]
    url: str
    _raw: urllib3.BaseHTTPResponse = field(repr=False)
    _max_bytes: int = field(repr=False)
    _watchdog: Optional[_Watchdog] = field(default=None, repr=False)

    def iter_bytes(self) -> Iterator[bytes]:
        total = 0
        try:
            while True:
                try:
                    chunk = self._raw.read1(_CHUNK)
                except Exception:
                    if self._watchdog is not None and self._watchdog.fired:
                        raise FetchRefused("the download took longer than allowed") from None
                    raise
                if not chunk:
                    # A shut-down socket reads as end of file: that is the
                    # watchdog's cut, not the end of the body.
                    if self._watchdog is not None and self._watchdog.fired:
                        raise FetchRefused("the download took longer than allowed")
                    return
                total += len(chunk)
                if total > self._max_bytes:
                    raise FetchRefused("the response is larger than allowed")
                yield chunk
        finally:
            self.close()

    def read_all(self) -> bytes:
        return b"".join(self.iter_bytes())

    def close(self) -> None:
        self._raw.close()
        if self._watchdog is not None:
            self._watchdog.stop()


def _same_origin(a, b) -> bool:
    """Same scheme, host and port; an http to https upgrade on the default
    ports of the same host counts as the same origin, as browsers and
    requests treat it."""
    if a == b:
        return True
    return (a[1] == b[1] and a[0] == "http" and a[2] == 80
            and b[0] == "https" and b[2] == 443)


def _open_once(method, scheme, host, port, ip, path, headers, body, timeout, watchdog=None):
    """Send one request to `ip`, presenting `host` (Host header, TLS name)."""
    name = f"[{host}]" if ":" in host else host  # an IPv6 literal
    host_header = name if port == (443 if scheme == "https" else 80) else f"{name}:{port}"
    if scheme == "https":
        conn = urllib3.connection.HTTPSConnection(
            ip, port=port, timeout=timeout,
            cert_reqs="CERT_REQUIRED", ca_certs=_CA_CERTS,
            assert_hostname=host, server_hostname=host,
        )
    else:
        conn = urllib3.connection.HTTPConnection(ip, port=port, timeout=timeout)
    try:
        conn.connect()
        if watchdog is not None:
            watchdog.watch(conn)
        conn.request(method, path, body=body, headers={**headers, "Host": host_header},
                     preload_content=False)
        return conn.getresponse()
    except BaseException:
        conn.close()
        raise


def open_url(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Mapping[str, str]] = None,
    body: Optional[bytes] = None,
    max_bytes: int,
    total_timeout: float,
    max_redirects: int = 3,
    idle_timeout: float = 30.0,
    allowed_private_hosts: Iterable[str] = (),
) -> SafeResponse:
    """Open `url` under the policy. Raises FetchRefused, or urllib3 errors
    for network failures. The caller must read or close the response."""
    allowed = tuple(allowed_private_hosts)
    deadline = time.monotonic() + total_timeout
    headers = dict(headers or {})
    origin = None
    for _ in range(max_redirects + 1):
        scheme, host, port, ip, path = vet(url, allowed)
        if origin is not None and not _same_origin(origin, (scheme, host, port)):
            if origin[0] == "https" and scheme == "http":
                raise FetchRefused("a redirect from https to http is not followed")
            if body is not None:
                raise FetchRefused("a request body is not sent to another origin")
            headers = {}  # never carry the caller's headers to another origin
        origin = (scheme, host, port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchRefused("the download took longer than allowed")
        wait = min(idle_timeout, remaining)
        watchdog = _Watchdog(deadline)
        try:
            raw = _open_once(method, scheme, host, port, ip, path, headers, body, wait,
                             watchdog=watchdog)
        except Exception:
            watchdog.stop()
            if watchdog.fired:
                raise FetchRefused("the download took longer than allowed") from None
            raise
        if raw.status in _REDIRECT_CODES and raw.headers.get("location"):
            location = raw.headers["location"]
            raw.close()
            watchdog.stop()
            url = urljoin(url, location)
            if raw.status == 303 or (raw.status in (301, 302) and method == "POST"):
                method, body = "GET", None
            continue
        return SafeResponse(status=raw.status, headers=raw.headers, url=url,
                            _raw=raw, _max_bytes=max_bytes, _watchdog=watchdog)
    raise FetchRefused("too many redirects")


def fetch_bytes(url: str, *, max_bytes: int, total_timeout: float,
                allowed_private_hosts: Iterable[str] = ()) -> bytes:
    """GET `url` under the policy and return its body; raises FetchRefused,
    urllib3 errors, or FetchRefused for a non-2xx status."""
    resp = open_url(url, max_bytes=max_bytes, total_timeout=total_timeout,
                    allowed_private_hosts=allowed_private_hosts)
    if not 200 <= resp.status < 300:
        resp.close()
        raise FetchRefused(f"the server answered {resp.status}")
    return resp.read_all()
