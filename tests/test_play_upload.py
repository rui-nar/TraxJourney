"""Play upload script (#582): call order, payloads, cleanup and secrecy.

The HTTP session is faked, so nothing here touches the network. A wrong order or
a leaked credential would only show up on a real release, so both are pinned.
"""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "play_upload",
    Path(__file__).resolve().parent.parent / "scripts" / "play_upload.py",
)
play_upload = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(play_upload)

PKG = "com.traxjourney.app"
SECRET = "SUPER-SECRET-KEY-MATERIAL"


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def _respond(self, name, body=None):
        if self.fail_on == name:
            return FakeResponse(403, {"error": {"message": "caller lacks permission"}})
        return FakeResponse(200, body)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url.endswith("/edits"):
            return self._respond("insert", {"id": "E1"})
        if "/bundles" in url:
            return self._respond("upload", {"versionCode": 4800})
        return self._respond("commit", {"id": "E1"})

    def put(self, url, **kw):
        self.calls.append(("PUT", url, kw))
        return self._respond("track", {})

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url, kw))
        return FakeResponse(204)


@pytest.fixture
def aab(tmp_path):
    path = tmp_path / "app.aab"
    path.write_bytes(b"AABDATA")
    return path


def _argv(aab, *extra):
    return ["play_upload.py", str(aab), "--package", PKG, "--track", "internal",
            "--release-name", "v0.48.0", *extra]


def test_call_order_and_payloads(aab, capsys):
    session = FakeSession()
    assert play_upload.main(_argv(aab), session=session) == 0

    steps = [(m, u.split("androidpublisher/v3/applications/")[1]) for m, u, _ in session.calls]
    assert steps == [
        ("POST", f"{PKG}/edits"),
        ("POST", f"{PKG}/edits/E1/bundles?uploadType=media"),
        ("PUT", f"{PKG}/edits/E1/tracks/internal"),
        ("POST", f"{PKG}/edits/E1:commit"),
    ]
    upload_kw = session.calls[1][2]
    assert upload_kw["data"] == b"AABDATA"
    assert upload_kw["headers"] == {"Content-Type": "application/octet-stream"}
    # The upload goes to the /upload/ host path, the rest to the plain API.
    assert "/upload/androidpublisher/" in session.calls[1][1]
    assert "/upload/" not in session.calls[0][1]
    assert "versionCode 4800" in capsys.readouterr().out


def test_internal_track_body(aab):
    session = FakeSession()
    play_upload.main(_argv(aab), session=session)
    assert session.calls[2][2]["json"] == {
        "track": "internal",
        "releases": [
            {"name": "v0.48.0", "versionCodes": ["4800"], "status": "completed"}
        ],
    }


@pytest.mark.parametrize("fail_on", ["upload", "track", "commit"])
def test_edit_deleted_on_failure(aab, capsys, fail_on):
    session = FakeSession(fail_on=fail_on)
    assert play_upload.main(_argv(aab), session=session) == 1
    method, url, _ = session.calls[-1]
    assert method == "DELETE" and url.endswith(f"/edits/E1")
    err = capsys.readouterr().err
    assert "HTTP 403" in err and "caller lacks permission" in err


def test_insert_failure_has_no_edit_to_delete(aab):
    session = FakeSession(fail_on="insert")
    assert play_upload.main(_argv(aab), session=session) == 1
    assert [m for m, _, _ in session.calls] == ["POST"]


def test_credential_never_in_output(aab, capsys, monkeypatch):
    monkeypatch.setenv(play_upload.CREDENTIAL_ENV, SECRET)
    # Not valid JSON: the parse error must not echo the credential.
    assert play_upload.main(_argv(aab)) == 1
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err

    session = FakeSession(fail_on="upload")
    play_upload.main(_argv(aab), session=session)
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_missing_credential_is_an_error(aab, capsys, monkeypatch):
    monkeypatch.delenv(play_upload.CREDENTIAL_ENV, raising=False)
    assert play_upload.main(_argv(aab)) == 1
    assert play_upload.CREDENTIAL_ENV in capsys.readouterr().err


def test_usage_error_on_missing_args(capsys):
    assert play_upload.main(["play_upload.py"], session=FakeSession()) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_missing_bundle_file(tmp_path, capsys):
    argv = _argv(tmp_path / "nope.aab")
    assert play_upload.main(argv, session=FakeSession()) == 1
    assert "not found" in capsys.readouterr().err
