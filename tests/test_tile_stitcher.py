"""Unit tests for src/poster/tile_stitcher.py (issue #14, Unit B).

Everything here runs offline: pure pixel-math is checked against
hand-verifiable reference values, and ``render_basemap`` is exercised with a
fake ``tile_fetcher`` that returns solid-colour PNG bytes instead of hitting
Mapbox. No test in this file requires MAPBOX_TOKEN or network access.
"""
from __future__ import annotations

import functools
import io
import math
import threading

import pytest
from PIL import Image

from src.exceptions.errors import APIError
from src.poster.tile_stitcher import (
    DEFAULT_MAX_ZOOM,
    DEFAULT_PIXEL_RATIO,
    DEFAULT_TILE_SIZE,
    MapboxTileClient,
    crop_rect_for_bounds,
    lonlat_to_pixel,
    render_basemap,
    tile_range_for_bounds,
    zoom_for_target_size,
)

# Standard Web Mercator latitude limit (where the projected square world map
# ends) — the well-known worked example: at zoom 0 the whole world is one tile.
_MAX_MERCATOR_LAT = 85.0511
_WORLD_BOUNDS = {"north": _MAX_MERCATOR_LAT, "south": -_MAX_MERCATOR_LAT, "east": 180.0, "west": -180.0}

# Small bbox reused across tests (matches tests/test_poster_api.py's fixture bounds).
_PARIS_BOUNDS = {"north": 48.9, "south": 48.8, "east": 2.4, "west": 2.3}


