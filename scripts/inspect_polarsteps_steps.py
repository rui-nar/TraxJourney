"""Read-only diagnostic: dump Polarsteps step fields to identify draft markers.

Issue #23: the import surfaces draft (unpublished) steps. The live Polarsteps
API is undocumented and the data export only contains published steps, so the
field that marks a draft is not knowable from public sources — we have to look
at a real response. This script fetches the raw steps for one trip using the
token already stored for a user and prints, per step, every top-level field
whose name hints at publication state, plus the full key set of the first step.

Nothing secret is printed (the remember_token is never echoed). Run it against
the DB that holds the connected account:

    # list the connected account's trips (so you can find the trip id)
    python scripts/inspect_polarsteps_steps.py "E:/Downloads/traxjourney (8).db"

    # then inspect one trip's steps
    python scripts/inspect_polarsteps_steps.py "E:/Downloads/traxjourney (8).db" --trip-id 1234567

If more than one user has a Polarsteps token, pass --user-email to disambiguate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Allow running as a plain script (python scripts/inspect_polarsteps_steps.py):
# put the project root on sys.path so the `models` / `src` packages import.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Top-level keys worth inspecting when hunting for the draft marker.
_FLAG_HINTS = (
    "draft", "publish", "status", "visib", "deleted", "sync",
    "type", "supertype", "is_", "state", "live", "offline",
)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db", help="Path to the SQLite DB holding the connected account")
    ap.add_argument("--trip-id", type=int, default=None,
                    help="Polarsteps trip id to inspect; omit to list trips")
    ap.add_argument("--user-email", default=None,
                    help="Disambiguate when several users have a token stored")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()

    # models.db creates its engine from DATABASE_URL at import time, so the DB
    # path must be in the environment before we import anything that touches it.
    db_path = args.db.replace("\\", "/")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    from sqlmodel import select

    from models.db import get_session
    from models.user import PolarstepsToken, UserInfo
    from src.api.polarsteps_client import PolarstepsClient, format_trip

    # Resolve the stored token (never printed).
    with get_session() as sess:
        if args.user_email:
            user = sess.exec(
                select(UserInfo).where(UserInfo.email == args.user_email)
            ).first()
            if user is None:
                sys.exit(f"No user with email {args.user_email!r}")
            tok = sess.exec(
                select(PolarstepsToken).where(
                    PolarstepsToken.user_info_id == user.id
                )
            ).first()
        else:
            toks = sess.exec(select(PolarstepsToken)).all()
            if not toks:
                sys.exit("No Polarsteps tokens stored in this DB.")
            if len(toks) > 1:
                sys.exit(
                    "Multiple Polarsteps tokens found; pass --user-email to pick one."
                )
            tok = toks[0]
        if tok is None:
            sys.exit("No Polarsteps token for that user.")
        remember_token = tok.remember_token
        ps_user_id = tok.polarsteps_user_id

    client = PolarstepsClient(remember_token)

    # No trip id → list trips so the user can pick one.
    if args.trip_id is None:
        raw_trips = client.get_trips(ps_user_id)
        print(f"{len(raw_trips)} trips for the connected account:\n")
        for t in raw_trips:
            ft = format_trip(t)
            print(f"  {ft['id']!s:>12}  {ft['start_date'] or '????-??-??'}  "
                  f"{(ft['name'] or '(unnamed)')[:40]:<40}  steps={ft['steps_count']}")
        print("\nRe-run with --trip-id <id> to inspect a trip's steps.")
        return 0

    # include_drafts so the diagnostic still shows draft steps — its whole point
    # is to surface the markers the import filters on.
    raw = client.get_trip_steps(args.trip_id, include_drafts=True)
    print(f"Fetched {len(raw)} raw steps for trip {args.trip_id}\n")
    if not raw:
        return 0

    # Full key set of the first step — every field name available.
    print("All top-level keys on the first step:")
    print("  " + ", ".join(sorted(raw[0].keys())))
    print()

    # Per-step candidate flag fields.
    print("Per-step publication-hint fields:")
    for s in raw:
        flags = {
            k: v for k, v in s.items()
            if any(h in k.lower() for h in _FLAG_HINTS)
            and not isinstance(v, (dict, list))
        }
        name = s.get("display_name") or s.get("name") or "(unnamed)"
        print(f"  step {s.get('id')!s:>12}  {name[:30]:<30}  {json.dumps(flags)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
