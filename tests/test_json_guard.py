"""The JSON body guard in front of every route (api/json_guard.py, issue #462).

It reads a JSON body before any route or authentication does, so it bounds
what it will read: a body past MAX_JSON_BYTES is answered 413, by its declared
length before a byte is read, or as soon as the bytes that arrive pass it. A
body sent without a Content-Type is judged as JSON, which is how FastAPI
before 0.132 parsed it. Its answers carry the request id, as every other
error the API gives does.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import api.json_guard as json_guard
from tests.test_elevation_non_finite import app  # noqa: F401


def _run(headers, chunks, cap):
    """Drive the guard alone: what it answered, how many chunks it read, and
    whether it passed the request on."""
    received = []
    sent = []
    reached = []

    async def inner(scope, receive, send):
        reached.append(True)

    async def receive():
        if len(received) < len(chunks):
            chunk = chunks[len(received)]
            received.append(chunk)
            return {"type": "http.request", "body": chunk,
                    "more_body": len(received) < len(chunks)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    guard = json_guard.RefuseUnstorableJson(inner)
    guard_cap = json_guard.MAX_JSON_BYTES
    json_guard.MAX_JSON_BYTES = cap
    try:
        asyncio.run(guard({"type": "http", "method": "PUT", "path": "/x",
                           "headers": headers}, receive, send))
    finally:
        json_guard.MAX_JSON_BYTES = guard_cap
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, len(received), bool(reached)


_JSON = [(b"content-type", b"application/json")]


def test_a_declared_length_past_the_cap_is_refused_unread():
    status, read, reached = _run(
        _JSON + [(b"content-length", b"1001")], [b"x" * 1001], cap=1000)

    assert (status, read, reached) == (413, 0, False)


def test_a_body_past_the_cap_is_refused_as_soon_as_it_passes_it():
    chunks = [b"[" + b"0," * 200] + [b"0," * 200] * 20 + [b"0]"]
    status, read, reached = _run(_JSON, chunks, cap=1000)

    assert (status, reached) == (413, False)
    assert read <= 3, read


def test_a_body_within_the_cap_goes_through():
    status, read, reached = _run(_JSON, [b'{"a": ', b"1}"], cap=1000)

    assert reached and status is None and read == 2


def test_a_body_without_a_content_type_is_judged_as_json():
    status, _, reached = _run([], [b'{"lat": NaN}'], cap=1000)

    assert (status, reached) == (422, False)


def test_a_body_of_another_type_is_left_alone():
    status, _, reached = _run([(b"content-type", b"text/plain")], [b"NaN" * 1000], cap=1000)

    assert reached and status is None


def test_the_cap_holds_the_largest_body_the_app_sends():
    """A track edit of a 48-hour activity recorded at 1 Hz (170,000 points,
    measured at 69 bytes a point in JSON) is 12 MB; its encrypted fields are
    less. The cap leaves four times that."""
    assert json_guard.MAX_JSON_BYTES >= 4 * 170_000 * 69


def test_its_answers_carry_the_request_id(app):  # noqa: F811
    client, _ = app

    r = client.post("/api/memories/", content=b'{"project_name": "Trip", "lat": NaN}',
                    headers={"Content-Type": "application/json"})

    assert r.status_code == 422, r.text
    assert r.json()["request_id"]
    assert r.json()["request_id"] == r.headers.get("x-request-id", r.json()["request_id"])


def test_the_floor_is_a_fastapi_that_refuses_a_body_without_a_content_type():
    """FastAPI 0.132 made strict_content_type the default: before it, a body
    without a Content-Type was parsed as JSON, so the guard judges it too."""
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    line = next(line for line in req.splitlines() if line.startswith("fastapi"))
    floor = tuple(int(p) for p in line.split(">=")[1].strip().split("."))

    assert floor >= (0, 132, 0), line