def _solid_png(color=(120, 140, 160), size=(256, 256)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# lonlat_to_pixel
# ---------------------------------------------------------------------------

def test_lonlat_to_pixel_nw_corner_is_origin():
    # abs tolerance is 1e-3 px (not 1e-6): _MAX_MERCATOR_LAT is a rounded
    # approximation of the true asymptotic Mercator latitude limit, so the
    # log/tan round-trip lands a fraction of a pixel off exact 0.
    x, y = lonlat_to_pixel(-180.0, _MAX_MERCATOR_LAT, zoom=0, tile_size=256)
    assert x == pytest.approx(0.0, abs=1e-6)
    assert y == pytest.approx(0.0, abs=1e-3)


def test_lonlat_to_pixel_se_corner_is_full_world():
    x, y = lonlat_to_pixel(180.0, -_MAX_MERCATOR_LAT, zoom=0, tile_size=256)
    assert x == pytest.approx(256.0, abs=1e-6)
    assert y == pytest.approx(256.0, abs=1e-3)


def test_lonlat_to_pixel_equator_prime_meridian_is_world_center():
    x, y = lonlat_to_pixel(0.0, 0.0, zoom=0, tile_size=256)
    assert x == pytest.approx(128.0, abs=1e-6)
    assert y == pytest.approx(128.0, abs=1e-6)


def test_lonlat_to_pixel_scales_with_zoom():
    """Doubling zoom doubles the world pixel size, so a fixed point's pixel
    coordinate (measured from the same NW origin) doubles too."""
    x0, y0 = lonlat_to_pixel(90.0, 45.0, zoom=1, tile_size=256)
    x1, y1 = lonlat_to_pixel(90.0, 45.0, zoom=2, tile_size=256)
    assert x1 == pytest.approx(x0 * 2, rel=1e-9)
    assert y1 == pytest.approx(y0 * 2, rel=1e-9)


# ---------------------------------------------------------------------------
# zoom_for_target_size
# ---------------------------------------------------------------------------

def test_zoom_for_target_size_whole_world_picks_zoom_zero():
    """Whole-globe bounds at a target just under one tile's native resolution
    should pick zoom 0 (the world fits in a single tile there).

    Target is intentionally 500 rather than the tile size (512): the Mercator
    latitude limit is asymptotic, so lonlat_to_pixel's floating-point result
    for the exact edge lands a hair under 512, not exactly at it.
    """
    z = zoom_for_target_size(_WORLD_BOUNDS, target_width=500, target_height=500, tile_size=512)
    assert z == 0


def test_zoom_for_target_size_never_upscales_below_target():
    """The chosen zoom's native resolution must meet or exceed the target in
    both dimensions (never pick a zoom that would need upscaling)."""
    target_w, target_h = 800, 600
    z = zoom_for_target_size(_PARIS_BOUNDS, target_w, target_h, tile_size=256, max_zoom=22)
    x0, y0 = lonlat_to_pixel(_PARIS_BOUNDS["west"], _PARIS_BOUNDS["north"], z, 256)
    x1, y1 = lonlat_to_pixel(_PARIS_BOUNDS["east"], _PARIS_BOUNDS["south"], z, 256)
    assert abs(x1 - x0) >= target_w
    assert abs(y1 - y0) >= target_h
    # One zoom level lower must NOT be sufficient (otherwise z isn't minimal).
    if z > 0:
        x0, y0 = lonlat_to_pixel(_PARIS_BOUNDS["west"], _PARIS_BOUNDS["north"], z - 1, 256)
        x1, y1 = lonlat_to_pixel(_PARIS_BOUNDS["east"], _PARIS_BOUNDS["south"], z - 1, 256)
        assert abs(x1 - x0) < target_w or abs(y1 - y0) < target_h


def test_zoom_for_target_size_respects_max_zoom_cap():
    """A tiny bbox against a huge target size can't reach the requested
    resolution at all — zoom must clamp to max_zoom rather than search forever
    or return something out of range."""
    tiny_bounds = {"north": 48.85001, "south": 48.85000, "east": 2.35001, "west": 2.35000}
    z = zoom_for_target_size(tiny_bounds, 9933, 14043, tile_size=512, max_zoom=22)
    assert z == 22


# ---------------------------------------------------------------------------
# tile_range_for_bounds / crop_rect_for_bounds
# ---------------------------------------------------------------------------

def test_tile_range_whole_world_at_zoom_zero_is_single_tile():
    x_min, x_max, y_min, y_max = tile_range_for_bounds(_WORLD_BOUNDS, zoom=0, tile_size=256)
    assert (x_min, x_max, y_min, y_max) == (0, 0, 0, 0)


def test_tile_range_matches_independently_computed_indices():
    """Cross-check against the textbook slippy-map formulas, computed here
    independently of the implementation under test."""
    zoom = 10
    tile_size = 256

    def expected_tile(lon, lat):
        n = 2 ** zoom
        x = int((lon + 180.0) / 360.0 * n)
        lat_rad = math.radians(lat)
        y = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)
        return x, y

    exp_x_min, exp_y_min = expected_tile(_PARIS_BOUNDS["west"], _PARIS_BOUNDS["north"])
    exp_x_max, exp_y_max = expected_tile(_PARIS_BOUNDS["east"], _PARIS_BOUNDS["south"])

    x_min, x_max, y_min, y_max = tile_range_for_bounds(_PARIS_BOUNDS, zoom, tile_size)
    assert (x_min, x_max, y_min, y_max) == (exp_x_min, exp_x_max, exp_y_min, exp_y_max)


def test_crop_rect_dimensions_match_bounds_pixel_span():
    zoom = 12
    tile_size = 256
    left, top, right, bottom = crop_rect_for_bounds(_PARIS_BOUNDS, zoom, tile_size)

    x0, y0 = lonlat_to_pixel(_PARIS_BOUNDS["west"], _PARIS_BOUNDS["north"], zoom, tile_size)
    x1, y1 = lonlat_to_pixel(_PARIS_BOUNDS["east"], _PARIS_BOUNDS["south"], zoom, tile_size)
    assert (right - left) == pytest.approx(x1 - x0, abs=1e-6)
    assert (bottom - top) == pytest.approx(y1 - y0, abs=1e-6)


def test_crop_rect_is_within_stitched_canvas_bounds():
    """The crop rect must fall inside the canvas assembled from the tile range
    at the same zoom (no negative offsets, nothing past the far edge)."""
    zoom = 12
    tile_size = 256
    x_min, x_max, y_min, y_max = tile_range_for_bounds(_PARIS_BOUNDS, zoom, tile_size)
    canvas_w = (x_max - x_min + 1) * tile_size
    canvas_h = (y_max - y_min + 1) * tile_size

    left, top, right, bottom = crop_rect_for_bounds(_PARIS_BOUNDS, zoom, tile_size)
    assert 0 <= left < right <= canvas_w
    assert 0 <= top < bottom <= canvas_h


