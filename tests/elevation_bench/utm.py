"""ETRS89 / UTM zone 33N, both directions (Krueger series on GRS80).

Split out of ``terrarium_measure.py`` for one reason: that script needs numpy,
which is not a dependency of the application or of CI, while this is pure
arithmetic that every measurement against Berlin's lidar silently depends on. An
error here is not loud — a coordinate wrong by a few tens of metres reads as
terrain-model error and charges the tileset for it — so it is tested in CI where
the measurement itself cannot be.
"""
from __future__ import annotations

import math
from typing import Tuple

_A = 6378137.0
_F = 1 / 298.257222101
_K0 = 0.9996
_LON0 = math.radians(15.0)
_FE = 500000.0
_N = _F / (2 - _F)
_AA = _A / (1 + _N) * (1 + _N ** 2 / 4 + _N ** 4 / 64)
_ALPHA = (_N / 2 - 2 * _N ** 2 / 3 + 5 * _N ** 3 / 16,
          13 * _N ** 2 / 48 - 3 * _N ** 3 / 5,
          61 * _N ** 3 / 240)
_BETA = (_N / 2 - 2 * _N ** 2 / 3 + 37 * _N ** 3 / 96,
         _N ** 2 / 48 + _N ** 3 / 15,
         17 * _N ** 3 / 480)
_DELTA = (2 * _N - 2 * _N ** 2 / 3 - 2 * _N ** 3,
          7 * _N ** 2 / 3 - 8 * _N ** 3 / 5,
          56 * _N ** 3 / 15)


def to_utm(lat: float, lon: float) -> Tuple[float, float]:
    """(easting, northing) in metres, ETRS89 / UTM 33N."""
    phi, lam = math.radians(lat), math.radians(lon) - _LON0
    t = math.sinh(math.atanh(math.sin(phi))
                  - 2 * math.sqrt(_N) / (1 + _N)
                  * math.atanh(2 * math.sqrt(_N) / (1 + _N) * math.sin(phi)))
    xi_p = math.atan2(t, math.cos(lam))
    eta_p = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    xi, eta = xi_p, eta_p
    for j, a in enumerate(_ALPHA, start=1):
        xi += a * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p)
        eta += a * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p)
    return _FE + _K0 * _AA * eta, _K0 * _AA * xi


def from_utm(easting: float, northing: float) -> Tuple[float, float]:
    """(lat, lon) in degrees from ETRS89 / UTM 33N."""
    xi = northing / (_K0 * _AA)
    eta = (easting - _FE) / (_K0 * _AA)
    xi_p, eta_p = xi, eta
    for j, b in enumerate(_BETA, start=1):
        xi_p -= b * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        eta_p -= b * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    chi = math.asin(math.sin(xi_p) / math.cosh(eta_p))
    phi = chi
    for j, d in enumerate(_DELTA, start=1):
        phi += d * math.sin(2 * j * chi)
    lam = _LON0 + math.atan2(math.sinh(eta_p), math.cos(xi_p))
    return math.degrees(phi), math.degrees(lam)
