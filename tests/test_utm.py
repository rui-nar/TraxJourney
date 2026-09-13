"""The UTM conversion the terrain-model measurement depends on (#386, unit 2).

Nothing in the application uses this. It is tested because every figure the
measurement produces rests on it, and an error here is silent: a coordinate that
is a few tens of metres out reads as terrain-model error and blames the tileset.
"""
from __future__ import annotations

import pytest

from tests.elevation_bench.utm import from_utm, to_utm


@pytest.mark.parametrize("lat", [52.35, 52.45, 52.52, 52.65])
@pytest.mark.parametrize("lon", [13.05, 13.25, 13.45, 13.75])
def test_the_two_directions_agree_to_well_under_a_millimetre(lat, lon):
    easting, northing = to_utm(lat, lon)
    back_lat, back_lon = from_utm(easting, northing)

    assert back_lat == pytest.approx(lat, abs=1e-8)       # ~1 mm of latitude
    assert back_lon == pytest.approx(lon, abs=1e-8)


def test_the_central_meridian_sits_on_the_false_easting():
    """Zone 33's central meridian is 15 deg E, mapped to easting 500 000 m. A
    wrong zone constant would move every point by hundreds of kilometres."""
    easting, _ = to_utm(52.0, 15.0)
    assert easting == pytest.approx(500000.0, abs=1e-6)


# ── Independent derivations ──────────────────────────────────────────────────
#
# A round trip proves the two directions agree with each other, not that either
# is right — they could share an error. These compute the same quantity by
# DIFFERENT mathematics, so an error in the Krueger series would have to be
# reproduced exactly by a numerical integral or by a separately published
# expansion to pass. No expected value below is typed in from memory.

_A = 6378137.0
_F = 1 / 298.257222101
_E2 = _F * (2 - _F)
_K0 = 0.9996


def _meridian_arc(lat_deg: float, steps: int = 20000) -> float:
    """Distance from the equator along the meridian, by Simpson's rule over the
    ellipsoid's own meridional radius of curvature M = a(1-e^2)/(1-e^2 sin^2)^1.5
    — the definition, not a series."""
    import math

    phi = math.radians(lat_deg)
    h = phi / steps

    def m(x):
        return _A * (1 - _E2) / (1 - _E2 * math.sin(x) ** 2) ** 1.5

    total = m(0.0) + m(phi)
    for i in range(1, steps):
        total += (4 if i % 2 else 2) * m(i * h)
    return total * h / 3


@pytest.mark.parametrize("lat", [0.0, 30.0, 45.0, 52.52, 60.0])
def test_northing_on_the_central_meridian_is_the_scaled_meridian_arc(lat):
    """On the central meridian a transverse Mercator northing is exactly k0 times
    the meridian arc — so integrate the ellipse and compare."""
    _, northing = to_utm(lat, 15.0)
    assert northing == pytest.approx(_K0 * _meridian_arc(lat), abs=0.002)


def _snyder_easting(lat_deg: float, lon_deg: float) -> float:
    """Easting from Snyder's transverse Mercator series (USGS Professional Paper
    1395, eqs. 8-9 to 8-13). A different expansion from Krueger's, accurate to a
    few centimetres within a few degrees of the central meridian."""
    import math

    phi = math.radians(lat_deg)
    lam = math.radians(lon_deg - 15.0)
    ep2 = _E2 / (1 - _E2)
    n = _A / math.sqrt(1 - _E2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = ep2 * math.cos(phi) ** 2
    a = lam * math.cos(phi)
    return 500000.0 + _K0 * n * (
        a + (1 - t + c) * a ** 3 / 6
        + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * a ** 5 / 120)


@pytest.mark.parametrize("lat, lon", [
    (52.4735, 13.4015),     # Tempelhofer Feld
    (52.4975, 13.2412),     # Teufelsberg
    (52.4167, 13.6333),     # Mueggelberge
    (52.0, 13.0),
])
def test_easting_agrees_with_an_independent_published_series(lat, lon):
    """Every site the measurement uses, against Snyder's separate expansion.
    The two are different series; agreeing to centimetres means neither is wrong
    at the scale that would matter here (tens of metres)."""
    easting, _ = to_utm(lat, lon)
    assert easting == pytest.approx(_snyder_easting(lat, lon), abs=0.05)