# ---------------------------------------------------------------------------
# render_basemap (fake tile_fetcher — no network)
# ---------------------------------------------------------------------------

def test_render_basemap_returns_exact_target_dimensions():
    calls = []

    def fake_fetcher(z, x, y):
        calls.append((z, x, y))
        return _solid_png(size=(256, 256))

    img = render_basemap(_PARIS_BOUNDS, target_width=800, target_height=600,
                          tile_fetcher=fake_fetcher, tile_size=256)
    assert img.size == (800, 600)
    assert len(calls) > 0


def test_render_basemap_does_not_touch_network_when_fetcher_injected(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("should not hit the network when tile_fetcher is injected")

    monkeypatch.setattr("src.poster.tile_stitcher.requests.get", _boom)
    monkeypatch.setattr("src.poster.tile_stitcher.requests.Session", _boom)

    img = render_basemap(
        _PARIS_BOUNDS, target_width=400, target_height=300,
        tile_fetcher=lambda z, x, y: _solid_png(size=(256, 256)), tile_size=256,
    )
    assert img.size == (400, 300)


def test_render_basemap_fetches_every_tile_in_computed_range():
    from src.poster.tile_stitcher import tile_range_for_bounds, zoom_for_target_size

    zoom = zoom_for_target_size(_PARIS_BOUNDS, 800, 600, tile_size=256, max_zoom=22)
    x_min, x_max, y_min, y_max = tile_range_for_bounds(_PARIS_BOUNDS, zoom, 256)
    expected_tiles = {
        (zoom, x, y) for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1)
    }

    seen = set()

    def fake_fetcher(z, x, y):
        seen.add((z, x, y))
        return _solid_png(size=(256, 256))

    render_basemap(_PARIS_BOUNDS, target_width=800, target_height=600,
                    tile_fetcher=fake_fetcher, tile_size=256)
    assert seen == expected_tiles


# ---------------------------------------------------------------------------
# Tile-count sanity cap
# ---------------------------------------------------------------------------

def test_render_basemap_realistic_a0_request_stays_under_default_cap():
    """A real A0-at-300dpi request (~9933x14043) over a France-sized bbox
    should NOT hit the tile cap: zoom_for_target_size normalizes resolution to
    the *output* size, so tile count stays on the order of ~1-2k regardless of
    the bbox's real-world extent (verified independently while designing this
    cap: Paris-sized ~0.1deg bbox needs ~2072 tiles, France-sized ~10deg bbox
    needs ~1218 tiles, at the default 512px tile size)."""
    france_bounds = {"north": 51.0, "south": 41.0, "east": 8.0, "west": -2.0}

    def fake_fetcher(z, x, y):
        return _solid_png(size=(512, 512))

    img = render_basemap(france_bounds, target_width=9933, target_height=14043,
                          tile_fetcher=fake_fetcher)
    assert img.size == (9933, 14043)


def test_render_basemap_raises_on_oblong_bounds_exceeding_tile_cap():
    """An extreme bbox that is very wide in longitude but a sliver in latitude
    forces a high zoom (to satisfy the thin dimension's resolution
    requirement) that blows up the wide dimension's tile count. This is the
    scenario the cap exists to catch (e.g. malformed bounds)."""
    oblong_bounds = {"north": 0.001, "south": -0.001, "east": 170.0, "west": -170.0}

    def fake_fetcher(z, x, y):
        return _solid_png(size=(256, 256))

    with pytest.raises(ValueError, match="exceeding the cap"):
        render_basemap(oblong_bounds, target_width=1000, target_height=1000,
                        tile_fetcher=fake_fetcher, tile_size=256)


