"""Compact encoding for activity elevation profiles (issue #295, Phase 4.2).

A profile is a distance/elevation series, one sample per GPS point. Serialised
as JSON text it costs roughly 20 bytes per sample — and on a 180-day trip the
full details payload it rides in measures **33 MB**, taking ~9 s to fetch and
~5 s to decode on a real device.

The waste is the representation, not the data. Delta + varint over integers is
the same trick Google's polyline encoding already uses for coordinates, and it
is *lossless at the precision the data actually has*:

* distance quantised to 1 m — GPS sample spacing is metres at best
* elevation quantised to 0.1 m — barometric and SRTM sources are within ~1 m

so nothing visible is lost, while consecutive deltas become small integers
that varint packs into one or two bytes instead of twenty.

This deliberately does not downsample. Resolution decisions belong to the
caller (the chart caps itself at 300 points; the map-to-chart cursor wants
more), and folding them into the wire format would make the two impossible to
reason about separately. See docs/plans/PERF_MAP_LOAD.md.

**There are two storage shapes and this module refuses to guess between
them.** ``Activity.elevation_profile`` is ``(distances_km[], elevations_m[])``
— parallel arrays — while ``ProjectIO.to_dict`` hands the REST client a list
of ``[distance, elevation]`` pairs. For a two-sample profile the two shapes
are *structurally identical*: ``([0.0, 0.012], [500.0, 501.5])`` is a valid
pairs list too, and reading it as one turns a 12 m activity into a 500 km one.
An earlier version of this file sniffed the shape and did exactly that, so the
entry points are now explicit and the caller says which it has.
"""
from __future__ import annotations

import math

# Quantisation. Distance in metres, elevation in decimetres — see module docs
# for why these are lossless in practice.
_DIST_SCALE = 1000.0   # km -> m
_ELEV_SCALE = 10.0     # m  -> dm


def _encode_signed(value: int, out: list[str]) -> None:
    """Append *value* as a zig-zag varint in the polyline alphabet."""
    v = ~(value << 1) if value < 0 else (value << 1)
    while v >= 0x20:
        out.append(chr((0x20 | (v & 0x1F)) + 63))
        v >>= 5
    out.append(chr(v + 63))


def encode_profile_pairs(pairs) -> str:
    """Encode ``[[distance_km, elevation_m], ...]`` to a compact ASCII string.

    Malformed and non-finite samples are skipped rather than raising: a
    profile is telemetry, one bad sample must not cost a whole activity its
    chart, and an unvalidated GPX ``<ele>`` can carry an infinity that would
    otherwise 500 the endpoint from inside ``round()``.
    """
    out: list[str] = []
    prev_d = 0
    prev_e = 0
    for point in pairs or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            dist = float(point[0])
            elev = float(point[1])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(dist) and math.isfinite(elev)):
            continue
        try:
            d = round(dist * _DIST_SCALE)
            e = round(elev * _ELEV_SCALE)
        except (OverflowError, ValueError):
            continue
        _encode_signed(d - prev_d, out)
        _encode_signed(e - prev_e, out)
        prev_d = d
        prev_e = e
    return "".join(out)


def encode_profile_arrays(profile) -> str:
    """Encode ``(distances_km[], elevations_m[])`` — the shape held on
    :class:`Activity`.

    A length mismatch is truncated to the shorter array *deliberately and
    visibly* rather than by `zip`'s silence: mismatched parallel arrays mean
    upstream corruption, and the samples that do pair up are still correct.
    """
    if not profile or len(profile) != 2:
        return ""
    distances, elevations = profile[0] or [], profile[1] or []
    n = min(len(distances), len(elevations))
    return encode_profile_pairs(
        [(distances[i], elevations[i]) for i in range(n)]
    )


def decode_profile(encoded: str) -> list[list[float]]:
    """Inverse of the encoders. Present for tests and any server-side consumer;
    a client decoder lives alongside whichever client first consumes this."""
    out: list[list[float]] = []
    index = 0
    length = len(encoded)
    d = 0
    e = 0
    while index < length:
        for which in (0, 1):
            result = 0
            shift = 0
            while True:
                if index >= length:
                    return out  # truncated input — return what decoded cleanly
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if which == 0:
                d += delta
            else:
                e += delta
        out.append([d / _DIST_SCALE, e / _ELEV_SCALE])
    return out


def build_elevation_payload(activities) -> dict:
    """``{"profiles": {...}, "encrypted": {...}}`` for the elevation endpoint.

    Split out from the route so the payload shape is testable without an HTTP
    harness — the route is then only auth, caching and gzip.

    Encrypted activities (issue #26) carry ``elevation_profile_enc``, an opaque
    envelope this server cannot read. Those go out under ``encrypted`` rather
    than being dropped: ``GET /{name}`` still serves the envelope, so silently
    omitting it here would make an E2EE trip indistinguishable from one with
    no elevation data at all, and the client would stop drawing a chart it is
    perfectly able to decrypt.

    Activities with neither are omitted rather than sent as null — absence is
    the common case for GPX and manual entries, and it costs bytes.
    """
    profiles: dict[str, str] = {}
    encrypted: dict[str, str] = {}
    for act in activities or []:
        key = str(getattr(act, "id", ""))
        enc = getattr(act, "elevation_profile_enc", None)
        if enc:
            encrypted[key] = enc
            continue
        encoded = encode_profile_arrays(getattr(act, "elevation_profile", None))
        if encoded:
            profiles[key] = encoded
    return {"profiles": profiles, "encrypted": encrypted}
