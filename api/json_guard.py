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
"""
from __future__ import annotations

import json
from email.message import Message

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.project.traxj_schema import _bad_value


def _is_json(scope: Scope) -> bool:
    for name, value in scope.get("headers") or ():
        if name == b"content-type":
            message = Message()
            message["content-type"] = value.decode("latin-1")
            subtype = message.get_content_subtype()
            return (message.get_content_maintype() == "application"
                    and (subtype == "json" or subtype.endswith("+json")))
    return False


def body_fault(body: bytes):
    """What no trip may hold in the JSON *body*, or None."""
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return None                     # not JSON: the route answers it
    if not isinstance(document, (dict, list)):
        return None
    return _bad_value(document)


class RefuseUnstorableJson:
    """ASGI middleware: 422 for a JSON body holding such a value."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_json(scope):
            await self.app(scope, receive, send)
            return
        chunks = []
        while True:
            message = await receive()
            if message["type"] != "http.request":   # the client went away
                await self.app(scope, _replay([message], receive), send)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        fault = body_fault(body)
        if fault is not None:
            response = JSONResponse(
                status_code=422,
                content={"detail": f"The request holds a value that cannot be stored: {fault}."})
            await response(scope, receive, send)
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