def test_render_basemap_never_allocates_a_canvas_bigger_than_the_output(monkeypatch):
    """Regression for a real production OOM (issue #14 follow-up): a
    Toulouse-to-Nordkapp trip's bounds needed 722 tiles at zoom 7 — well
    under DEFAULT_MAX_TILES, but the old algorithm pasted all 722 into one
    canvas at *native tile resolution* (~757M px, ~2.3GB RGB) before cropping
    it down to the ~35M px final target, and that intermediate is what
    OOM-killed the worker. Every Image.new(...) call must now be sized at or
    under the final output, never at the pre-crop tile-grid size."""
    real_bounds = {
        "north": 77.99885689576553, "south": 23.409510750792926,
        "east": 42.17284836188753, "west": -10.724279708594967,
    }
    target_w, target_h = 4967, 7022  # the real job's A0-portrait-at-150dpi size

    zoom = zoom_for_target_size(real_bounds, target_w, target_h, DEFAULT_TILE_SIZE, DEFAULT_MAX_ZOOM)
    x_min, x_max, y_min, y_max = tile_range_for_bounds(real_bounds, zoom, DEFAULT_TILE_SIZE)
    tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)
    assert tile_count > 700, (
        f"only {tile_count} tiles at zoom {zoom} — this bounds fixture no "
        "longer reproduces the real large-canvas scenario"
    )

    sizes_allocated = []
    real_new = Image.new

    def _tracking_new(mode, size, *a, **k):
        sizes_allocated.append(size)
        return real_new(mode, size, *a, **k)

    monkeypatch.setattr(Image, "new", _tracking_new)

    def fake_fetcher(z, x, y):
        return _solid_png(size=(DEFAULT_TILE_SIZE * DEFAULT_PIXEL_RATIO,) * 2)

    img = render_basemap(
        real_bounds, target_width=target_w, target_height=target_h,
        tile_fetcher=fake_fetcher, max_tiles=tile_count + 10,
    )

    assert img.size == (target_w, target_h)
    max_area_allocated = max(w * h for w, h in sizes_allocated)
    assert max_area_allocated <= target_w * target_h, (
        f"allocated a canvas up to {max_area_allocated} px "
        f"(output is only {target_w * target_h} px) — the pre-crop giant "
        "intermediate is back"
    )


def test_render_basemap_respects_custom_max_tiles():
    def fake_fetcher(z, x, y):
        return _solid_png(size=(256, 256))

    with pytest.raises(ValueError, match="exceeding the cap of 10"):
        render_basemap(_PARIS_BOUNDS, target_width=800, target_height=600,
                        tile_fetcher=fake_fetcher, tile_size=256, max_tiles=10)


def test_render_basemap_raises_timeout_once_deadline_elapses(monkeypatch):
    """A tile count under max_tiles doesn't bound *time* — each fetch is
    sequential, so a merely-slow (not failing) connection must still be cut
    off by a wall-clock deadline rather than running unbounded (issue #14:
    "preview failed with a timeout")."""
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "src.poster.tile_stitcher.time.monotonic", lambda: clock["t"]
    )

    def slow_fetcher(z, x, y):
        clock["t"] += 100.0  # every fetch "takes" longer than the budget
        return _solid_png(size=(256, 256))

    with pytest.raises(TimeoutError, match="time budget"):
        render_basemap(_PARIS_BOUNDS, target_width=800, target_height=600,
                        tile_fetcher=slow_fetcher, tile_size=256,
                        deadline=10.0)


def test_render_basemap_ignores_deadline_when_not_given():
    """Deadline is opt-in — the full-resolution render passes none, and must
    not be time-bounded regardless of how long fetching takes."""
    img = render_basemap(_PARIS_BOUNDS, target_width=800, target_height=600,
                          tile_fetcher=lambda z, x, y: _solid_png(size=(256, 256)),
                          tile_size=256, deadline=None)
    assert img.size == (800, 600)


# ---------------------------------------------------------------------------
# MapboxTileClient
# ---------------------------------------------------------------------------

def test_mapbox_tile_client_requires_token():
    with pytest.raises(APIError):
        MapboxTileClient("")


class _Resp:
    """A minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code=200, content=b"", text="", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = headers or {}


def _install_sessions(monkeypatch, get):
    """Make every ``requests.Session`` the client creates route ``get`` to
    *get*. Returns the list of sessions created, in creation order."""
    sessions = []

    class _FakeSession:
        def __init__(self):
            self.threads = []
            sessions.append(self)

        def get(self, url, **kwargs):
            self.threads.append(threading.get_ident())
            return get(url, **kwargs)

    monkeypatch.setattr("src.poster.tile_stitcher.requests.Session", _FakeSession)
    return sessions


class _Clock:
    """Fake wall clock, monotonic clock and sleep: sleeping advances both."""

    def __init__(self, wall=1_700_000_000.0):
        self.wall = wall
        self.mono = 1000.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.wall += seconds
        self.mono += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr("src.poster.tile_stitcher.time.time", lambda: c.wall)
    monkeypatch.setattr("src.poster.tile_stitcher.time.monotonic", lambda: c.mono)
    monkeypatch.setattr("src.poster.tile_stitcher.time.sleep", c.sleep)
    return c


def _responses(*resps):
    """A fake ``get`` serving *resps* in order, recording each call's kwargs."""
    queue = list(resps)
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return queue.pop(0)

    get.calls = calls
    return get


