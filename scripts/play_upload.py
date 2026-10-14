#!/usr/bin/env python
"""Upload a signed Android App Bundle to a Google Play track.

Play requires an AAB for new apps, and a release is only visible once an *edit*
is committed. The Android Publisher v3 API therefore takes four calls in order:

    edits.insert   open an edit
    bundles.upload send the AAB; Play answers with the bundle's versionCode
    tracks.update  put that versionCode in a ``completed`` release on the track
    edits.commit   publish the edit

If anything after the insert fails the edit is deleted, so a half-built edit
never lingers and blocks the next attempt.

This is our own script rather than a third-party action on purpose: the
service-account key can publish the app, and an action would receive it. Here it
is read from the ``PLAY_SERVICE_ACCOUNT_JSON`` environment variable only, never
from argv, and never printed. Errors report the HTTP status and the API's error
message, nothing else.

Used by .github/workflows/android-release.yml:

    python scripts/play_upload.py app.aab --package com.traxjourney.app \\
        --track internal --release-name "$TAG"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
_UPLOAD = (
    "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications"
)
CREDENTIAL_ENV = "PLAY_SERVICE_ACCOUNT_JSON"


class PlayError(Exception):
    """An API call failed; the message carries only the status and API message."""


def _check(response, what: str) -> dict:
    """Return the JSON body, or raise PlayError with status + API message only."""
    if response.status_code >= 400:
        try:
            message = response.json().get("error", {}).get("message", "")
        except ValueError:
            message = ""
        raise PlayError(f"{what} failed: HTTP {response.status_code} {message}".strip())
    try:
        return response.json()
    except ValueError:
        return {}


def upload(session, aab: Path, package: str, track: str, release_name: str) -> int:
    """Run the four edit calls on ``session``; return the uploaded versionCode."""
    base = f"{_API}/{package}"
    edit = _check(session.post(f"{base}/edits", json={}), "edits.insert")
    edit_id = edit["id"]
    try:
        bundle = _check(
            session.post(
                f"{_UPLOAD}/{package}/edits/{edit_id}/bundles?uploadType=media",
                data=aab.read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
            ),
            "bundles.upload",
        )
        version_code = int(bundle["versionCode"])
        _check(
            session.put(
                f"{base}/edits/{edit_id}/tracks/{track}",
                json={
                    "track": track,
                    "releases": [
                        {
                            "name": release_name,
                            "versionCodes": [str(version_code)],
                            "status": "completed",
                        }
                    ],
                },
            ),
            "tracks.update",
        )
        _check(session.post(f"{base}/edits/{edit_id}:commit"), "edits.commit")
    except Exception:
        # Best effort: the original failure is the one worth reporting.
        try:
            session.delete(f"{base}/edits/{edit_id}")
        except Exception:
            pass
        raise
    return version_code


def _default_session(credential_json: str):
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(credential_json), scopes=[_SCOPE]
    )
    return AuthorizedSession(credentials)


def main(argv: list[str], session=None) -> int:
    parser = argparse.ArgumentParser(prog=argv[0], description=__doc__.split("\n")[0])
    parser.add_argument("aab", type=Path, help="path to the signed .aab")
    parser.add_argument("--package", required=True)
    parser.add_argument("--track", required=True)
    parser.add_argument("--release-name", required=True)
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:  # argparse already printed the usage error
        return int(exc.code or 0)

    if not args.aab.is_file():
        print(f"bundle not found: {args.aab}", file=sys.stderr)
        return 1

    if session is None:
        credential = os.environ.get(CREDENTIAL_ENV, "")
        if not credential:
            print(f"{CREDENTIAL_ENV} is not set", file=sys.stderr)
            return 1
        try:
            session = _default_session(credential)
        except (ValueError, KeyError):
            # Do not echo the exception: it can quote the credential.
            print(f"{CREDENTIAL_ENV} is not a valid service-account key", file=sys.stderr)
            return 1

    try:
        version_code = upload(
            session, args.aab, args.package, args.track, args.release_name
        )
    except PlayError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"uploaded versionCode {version_code} to the {args.track} track "
        f"as {args.release_name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
