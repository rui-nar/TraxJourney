#!/usr/bin/env python
"""Map a release tag to the iOS build name and build number.

App Store Connect files every upload under its ``CFBundleShortVersionString``
(the build name) and refuses a ``CFBundleVersion`` (the build number) it has
already seen under that version. It also requires the build name to be
numeric, and once a version is released on the App Store it closes that
version to new builds (ITMS-90186).

A real ``vX.Y.Z`` tag uses ``X.Y.Z`` and the same integer as Android's
versionCode (android_version_code.py), so one release reads the same in both
stores:

    v1.0.0  ->  1.0.0 10000

The floating ``validation`` tag has no version of its own and is moved by hand
(issue #190). It is filed under the latest real tag with the patch bumped, and
numbered ``<latest real code>.<run>``:

    validation, run 17, latest v1.0.0  ->  1.0.1 10000.17
    validation, run 17, no release yet ->  0.0.1 0.17

The latest released version may already be closed, so the patch-bumped one is
used instead: it is never a released version until its real tag ships. Its
builds stay unique and increasing from one run to the next, and App Store
Connect compares build numbers component by component, so ``10000.17`` sorts
below the ``10001`` that the real ``v1.0.1`` will get.
(docs/STORE_DISTRIBUTION_PLAN.md D11.)

Used by .github/workflows/ios-release.yml:

    read BUILD_NAME BUILD_NUMBER < <(python3 scripts/ios_build_number.py "$TAG" \\
        --validation-run "$RUN" --latest-tag "$LATEST_TAG")
"""

from __future__ import annotations

import argparse
import re
import sys

# Import the versionCode formula, never copy it. The tests import this module
# as scripts.ios_build_number; the workflow runs it as a file, which puts
# scripts/ itself on the path instead.
try:
    from scripts.android_version_code import version_code
except ModuleNotFoundError:
    from android_version_code import version_code

VALIDATION_TAG = "validation"

# Stricter than android_version_code's pattern: App Store Connect rejects a
# build name with a suffix, so v1.0.0-rc1 must fail here rather than at upload.
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _parse(tag: str) -> tuple[int, int, int]:
    match = _TAG_RE.match(tag.strip())
    if not match:
        raise ValueError(f"not a release tag: {tag!r} (expected vX.Y.Z)")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def build_number(
    tag: str,
    validation_run: int | None = None,
    latest_tag: str | None = None,
) -> tuple[str, str]:
    """Return ``(build_name, build_number)`` for ``tag``.

    ``validation_run`` (the workflow's run number) is required for the
    ``validation`` tag and refused for any other. ``latest_tag`` is the latest
    real release, or None/empty before the first one.

    Raises ValueError on anything else, so a mistyped tag fails the build
    rather than uploading a build App Store Connect will refuse.
    """
    if tag != VALIDATION_TAG:
        if validation_run is not None or latest_tag:
            raise ValueError(
                f"--validation-run and --latest-tag apply to the "
                f"{VALIDATION_TAG!r} tag only, not {tag!r}"
            )
        major, minor, patch = _parse(tag)
        return f"{major}.{minor}.{patch}", str(version_code(tag))

    if validation_run is None or validation_run < 1:
        raise ValueError(
            f"the {VALIDATION_TAG!r} tag needs --validation-run N (N >= 1)"
        )
    if not latest_tag:
        return "0.0.1", f"0.{validation_run}"
    major, minor, patch = _parse(latest_tag)
    return (
        f"{major}.{minor}.{patch + 1}",
        f"{version_code(latest_tag)}.{validation_run}",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=argv[0],
        description="Print '<build_name> <build_number>' for an iOS build.",
    )
    parser.add_argument("tag", help=f"vX.Y.Z, or {VALIDATION_TAG!r}")
    parser.add_argument(
        "--validation-run",
        type=int,
        help="the workflow run number (validation tag only)",
    )
    parser.add_argument(
        "--latest-tag",
        help="the latest real vX.Y.Z release (validation tag only; "
        "omit or leave empty before the first release)",
    )
    args = parser.parse_args(argv[1:])
    try:
        name, number = build_number(args.tag, args.validation_run, args.latest_tag)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(name, number)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