def test_mapbox_tile_client_fetch_tile_success(monkeypatch):
    _install_sessions(monkeypatch, lambda *a, **k: _Resp(content=b"fake-png-bytes"))
    client = MapboxTileClient("tok", tile_size=256)
    assert client.fetch_tile(5, 10, 12) == b"fake-png-bytes"


def test_mapbox_tile_client_retries_then_raises_on_persistent_5xx(monkeypatch):
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Resp(503, text="unavailable")

    _install_sessions(monkeypatch, fake_get)
    monkeypatch.setattr("src.poster.tile_stitcher.time.sleep", lambda s: None)
    client = MapboxTileClient("tok")
    with pytest.raises(APIError, match="after 3 attempts"):
        client.fetch_tile(1, 0, 0)
    assert calls["n"] == client.MAX_RETRIES


def test_mapbox_tile_client_raises_immediately_on_4xx(monkeypatch):
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Resp(401, text="invalid token")

    _install_sessions(monkeypatch, fake_get)
    client = MapboxTileClient("tok")
    with pytest.raises(APIError, match="401"):
        client.fetch_tile(1, 0, 0)
    assert calls["n"] == 1


def test_mapbox_tile_client_404_raises_at_once_without_sleeping(monkeypatch, clock):
    get = _responses(_Resp(404, text="not found"))
    _install_sessions(monkeypatch, get)
    with pytest.raises(APIError, match="404"):
        MapboxTileClient("tok").fetch_tile(1, 0, 0)
    assert len(get.calls) == 1
    assert clock.sleeps == []


# ---------------------------------------------------------------------------
# MapboxTileClient: connection reuse (#517)
# ---------------------------------------------------------------------------

def test_mapbox_tile_client_reuses_one_session_per_thread(monkeypatch):
    """Tiles reuse keep-alive connections through one Session per thread;
    a second thread gets its own, since Session isn't documented as
    thread-safe."""
    sessions = _install_sessions(monkeypatch, lambda *a, **k: _Resp(content=b"t"))
    client = MapboxTileClient("tok")

    client.fetch_tile(1, 0, 0)
    client.fetch_tile(1, 1, 0)
    assert len(sessions) == 1

    worker = threading.Thread(target=lambda: client.fetch_tile(1, 0, 1))
    worker.start()
    worker.join()
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert sessions[0].threads == [threading.get_ident()] * 2
    assert len(sessions[1].threads) == 1
    assert sessions[1].threads[0] != threading.get_ident()

    client.fetch_tile(1, 1, 1)  # back on the first thread: its session again
    assert len(sessions) == 2
    assert len(sessions[0].threads) == 3


# ---------------------------------------------------------------------------
# MapboxTileClient: 429 retry and deadline (#517)
# ---------------------------------------------------------------------------

def test_429_waits_until_rate_limit_reset_plus_one_second(monkeypatch, clock):
    get = _responses(
        _Resp(429, headers={"X-Rate-Limit-Reset": str(int(clock.wall) + 10)}),
        _Resp(content=b"tile"),
    )
    _install_sessions(monkeypatch, get)
    assert MapboxTileClient("tok").fetch_tile(1, 0, 0) == b"tile"
    assert clock.sleeps == [11]


def test_429_wait_is_clamped_to_30_seconds(monkeypatch, clock):
    get = _responses(
        _Resp(429, headers={"X-Rate-Limit-Reset": str(clock.wall + 120)}),
        _Resp(content=b"tile"),
    )
    _install_sessions(monkeypatch, get)
    assert MapboxTileClient("tok").fetch_tile(1, 0, 0) == b"tile"
    assert clock.sleeps == [30]


