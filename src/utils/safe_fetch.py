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
- follows redirects itself, vetting every hop the same way;
- caps the bytes read and the total time taken.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import urllib3

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
            or addr.is_unspecified or addr.is_reserved)


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


def _normalise_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


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
        raise FetchRefused("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise FetchRefused("URLs with credentials are not allowed")
    host = parts.hostname
    if not host:
        raise FetchRefused("the URL has no host")
    port = port or (443 if scheme == "https" else 80)
    private_ok = _normalise_host(host) in {_normalise_host(h) for h in allowed_private_hosts if h}
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
            raise FetchRefused("the host resolves to an address that is not allowed")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, addresses[0], path


@dataclass
class SafeResponse:
    """A vetted response. Read it with `iter_bytes` or `read_all`, then close."""
    status: int
    headers: Mapping[str, str]
    url: str
    _raw: urllib3.BaseHTTPResponse = field(repr=False)
    _deadline: float = field(repr=False)
    _max_bytes: int = field(repr=False)

    def iter_bytes(self) -> Iterator[bytes]:
        total = 0
        try:
            for chunk in self._raw.stream(_CHUNK):
                total += len(chunk)
                if total > self._max_bytes:
                    raise FetchRefused("the response is larger than allowed")
                if time.monotonic() > self._deadline:
                    raise FetchRefused("the download took longer than allowed")
                yield chunk
        finally:
            self.close()

    def read_all(self) -> bytes:
        return b"".join(self.iter_bytes())

    def close(self) -> None:
        self._raw.release_conn()
        self._raw.close()


def _open_once(method, scheme, host, port, ip, path, headers, body, timeout):
    host_header = host if port == (443 if scheme == "https" else 80) else f"{host}:{port}"
    if scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            ip, port=port, timeout=timeout, retries=False, maxsize=1,
            cert_reqs="CERT_REQUIRED", ca_certs=_CA_CERTS,
            assert_hostname=host, server_hostname=host,
        )
    else:
        pool = urllib3.HTTPConnectionPool(ip, port=port, timeout=timeout, retries=False, maxsize=1)
    return pool.urlopen(
        method, path, body=body, headers={**headers, "Host": host_header},
        redirect=False, retries=False, preload_content=False, assert_same_host=False,
    )


def open_url(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Mapping[str, str]] = None,
    body: Optional[bytes] = None,
    max_bytes: int,
    total_timeout: float,
    max_redirects: int = 3,
    allowed_private_hosts: Iterable[str] = (),
) -> SafeResponse:
    """Open `url` under the policy. Raises FetchRefused, or urllib3 errors
    for network failures. The caller must read or close the response."""
    allowed = tuple(allowed_private_hosts)
    deadline = time.monotonic() + total_timeout
    headers = dict(headers or {})
    for _ in range(max_redirects + 1):
        scheme, host, port, ip, path = vet(url, allowed)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchRefused("the download took longer than allowed")
        timeout = urllib3.Timeout(connect=min(10.0, remaining), read=remaining)
        raw = _open_once(method, scheme, host, port, ip, path, headers, body, timeout)
        if raw.status in _REDIRECT_CODES and raw.headers.get("location"):
            location = raw.headers["location"]
            raw.release_conn()
            raw.close()
            url = urljoin(url, location)
            if raw.status == 303 or (raw.status in (301, 302) and method == "POST"):
                method, body = "GET", None
            continue
        return SafeResponse(status=raw.status, headers=raw.headers, url=url,
                            _raw=raw, _deadline=deadline, _max_bytes=max_bytes)
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
