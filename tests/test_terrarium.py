"""The terrarium tile reader (issue #386, unit 2).

No network: every tile here is synthesised in memory with known values, so each
test can say exactly what the right answer is. What these pin is not whether
the tileset is accurate — that is a measurement, recorded with the gate it
feeds — but that the reader returns the tileset's own value, in the right place,
the same way every time.
"""
from __future__ import annotations

import io
import math
import sys

import pytest
from PIL import Image

from tests.elevation_bench.tile_reader import (
    MISSING,
    TILE_SIZE,
    TerrariumReader,
    TransientTileError,
    decode_terrarium,
    lonlat_to_pixel,
    sample_along,
)


# ── Synthetic tiles ──────────────────────────────────────────────────────────
def encode(elevation: float):
    """The terrarium RGB for an elevation — the inverse of decode_terrarium."""
    v = elevation + 32768.0
    r = int(v // 256)
    g = int(v - r * 256)
    b = int(round((v - r * 256 - g) * 256))
    if b == 256:
        g, b = g + 1, 0
    return r, g, b


def tile_png(value_at) -> bytes:
    """A 256x256 terrarium PNG whose pixel (px, py) decodes to value_at(px, py)."""
    image = Image.new("RGB", (TILE_SIZE, TILE_SIZE))
    image.putdata([encode(value_at(px, py))
                   for py in range(TILE_SIZE) for px in range(TILE_SIZE)])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeFetch:
    """Serves synthetic tiles and records every request."""

    def __init__(self, tiles=None, missing=(), transient=()):
        self.tiles = dict(tiles or {})
        self.missing = set(missing)
        self.transient = set(transient)
        self.calls = []

    def __call__(self, z, x, y):
        self.calls.append((z, x, y))
        if (z, x, y) in self.transient:
            raise TransientTileError(f"{z}/{x}/{y} is having a moment")
        if (z, x, y) in self.missing:
            return MISSING
        return self.tiles.get((z, x, y), MISSING)


def lon_at_global_px(gx: float, zoom: int) -> float:
    return gx / (TILE_SIZE * (1 << zoom)) * 360.0 - 180.0


def lat_at_global_py(gy: float, zoom: int) -> float:
    n = math.pi - 2.0 * math.pi * gy / (TILE_SIZE * (1 << zoom))
    return math.degrees(math.atan(math.sinh(n)))


# ── Decoding and projection ──────────────────────────────────────────────────
def test_decode_matches_the_terrarium_formula():
    assert decode_terrarium(128, 0, 0) == 0.0
    assert decode_terrarium(128, 100, 128) == pytest.approx(100.5)
    assert decode_terrarium(127, 255, 0) == -1.0
    for elevation in (-411.0, 0.0, 37.25, 8848.75):
        assert decode_terrarium(*encode(elevation)) == pytest.approx(
            elevation, abs=1 / 256)


def test_projection_places_the_world_on_the_expected_pixels():
    assert lonlat_to_pixel(0.0, 0.0, 0) == pytest.approx((128.0, 128.0))
    x, y = lonlat_to_pixel(-180.0, 85.05112878, 0)
    assert x == pytest.approx(0.0)
    assert y == pytest.approx(0.0, abs=1e-6)
    # One zoom deeper doubles every coordinate.
    x1, y1 = lonlat_to_pixel(13.405, 52.52, 13)
    x2, y2 = lonlat_to_pixel(13.405, 52.52, 14)
    assert (x2, y2) == pytest.approx((2 * x1, 2 * y1))


def test_latitudes_beyond_mercator_are_clamped_not_nan():
    x, y = lonlat_to_pixel(0.0, 89.9, 2)
    assert math.isfinite(y)
    assert y == pytest.approx(lonlat_to_pixel(0.0, 85.05112878, 2)[1])


# ── The half pixel ───────────────────────────────────────────────────────────
def test_a_pixel_s_value_sits_at_its_centre_not_its_corner():
    """Half a pixel is about 6 m at zoom 13 and 52 degrees north.

    Tile whose pixel value is its own column number. At global x = i + 0.5 — the
    CENTRE of pixel i — the answer must be exactly i. A reader interpolating
    between pixel corners returns i + 0.5 there instead, shifting every sample
    by half a pixel: a bias that is invisible until it is compared against
    anything else.
    """
    zoom = 0
    fetch = FakeFetch({(0, 0, 0): tile_png(lambda px, py: float(px))})
    reader = TerrariumReader(zoom=zoom, fetch=fetch)
    lat = lat_at_global_py(100.5, zoom)

    for column in (40, 41, 200):
        centre = reader.elevation(lat, lon_at_global_px(column + 0.5, zoom))
        corner = reader.elevation(lat, lon_at_global_px(float(column), zoom))
        assert centre == pytest.approx(column, abs=1e-3)
        assert corner == pytest.approx(column - 0.5, abs=1e-3)


def test_interpolation_is_bilinear_between_the_four_centres():
    zoom = 0
    fetch = FakeFetch({(0, 0, 0): tile_png(lambda px, py: 10.0 * px + py)})
    reader = TerrariumReader(zoom=zoom, fetch=fetch)

    # A quarter of the way from centre (50, 60) toward (51, 61) on each axis.
    got = reader.elevation(lat_at_global_py(60.5 + 0.25, zoom),
                           lon_at_global_px(50.5 + 0.25, zoom))

    assert got == pytest.approx(10.0 * 50.25 + 60.25, abs=0.02)


# ── Tile edges ───────────────────────────────────────────────────────────────
def test_a_read_straddling_two_tiles_is_continuous():
    """The four samples of one bilinear read can live in two tiles.

    Two tiles holding one continuous eastward ramp. Reading just either side of
    their shared edge must give the ramp's own values, not a clamp at the edge
    of one tile and not an index past it.
    """
    zoom = 1
    fetch = FakeFetch({
        (1, 0, 0): tile_png(lambda px, py: float(px)),
        (1, 1, 0): tile_png(lambda px, py: float(TILE_SIZE + px)),
    })
    reader = TerrariumReader(zoom=zoom, fetch=fetch)
    lat = lat_at_global_py(100.5, zoom)

    for gx in (TILE_SIZE - 0.75, TILE_SIZE - 0.5, TILE_SIZE, TILE_SIZE + 0.25):
        got = reader.elevation(lat, lon_at_global_px(gx, zoom))
        assert got == pytest.approx(gx - 0.5, abs=1e-3), (
            f"global x {gx}: expected the ramp's {gx - 0.5}, got {got}")


def test_longitude_wraps_across_the_antimeridian():
    zoom = 1
    fetch = FakeFetch({
        (1, 0, 0): tile_png(lambda px, py: 5.0),
        (1, 1, 0): tile_png(lambda px, py: 5.0),
    })
    reader = TerrariumReader(zoom=zoom, fetch=fetch)

    assert reader.elevation(lat_at_global_py(100.5, zoom), 179.99) == (
        pytest.approx(5.0))


# ── No data, and not-now ─────────────────────────────────────────────────────
def test_a_missing_corner_gives_none_not_a_three_corner_guess():
    """Ocean, or the edge of the tileset. An interpolation that silently leans
    on three corners — or on a zero for the fourth — is a wrong number that
    looks like a right one."""
    zoom = 1
    fetch = FakeFetch({(1, 0, 0): tile_png(lambda px, py: 100.0)},
                      missing={(1, 1, 0)})
    reader = TerrariumReader(zoom=zoom, fetch=fetch)
    lat = lat_at_global_py(100.5, zoom)

    assert reader.elevation(lat, lon_at_global_px(100.5, zoom)) == (
        pytest.approx(100.0))
    assert reader.elevation(lat, lon_at_global_px(TILE_SIZE - 0.25, zoom)) is None


def test_an_absent_tile_is_remembered_so_it_is_not_asked_for_again():
    fetch = FakeFetch(missing={(0, 0, 0)})
    reader = TerrariumReader(zoom=0, fetch=fetch)

    assert reader.elevation(10.0, 10.0) is None
    assert reader.elevation(11.0, 11.0) is None
    assert fetch.calls == [(0, 0, 0)]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_transient_failure_is_never_remembered_as_absent(tmp_path):
    """Remembering a 5xx as "no tile" blinds the reader to that tile for good
    because the bucket hiccupped once. Neither disk nor the retry window may
    keep it."""
    clock = Clock()
    fetch = FakeFetch({(0, 0, 0): tile_png(lambda px, py: 42.0)},
                      transient={(0, 0, 0)})
    reader = TerrariumReader(str(tmp_path), zoom=0, fetch=fetch,
                             retry_after_s=30.0, clock=clock)

    assert reader.elevation(10.0, 10.0) is None
    assert not any(tmp_path.rglob("*.png")), "a failure was written to disk"

    fetch.transient.clear()                 # the bucket recovers
    clock.now += 31.0                       # ...and the window has passed
    assert reader.elevation(10.0, 10.0) == pytest.approx(42.0)
    assert len(fetch.calls) == 2, "the tile was not asked for again"


def test_a_failing_tile_is_fetched_once_per_read_not_once_per_corner():
    """One bilinear read touches four pixels, here all in one tile. Without a
    short back-off the failing tile was re-fetched for each corner — each with
    its own backed-off retries — so a 4000-point track during a bucket outage
    would have spent hours asleep before returning "no data"."""
    clock = Clock()
    fetch = FakeFetch(transient={(0, 0, 0)})
    reader = TerrariumReader(zoom=0, fetch=fetch, retry_after_s=30.0,
                             clock=clock)

    for lat, lon in [(10.0, 10.0), (10.1, 10.1), (10.2, 10.2)]:
        assert reader.elevation(lat, lon) is None

    assert len(fetch.calls) == 1, (
        f"{len(fetch.calls)} fetches for three points in one failing tile")


# ── The disk cache ───────────────────────────────────────────────────────────
def test_a_second_reader_serves_from_disk_without_the_network(tmp_path):
    first = FakeFetch({(0, 0, 0): tile_png(lambda px, py: 7.0)})
    TerrariumReader(str(tmp_path), zoom=0, fetch=first).elevation(1.0, 1.0)

    offline = FakeFetch()                   # would answer "missing" for anything
    second = TerrariumReader(str(tmp_path), zoom=0, fetch=offline)

    assert second.elevation(1.0, 1.0) == pytest.approx(7.0)
    assert offline.calls == []


def test_absence_is_remembered_on_disk_too(tmp_path):
    first = FakeFetch(missing={(0, 0, 0)})
    TerrariumReader(str(tmp_path), zoom=0, fetch=first).elevation(1.0, 1.0)

    later = FakeFetch({(0, 0, 0): tile_png(lambda px, py: 7.0)})
    assert TerrariumReader(
        str(tmp_path), zoom=0, fetch=later).elevation(1.0, 1.0) is None
    assert later.calls == []


def test_a_write_does_not_trip_over_another_process_writing_the_same_tile(tmp_path):
    # Two measurement processes once fetched the same tile, wrote the same fixed
    # "{tile}.part" and renamed it; one crashed. A leftover temporary under the
    # old fixed name — here a directory, so it cannot be overwritten or renamed —
    # must not stop a write.
    tile_dir = tmp_path / "terrarium" / "0" / "0"
    tile_dir.mkdir(parents=True)
    (tile_dir / "0.png.part").mkdir()
    fetch = FakeFetch({(0, 0, 0): tile_png(lambda px, py: 7.0)})

    assert TerrariumReader(str(tmp_path), zoom=0, fetch=fetch).elevation(
        1.0, 1.0) == pytest.approx(7.0)
    assert (tile_dir / "0.png").exists()
    assert sorted(p.name for p in tile_dir.iterdir()) == ["0.png", "0.png.part"]


def test_a_rename_refused_because_another_process_holds_the_tile_is_not_an_error(
        tmp_path, monkeypatch):
    # On Windows, os.replace onto a file another process has open for reading
    # raises PermissionError. That process wrote the same tile, so the write is
    # already done: the temporary is dropped and the read carries on.
    from tests.elevation_bench import tile_reader

    def held_open(src, dst):
        with open(dst, "wb") as handle:
            with open(src, "rb") as tmp:
                handle.write(tmp.read())
        raise PermissionError(5, "Access is denied", dst)

    monkeypatch.setattr(tile_reader.os, "replace", held_open)
    fetch = FakeFetch({(0, 0, 0): tile_png(lambda px, py: 7.0)})

    assert TerrariumReader(str(tmp_path), zoom=0, fetch=fetch).elevation(
        1.0, 1.0) == pytest.approx(7.0)
    tile_dir = tmp_path / "terrarium" / "0" / "0"
    assert sorted(p.name for p in tile_dir.iterdir()) == ["0.png"]


def test_a_refused_rename_with_no_tile_on_disk_still_raises(tmp_path, monkeypatch):
    from tests.elevation_bench import tile_reader

    def refused(src, dst):
        raise PermissionError(5, "Access is denied", dst)

    monkeypatch.setattr(tile_reader.os, "replace", refused)
    fetch = FakeFetch({(0, 0, 0): tile_png(lambda px, py: 7.0)})

    with pytest.raises(PermissionError):
        TerrariumReader(str(tmp_path), zoom=0, fetch=fetch).elevation(1.0, 1.0)
    tile_dir = tmp_path / "terrarium" / "0" / "0"
    assert list(tile_dir.iterdir()) == []


def test_a_temporary_a_scanner_holds_open_does_not_turn_a_done_write_into_an_error(
        tmp_path, monkeypatch):
    # The rename was refused because another process wrote the tile, AND the
    # temporary cannot be removed because something (an antivirus scanner) has it
    # open. The tile is on disk, so the read carries on; the removal's own error
    # must not escape in place of that outcome.
    from tests.elevation_bench import tile_reader

    def held_open(src, dst):
        with open(dst, "wb") as handle:
            with open(src, "rb") as tmp:
                handle.write(tmp.read())
        raise PermissionError(5, "Access is denied", dst)

    def scanned(path):
        raise PermissionError(32, "being used by another process", path)

    monkeypatch.setattr(tile_reader.os, "replace", held_open)
    monkeypatch.setattr(tile_reader.os, "remove", scanned)
    fetch = FakeFetch({(0, 0, 0): tile_png(lambda px, py: 7.0)})

    assert TerrariumReader(str(tmp_path), zoom=0, fetch=fetch).elevation(
        1.0, 1.0) == pytest.approx(7.0)


@pytest.mark.skipif(sys.platform != "win32",
                    reason="only Windows refuses a rename onto an open file")
def test_a_real_open_handle_on_the_tile_is_tolerated(tmp_path, monkeypatch):
    # No monkeypatched os calls: another reader really holds the tile open while
    # this one, which checked the disk before that tile was written, writes it.
    from tests.elevation_bench import tile_reader

    png = tile_png(lambda px, py: 7.0)
    tile_dir = tmp_path / "terrarium" / "0" / "0"
    tile_dir.mkdir(parents=True)
    (tile_dir / "0.png").write_bytes(png)
    monkeypatch.setattr(tile_reader.TerrariumReader, "_read_disk",
                        lambda self, z, x, y: None)
    fetch = FakeFetch({(0, 0, 0): png})

    with open(tile_dir / "0.png", "rb"):
        assert TerrariumReader(str(tmp_path), zoom=0, fetch=fetch).elevation(
            1.0, 1.0) == pytest.approx(7.0)
    assert sorted(p.name for p in tile_dir.iterdir()) == ["0.png"]


def test_the_memory_cache_is_bounded():
    fetch = FakeFetch({(2, x, y): tile_png(lambda px, py: 1.0)
                       for x in range(4) for y in range(4)})
    reader = TerrariumReader(zoom=2, fetch=fetch, memory_tiles=3)

    for tx in range(4):
        reader.elevation(lat_at_global_py(TILE_SIZE * 1.5, 2),
                         lon_at_global_px(TILE_SIZE * tx + 128.5, 2))

    assert len(reader._memory) <= 3


# ── Along a track ────────────────────────────────────────────────────────────
def test_sampling_a_track_is_all_or_nothing():
    """terrain_corrected_gain refuses a terrain series whose length does not
    match the recording, so a hole must mean "no terrain" — which falls back to
    the recording — rather than a patched-in value."""
    zoom = 1
    fetch = FakeFetch({(1, 0, 0): tile_png(lambda px, py: 50.0)},
                      missing={(1, 1, 0)})
    reader = TerrariumReader(zoom=zoom, fetch=fetch)
    lat = lat_at_global_py(100.5, zoom)
    inside = [(lat, lon_at_global_px(g, zoom)) for g in (50.5, 60.5, 70.5)]
    crossing = inside + [(lat, lon_at_global_px(TILE_SIZE + 40.5, zoom))]

    assert sample_along(reader, inside) == pytest.approx([50.0, 50.0, 50.0])
    assert sample_along(reader, crossing) is None


def test_unreadable_coordinates_are_no_data():
    reader = TerrariumReader(zoom=0, fetch=FakeFetch())
    assert reader.elevation(float("nan"), 0.0) is None
    assert reader.elevation(0.0, float("inf")) is None


def test_zoom_outside_the_tileset_is_refused():
    with pytest.raises(ValueError):
        TerrariumReader(zoom=16, fetch=FakeFetch())


# ── Statuses that are not absence ────────────────────────────────────────────
class FakeResponse:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
def test_a_rate_limit_or_timeout_is_never_stored_as_absence(
        status, tmp_path, monkeypatch):
    """A 429 is a 4xx, and says nothing about whether the tile exists. An
    earlier version wrote any non-5xx refusal to disk as "no such tile", so one
    rate limit blinded the reader to that tile for good — the failure this
    module's docstring says it prevents."""
    import tests.elevation_bench.tile_reader as tr

    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    monkeypatch.setattr(tr.requests, "get",
                        lambda url, timeout: FakeResponse(status))
    with pytest.raises(tr.TransientTileError):
        tr.fetch_terrarium_tile(13, 4400, 2688)

    reader = TerrariumReader(str(tmp_path), zoom=0,
                             fetch=tr.fetch_terrarium_tile, retry_after_s=0.0)
    assert reader.elevation(1.0, 1.0) is None
    assert not any(tmp_path.rglob("*.png")), (
        f"HTTP {status} was persisted as a missing tile")


@pytest.mark.parametrize("status", [400, 401, 405])
def test_a_malformed_request_is_loud_not_missing(status, tmp_path, monkeypatch):
    """A 400 or 401 means the request is wrong. Swallowed into "no data", a
    misconfigured URL would look exactly like a world without terrain and never
    be noticed — so it raises, is not retried, and nothing is persisted."""
    import tests.elevation_bench.tile_reader as tr

    calls = []

    def get(url, timeout):
        calls.append(url)
        return FakeResponse(status)

    monkeypatch.setattr(tr.requests, "get", get)
    with pytest.raises(tr.TileConfigurationError):
        tr.fetch_terrarium_tile(13, 4400, 2688)
    assert len(calls) == 1, "a request that cannot succeed was retried"

    reader = TerrariumReader(str(tmp_path), zoom=0,
                             fetch=tr.fetch_terrarium_tile)
    with pytest.raises(tr.TileConfigurationError):
        reader.elevation(1.0, 1.0)
    assert not any(tmp_path.rglob("*.png"))


@pytest.mark.parametrize("status", [403, 404, 410])
def test_only_403_and_404_mean_the_tile_is_not_there(status, monkeypatch):
    import tests.elevation_bench.tile_reader as tr

    monkeypatch.setattr(tr.requests, "get",
                        lambda url, timeout: FakeResponse(status))
    assert tr.fetch_terrarium_tile(13, 4400, 2688) == MISSING


def test_the_back_off_map_does_not_grow_without_bound():
    """A long outage across many tiles must not keep one entry per tile for
    ever: entries whose window has passed are dropped."""
    clock = Clock()
    failing = {(3, x, y) for x in range(8) for y in range(8)}
    reader = TerrariumReader(zoom=3, fetch=FakeFetch(transient=failing),
                             retry_after_s=30.0, clock=clock)

    for x in range(8):
        for y in range(8):
            reader._tile(3, x, y)
            clock.now += 10.0               # each failure 10 s after the last

    assert len(reader._unavailable) <= 3, (
        f"{len(reader._unavailable)} entries survive a 30 s window")