def test_429_survives_a_window_used_up_at_its_start(monkeypatch, clock):
    """The account's per-minute budget used up by others: every request
    429s with the reset 60 s ahead until it passes. The tile must still
    arrive, after a total sleep within the 65 s budget (review R2-3)."""
    reset = clock.wall + 60

    def get(url, **kwargs):
        if clock.wall < reset:
            return _Resp(429, headers={"X-Rate-Limit-Reset": str(reset)})
        return _Resp(content=b"tile")

    _install_sessions(monkeypatch, get)
    assert MapboxTileClient("tok").fetch_tile(1, 0, 0) == b"tile"
    assert sum(clock.sleeps) <= MapboxTileClient.MAX_RATE_LIMIT_WAIT_S


def test_429_retry_after_is_used_without_a_reset_header(monkeypatch, clock):
    get = _responses(_Resp(429, headers={"Retry-After": "5"}), _Resp(content=b"tile"))
    _install_sessions(monkeypatch, get)
    assert MapboxTileClient("tok").fetch_tile(1, 0, 0) == b"tile"
    assert clock.sleeps == [5]


@pytest.mark.parametrize("make_headers", [
    lambda wall: {"X-Rate-Limit-Reset": "soon"},
    lambda wall: {"X-Rate-Limit-Reset": "nan"},
    lambda wall: {"X-Rate-Limit-Reset": "inf"},
    lambda wall: {"X-Rate-Limit-Reset": ""},
    lambda wall: {"X-Rate-Limit-Reset": str(wall - 5)},  # already past
    lambda wall: {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
    lambda wall: {"Retry-After": "-3"},
], ids=["word", "nan", "inf", "empty", "past-reset", "http-date", "negative"])
def test_429_malformed_or_past_headers_fall_back_to_backoff(monkeypatch, clock, make_headers):
    get = _responses(_Resp(429, headers=make_headers(clock.wall)), _Resp(content=b"tile"))
    _install_sessions(monkeypatch, get)
    assert MapboxTileClient("tok").fetch_tile(1, 0, 0) == b"tile"
    assert clock.sleeps == [1]  # 2 ** 0


@pytest.mark.parametrize("headers", [
    {},
    {"X-Rate-Limit-Reset": "soon", "Retry-After": "later"},
])
def test_persistent_429_raises_api_error_once_the_budget_is_spent(monkeypatch, clock, headers):
    _install_sessions(monkeypatch, lambda *a, **k: _Resp(429, headers=headers))
    with pytest.raises(APIError, match="429"):
        MapboxTileClient("tok").fetch_tile(1, 0, 0)
    assert clock.sleeps == [1, 2, 4, 8, 16, 30]
    assert sum(clock.sleeps) <= MapboxTileClient.MAX_RATE_LIMIT_WAIT_S


def test_persistent_429_does_not_use_up_the_5xx_retries(monkeypatch, clock):
    """429s have their own budget: a 5xx after them still gets MAX_RETRIES."""
    get = _responses(
        *[_Resp(429) for _ in range(4)],
        _Resp(503), _Resp(503), _Resp(content=b"tile"),
    )
    _install_sessions(monkeypatch, get)
    assert MapboxTileClient("tok").fetch_tile(1, 0, 0) == b"tile"
    assert clock.sleeps == [1, 2, 4, 8, 1, 2]


def test_429_wait_past_the_deadline_raises_without_sleeping(monkeypatch, clock):
    get = _responses(_Resp(429, headers={"X-Rate-Limit-Reset": str(clock.wall + 60)}))
    _install_sessions(monkeypatch, get)
    client = MapboxTileClient("tok", deadline=clock.mono + 10)
    with pytest.raises(APIError, match="deadline"):
        client.fetch_tile(1, 0, 0)
    assert clock.sleeps == []
    assert len(get.calls) == 1


def test_5xx_backoff_past_the_deadline_raises_without_sleeping(monkeypatch, clock):
    get = _responses(_Resp(503))
    _install_sessions(monkeypatch, get)
    client = MapboxTileClient("tok", deadline=clock.mono + 1.2)
    with pytest.raises(APIError, match="deadline"):
        client.fetch_tile(1, 0, 0)
    assert clock.sleeps == []


@pytest.mark.parametrize("left", [0.2, 0.0, -5.0])
def test_no_request_is_sent_when_the_deadline_leaves_too_little(monkeypatch, clock, left):
    """Under MIN_REQUEST_S left (or already past), the client raises without
    sending, so requests never gets a timeout <= 0 (review R3-2)."""
    get = _responses()
    sessions = _install_sessions(monkeypatch, get)
    client = MapboxTileClient("tok", deadline=clock.mono + left)
    with pytest.raises(APIError, match="deadline"):
        client.fetch_tile(1, 0, 0)
    assert get.calls == []
    assert all(s.threads == [] for s in sessions)


def test_request_timeout_is_cut_to_the_time_left(monkeypatch, clock):
    get = _responses(_Resp(content=b"a"), _Resp(content=b"b"))
    _install_sessions(monkeypatch, get)
    MapboxTileClient("tok", timeout=15.0, deadline=clock.mono + 5).fetch_tile(1, 0, 0)
    MapboxTileClient("tok", timeout=15.0).fetch_tile(1, 0, 0)
    assert get.calls[0]["timeout"] == pytest.approx(5.0)
    assert get.calls[1]["timeout"] == 15.0


def test_render_basemap_hands_its_deadline_to_the_default_client(monkeypatch):
    """With no injected fetcher, render_basemap's deadline reaches the client,
    so a 429 wait can't run the poster preview past its budget (review R2-1)."""
    import src.poster.tile_stitcher as tile_stitcher

    built = []
    real_client = tile_stitcher.MapboxTileClient

    def spy_client(*args, **kwargs):
        client = real_client(*args, **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(tile_stitcher, "_mapbox_token", lambda: "tok")
    monkeypatch.setattr(tile_stitcher, "MapboxTileClient", spy_client)
    _install_sessions(monkeypatch, lambda *a, **k: _Resp(content=_solid_png(size=(256, 256))))

    deadline = tile_stitcher.time.monotonic() + 600
    render_basemap(_PARIS_BOUNDS, target_width=400, target_height=300,
                   tile_size=256, deadline=deadline)
    assert [c.deadline for c in built] == [deadline]


# ---------------------------------------------------------------------------
# Retina (@2x) tiles: pixel_ratio vs logical tile_size
#
# Mapbox returns `tile_size * pixel_ratio` real pixels per tile — a @2x request
# at tilesize=512 yields a 1024x1024 image. Stitching those on a `tile_size`
# stride silently overwrites three quarters of every tile, producing a
# scrambled, 2x-zoomed basemap that no longer lines up with the projected route
# and pins. These tests pin the separation of the two concepts.
# ---------------------------------------------------------------------------

def _tile_id_blue(x: int, y: int) -> int:
    """A per-tile identifying value carried in the blue channel."""
    return 40 + (x * 53 + y * 97) % 200


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@functools.lru_cache(maxsize=None)
def _ramp_png(blue: int, px: int) -> bytes:
    """A tile with *internal structure*: red and green ramp from 0 at the
    tile's edges up to 255 at its centre, with ``blue`` identifying the tile.

    Internal structure is essential. A tile painted one flat colour cannot
    detect the stride bug at all: pasting `tile_size * pixel_ratio` px tiles on
    a `tile_size` stride still leaves the correct tile covering each slot — it
    just draws that tile's top-left quadrant blown up 2x. Flat colours look
    identical either way, so the ramps are what make the corruption visible.

    The ramp is a triangle wave rather than a sawtooth so it is continuous
    across tile seams: the only difference between a 1x and a 2x render is
    then resampling, not ringing at a hard edge.
    """
    ramp = bytes(int(255 * (2 * u if u < 0.5 else 2 * (1 - u)))
                 for u in (i / px for i in range(px)))
    red = Image.frombytes("L", (px, px), ramp * px)
    green = Image.frombytes("L", (px, px), b"".join(bytes([v]) * px for v in ramp))
    return _png_bytes(Image.merge("RGB", (red, green, Image.new("L", (px, px), blue))))


def _ramp_fetcher(tile_size: int, pixel_ratio: int):
    px = tile_size * pixel_ratio
    return lambda z, x, y: _ramp_png(_tile_id_blue(x, y), px)


def _output_xy_for_world_point(bounds, zoom, tile_size, wx, wy, target_w, target_h):
    """Map a logical world-pixel point to output-image pixel coords, mirroring
    render_basemap's crop + resize (and poster_renderer._Projector)."""
    x_min, _, y_min, _ = tile_range_for_bounds(bounds, zoom, tile_size)
    left, top, right, bottom = crop_rect_for_bounds(bounds, zoom, tile_size)
    abs_left = x_min * tile_size + left
    abs_top = y_min * tile_size + top
    ox = (wx - abs_left) * target_w / (right - left)
    oy = (wy - abs_top) * target_h / (bottom - top)
    return ox, oy


def test_render_basemap_places_each_retina_tile_at_its_geographic_position():
    """Each tile's *centre* must land at that geographic point's output
    position, showing the tile's own centre (mid-ramp, ~127/127).

    Pasting `tile_size * 2` px tiles on a `tile_size` stride draws each tile's
    top-left quadrant stretched 2x, so a tile's centre pixel would instead show
    that quadrant's own mid-ramp (~127) rather than the tile centre's peak
    (255) — which this asserts against.
    """
    tile_size = 256
    target_w, target_h = 700, 700
    zoom = zoom_for_target_size(_PARIS_BOUNDS, target_w, target_h, tile_size, 22)
    x_min, x_max, y_min, y_max = tile_range_for_bounds(_PARIS_BOUNDS, zoom, tile_size)
    assert (x_max - x_min + 1) * (y_max - y_min + 1) >= 4, "need a multi-tile range"

    img = render_basemap(
        _PARIS_BOUNDS, target_width=target_w, target_height=target_h,
        tile_fetcher=_ramp_fetcher(tile_size, 2), tile_size=tile_size, pixel_ratio=2,
    )
    pixels = img.convert("RGB").load()

    checked = 0
    for tx in range(x_min, x_max + 1):
        for ty in range(y_min, y_max + 1):
            wx = (tx + 0.5) * tile_size  # tile centre, in logical world pixels
            wy = (ty + 0.5) * tile_size
            ox, oy = _output_xy_for_world_point(
                _PARIS_BOUNDS, zoom, tile_size, wx, wy, target_w, target_h)
            if not (4 <= ox < target_w - 4 and 4 <= oy < target_h - 4):
                continue
            r, g, b = pixels[int(ox), int(oy)]
            assert b == _tile_id_blue(tx, ty), (
                f"wrong tile at ({int(ox)},{int(oy)}): expected tile ({tx},{ty})")
            assert r >= 245 and g >= 245, (
                f"tile ({tx},{ty}) centre shows ramp ({r},{g}), expected ~(255,255) "
                "- tile content is scaled wrong")
            checked += 1
    assert checked >= 2, "test asserted on too few tiles to be meaningful"


def test_render_basemap_output_identical_for_1x_and_2x_tiles():
    """The same map data at two pixel densities must render the same picture."""
    tile_size = 256
    kwargs = dict(bounds=_PARIS_BOUNDS, target_width=300, target_height=240,
                  tile_size=tile_size)
    one_x = render_basemap(**kwargs, pixel_ratio=1,
                           tile_fetcher=_ramp_fetcher(tile_size, 1))
    two_x = render_basemap(**kwargs, pixel_ratio=2,
                           tile_fetcher=_ramp_fetcher(tile_size, 2))

    a, b = one_x.convert("RGB").load(), two_x.convert("RGB").load()
    # Red/green only: the blue tile-id channel steps at every seam, and that
    # discontinuity resamples differently from a 512px than from a 256px
    # source. The ramps carry the signal that matters here.
    worst = max(
        max(abs(a[i, j][c] - b[i, j][c]) for c in (0, 1))
        for j in range(0, 240, 3) for i in range(0, 300, 3)
    )
    # A stride bug halves the ramp's slope, a >100/255 divergence.
    assert worst <= 12, f"1x and 2x renders disagree by {worst}/255"


def test_fetch_tile_requests_retina_suffix_only_for_pixel_ratio_2(monkeypatch):
    urls = []

    def fake_get(url, **k):
        urls.append(url)
        return _Resp(content=b"x")

    _install_sessions(monkeypatch, fake_get)

    MapboxTileClient("tok", tile_size=512, pixel_ratio=2).fetch_tile(3, 4, 5)
    MapboxTileClient("tok", tile_size=512, pixel_ratio=1).fetch_tile(3, 4, 5)

    assert urls[0].endswith("/512/3/4/5@2x")
    assert urls[1].endswith("/512/3/4/5")
