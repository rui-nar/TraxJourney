"""Refuse a JSON request body holding a value no trip may hold (issue #462).

Python's JSON parser reads ``NaN``, ``Infinity``, a literal too large for a
float, an integer of any length and a lone ``\\ud800`` escape, and pydantic
passes them all on unless a field says otherwise. Stored, each breaks the
trip: the client's JSON parser refuses the first two, SQLite refuses to bind
the last two, and the trip-file import refuses all of them, so a trip holding
one could not be exported and imported back.

The trip-file import checks a whole document for these with
:func:`src.project.traxj_schema._bad_value`; this runs the same check on every
JSON request body, in front of every route, so that no field of any endpoint,
however loosely its model types it, can store one. A body that is not valid
JSON is left to the route, which answers it as it always has.

It reads the body before any route or authentication does, so it bounds what
it reads: past :data:`MAX_JSON_BYTES` it answers 413, by the declared length
before reading a byte, or as soon as the bytes that arrive pass it. A body
sent without a Content-Type is judged as JSON: FastAPI parsed one as JSON
before 0.132, and the requirements' floor is not all that stands between.
"""
from __future__ import annotations

import json
from email.message import Message

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.project.traxj_schema import _bad_value
from src.utils.logging import request_id_var

#: The largest JSON body read, 50 MB. The largest the app sends is a track
#: edit: 69 bytes a point measured, so 12 MB for a 48-hour activity recorded
#: at 1 Hz (170,000 points); the encrypted-fields update of the same activity
#: is less (51 bytes a point). Four times the first leaves room for any real
#: activity, and it is the budget MAX_IMPORT_BYTES already gives a trip file.
MAX_JSON_BYTES = 50 * 1024 * 1024


def _is_json(scope: Scope) -> bool:
    for name, value in scope.get("headers") or ():
        if name == b"content-type":
            message = Message()
            message["content-type"] = value.decode("latin-1")
            subtype = message.get_content_subtype()
            return (message.get_content_maintype() == "application"
                    and (subtype == "json" or subtype.endswith("+json")))
    return True                         # none given: judged as JSON


def _declared_length(scope: Scope):
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def body_fault(body: bytes):
    """What no trip may hold in the JSON *body*, or None."""
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return None                     # not JSON: the route answers it
    if not isinstance(document, (dict, list)):
        return None
    return _bad_value(document)


def _answer(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"detail": detail, "request_id": request_id_var.get()})


def _too_large() -> JSONResponse:
    return _answer(413, f"The request is too large. The limit is "
                        f"{MAX_JSON_BYTES // (1024 * 1024)} MB.")


class RefuseUnstorableJson:
    """ASGI middleware: 413 for a JSON body past the cap, 422 for one
    holding a value no trip may hold."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_json(scope):
            await self.app(scope, receive, send)
            return
        declared = _declared_length(scope)
        if declared is not None and declared > MAX_JSON_BYTES:
            await _too_large()(scope, receive, send)
            return
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":   # the client went away
                await self.app(scope, _replay([message], receive), send)
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_JSON_BYTES:
                await _too_large()(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        fault = body_fault(body)
        if fault is not None:
            await _answer(422, f"The request holds a value that cannot be stored: {fault}.")(
                scope, receive, send)
            return
        await self.app(scope, _replay(
            [{"type": "http.request", "body": body, "more_body": False}], receive), send)


def _replay(messages, receive: Receive) -> Receive:
    """A receive channel that hands out *messages*, then the real one."""
    pending = list(messages)

    async def replayed():
        if pending:
            return pending.pop(0)
        return await receive()
    return replayed
